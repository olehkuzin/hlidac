"""The model gateway (WP7, D5, guard G6). Every model call in the app goes through here.

- Patient text goes only to allow-listed endpoints: the team's Azure OpenAI deployment (HTTPS,
  exactly the configured host, which must be an Azure OpenAI domain or the organisers' Azure API
  Management gateway in front of it, `*.azure-api.net`, D39) or a local model on this
  machine (a loopback address). Anything else raises `EndpointNotAllowed`. It is checked when the
  client is built, before every call, and on every HTTP request the client actually sends; the
  client follows no redirects and ignores proxy settings, so the checked host is the destination.
- Patient text is pseudonymised before it is sent, on both backends (the spec asks for it before
  non-local calls; masking local calls too keeps both backends comparable for the evaluation):
  the patient's known names, birth numbers, phone numbers, email and street addresses.
- No records: a call carries only approved instructions (Questionnaire items, schema) and the
  current message text. There is no parameter for context, identifiers or dates, and instructions
  or a schema that contain a known name or an identifier pattern are refused.
- Deterministic as far as the backend allows (G1): temperature 0, a fixed seed, strict JSON-schema
  output, the model pinned in config (the Azure deployment, or a local model tag). Reasoning models
  accept only their default temperature: with `AZURE_OPENAI_REASONING_EFFORT` set, the call sends
  that effort and no temperature (D39); the seed, the schema and the pin stay, and the audit
  records the model version that answered.
- Audio never leaves the app process: transcription runs only in-process (mlx-whisper, WP17).
- Every call, failed ones too, is written to the audit exactly as sent (inside IRIS, principle 8),
  and so is every refused attempt (without the text).
"""

import hashlib
import ipaddress
import json
import logging
import re
import time
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from openai import APIStatusError, AzureOpenAI, DefaultHttpxClient, OpenAI

from hlidac import audit
from hlidac.clock import Clock
from hlidac.config import Settings
from hlidac.schemas import DataClass, ModelBackend, ModelCall
from hlidac.store import Store

SEED = 20261017
TIMEOUT_SECONDS = 30.0  # S3 targets ~10 s per message; fail fast into a visible error
# Structured outputs (json_schema) need this Azure api-version or later.
MIN_AZURE_API_VERSION = "2024-08-01"
# Hosts of Azure OpenAI deployments (classic resources and AI Foundry), and Azure API Management,
# which the organisers put in front of their Azure OpenAI deployments (D39). Never api.openai.com.
# Only the one configured host passes (`pinned_host`), never the whole domain.
AZURE_HOST_SUFFIXES = (
    ".openai.azure.com",
    ".cognitiveservices.azure.com",
    ".services.ai.azure.com",
    ".azure-api.net",
)
# The OpenAI v1 surface of Azure OpenAI: no api-version, the key in the `api-key` header.
AZURE_V1_PATH = "/openai/v1"
IN_PROCESS = "in-process"

# At DEBUG level the SDK and the HTTP client log request options, which include the messages.
for _name in ("openai", "httpx", "httpx2"):
    logging.getLogger(_name).setLevel(logging.WARNING)


class EndpointNotAllowed(RuntimeError):
    """Patient data was about to go somewhere it may not go (D5)."""


class PatientDataInInstructions(ValueError):
    """Instructions and schemas are approved, public content; patient data goes only in the
    pseudonymised message text."""


class ModelCallFailed(RuntimeError):
    """The backend failed or returned no usable text; the call is in the audit as failed."""


# --- the endpoint guard -------------------------------------------------------------------------


def _host(url: str) -> str:
    host = urlsplit(url).hostname
    if not host:
        raise EndpointNotAllowed("the model endpoint has no host")
    return host.lower()


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_azure_openai(url: str) -> bool:
    return urlsplit(url).scheme == "https" and _host(url).endswith(AZURE_HOST_SUFFIXES)


