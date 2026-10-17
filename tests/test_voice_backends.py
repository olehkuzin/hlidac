"""D52: the two network voice backends. Our own Whisper server and the opt-in ElevenLabs API get
exactly the checked WAV in Czech, the gateway lets audio reach only the configured origin, and the
audit names where it went. Invented audio (silence) only; no network (mock transport)."""

from datetime import date

import httpx
import pytest
from pydantic import ValidationError
from test_voice import make_wav

from hlidac.app import _voice_engine
from hlidac.clock import DemoClock
from hlidac.config import Settings
from hlidac.models import EndpointNotAllowed, VoiceGateway, check_audio_destination, origin
from hlidac.schemas import ModelCall
from hlidac.store import InMemoryStore
from hlidac.transcription import ELEVENLABS_URL, ElevenLabsScribe, WhisperServer


def recorder(reply: dict):
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=reply)

    return seen, httpx.MockTransport(handle)


def gateway(allowed: frozenset[str]) -> tuple[VoiceGateway, InMemoryStore]:
    store = InMemoryStore()
    return VoiceGateway(store, DemoClock.from_as_of(date(2026, 10, 17)), allowed), store


def test_the_whisper_server_gets_the_wav_in_czech():
    seen, transport = recorder({"text": "  mám   horečku "})
    whisper = WhisperServer("http://whisper.internal:8001", "model-x", "k", transport=transport)
    assert whisper(make_wav()) == "mám horečku"
    [request] = seen
    assert str(request.url) == "http://whisper.internal:8001/v1/audio/transcriptions"
    assert request.headers["authorization"] == "Bearer k"
    body = request.read()
    assert b'name="language"\r\n\r\ncs' in body and b'name="model"\r\n\r\nmodel-x' in body


def test_elevenlabs_gets_the_wav_in_czech_with_its_key():
    seen, transport = recorder({"text": "bolí mě hlava"})
    scribe = ElevenLabsScribe("sk-test", transport=transport)
    assert scribe(make_wav()) == "bolí mě hlava"
    [request] = seen
    assert str(request.url) == f"{ELEVENLABS_URL}/v1/speech-to-text"
    assert request.headers["xi-api-key"] == "sk-test"
    assert b'name="language_code"\r\n\r\nces' in request.read()


def test_audio_reaches_only_an_allowed_origin():
    check_audio_destination(None)  # in-process
    allowed = frozenset({origin("https://api.elevenlabs.io")})
    check_audio_destination("https://api.elevenlabs.io/", allowed)
    for other in ("https://api.openai.com", "http://api.elevenlabs.io", "https://evil.example"):
        with pytest.raises(EndpointNotAllowed):
            check_audio_destination(other, allowed)
    with pytest.raises(EndpointNotAllowed):  # nothing allowed: in-process only
        check_audio_destination("https://api.elevenlabs.io")


def test_the_audit_names_elevenlabs_and_never_keeps_the_audio():
    _, transport = recorder({"text": "mám horečku"})
    scribe = ElevenLabsScribe("sk-test", transport=transport)
    voice, store = gateway(frozenset({origin(ELEVENLABS_URL)}))
    assert voice.transcribe(make_wav(), scribe, target="checkin:c1") == "mám horečku"
    [entry] = [e for e in store.list_audit() if e.action == "model_call"]
    call = entry.model_call
    assert (call.backend, call.endpoint_host) == ("elevenlabs", "api.elevenlabs.io")
    assert call.request_text.startswith("[audio ")  # size and hash only


def test_a_transcriber_swapped_to_another_host_is_refused_before_sending():
    seen, transport = recorder({"text": "x"})
    scribe = ElevenLabsScribe("sk-test", endpoint="https://evil.example", transport=transport)
    voice, store = gateway(frozenset({origin(ELEVENLABS_URL)}))
    with pytest.raises(EndpointNotAllowed):
        voice.transcribe(make_wav(), scribe, target="checkin:c1")
    assert seen == []
    assert [e.action for e in store.list_audit()] == ["model_call_refused"]


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({"voice_backend": "elevenlabs"}, None),  # no key: no voice button
        ({"voice_backend": "elevenlabs", "elevenlabs_api_key": "sk-x"}, ElevenLabsScribe),
        ({"voice_backend": "server"}, None),  # no URL
        ({"voice_backend": "server", "whisper_server_url": "http://w:8001"}, WhisperServer),
    ],
)
def test_voice_backend_setting(settings, expected):
    engine = _voice_engine(Settings(_env_file=None, **settings))
    assert engine is None if expected is None else isinstance(engine, expected)


def test_elevenlabs_is_never_a_text_model():
    with pytest.raises(ValidationError):
        ModelCall(
            purpose="extraction",
            data_class="PATIENT",
            backend="elevenlabs",
            endpoint_host="api.elevenlabs.io",
            model="scribe_v1",
            request_text="x",
            response_text="y",
            duration_ms=1,
            ok=True,
        )
