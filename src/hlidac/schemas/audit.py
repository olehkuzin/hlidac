"""The audit trail, including exactly what each model saw (principle 8). Kept in IRIS only."""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from hlidac.schemas._base import Count, Id, NonEmpty, Strict

DataClass = Literal["PATIENT", "PUBLIC"]
ModelBackend = Literal["azure", "local", "elevenlabs"]  # elevenlabs: voice only, opt-in (D52)
ModelPurpose = Literal["extraction", "transcription"]


class ModelCall(Strict):
    purpose: ModelPurpose
    data_class: DataClass
    backend: ModelBackend
    endpoint_host: NonEmpty
    model: NonEmpty
    model_version: NonEmpty | None = None
    request_text: str  # exactly as sent, after pseudonymisation
    response_text: str
    duration_ms: Count
    ok: bool
    error: NonEmpty | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "ModelCall":
        # Voice: our own Whisper (local) or, opt-in, ElevenLabs (D52); never the Azure text model.
        if self.purpose == "transcription" and self.backend == "azure":
            raise ValueError("voice is never sent to the text model's endpoint (D5, D52)")
        if self.purpose == "extraction" and self.backend == "elevenlabs":
            raise ValueError("ElevenLabs is for voice only (D52)")
        if self.ok != (self.error is None):
            raise ValueError("a failed call says why, and only a failed one")
        return self


class AuditEntry(Strict):
    audit_id: Id
    at: AwareDatetime
    actor: Id  # an account id, or "system"
    action: NonEmpty  # e.g. "view_case", "model_call", "grade_confirmed"
    target: NonEmpty | None = None  # e.g. "checkin:abc"
    details: dict[NonEmpty, str | int | float | bool | None] = Field(default_factory=dict)
    model_call: ModelCall | None = None

    @model_validator(mode="after")
    def _model_call_matches_action(self) -> "AuditEntry":
        if (self.action == "model_call") != (self.model_call is not None):
            raise ValueError("model_call entries, and only they, carry the call")
        return self