def check_endpoint(
    url: str,
    backend: ModelBackend,
    data_class: DataClass = "PATIENT",
    pinned_host: str | None = None,
) -> str:
    """Raise unless `url` is allowed for this backend; return its host for the audit.

    A local backend must be on this machine. An Azure backend must be an Azure OpenAI deployment
    over HTTPS and, once configured, exactly that deployment's host (`pinned_host`), so another
    Azure resource with other data terms is refused too. Public data follows the same allow-list:
    one rule is easier to prove than two.
    """
    host = _host(url)
    if backend == "local" and _is_loopback(host):
        return host
    if backend == "azure" and _is_azure_openai(url) and pinned_host in (None, host):
        return host
    raise EndpointNotAllowed(f"{data_class} data may not go to {host} as a {backend} backend")


def origin(url: str) -> str:
    """scheme://host:port, lower-case, with the default port made explicit."""
    parts = urlsplit(url)
    port = parts.port or {"https": 443, "http": 80}.get(parts.scheme, 0)
    return f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}:{port}"


def check_audio_destination(endpoint: str | None, allowed: frozenset[str] = frozenset()) -> None:
    """Audio goes to an in-process transcriber, or to an origin the voice settings allow (our
    own Whisper server; ElevenLabs only with VOICE_BACKEND=elevenlabs, D52). Anything else
    raises (D5)."""
    if endpoint is not None and origin(endpoint) not in allowed:
        raise EndpointNotAllowed("audio goes only to the configured transcriber")


# --- pseudonymisation ---------------------------------------------------------------------------

