"""Voice transcription (WP17, D5, D52). VOICE_BACKEND picks one of three transcribers:
`MlxWhisper` in the app on the demo Mac (no network); `WhisperServer`, our own Whisper server
(OpenAI-compatible, e.g. speaches, in a container on our infrastructure); `ElevenLabsScribe`, the
ElevenLabs cloud API, opt-in only, since the audio then leaves our infrastructure (D52).

- The browser records, decodes and resamples the recording itself and uploads a small 16 kHz mono
  16-bit PCM WAV (`static/voice.js`), so the app needs no ffmpeg and checks one simple format.
- `read_wav` is the upload check (system-design §9, "Malicious upload"): only that WAV format,
  bounded size and length, decoded with the standard library. Anything else raises `BadAudio`.
- `MlxWhisper` is the `LocalTranscriber` the gateway calls (`models.VoiceGateway`): its
  `endpoint` is None, it loads the pinned model from the local cache and transcribes in Czech.
  The audio is never stored or logged; the audit keeps its size and hash and the transcript.
- The transcript is never trusted on its own: the patient sees it in the text box, corrects it if
  needed and sends it with the write flow, which treats it like typed text (G5).
"""

import io
import struct
import threading
import wave
from dataclasses import dataclass, field

import httpx

SAMPLE_RATE = 16_000
MAX_SECONDS = 300  # a long check-in message; no time limit on recording, a limit on one upload
MIN_SECONDS = 0.3
MAX_BYTES = 44 + SAMPLE_RATE * 2 * MAX_SECONDS


class BadAudio(ValueError):
    """The upload is not the 16 kHz mono 16-bit WAV the page sends, or is too short or long."""


def read_wav(data: bytes) -> bytes:
    """Check the upload and return its raw 16-bit little-endian PCM frames."""
    if len(data) > MAX_BYTES:
        raise BadAudio("too long")
    if not data.startswith(b"RIFF") or data[8:12] != b"WAVE":
        raise BadAudio("not a WAV file")
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            shape = (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype())
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError, RuntimeError, ValueError, struct.error) as error:
        raise BadAudio("unreadable WAV") from error
    if shape != (1, 2, SAMPLE_RATE, "NONE"):
        raise BadAudio("not 16 kHz mono 16-bit PCM")
    seconds = len(frames) / (2 * SAMPLE_RATE)
    if seconds < MIN_SECONDS:
        raise BadAudio("too short")
    if seconds > MAX_SECONDS:
        raise BadAudio("too long")
    return frames


@dataclass
class MlxWhisper:
    """In-process Czech speech-to-text with the pinned mlx-whisper model (Apple Silicon only)."""

    model: str
    endpoint: str | None = None  # in-process: the gateway refuses any endpoint
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __call__(self, audio: bytes) -> str:
        import mlx_whisper
        import numpy as np

        frames = read_wav(audio)
        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
        with self._lock:  # one transcription at a time on the GPU
            result = mlx_whisper.transcribe(
                samples,
                path_or_hf_repo=self.model,
                language="cs",
                task="transcribe",
                temperature=0.0,
                condition_on_previous_text=False,
                verbose=None,
            )
        return " ".join(str(result.get("text", "")).split())

    def warm_up(self) -> None:
        """Load the model once on invented silence (no patient data), so the first patient
        recording doesn't wait for the load (~9 s cold, ~2 s warm on the demo Mac)."""
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(bytes(SAMPLE_RATE * 2))
        silence = buffer.getvalue()
        self(silence)


ELEVENLABS_URL = "https://api.elevenlabs.io"
TIMEOUT_SECONDS = 120.0


def _post(transport, url: str, headers: dict, audio: bytes, data: dict) -> dict:
    """One multipart upload of the checked WAV; no redirects, no proxy settings from the
    environment, so the audio reaches exactly the origin the gateway allowed."""
    with httpx.Client(
        timeout=TIMEOUT_SECONDS, follow_redirects=False, trust_env=False, transport=transport
    ) as client:
        response = client.post(
            url, headers=headers, files={"file": ("audio.wav", audio, "audio/wav")}, data=data
        )
    response.raise_for_status()
    return response.json()


@dataclass
class WhisperServer:
    """Our own Whisper server (`POST /v1/audio/transcriptions`, OpenAI-compatible), in Czech."""

    endpoint: str  # base URL, e.g. http://localhost:8001
    model: str
    api_key: str | None = field(default=None, repr=False)
    backend: str = "local"  # our infrastructure (audit)
    transport: httpx.BaseTransport | None = field(default=None, repr=False)

    def __call__(self, audio: bytes) -> str:
        read_wav(audio)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        reply = _post(
            self.transport,
            f"{self.endpoint.rstrip('/')}/v1/audio/transcriptions",
            headers,
            audio,
            {"model": self.model, "language": "cs", "temperature": "0", "response_format": "json"},
        )
        return " ".join(str(reply.get("text", "")).split())


@dataclass
class ElevenLabsScribe:
    """ElevenLabs speech-to-text (`POST /v1/speech-to-text`), in Czech. A cloud API: only with
    VOICE_BACKEND=elevenlabs, and the audit names its host (D52)."""

    api_key: str = field(repr=False)
    model: str = "scribe_v1"
    endpoint: str = ELEVENLABS_URL
    backend: str = "elevenlabs"
    transport: httpx.BaseTransport | None = field(default=None, repr=False)

    def __call__(self, audio: bytes) -> str:
        read_wav(audio)
        reply = _post(
            self.transport,
            f"{self.endpoint}/v1/speech-to-text",
            {"xi-api-key": self.api_key},
            audio,
            {
                "model_id": self.model,
                "language_code": "ces",
                "tag_audio_events": "false",
                "diarize": "false",
            },
        )
        return " ".join(str(reply.get("text", "")).split())
