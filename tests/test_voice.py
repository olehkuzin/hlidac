"""WP17: voice input. The upload check, the in-process transcriber, the route (access, CSRF,
nothing saved until the patient sends the checked text) and the page controls. No live model:
mlx-whisper is replaced by a stand-in; invented audio only (silence and a tone), no patient data."""

import io
import math
import socket
import struct
import sys
import types
import wave

import pytest
from patient_helpers import make_app
from security.auth_helpers import PATIENT_A, PATIENT_B, csrf, login, new_client
from test_patient_app import follow, logged_in, post, start

from hlidac import transcription
from hlidac.clock import DemoClock
from hlidac.models import EndpointNotAllowed, ModelCallFailed, VoiceGateway
from hlidac.store import InMemoryStore
from hlidac.transcription import BadAudio, MlxWhisper, read_wav

T = "2026-10-17T08:00:00+02:00"


def make_wav(seconds=1.0, rate=16_000, channels=1, width=2) -> bytes:
    frames = int(seconds * rate)
    samples = [int(8000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(frames)]
    pcm = struct.pack(f"<{frames}h", *samples) if width == 2 else bytes(frames * width)
    if channels == 2:
        pcm = b"".join(pcm[i : i + 2] * 2 for i in range(0, len(pcm), 2))
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return out.getvalue()


# --- the upload check --------------------------------------------------------------------------


def test_read_wav_accepts_the_pages_format():
    assert len(read_wav(make_wav(1.0))) == 32_000


@pytest.mark.parametrize(
    "audio",
    [
        b"",
        b"not audio at all",
        b"RIFF\x00\x00\x00\x00WAVEgarbage",
        make_wav(rate=44_100),
        make_wav(channels=2),
        make_wav(width=1),
        make_wav(seconds=0.1),
    ],
)
def test_read_wav_rejects_anything_else(audio):
    with pytest.raises(BadAudio):
        read_wav(audio)


def test_read_wav_rejects_a_too_long_upload_before_decoding():
    with pytest.raises(BadAudio):
        read_wav(b"RIFF" + bytes(transcription.MAX_BYTES))


# --- the in-process transcriber ----------------------------------------------------------------


@pytest.fixture
def fake_mlx(monkeypatch):
    calls = []
    module = types.ModuleType("mlx_whisper")

    def transcribe(samples, **options):
        calls.append((samples, options))
        return {"text": "  Dnes jsem zvracela.  "}

    module.transcribe = transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", module)
    return calls


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("voice must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_mlx_whisper_is_in_process_czech_and_audited_without_the_audio(fake_mlx, no_network):
    pytest.importorskip("numpy")
    store = InMemoryStore()
    gateway = VoiceGateway(store, DemoClock.from_as_of(__import__("datetime").date(2026, 10, 17)))
    whisper = MlxWhisper("pinned/whisper-model")
    assert whisper.endpoint is None
    audio = make_wav(1.0)
    assert gateway.transcribe(audio, whisper, target="checkin:c1") == "Dnes jsem zvracela."
    [(samples, options)] = fake_mlx
    assert len(samples) == 16_000 and max(abs(samples)) <= 1.0
    assert options["language"] == "cs" and options["path_or_hf_repo"] == "pinned/whisper-model"
    [entry] = store.list_audit("checkin:c1")
    call = entry.model_call
    assert (call.purpose, call.backend, call.endpoint_host) == (
        "transcription",
        "local",
        "in-process",
    )
    assert call.request_text.startswith(f"[audio {len(audio)} B, sha256 ")
    assert call.response_text == "Dnes jsem zvracela."


def test_a_transcriber_with_an_endpoint_is_refused_and_never_hears_the_audio():
    store = InMemoryStore()
    gateway = VoiceGateway(store, DemoClock.from_as_of(__import__("datetime").date(2026, 10, 17)))
    whisper = MlxWhisper("pinned/whisper-model", endpoint="https://api.openai.com/v1/audio")
    with pytest.raises(EndpointNotAllowed):
        gateway.transcribe(make_wav(), whisper, target="checkin:c1")
    [entry] = store.list_audit("checkin:c1")
    assert entry.action == "model_call_refused"


def test_a_failed_transcription_is_audited_and_raised():
    store = InMemoryStore()
    gateway = VoiceGateway(store, DemoClock.from_as_of(__import__("datetime").date(2026, 10, 17)))

    class Broken:
        model, endpoint = "broken", None

        def __call__(self, audio):
            raise RuntimeError("model crashed")

    with pytest.raises(ModelCallFailed):
        gateway.transcribe(make_wav(), Broken(), target="checkin:c1")
    [entry] = store.list_audit("checkin:c1")
    assert entry.model_call.ok is False and entry.model_call.error == "RuntimeError"


# --- the route ---------------------------------------------------------------------------------


class Heard:
    def __init__(self, text="Mám horečku."):
        self.text, self.calls = text, []

    def __call__(self, audio, target):
        self.calls.append((len(audio), target))
        return self.text


def upload(client, base, audio, token=None, page=None):
    token = token if token is not None else csrf(client, page or f"{base}/napsat")
    headers = {"content-type": "audio/wav"}
    if token:
        headers["x-csrf-token"] = token
    return client.post(f"{base}/prepis", content=audio, headers=headers)


def test_voice_returns_the_transcript_and_saves_nothing_until_sent():
    heard = Heard()
    app, store, mailbox = make_app(transcribe=heard)
    client = logged_in(app, mailbox)
    base = start(client)
    response = upload(client, base, make_wav())
    assert response.status_code == 200 and response.json() == {"text": "Mám horečku."}
    checkin_id = base.rsplit("/", 1)[-1]
    assert heard.calls == [(len(make_wav()), f"checkin:{checkin_id}")]
    [saved] = store.list_checkins(PATIENT_A.patient_id)
    assert saved.messages == []  # the transcript counts only once the patient sends it

    sent = post(client, f"{base}/napsat", text="Mám horečku.", voice="1")
    assert follow(client, sent) == f"{base}/dalsi"
    [saved] = store.list_checkins(PATIENT_A.patient_id)
    assert [m.text for m in saved.messages] == ["Mám horečku."]
    [entry] = [e for e in store.list_audit(f"checkin:{checkin_id}") if e.action == "checkin_text"]
    assert entry.details["via"] == "voice"


def test_voice_needs_the_csrf_header():
    heard = Heard()
    app, _, mailbox = make_app(transcribe=heard)
    client = logged_in(app, mailbox)
    base = start(client)
    assert upload(client, base, make_wav(), token="").status_code == 403
    assert upload(client, base, make_wav(), token="wrong").status_code == 403
    assert heard.calls == []


def test_voice_is_scoped_to_the_patients_own_checkin():
    heard = Heard()
    app, _, mailbox = make_app(transcribe=heard)
    base = start(logged_in(app, mailbox, PATIENT_A))
    other = new_client(app)
    login(other, mailbox, PATIENT_B.email)
    response = upload(other, base, make_wav(), page="/pacient")
    assert response.status_code == 403
    assert heard.calls == []


def test_voice_needs_a_login():
    heard = Heard()
    app, _, mailbox = make_app(transcribe=heard)
    base = start(logged_in(app, mailbox))
    response = new_client(app).post(
        f"{base}/prepis", content=make_wav(), headers={"content-type": "audio/wav"}
    )
    assert response.status_code in (303, 403)
    assert heard.calls == []


def test_bad_audio_never_reaches_the_transcriber():
    heard = Heard()
    app, store, mailbox = make_app(transcribe=heard)
    client = logged_in(app, mailbox)
    base = start(client)
    response = upload(client, base, b"RIFF....WAVE<script>")
    assert response.status_code == 400 and "Nahrávku se nepodařilo přečíst" in response.json()["error"]
    assert heard.calls == []
    checkin_id = base.rsplit("/", 1)[-1]
    assert any(e.action == "voice_rejected" for e in store.list_audit(f"checkin:{checkin_id}"))


def test_silence_and_failures_give_a_fixed_message():
    app, _, mailbox = make_app(transcribe=Heard(text="   "))
    client = logged_in(app, mailbox)
    base = start(client)
    response = upload(client, base, make_wav())
    assert response.status_code == 422 and "Nic jsme neslyšeli" in response.json()["error"]

    def broken(audio, target):
        raise ModelCallFailed("RuntimeError")

    app, _, mailbox = make_app(transcribe=broken)
    client = logged_in(app, mailbox)
    base = start(client)
    response = upload(client, base, make_wav())
    assert response.status_code == 502 and "Přepis se nepovedl" in response.json()["error"]


def test_voice_off_hides_the_controls_and_refuses_uploads():
    app, _, mailbox = make_app()
    client = logged_in(app, mailbox)
    base = start(client)
    assert "voice.js" not in client.get(base).text
    assert "voice-toggle" not in client.get(f"{base}/napsat").text
    assert upload(client, base, make_wav()).status_code == 503


def test_voice_on_shows_the_controls_hidden_until_the_browser_can_record():
    app, _, mailbox = make_app(transcribe=Heard())
    client = logged_in(app, mailbox)
    base = start(client)
    chat = client.get(base).text  # the chat's composer: voice controls shown by voice.js
    assert "Namluvit" in chat and 'id="voice" hidden' in chat and "voice.js" in chat
    write = client.get(f"{base}/napsat").text
    assert 'id="voice" hidden' in write and 'id="voice-toggle"' in write
    assert 'role="status"' in write and "voice.js" in write