_UPPER = "[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]"
# After a weak street signal, the number must look like a house number, not a count, dose or
# date: the end, punctuation (not a date's dot), a postcode, a capitalised town, or "v/ve Town".
_HOUSE_NUMBER_END = (
    rf"(?=\s*(?:$|[,;)!?]|\.(?!\s*\d)|\d{{3}}\s?\d{{2}}|{_UPPER}|(?:v|ve)\s+{_UPPER}))"
)
# Identifier patterns, applied before the names (a masked name would break the email pattern).
# Order matters: birth numbers before phone numbers (both are runs of digits).
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Also "(at)", "zavináč" and dictated "tečka" (Whisper writes dictated addresses out).
    (
        "[EMAIL]",
        re.compile(
            r"[\w.+-]+(?:\s+tečka\s+[\w-]+)*\s*(?:@|\(at\)|\[at\]|\bzavináč\b)\s*[\w-]+"
            r"(?:\s*(?:\.|\btečka\b)\s*[\w-]+)*",
            re.IGNORECASE,
        ),
    ),
    # Czech birth number: 6 digits, an optional slash or dash, 3 or 4 digits (CLAUDE.md §5); or
    # grouped 2-2-2, which then needs the slash or dash, so a date like "12.10.26 1500" survives.
    (
        "[RČ]",
        re.compile(r"(?<!\d)(?:\d{6}\s?[/-]?\s?|\d{2}[\s.]\d{2}[\s.]\d{2}\s?[/-]\s?)\d{3,4}(?!\d)"),
    ),
    # Nine digits starting 2-9 (Czech numbers), grouped 3-3-3 or 3-2-2-2, with an optional
    # +420 / 00420. Readings like "110 115 120" don't start a Czech number pattern often enough
    # to matter; when they do, they are masked (information lost, nothing leaked).
    (
        "[TELEFON]",
        re.compile(
            r"(?<![\d+])(?:(?:\+|00)\s?\d{3}[\s.-]*)?"
            r"(?:[2-9]\d{2}[\s./-]{0,2}\d{3}[\s./-]{0,2}\d{3}"
            r"|[2-9]\d{2}[\s.-]?\d{2}[\s.-]?\d{2}[\s.-]?\d{2})(?!\d)"
        ),
    ),
    # Street addresses. A capitalised word and a number is not enough: "Teplota 38", "Tlak 140/90",
    # "Poslední 3 dny" and "Na CT 12. 10." must survive. Either a street keyword followed only by
    # capitalised words ("ulice Dlouhá 5", "namesti Miru 5"), or a weak signal (a street-name
    # ending: Vinohradská, Masarykova, Lipová, Komenského, Zahradní; or a preposition street:
    # Na Příkopě, V Úvalu) with a number that looks like a house number. Then an optional
    # postcode and town.
    (
        "[ADRESA]",
        re.compile(
            r"(?:\b(?:ul\.|ulice|ulici|n[aá]m\.|n[aá]m[eě]st[ií]|t[rř][ií]da|t[rř]\.|n[aá]b[rř]\."
            rf"|n[aá]b[rř]e[zž][ií])\s+(?:{_UPPER}\w*\.?\s+){{0,3}}"
            r"\d+(?:/\d+)?[a-wyzA-Z]?\b"
            r"|"
            rf"(?:\b(?:{_UPPER}\w*\s+){{0,2}}{_UPPER}\w*"
            r"(?:sk[aáé]|ck[aáé]|ov[aáo]|[eé]ho|[^\W\daeiouyáéěíóúůý]n[ií])\s+"
            rf"|\b(?:Na|U|V|Ve|K|Ke|Pod|Nad|Za|Před|Pred)\s+{_UPPER}\w*\s+)"
            rf"\d+(?:/\d+)?[a-wyzA-Z]?\b{_HOUSE_NUMBER_END}"
            r")"
            rf"(?:,?\s*\d{{3}}\s?\d{{2}}(?:\s+{_UPPER}\w*(?:\s+\d+\b)?)?)?"
        ),
    ),
    # A postcode and a town on their own, only after a comma, at a line start or after "PSČ", so
    # "tlak 140 90 Puls 96" survives.
    (
        "[ADRESA]",
        re.compile(
            r"(?:(?<=,\s)|(?<=,)|^|(?<=PSČ\s)|(?<=PSC\s))\d{3}\s?\d{2}(?:\s+\w+(?:\s+\d+\b)?)?",
            re.MULTILINE,
        ),
    ),
]
NAME_MASK = "[JMÉNO]"
_WORD = re.compile(r"\w+")
# Czech declension endings, without diacritics: "Jana" → "Janě", "Novák" → "Novákovi",
# "Nováková" → "Novákové", "Jiří" → "Jiřího". Only these, so a short name can't swallow a
# symptom word ("Teplá" must not mask "teplota" or "teplo").
# fmt: off
_NAME_ENDINGS = {
    "", "a", "e", "i", "u", "y", "ou", "em", "ovi", "ova", "ove", "ovou", "eho", "emu", "ym",
    "ech", "ami", "ach", "ovy", "ovych", "ovym", "ovymi", "ymi", "iho", "imu", "im", "in", "ina",
    "inu", "iny", "ino", "inou", "uv", "ovo", "ovu", "um",
}
# fmt: on


def _fold(text: str) -> str:
    """Lower case without diacritics, so "Novák" matches "novak"."""
    return "".join(
        c for c in unicodedata.normalize("NFD", text.casefold()) if not unicodedata.combining(c)
    )


def _stems(names: Iterable[str]) -> dict[str, set[str]]:
    """Each stem of the known names, with the extra endings allowed for it."""
    stems: dict[str, set[str]] = {}
    for name in names:
        for raw in _WORD.findall(unicodedata.normalize("NFC", name.casefold())):
            token = _fold(raw)
            if len(token) < 2:
                continue
            stems.setdefault(token, set())
            if len(token) >= 3 and token[-1] in "aeiouy":  # Eva → Evě, Jana → Janu
                # The vocative "Jano" only for a short final -a, so "Teplá" leaves "teplo" alone.
                stems.setdefault(token[:-1], set()).update({"o"} if raw.endswith("a") else set())
            dropped = re.fullmatch(r"(.{2,})e([klcnr])", token)  # Marek → Marka, Pavel → Pavla
            if dropped:
                stems.setdefault(dropped[1] + dropped[2], set())
            if raw.endswith("ová") and len(token) > 5:  # Nováková → Novák (the family name)
                stems.setdefault(token[:-3], set())
    return stems


