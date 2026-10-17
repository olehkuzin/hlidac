"""Writing the audit trail (principle 8). Entries live in the store (IRIS), never in app logs."""

import secrets
from datetime import datetime

from hlidac.schemas import AuditEntry, ModelCall
from hlidac.store import Store


def record(
    store: Store,
    at: datetime,
    actor: str,
    action: str,
    target: str | None = None,
    *,
    model_call: ModelCall | None = None,
    **details: str | float | bool | None,
) -> AuditEntry:
    entry = AuditEntry(
        audit_id=secrets.token_hex(16),
        at=at,
        actor=actor,
        action=action,
        target=target,
        details=details,
        model_call=model_call,
    )
    store.append_audit(entry)
    return entry
