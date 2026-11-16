"""本地模型失败语义与真正的 AstrLink/HTTP 出站集成；缺少权重时真实测试明确跳过。"""
import asyncio
import json
import time

import httpx2
import pytest

from core import local_pii
from core.local_pii import Span
from core.model_masking import MaskingError, MaskingSession, MaskingTransport, mask_request, masking_scope, prepare_request


@pytest.mark.parametrize("name", ["王小明", "张伟"])
def test_short_chinese_model_spans_are_applied_and_scrubbed(name):
    from core.redaction import redact_text
    with masking_scope("tenant:alice", "c") as session:
        session.findings = {"我叫" + name: (Span(2, 2 + len(name), "PERSON", .99),)}
        body = mask_request({"messages": [{"role": "user", "content": "我叫" + name}]}, session)
        assert body["messages"][0]["content"].startswith("我叫{{PERSON_")
        assert redact_text("联系人" + name + "已确认") == "联系人[REDACTED]已确认"


@pytest.mark.parametrize("mode", ["on", "auto", "off"])
def test_model_failure_policy_before_network(monkeypatch, mode):
    monkeypatch.setenv("IT_COPILOT_MASK_MODEL", mode)
    monkeypatch.setattr(local_pii, "installed", lambda: True)
    def fail():
        raise ValueError("private user content must not appear in errors")
    monkeypatch.setattr(local_pii, "detector", fail)
    async def run():
        calls = []
        def upstream(request):
            calls.append(json.loads(request.content))
            return httpx2.Response(200, json={"choices": []})
        async with httpx2.AsyncClient(transport=MaskingTransport(httpx2.MockTransport(upstream))) as client:
            operation = client.post("http://local.test/chat/completions", json={"messages": [{"role": "user", "content": "密码是 Abc!2026"}]})
            if mode == "on":
                with pytest.raises(MaskingError, match="nothing sent"):
                    await operation
                assert not calls
            else:
                await operation
                assert "Abc!2026" not in json.dumps(calls)
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["on", "auto"])
def test_missing_model_starts_download_without_sending_text(monkeypatch, mode):
    monkeypatch.setenv("IT_COPILOT_MASK_MODEL", mode)
    monkeypatch.setattr(local_pii, "installed", lambda: False)
    downloads = []
    monkeypatch.setattr(local_pii, "_start_download", lambda: downloads.append(True))
    if mode == "on":
        with pytest.raises(local_pii.LocalPIIError, match="downloading"):
            asyncio.run(local_pii.find_texts(["private content"]))
    else:
        assert asyncio.run(local_pii.find_texts(["private content"])) == {}
    assert downloads == [True]


def test_checksum_failure_rejects_model(tmp_path):
    for name, (size, _) in local_pii.FILES.items():
        with (tmp_path / name).open("wb") as file:
            file.truncate(size)
    with pytest.raises(local_pii.LocalPIIError, match="integrity"):
        local_pii.verify(tmp_path)


def test_threshold_and_excluded_categories(monkeypatch):
    class Fake:
        def detect(self, text, **kwargs):
            return (Span(0, 3, "PERSON", .79), Span(3, 6, "PERSON", .81))
    monkeypatch.setenv("IT_COPILOT_MASK_MODEL", "on")
    monkeypatch.setenv("IT_COPILOT_MASK_MODEL_THRESHOLD", "0.8")
    monkeypatch.setattr(local_pii, "installed", lambda: True)
    monkeypatch.setattr(local_pii, "detector", lambda: Fake())
    assert asyncio.run(local_pii.find_texts(["abcdef"])) == {"abcdef": (Span(3, 6, "PERSON", .81),)}
    assert not ({"ip_address", "url", "private_date"} & local_pii.KINDS.keys())


def test_explicit_model_timeout_does_not_mutate_session(monkeypatch):
    class Slow:
        def detect(self, text, **kwargs):
            time.sleep(.03)
            return (Span(0, 3, "PERSON", .99),)
    monkeypatch.setenv("IT_COPILOT_MASK_MODEL", "on")
    monkeypatch.setenv("IT_COPILOT_MASK_MODEL_TIMEOUT_S", "0.005")
    monkeypatch.setattr(local_pii, "installed", lambda: True)
    monkeypatch.setattr(local_pii, "detector", lambda: Slow())
    session = MaskingSession()
    async def run():
        with pytest.raises(local_pii.LocalPIIError):
            await prepare_request({"messages": [{"role": "user", "content": "王小明"}]}, session)
        await asyncio.sleep(.05)
        assert not session.values and not session.findings
    asyncio.run(run())