def pseudonymise(text: str, known_names: Iterable[str] = ()) -> str:
    """Mask the identifier patterns, then the patient's known names. Over-masking is accepted; a
    leak is not. A name is known only if the caller passes it (the patient's, a caregiver's): a
    name the patient mentions that we don't know, or a nickname, is not caught, so minimisation
    stays the first line of defence. A known name that is also a common word ("Černý", "Hlava")
    masks that word too; the red-flag scan (WP22) runs on the unmasked text, so this only costs
    extraction detail, and red-flag items left unclear are asked with buttons (G3)."""
    text = unicodedata.normalize("NFC", text)
    for mask, pattern in _PATTERNS:
        text = pattern.sub(mask, text)
    stems = _stems(known_names)
    if not stems:
        return text

    def mask_name(match: re.Match[str]) -> str:
        word = re.sub(r"[\d_]+$", "", _fold(match.group()))  # "novakova76"
        for stem, extra in stems.items():
            if word.startswith(stem) and (
                word[len(stem) :] in _NAME_ENDINGS or word[len(stem) :] in extra
            ):
                return NAME_MASK
        return match.group()

    return _WORD.sub(mask_name, text)


# --- the gateway --------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelReply:
    text: str
    model: str
    model_version: str | None


class LocalTranscriber(Protocol):
    """Speech-to-text (WP17, D52). `endpoint` None means in-process; otherwise the gateway lets the
    audio reach only an origin the voice settings allow."""

    model: str
    endpoint: str | None

    def __call__(self, audio: bytes) -> str: ...


def _is_v1(endpoint: str) -> bool:
    return urlsplit(endpoint).path.rstrip("/") == AZURE_V1_PATH


def _endpoint(settings: Settings) -> str:
    if settings.extraction_backend == "azure":
        endpoint = settings.azure_openai_endpoint or ""
        needed = ("endpoint", "api_key", "deployment")
        if not _is_v1(endpoint):
            needed += ("api_version",)
        missing = [name for name in needed if not getattr(settings, f"azure_openai_{name}")]
        if missing:
            raise ValueError(f"Azure OpenAI is not configured: {', '.join(missing)}")
        if _is_v1(endpoint):
            return endpoint
        if settings.azure_openai_api_version[:10] < MIN_AZURE_API_VERSION:
            raise ValueError(
                f"structured outputs need api-version {MIN_AZURE_API_VERSION} or later"
            )
        if urlsplit(endpoint).path not in ("", "/"):
            raise ValueError(
                "AZURE_OPENAI_ENDPOINT is the resource root, https://<res>.openai.azure.com,"
                f" or an OpenAI v1 base URL ending in {AZURE_V1_PATH}"
            )
        return endpoint
    if not settings.local_model:
        raise ValueError("the local model is not configured (LOCAL_MODEL)")
    return settings.local_base_url


def build_client(settings: Settings, transport: Any = None) -> OpenAI:
    """The `openai` SDK client for the configured backend. No retries here: WP8 bounds them.

    The HTTP client follows no redirects, ignores proxy settings (environment and macOS system
    proxies), and checks the destination of every request it sends. `transport` is for tests.
    """
    backend = settings.extraction_backend
    pinned = check_endpoint(_endpoint(settings), backend)

    def guard(request: Any) -> None:
        check_endpoint(str(request.url), backend, pinned_host=pinned)

    http_client = DefaultHttpxClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
        event_hooks={"request": [guard]},
    )
    if backend == "azure" and _is_v1(settings.azure_openai_endpoint or ""):
        key = settings.azure_openai_api_key.get_secret_value()
        return OpenAI(
            base_url=settings.azure_openai_endpoint,
            api_key=key,
            default_headers={"api-key": key},  # Azure (and its API Management) reads this header
            timeout=TIMEOUT_SECONDS,
            max_retries=0,
            http_client=http_client,
        )
    if backend == "azure":
        return AzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key.get_secret_value(),
            api_version=settings.azure_openai_api_version,
            timeout=TIMEOUT_SECONDS,
            max_retries=0,
            http_client=http_client,
        )
    return OpenAI(
        base_url=settings.local_base_url,
        api_key="local",  # Ollama ignores it; the SDK requires one
        timeout=TIMEOUT_SECONDS,
        max_retries=0,
        http_client=http_client,
    )


