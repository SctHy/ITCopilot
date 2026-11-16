"""Short voice input via DeepSeek ASR. Audio stays in memory and is never logged."""
import io
import os
import wave
from urllib.parse import urlsplit

import httpx2

MAX_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 120
ASR_URL = "https://api.deepseek.com/v1/tools/asr"


class SpeechError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(message)


def api_key() -> str:
    key = os.getenv("DEEPSEEK_ASR_API_KEY", "").strip()
    # Never send a different provider's key to DeepSeek.
    base = urlsplit(os.getenv("OPENAI_BASE_URL", ""))
    if not key and base.scheme == "https" and base.hostname == "api.deepseek.com":
        key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise SpeechError(503, "语音识别未配置，请联系管理员设置 DeepSeek API Key")
    return key


def validate_wav(audio: bytes) -> None:
    if len(audio) > MAX_BYTES:
        raise SpeechError(413, "录音不能超过 8 MiB")
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            frames, rate = wav.getnframes(), wav.getframerate()
            if wav.getsampwidth() != 2 or wav.getcomptype() != "NONE" or wav.getnchannels() != 1:
                raise ValueError()
            if rate not in {8000, 16000, 22050, 24000, 32000, 44100, 48000}:
                raise ValueError()
            if frames <= 0 or frames / rate > MAX_SECONDS:
                raise SpeechError(400, "录音须大于 0 秒且不超过 120 秒")
            if len(wav.readframes(frames)) != frames * 2:
                raise ValueError()
    except (wave.Error, EOFError, ValueError):
        raise SpeechError(400, "请上传完整的单声道 16 位 PCM WAV 录音") from None


async def transcribe(audio: bytes, key: str) -> dict:
    validate_wav(audio)
    try:
        async with httpx2.AsyncClient(timeout=60.0) as client:
            response = await client.post(ASR_URL, headers={"Authorization": f"Bearer {key}"},
                                         files={"file": ("speech.wav", audio, "audio/wav")})
    except httpx2.TimeoutException:
        raise SpeechError(504, "语音识别超时，请稍后重试") from None
    except httpx2.RequestError:
        raise SpeechError(502, "暂时无法连接语音识别服务") from None
    if response.status_code != 200:
        status, message = {
            400: (400, "语音服务无法识别此录音"),
            413: (413, "录音超过语音服务限制"),
            429: (429, "语音识别繁忙，请稍后重试"),
            408: (504, "语音上传超时，请重试"),
            504: (504, "语音识别超时，请重试"),
        }.get(response.status_code, (502, "语音识别服务暂不可用，请稍后重试或联系管理员"))
        raise SpeechError(status, message)
    try:
        data = response.json()
        text = data["text"]
        if not isinstance(text, str) or len(text) > 8000:
            raise ValueError()
        return {"text": text.strip()}
    except (ValueError, KeyError, TypeError):
        raise SpeechError(502, "语音识别服务返回了无效结果") from None
