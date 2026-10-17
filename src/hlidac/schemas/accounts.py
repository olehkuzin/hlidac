"""Who can log in, and the one-time login codes (passwordless, D18)."""

from typing import Literal

from pydantic import AwareDatetime, model_validator

from hlidac.schemas._base import Count, Email, Id, NonEmpty, Strict

Role = Literal["patient", "nurse"]


class Account(Strict):
    account_id: Id
    email: Email
    role: Role
    display_name: NonEmpty  # masked in the demo
    patient_id: Id | None = None  # patients only: the one patient this login may see
    team_id: Id | None = None  # nurses only

    @model_validator(mode="after")
    def _scoped(self) -> "Account":
        if self.email != self.email.lower():
            raise ValueError("emails are stored lower-case")
        if self.role == "patient" and (self.patient_id is None or self.team_id is not None):
            raise ValueError("a patient account is bound to exactly one patient")
        if self.role == "nurse" and (self.team_id is None or self.patient_id is not None):
            raise ValueError("a nurse account belongs to a team and to no patient")
        return self


class LoginCode(Strict):
    """Only the hash of the code is stored."""

    code_id: Id
    account_id: Id
    code_hash: NonEmpty
    created_at: AwareDatetime
    expires_at: AwareDatetime
    used_at: AwareDatetime | None = None
    attempts: Count = 0

    @model_validator(mode="after")
    def _expires_later(self) -> "LoginCode":
        if self.expires_at <= self.created_at:
            raise ValueError("a code expires after it is created")
        return self