def _error_label(error: BaseException) -> str:
    """For the audit: the exception type, the fixed refusal/finish reason we raise, or the API's
    error code (e.g. "BadRequestError:content_filter"). Never a message: those may echo the
    request."""
    if isinstance(error, ModelCallFailed):
        return str(error)
    code = getattr(error, "code", None) if isinstance(error, APIStatusError) else None
    return f"{type(error).__name__}:{code}" if isinstance(code, str) else type(error).__name__


def _refused(error: BaseException) -> EndpointNotAllowed | None:
    """The guard's refusal, if the SDK wrapped it (a request hook raising → APIConnectionError)."""
    while error is not None:
        if isinstance(error, EndpointNotAllowed):
            return error
        error = error.__cause__ or error.__context__
    return None


class ModelGateway:
    def __init__(self, settings: Settings, store: Store, clock: Clock, client: OpenAI) -> None:
        self.backend: ModelBackend = settings.extraction_backend
        self.model = (
            settings.azure_openai_deployment if self.backend == "azure" else settings.local_model
        )
        if not self.model:
            raise ValueError("no model is pinned for the configured backend")
        self._client = client
        # Reasoning models (the organisers' deployments) take an effort instead of temperature 0.
        effort = settings.azure_openai_reasoning_effort if self.backend == "azure" else None
        self._sampling: dict[str, Any] = (
            {"reasoning_effort": effort} if effort else {"temperature": 0}
        )
        self._store = store
        self._clock = clock
        self._pinned = _host(_endpoint(settings)) if self.backend == "azure" else None
        self._check(str(client.base_url), target="gateway")

    def _check(self, url: str, *, target: str, data_class: DataClass = "PATIENT") -> str:
        """The endpoint guard, with every refusal audited (the host only, never the text)."""
        try:
            return check_endpoint(url, self.backend, data_class, self._pinned)
        except EndpointNotAllowed:
            self._refuse(target, host=urlsplit(url).hostname or "none", backend=self.backend)
            raise

    def _refuse(self, target: str, **details: str) -> None:
        audit.record(
            self._store, self._clock.now(), "system", "model_call_refused", target, **details
        )

    def extract_json(
        self,
        *,
        instructions: str,
        patient_text: str,
        schema: dict[str, Any],
        schema_name: str,
        target: str,
        known_names: Iterable[str] = (),
    ) -> ModelReply:
        """One strict-JSON extraction call. `instructions` are approved, public content (the
        Questionnaire items and the task); `patient_text` is the current message only, and is
        pseudonymised here. `target` names what the call is about in the audit (e.g. "message:m1");
        it is never sent. Returns the raw text: validating it is the caller's job (WP8)."""
        known_names = list(known_names)
        schema_json = json.dumps(schema, ensure_ascii=False, sort_keys=True)
        for part in (instructions, schema_json, schema_name):
            if pseudonymise(part, known_names) != unicodedata.normalize("NFC", part):
                self._refuse(target, reason="patient_data_in_instructions")
                raise PatientDataInInstructions("instructions and schemas carry no patient data")
        # Checked again right before sending, in case the client was swapped after construction.
        host = self._check(str(self._client.base_url), target=target)
        sent_text = pseudonymise(patient_text, known_names)
        # Exactly what the model saw besides the message (principle 8).
        details = {"instructions": instructions, "schema_name": schema_name, "schema": schema_json}
        details |= {name: str(value) for name, value in self._sampling.items()}
        messages = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": sent_text},
        ]
        started = time.monotonic()
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                **self._sampling,
                seed=SEED,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "schema": schema, "strict": True},
                },
            )
            choice = response.choices[0]
            text = choice.message.content
            if getattr(choice.message, "refusal", None):
                raise ModelCallFailed("refusal")
            if choice.finish_reason != "stop":  # "length" is truncated, "content_filter" partial
                raise ModelCallFailed(f"finish_reason={choice.finish_reason}")
            if not text:
                raise ModelCallFailed("empty")
        except Exception as error:
            refused = _refused(error)
            if refused is not None:  # the request hook stopped it: nothing was sent
                self._refuse(target, host="redirect-or-swap", backend=self.backend)
                raise refused from None
            self._log(
                host=host,
                target=target,
                sent_text=sent_text,
                response_text="",
                started=started,
                model_version=None,
                error=_error_label(error),
                details=details,
            )
            if isinstance(error, ModelCallFailed):
                raise
            raise ModelCallFailed(_error_label(error)) from None
        version = response.model or None
        if response.system_fingerprint:  # seed is best-effort; a new fingerprint explains drift
            details["system_fingerprint"] = response.system_fingerprint
        self._log(
            host=host,
            target=target,
            sent_text=sent_text,
            response_text=text,
            started=started,
            model_version=version,
            details=details,
        )
        return ModelReply(text=text, model=self.model, model_version=version)

    def transcribe(self, audio: bytes, transcriber: LocalTranscriber, *, target: str) -> str:
        """Transcribe in-process (see `VoiceGateway`)."""
        return VoiceGateway(self._store, self._clock).transcribe(audio, transcriber, target=target)

    def _log(
        self,
        *,
        host: str,
        target: str,
        sent_text: str,
        response_text: str,
        started: float,
        model_version: str | None,
        error: str | None = None,
        purpose: str = "extraction",
        backend: ModelBackend | None = None,
        model: str | None = None,
        details: dict[str, str] | None = None,
    ) -> None:
        call = ModelCall(
            purpose=purpose,
            data_class="PATIENT",
            backend=backend or self.backend,
            endpoint_host=host,
            model=model or self.model,
            model_version=model_version,
            request_text=sent_text,
            response_text=response_text,
            duration_ms=int((time.monotonic() - started) * 1000),
            ok=error is None,
            error=error,
        )
        audit.record(
            self._store,
            self._clock.now(),
            "system",
            "model_call",
            target,
            model_call=call,
            **(details or {}),
        )


