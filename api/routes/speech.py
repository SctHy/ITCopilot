"""Authenticated voice-to-draft endpoint; never executes an Agent turn."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request

from api.deps import current_principal, enforce_rate_limit
from core.identity import Principal
from core.rate_limit import AGENT_TURN
from core import speech

router = APIRouter()


@router.post("/speech/transcribe")
async def transcribe(request: Request, principal: Principal = Depends(current_principal)):
    await enforce_rate_limit(principal, AGENT_TURN)
    try:
        key = speech.api_key()
        if request.headers.get("content-type", "").split(";")[0] != "audio/wav":
            raise speech.SpeechError(415, "请上传 WAV 录音")
        # Read with a hard bound before buffering/parsing; no temporary audio files.
        audio = bytearray()
        async with asyncio.timeout(30):
            async for chunk in request.stream():
                if len(audio) + len(chunk) > speech.MAX_BYTES:
                    raise speech.SpeechError(413, "录音不能超过 8 MiB")
                audio.extend(chunk)
        return await speech.transcribe(bytes(audio), key)
    except TimeoutError:
        raise HTTPException(408, "录音上传超时") from None
    except speech.SpeechError as exc:
        raise HTTPException(exc.status, exc.message) from None
