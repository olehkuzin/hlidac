"""Alerts for the care team, and notifications (in the app, mirrored to email)."""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from hlidac.schemas._base import AppPath, Count, Id, NonEmpty, Strict, Version

AlertPriority = Literal["urgent", "routine"]
AlertStatus = Literal["open", "acknowledged", "resolved"]
Channel = Literal["in_app", "email"]
NotificationKind = Literal[
    "checkin_due",
    "clarification_needed",
    "report_received",
    "team_notified",
    "urgent_alert",
    "alert_escalated",
    "team_message",
]


class Alert(Strict):
    """One alert, owned by a team. Its FHIR Task carries the identifier `alert_id`."""

    alert_id: Id
    patient_id: Id
    assessment_id: Id
    priority: AlertPriority
    owner_team_id: Id
    created_at: AwareDatetime
    ack_deadline: AwareDatetime
    status: AlertStatus = "open"
    acknowledged_by: Id | None = None
    acknowledged_at: AwareDatetime | None = None
    resolved_by: Id | None = None
    resolved_at: AwareDatetime | None = None
    resolution_note: NonEmpty | None = None
    fallback_level: Count = 0

    @model_validator(mode="after")
    def _consistent(self) -> "Alert":
        if self.ack_deadline <= self.created_at:
            raise ValueError("the acknowledgement deadline is after creation")
        needs_ack = self.status in ("acknowledged", "resolved")
        needs_resolution = self.status == "resolved"
        ack = (self.acknowledged_by, self.acknowledged_at)
        resolution = (self.resolved_by, self.resolved_at, self.resolution_note)
        if any((v is None) == needs_ack for v in ack) or any(
            (v is None) == needs_resolution for v in resolution
        ):
            raise ValueError(f"an {self.status} alert has exactly the matching ack/resolve fields")
        return self


class Delivery(Strict):
    """In app: delivered when stored. Email: its state is the outbox row's state."""

    channel: Channel
    at: AwareDatetime
    outbox_id: Id | None = None

    @model_validator(mode="after")
    def _email_has_outbox(self) -> "Delivery":
        if (self.channel == "email") != (self.outbox_id is not None):
            raise ValueError("email deliveries, and only they, point to an outbox row")
        return self


class Notification(Strict):
    """Rendered only from an approved template. The email part carries no clinical content."""

    notification_id: Id
    recipient_account_id: Id
    kind: NotificationKind
    template_id: Id
    template_version: Version
    link: AppPath
    created_at: AwareDatetime
    read_at: AwareDatetime | None = None
    deliveries: list[Delivery] = Field(default_factory=list)