class VoiceGateway:
    """The audio half of the gateway (WP17). It needs no text model, so voice works even when
    free-text extraction is off. Audio goes only to an in-process transcriber (D5); the audit
    keeps the audio's size and hash and the transcript, never the audio."""

    def __init__(self, store: Store, clock: Clock, allowed: frozenset[str] = frozenset()) -> None:
        self._store = store
        self._clock = clock
        self._allowed = allowed  # origins audio may reach (D52); empty: in-process only

    def transcribe(self, audio: bytes, transcriber: LocalTranscriber, *, target: str) -> str:
        try:
            check_audio_destination(transcriber.endpoint, self._allowed)
        except EndpointNotAllowed:
            host = urlsplit(transcriber.endpoint).hostname or "none"
            audit.record(
                self._store, self._clock.now(), "system", "model_call_refused", target,
                host=host, backend="audio",
            )  # fmt: skip
            raise
        digest = hashlib.sha256(audio).hexdigest()[:16]
        sent = f"[audio {len(audio)} B, sha256 {digest}]"
        started = time.monotonic()
        error: str | None = None
        text = ""
        try:
            text = transcriber(audio)
        except Exception as failure:  # noqa: BLE001 - audited, then raised as ModelCallFailed
            error = type(failure).__name__
        endpoint = transcriber.endpoint
        call = ModelCall(
            purpose="transcription",
            data_class="PATIENT",
            backend=getattr(transcriber, "backend", "local"),
            endpoint_host=IN_PROCESS if endpoint is None else urlsplit(endpoint).hostname or "none",
            model=transcriber.model,
            model_version=None,
            request_text=sent,
            response_text=text,
            duration_ms=int((time.monotonic() - started) * 1000),
            ok=error is None,
            error=error,
        )
        audit.record(self._store, self._clock.now(), "system", "model_call", target, model_call=call)
        if error is not None:
            raise ModelCallFailed(error)
        return text
