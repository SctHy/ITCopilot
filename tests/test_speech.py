"""Voice boundary checks with synthetic WAV and a mocked upstream (no paid calls)."""
import io
import wave
from unittest.mock import AsyncMock

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import state
from api.deps import current_principal
from api.routes import speech as route
from core import speech
from core.identity import Principal
from core.rate_limit import AGENT_TURN, MemoryRateLimiter, RateRule


def wav(seconds=1, rate=16000):
    out = io.BytesIO()
    with wave.open(out, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b'\0\0' * int(seconds * rate))
    return out.getvalue()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_ASR_API_KEY', 'test-asr-key')
    monkeypatch.setattr(state, 'rate_limiter', None)
    monkeypatch.setattr(state, 'tenants', None)
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[current_principal] = lambda: Principal('alice', 'alice', 'IT')
    return TestClient(app)


def test_authentication_required(client):
    client.app.dependency_overrides.clear()
    assert client.post('/speech/transcribe', content=wav(), headers={'Content-Type': 'audio/wav'}).status_code == 401


def test_valid_audio_forwarded_as_multipart(client, monkeypatch):
    post = AsyncMock(return_value=httpx2.Response(200, json={'text': '  申请 GitLab 权限  ', 'duration_ms': 1000}))
    monkeypatch.setattr(httpx2.AsyncClient, 'post', post)
    audio = wav()
    response = client.post('/speech/transcribe', content=audio, headers={'Content-Type': 'audio/wav'})
    assert response.json() == {'text': '申请 GitLab 权限'}
    assert post.call_args.args == (speech.ASR_URL,)
    assert post.call_args.kwargs['files']['file'][1] == audio
    assert post.call_args.kwargs['headers']['Authorization'] == 'Bearer test-asr-key'


@pytest.mark.parametrize('audio,status', [(b'bad', 400), (wav()[:-10], 400), (wav(0), 400), (wav(121), 400), (b'x' * (speech.MAX_BYTES + 1), 413)])
def test_invalid_audio_never_sent(client, monkeypatch, audio, status):
    post = AsyncMock()
    monkeypatch.setattr(httpx2.AsyncClient, 'post', post)
    response = client.post('/speech/transcribe', content=audio, headers={'Content-Type': 'audio/wav'})
    assert response.status_code == status
    post.assert_not_called()


def test_content_type_and_rate_limit(client, monkeypatch):
    assert client.post('/speech/transcribe', content=wav()).status_code == 415
    monkeypatch.setattr(state, 'rate_limiter', MemoryRateLimiter({AGENT_TURN: RateRule(1, 60)}))
    client.post('/speech/transcribe', content=b'bad', headers={'Content-Type': 'audio/wav'})
    assert client.post('/speech/transcribe', content=wav(), headers={'Content-Type': 'audio/wav'}).status_code == 429


@pytest.mark.parametrize('status,expected', [(401, 502), (429, 429), (503, 502), (504, 504)])
def test_upstream_errors_do_not_expose_payload(client, monkeypatch, status, expected):
    monkeypatch.setattr(httpx2.AsyncClient, 'post', AsyncMock(return_value=httpx2.Response(status, text='private upstream details')))
    response = client.post('/speech/transcribe', content=wav(), headers={'Content-Type': 'audio/wav'})
    assert response.status_code == expected
    assert 'private' not in response.text


def test_timeout_and_invalid_result(client, monkeypatch):
    monkeypatch.setattr(httpx2.AsyncClient, 'post', AsyncMock(side_effect=httpx2.ReadTimeout('private details')))
    assert client.post('/speech/transcribe', content=wav(), headers={'Content-Type': 'audio/wav'}).status_code == 504
    monkeypatch.setattr(httpx2.AsyncClient, 'post', AsyncMock(return_value=httpx2.Response(200, json={'text': ['bad']})))
    assert client.post('/speech/transcribe', content=wav(), headers={'Content-Type': 'audio/wav'}).status_code == 502


def test_key_fallback_restricted_to_deepseek(monkeypatch):
    monkeypatch.delenv('DEEPSEEK_ASR_API_KEY', raising=False)
    monkeypatch.setenv('OPENAI_API_KEY', 'existing-key')
    for base in ['https://other.example/v1', 'https://api.deepseek.com.evil.test', 'http://api.deepseek.com']:
        monkeypatch.setenv('OPENAI_BASE_URL', base)
        with pytest.raises(speech.SpeechError):
            speech.api_key()
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://api.deepseek.com/v1')
    assert speech.api_key() == 'existing-key'
