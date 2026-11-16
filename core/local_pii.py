"""AstrLink Guard v0.1.3 本地推理；模型/阈值/窗口/BIO 合并对齐 Magpie。

只下载固定模型文件，不上传待检测文本。缓存仅保存摘要键和位置，不落盘。
ONNX Runtime CPU 后端复用项目依赖；不使用 Magpie 的 Go/CoreML 自定义后端。
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

from monitor import metrics
from core.pii_rules import business_identifier_context, chinese_name_spans, iban_spans

REVISION = "7216b6a3b897ef903d4b1ba367ae6fb95b0fa5e4"
FILES = {
    "model_int8.onnx": (330781, "0a070d3113615e33ba3b84d6bd486f663a377c04c4b056e7381732179f51832a"),
    "model_int8.onnx_data": (37124608, "0ba34216ceead01e0ae2dfd8e46d52b67e7aee80b04f722d9b758613a91c0067"),
    "tokenizer.json": (1502881, "0cf3b235ef125658015a6c8026a5fe6f28088bf825101a990ec18a10200feef4"),
}
LABELS = ["O"] + [tag + "-" + kind for kind in (
    "email", "phone", "account", "payment_card", "ip_address", "url", "common_secret",
    "private_address", "private_date", "private_person") for tag in ("B", "I")]
KINDS = {"email": "EMAIL", "phone": "PHONE", "account": "ACCOUNT", "payment_card": "ACCOUNT",
         "common_secret": "SECRET", "private_address": "ADDRESS", "private_person": "PERSON"}
_WINDOW, _OVERLAP = 510, 128
_LOAD_LOCK = threading.Lock()
_DOWNLOAD_LOCK = threading.Lock()
_detector = None
_download = None

# Keep the existing street-address policy: cities/counties/states are not private
# addresses by themselves. Shape/context checks, never a gazetteer of city names.
_STREET_MARKER = re.compile(
    r"(?i)\b(?:street|road|avenue|boulevard|lane|drive|court|way|place|terrace|trail|highway|circle|crescent|square|suite|apt|apartment|flat|studio|p\.?o\.?\s+box|rue|rua|via|viale|piazza|calle|carrer|avenida|avda|paseo|chemin|route|quai|plaza|ronda|acceso|urbanización|cañada|callejón|alameda|borgo|vicolo|contrada|stretto|corso|largo|salita)\b"
    r"|(?:straße|strasse|straat|gatan|vägen|strada|gasse|platz|allee|allée|allé|weg|damm|kai|laan|gracht|plein|steeg|dreef|hof|pad|singel|baan|gränd|stigen|torget|backen)\b|[街路巷弄号號楼樓栋棟室]")
_STREET_ABBREVIATION = re.compile(r"[^\W_]\s+(?:[Ss]t|[Rr]d|[Aa]ve|[Bb]lvd|[Ll]n|[Dd]r|[Cc]t|[Pp]l|[Hh]wy)\b|(?i:str)\.\s*\d")


def private_address_shape(value):
    value = value.lstrip("([ ")
    if _STREET_MARKER.search(value) or _STREET_ABBREVIATION.search(value):
        return True
    # A street can have an unlisted name; require a leading house number and
    # actual street words, not '05067, USA' or a postcode at the end of a city.
    if re.search(r"(?:^|[\n,]\s*)[0-9]{1,6}[A-Za-z]?(?:[-/][0-9]{1,6})?\s+[^\W\d_][^,\n]*", value):
        return True
    if re.match(r"[0-9]{1,4}[A-Za-z]?\s*,\s*[^\W\d_]", value):
        return True
    # Numbered streets and language-independent house-number notation also
    # occur in high-confidence ADDRESS spans. Keep street words mandatory.
    if re.match(r"[0-9]{1,6}\s+[0-9]{1,4}(?:st|nd|rd|th)?(?:\s+[0-9]/[0-9])?\s+[^\W\d_]", value, re.I):
        return True
    house = re.search(r"\b(?:No|Nr)\.?\s*[:#]?\s*[0-9]{1,6}(?![\w.])", value, re.I)
    if house and any(c.isalpha() for c in value[:house.start()]):
        return True
    # Number-last addresses need no gazetteer. Do not mistake five/six-digit
    # postcodes, Dutch postcode suffixes, or decimal coordinates for a house.
    return bool(re.search(r"[^\W\d_][^\n,]*(?:,\s*|\s+)[0-9]{1,4}[A-Za-z]?(?![\w.]|\s+[A-Z]{2}\b)(?=[,\n /-]|$)", value))


class LocalPIIError(RuntimeError):
    """安全错误：不携带文本、模型张量或文件内容。"""


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    kind: str
    score: float


def model_dir() -> Path:
    return Path(os.getenv("IT_COPILOT_ASTRLINK_DIR", str(Path(__file__).resolve().parents[1] / "data/models/astrlink-guard" / REVISION[:12])))


def installed() -> bool:
    return all((model_dir() / name).is_file() and (model_dir() / name).stat().st_size == size for name, (size, _) in FILES.items())


def verify(directory: Path):
    for name, (size, digest) in FILES.items():
        path = directory / name
        if not path.is_file() or path.stat().st_size != size or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise LocalPIIError("AstrLink model integrity check failed; run install_astrlink_guard.py")


def install() -> Path:
    import httpx2
    directory = model_dir()
    with _DOWNLOAD_LOCK:
        directory.mkdir(parents=True, exist_ok=True)
        # 同机 Magpie 已有的模型可复用，但先按固定哈希验证，绝不读取其会话缓存。
        cached = Path.home() / ".cache/magpie/astrlink-guard" / REVISION[:12]
        if cached != directory:
            try:
                verify(cached)
            except LocalPIIError:
                pass
            else:
                for name in FILES:
                    target = directory / name
                    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == FILES[name][1]:
                        continue
                    temp = directory / (name + "." + uuid.uuid4().hex + ".part")
                    try:
                        shutil.copyfile(cached / name, temp)
                        temp.replace(target)
                    finally:
                        temp.unlink(missing_ok=True)
                verify(directory)
                return directory
        with httpx2.Client(follow_redirects=True, timeout=60) as client:
            for name, (size, digest) in FILES.items():
                target = directory / name
                if target.is_file() and target.stat().st_size == size and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                    continue
                temp = directory / (name + "." + uuid.uuid4().hex + ".part")
                try:
                    with client.stream("GET", f"https://huggingface.co/QuantumNous/astrlink-guard/resolve/{REVISION}/int8/{name}?download=true") as response:
                        response.raise_for_status()
                        with temp.open("wb") as output:
                            for chunk in response.iter_bytes():
                                output.write(chunk)
                                if output.tell() > size:
                                    raise LocalPIIError("AstrLink download exceeded pinned size")
                    if temp.stat().st_size != size or hashlib.sha256(temp.read_bytes()).hexdigest() != digest:
                        raise LocalPIIError("AstrLink download checksum mismatch")
                    temp.replace(target)
                except Exception:
                    raise LocalPIIError("AstrLink download failed; no request text was sent") from None
                finally:
                    temp.unlink(missing_ok=True)
    verify(directory)
    return directory


def _start_download():
    global _download
    with _LOAD_LOCK:
        if _download is None or not _download.is_alive():
            def run():
                try:
                    install()
                except LocalPIIError:
                    metrics.LOCAL_PII_REQUESTS.labels(outcome="download_failed").inc()
            _download = threading.Thread(target=run, daemon=True, name="astrlink-download")
            _download.start()


def accept_span(text: str, start: int, end: int, kind: str) -> bool:
    """对应 Magpie 的 modelTakes/modelFits 常用保护：保留协议和业务标识。

    按 Magpie personFits / numberLike 处理姓名缩写、复合大写名和数字账号。
    短口令由带上下文的规则补足，不降低自由文本模型 SECRET 的门槛。
    """
    value = text[start:end]
    if not value or re.fullmatch(r"0x[\da-fA-F]{1,16}", value):
        return False
    ascii_word = lambda c: c.isascii() and (c.isalnum() or c == "_")  # noqa: E731
    if (start and ascii_word(text[start - 1]) and ascii_word(value[0])) or (end < len(text) and ascii_word(value[-1]) and ascii_word(text[end])):
        return False
    before = text[max(0, start - 64):start].lower()
    if business_identifier_context(text, start) and (kind != "PERSON" or not before.rstrip().endswith(",")):
        return False
    if re.search(r"(?:order[_ -]?id|订单号|订单编号|工号|设备号|资产号)\s*[:：=]?\s*$", before):
        return False
    if kind == "PERSON":
        if re.search(r"(?:race(?:/ethnicity)?|ethnicity|gender|nationality)[?\s*\"']*(?:[:=]|is)?[\s*\"'-]*$", before):
            return False
        if value.lower() in {"white", "black", "asian", "caucasian"} and re.search(r"identif(?:y|ies) as\s*$", before):
            return False
        if len(value) < 2 or any(c.isdigit() for c in value) or any(c in value for c in "_/@\\<>|=:"):
            return False
        if re.search(r"\.[A-Za-z]", value) or (start and text[start-1] in "/\\") or (end < len(text) and text[end] in "/\\"):
            return False
        if value.lower() in {"claude", "chatgpt", "codex", "gemini", "copilot", "grok", "kimi", "qwen", "deepseek", "cursor", "magpie", "jev", "gitlab"}:
            return False
        if value.isascii():
            words = value.split()
            if not any(c.isupper() for c in value) or (len(words) == 1 and value.isupper()):
                return False
            # Magpie camelProduct: OpenAI / CodeRabbit, while McDonald stays a name.
            if len(words) == 1 and re.search(r"[a-z]{3,}[A-Z]", value):
                return False
            particles = {"van", "von", "der", "den", "de", "da", "del", "della", "di", "du", "la", "le", "bin", "binti", "al", "el", "y", "dos", "das", "do", "ter", "ten", "zu"}
            if any(w[0].islower() and (i == 0 or w not in particles) for i, w in enumerate(words)):
                return False
        return any(c.isalpha() for c in value)
    if kind in {"PHONE", "ACCOUNT"}:
        # This product preserves employee/customer IDs. Magpie's numberLike
        # permits up to four letters, which also admits EMP123456 / CUST1234.
        # Only a checksum-valid IBAN is a safe alphanumeric account exception.
        if kind == "ACCOUNT" and any(c.isalpha() for c in value):
            return any(a == 0 and b == len(value) for a, b, _ in iban_spans(value))
        digits = sum(c.isdigit() for c in value)
        letters = sum(c.isalpha() for c in value)
        if (not re.fullmatch(r"[A-Za-z\d +()/.-]+", value) or letters > 4
                or digits * 2 <= len(value) or ".." in value
                or re.fullmatch(r"v?\d+(?:\.\d+)+", value)
                or re.fullmatch(r"\d+[xX]\d+", value)):
            return False
        if kind == "ACCOUNT" and re.fullmatch(r"[\da-f]+", value) and any(c in "abcdef" for c in value):
            return False
        if kind == "PHONE" and (digits < 7 or value[0].isalpha() or re.fullmatch(r"1(?:\d{9}|\d{12})", value)):
            return False
        if value.isdigit():
            cues = r"phone|tel|mobile|call|电话|手机|联系|致电" if kind == "PHONE" else r"account|acct|iban|routing|card|账号|帐号|账户|卡号"
            return bool(re.search(cues, before)) or (kind == "PHONE" and value.startswith("0"))
        return True
    if kind == "SECRET":
        if len(value) < 8 or re.match(r"(?:req|call|toolu|msg)_", value) or re.fullmatch(r"[\da-fA-F]{8}(?:-[\da-fA-F]+){4}", value):
            return False
        if value.startswith(("/", "~/", "./", "../")) or "://" in value:
            return False
        if re.fullmatch(r"[a-zA-Z -]+", value):
            return bool(re.search(r"(?:password|passwd|pwd|secret|token|key|密码|密钥|令牌)[\s\"']*(?:[:：=]|is|was|是|为)[\s\"']*$", before))
        return True
    if kind == "EMAIL":
        return "@" in value and not (value.startswith("git@") and text[end:].startswith(":"))
    if kind == "ADDRESS":
        return (any(c.isalpha() for c in value) and "_" not in value
                and not value.startswith(("/", "~/", "./")) and private_address_shape(value))
    return False


class AstrLinkGuard:
    def __init__(self, directory: Path):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        verify(directory)
        self.directory = directory
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 2) // 2))
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(directory / "model_int8.onnx"), sess_options=options, providers=["CPUExecutionProvider"])
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.lock = threading.Lock()
        self.cache_lock = threading.Lock()
        self.cache: OrderedDict[bytes, tuple[Span, ...]] = OrderedDict()
        self.cache_hits = 0
        self.inferences = 0

    def cached(self, text: str) -> tuple[Span, ...] | None:
        """Short LRU critical section, independent of the inference lock."""
        return self._cached_key(hashlib.sha256(text.encode()).digest())

    def _cached_key(self, key: bytes) -> tuple[Span, ...] | None:
        with self.cache_lock:
            value = self.cache.get(key)
            if value is not None:
                self.cache.move_to_end(key)
                self.cache_hits += 1
            return value

    def detect(self, text: str, *, deadline: float, use_cache: bool = True) -> tuple[Span, ...]:
        import numpy as np
        if len(text) > 512000:
            raise LocalPIIError("AstrLink input exceeds local limit")
        key = hashlib.sha256(text.encode()).digest()
        if use_cache:
            value = self._cached_key(key)
            if value is not None:
                return value
        if not self.lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise LocalPIIError("AstrLink inference queue timed out")
        try:
            if use_cache:
                # Another queued request may have filled this entry meanwhile.
                value = self._cached_key(key)
                if value is not None:
                    return value
            enc = self.tokenizer.encode(text, add_special_tokens=False)
            n = len(enc.ids)
            tags, scores, margins = np.zeros(n, dtype=int), np.zeros(n), np.full(n, -1)
            for start in range(0, n, _WINDOW - _OVERLAP):
                if time.monotonic() >= deadline:
                    raise LocalPIIError("AstrLink inference timed out")
                end = min(start + _WINDOW, n)
                ids = np.array([[0, *enc.ids[start:end], 2]], dtype=np.int64)
                logits = self.session.run(["logits"], {"input_ids": ids, "attention_mask": np.ones_like(ids), "token_type_ids": np.zeros_like(ids)})[0][0][1:-1]
                probs = np.exp(logits.astype(np.float64) - logits.max(-1, keepdims=True))
                probs /= probs.sum(-1, keepdims=True)
                for i in range(end - start):
                    margin = min(i, end - start - 1 - i)
                    if margin > margins[start + i]:
                        margins[start + i] = margin
                        tags[start + i], scores[start + i] = int(probs[i].argmax()), float(probs[i].max())
                if end == n:
                    break
            spans, current = [], None
            for (a, b), label, score in zip(enc.offsets, tags, scores):
                if a == b:
                    continue
                tag = LABELS[label]
                if tag == "O":
                    current = None
                    continue
                kind = tag[2:]
                if tag[0] == "B" or current is None or current[2] != kind:
                    current = [a, b, kind, score]
                    spans.append(current)
                else:
                    current[1], current[3] = b, min(current[3], score)
            result = []
            for a, b, label, score in spans:
                kind = KINDS.get(label)
                if not kind or score < .5:
                    continue
                edges = " \t\r\n" if kind == "SECRET" else " \t\r\n\"',;:"
                while a < b and text[a] in edges:
                    a += 1
                while b > a and text[b - 1] in edges:
                    b -= 1
                if accept_span(text, a, b, kind):
                    result.append(Span(a, b, kind, float(score)))
            # Strong role/surname context supplements model omissions without a threshold change.
            result.extend(Span(a, b, kind, 1.0) for a, b, kind in chinese_name_spans(text))
            result = tuple(result)
            self.inferences += 1
            if use_cache and len(result) <= 4096:
                with self.cache_lock:
                    self.cache[key] = result
                    if len(self.cache) > 1024:
                        self.cache.popitem(last=False)
            return result
        finally:
            self.lock.release()


def detector() -> AstrLinkGuard:
    global _detector
    with _LOAD_LOCK:
        if _detector is None or _detector.directory != model_dir():
            _detector = AstrLinkGuard(model_dir())
        return _detector


def settings():
    mode = os.getenv("IT_COPILOT_MASK_MODEL", "on").lower()
    try:
        threshold = float(os.getenv("IT_COPILOT_MASK_MODEL_THRESHOLD", "0.8"))
        timeout = float(os.getenv("IT_COPILOT_MASK_MODEL_TIMEOUT_S", "10"))
        if mode not in {"off", "auto", "on"} or not .5 <= threshold <= 1 or not 0 < timeout <= 300:
            raise ValueError
    except ValueError:
        raise LocalPIIError("invalid local masking model configuration") from None
    return mode, threshold, timeout


async def find_texts(texts: list[str]) -> dict[str, tuple[Span, ...]]:
    mode, threshold, timeout = settings()
    if mode == "off":
        return {}
    if not installed():
        _start_download()
        metrics.LOCAL_PII_REQUESTS.labels(outcome="downloading").inc()
        if mode == "auto":
            return {}
        raise LocalPIIError("AstrLink is downloading; request held until model is ready")
    started = time.monotonic()
    deadline = started + timeout
    texts = [text for text in dict.fromkeys(texts) if len(text) >= 3]
    cached = {}
    def run():
        model = detector()
        return {**cached, **{text: tuple(s for s in model.detect(text, deadline=deadline) if s.score >= threshold)
                            for text in texts if text not in cached}}
    try:
        # Fully cached requests must also bypass a thread pool full of misses.
        model = _detector
        if model is not None and model.directory == model_dir():
            for text in texts:
                if time.monotonic() >= deadline:
                    raise LocalPIIError("AstrLink cache lookup timed out")
                value = model.cached(text)
                if value is not None:
                    cached[text] = tuple(s for s in value if s.score >= threshold)
        if time.monotonic() >= deadline:
            raise LocalPIIError("AstrLink detection timed out")
        result = cached if len(cached) == len(texts) else await asyncio.wait_for(asyncio.to_thread(run), max(0, deadline-time.monotonic()))
        metrics.LOCAL_PII_REQUESTS.labels(outcome="success").inc()
        return result
    except Exception:
        metrics.LOCAL_PII_REQUESTS.labels(outcome="blocked" if mode == "on" else "rules_fallback").inc()
        if mode == "on":
            raise LocalPIIError("AstrLink detection failed; model request held") from None
        return {}
    finally:
        metrics.LOCAL_PII_LATENCY.observe(time.monotonic() - started)
