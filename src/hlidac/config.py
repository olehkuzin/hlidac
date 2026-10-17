"""Settings loaded from environment / .env. Never hardcode endpoints or model names."""

from datetime import date
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # An empty line in .env (e.g. `IRIS_PORT=` as copied from .env.example) means "not set": the
    # default applies instead of failing to parse "" as a number or an allowed value.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    # Fixed reference date for all time-window logic (deterministic results).
    as_of: date = date(2026, 10, 16)

    # memory: InMemoryStore + hand-made fixtures; iris: the team's IRIS (through the SSH tunnel).
    store_backend: Literal["memory", "iris"] = "memory"

    # IRIS for Health — take the values from the FHIR-AI-Hackathon-Kit notebooks. The user is the
    # app's least-privilege account (`python -m hlidac.iris.setup` creates it), never _SYSTEM.
    iris_host: str | None = None
    iris_port: int | None = None
    iris_namespace: str | None = None
    iris_user: str | None = None
    iris_password: SecretStr | None = None
    fhir_base_url: str | None = None

    # Model gateway (D5): patient text only to these endpoints, pseudonymised; audio stays in the app.
    extraction_backend: Literal["azure", "local"] = "azure"
    azure_openai_endpoint: str | None = None
    azure_openai_api_key: SecretStr | None = None
    azure_openai_deployment: str | None = None
    azure_openai_api_version: str | None = None
    # Reasoning models accept only their default temperature; set this for them (D39), and the
    # gateway sends the effort instead of temperature 0.
    azure_openai_reasoning_effort: Literal["low", "medium", "high"] | None = None
    local_base_url: str = "http://localhost:11434/v1"
    local_model: str | None = None
    local_embedding_model: str | None = None
    whisper_model: str | None = None  # mlx-whisper, in process
    # Voice backend (D52): mlx = in the app on Apple Silicon (WHISPER_MODEL); server = our own
    # Whisper server, OpenAI-compatible (WHISPER_SERVER_*); elevenlabs = the ElevenLabs cloud API,
    # opt-in only: the audio then leaves our infrastructure (ELEVENLABS_*).
    voice_backend: Literal["mlx", "server", "elevenlabs"] = "mlx"
    whisper_server_url: str | None = None
    whisper_server_model: str = "deepdml/faster-whisper-large-v3-turbo-ct2"
    whisper_server_api_key: SecretStr | None = None
    elevenlabs_api_key: SecretStr | None = None
    elevenlabs_stt_model: str = "scribe_v1"

    # Security (WP30). Signs session cookies and keys the login-code hashes. Unset: a random key
    # per process, so sessions and codes don't survive a restart, and nothing weak is a default.
    secret_key: SecretStr | None = None
    # Secure cookies need HTTPS; Chrome and Firefox also accept them on http://localhost.
    session_https_only: bool = True

    # Email: app = the outbox runner in the app sends to SMTP (Mailpit). It is the only email
    # sender (the IRIS production, WP11, writes only the FHIR rows; D40), so keep `app` in every
    # real setup. off = no email is sent at all, for tests only.
    # Login codes and notices go through the outbox either way.
    outbox_runner: Literal["app", "off"] = "app"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    mail_from: str = "hlidac@demo.local"
    # A real SMTP server (D14, only with the team's OK): login over STARTTLS, and mail goes only to
    # the comma-separated SMTP_ALLOWED_RECIPIENTS; any other address fails visibly in Unresolved.
    # `*` sends to every account's address. MAIL_FROM may carry a display name ("Name <address>");
    # Gmail keeps only the address of the logged-in account or of its verified aliases.
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_allowed_recipients: str = ""

    def allowed_recipients(self) -> frozenset[str] | None:
        found = {a.strip().lower() for a in self.smtp_allowed_recipients.split(",") if a.strip()}
        return frozenset(found) or None

    # Where the app is reached; a care-team email links to its nurse queue (WP16), nothing else.
    app_base_url: str = "http://localhost:8000"

    # The demo panel (/demo, nurses only): moves the demo clock and invites patients to a check-in.
    # Off unless the demo .env turns it on; never in a hospital deployment.
    demo_panel: bool = False
    demo_mailbox_url: str = "http://localhost:8025"  # Mailpit's web view, linked from the panel
    # TEMPORARY, demo only: a "skip verification" button on the code page logs in the account of
    # the email just entered, without the code. Accepted only from this machine (localhost).
    # Never on in a hospital deployment; remove after the demo.
    demo_skip_login: bool = False

    # Periodic check-in invitations (D57): every N minutes of real time, each patient of the team
    # gets the approved invitation (email only to deliverable addresses). Unset = off; only the
    # Azure deployment sets it, and a second copy with the same value sends nothing twice.
    checkin_invite_minutes: int | None = Field(default=None, ge=1)
    checkin_invite_team: str = "team-demo"


settings = Settings()