@pytest.mark.skipif(not local_pii.installed(), reason="requires verified local AstrLink weights; never downloads in tests")
def test_real_guard_four_previous_blind_spots_wire_and_restore(monkeypatch):
    monkeypatch.setenv("IT_COPILOT_MASK_MODEL", "on")
    async def run():
        values = [("我叫王小明", "王小明"), ("送到北京市测试路12号", "北京市测试路12号"),
                  ("my password is sunflower", "sunflower"), ("联系电话 +44 20 7946 0958", "+44 20 7946 0958")]
        for text, secret in values:
            session = MaskingSession()
            body = {"messages": [{"role": "user", "content": text}]}
            await prepare_request(body, session)
            masked = mask_request(body, session)
            assert secret not in json.dumps(masked, ensure_ascii=False)
            token = session.reverse[secret]
            assert json.loads(session.restore(json.dumps({"value": token}), tool=True))["value"] == secret
    asyncio.run(run())


@pytest.mark.skipif(not local_pii.installed(), reason="requires local AstrLink weights")
def test_real_guard_long_input_overlap_unicode_and_cache():
    model = local_pii.detector()
    text = "这是普通的测试说明。" * 200 + "请联系张伟，收货地址北京市海淀区中关村大街27号。"
    first = model.detect(text, deadline=time.monotonic() + 30)
    assert any(text[s.start:s.end] == "张伟" and s.score >= .8 for s in first)
    assert any("中关村" in text[s.start:s.end] and s.kind == "ADDRESS" for s in first)
    hits = model.cache_hits
    assert model.detect(text, deadline=time.monotonic() + 30) == first
    assert model.cache_hits == hits + 1
    assert all(isinstance(key, bytes) for key in model.cache)


def cached_guard(text, value=()):
    import hashlib
    import threading
    from collections import OrderedDict
    guard = local_pii.AstrLinkGuard.__new__(local_pii.AstrLinkGuard)
    guard.directory = local_pii.model_dir()
    guard.lock = threading.Lock()
    guard.cache_lock = threading.Lock()
    guard.cache = OrderedDict({hashlib.sha256(text.encode()).digest(): value})
    guard.cache_hits = guard.inferences = 0
    return guard


def test_cached_empty_result_does_not_wait_for_inference_lock():
    guard = cached_guard("ordinary cached text")
    with guard.lock:
        assert guard.detect("ordinary cached text", deadline=time.monotonic()+.01) == ()
        with pytest.raises(local_pii.LocalPIIError, match="queue timed out"):
            guard.detect("new uncached text", deadline=time.monotonic()+.01)
    assert guard.cache_hits == 1


def test_cached_request_bypasses_saturated_executor_and_applies_current_threshold(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    text = "cached person"
    guard = cached_guard(text, (Span(0, 6, "PERSON", .79), Span(7, 13, "PERSON", .91)))
    monkeypatch.setattr(local_pii, "_detector", guard)
    monkeypatch.setattr(local_pii, "installed", lambda: True)
    monkeypatch.setenv("IT_COPILOT_MASK_MODEL", "on")
    monkeypatch.setenv("IT_COPILOT_MASK_MODEL_THRESHOLD", "0.8")
    async def run():
        loop = asyncio.get_running_loop()
        pool = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(pool)
        release = threading.Event()
        busy = pool.submit(release.wait, 2)
        guard.lock.acquire()
        try:
            result = await asyncio.wait_for(local_pii.find_texts([text, text]), .5)
            assert result == {text: (Span(7, 13, "PERSON", .91),)}
            assert not busy.done(), "must return while the worker is still occupied"
        finally:
            guard.lock.release()
            release.set()
            await asyncio.wrap_future(busy)
    asyncio.run(run())


@pytest.mark.skipif(not local_pii.installed(), reason="requires local AstrLink weights")
def test_concurrent_same_text_miss_infers_once():
    from concurrent.futures import ThreadPoolExecutor
    model = local_pii.detector()
    text = "Cache coalescing probe: 联系电话 +44 20 7946 0958"
    import hashlib
    with model.cache_lock:
        model.cache.pop(hashlib.sha256(text.encode()).digest(), None)
    before = model.inferences
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: model.detect(text, deadline=time.monotonic()+10), range(16)))
    assert all(result == results[0] for result in results)
    assert model.inferences == before + 1


def test_all_cached_request_still_obeys_deadline(monkeypatch):
    text = "cached deadline probe"
    model = cached_guard(text)
    monkeypatch.setattr(local_pii, "_detector", model)
    monkeypatch.setattr(local_pii, "installed", lambda: True)
    monkeypatch.setenv("IT_COPILOT_MASK_MODEL", "on")
    monkeypatch.setenv("IT_COPILOT_MASK_MODEL_TIMEOUT_S", "0.001")
    cached = model.cached
    def slow_cache(value):
        time.sleep(.01)
        return cached(value)
    monkeypatch.setattr(model, "cached", slow_cache)
    with pytest.raises(local_pii.LocalPIIError, match="held"):
        asyncio.run(local_pii.find_texts([text]))
