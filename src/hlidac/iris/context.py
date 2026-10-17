"""`IrisContextSource`: reads treatment context from the hospital FHIR data in IRIS (WP14, D43).

Read-only: queries only the three hospital FHIR search tables `setup.py` grants SELECT on
(`MedicationAdministration`, `Procedure` and `Procedure.code`). Never modifies hospital data.

The dataset has almost no structured regimens: SACT doses appear as `MedicationAdministration`
(status `completed`) or the `Procedure` with VZP code `42520` ("APLIKACE PROTINÁDOROVÉ TERAPIE",
status `completed`). The latest completed dose at or before the current clock time becomes
`last_sact_dose_at`, with the FHIR references we read in `source_refs`. Anything not explicitly
in the record (`regimen`, `immunocompromised`) stays `None` (unknown, never assumed False, so the
assessment engine applies the stricter "on SACT" rule when uncertain).
"""

from collections.abc import Callable
from datetime import datetime

from hlidac.iris.db import Database
from hlidac.schemas import PatientContext

FHIR_SEARCH_SCHEMA = "HSFHIR_X0001_S"
PROC_CODE_SCHEMA = "HSFHIR_X0001_S_Procedure"
SACT_PROCEDURE_CODE = "42520"  # VZP: APLIKACE PROTINÁDOROVÉ TERAPIE

# The three read-only hospital search tables `hlidac_app` needs SELECT on (granted in setup.py).
READ_ONLY_FHIR_TABLES: tuple[tuple[str, str], ...] = (
    (FHIR_SEARCH_SCHEMA, "MedicationAdministration"),
    (FHIR_SEARCH_SCHEMA, "Procedure"),
    (PROC_CODE_SCHEMA, "code"),
)

_DOSES_SQL = f"""
SELECT Key, effectiveTime_StartRaw
FROM "{FHIR_SEARCH_SCHEMA}"."MedicationAdministration"
WHERE patient_Reference = ? AND status_Value = 'completed'
UNION ALL
SELECT p.Key, p.date_StartRaw
FROM "{FHIR_SEARCH_SCHEMA}"."Procedure" p
JOIN "{PROC_CODE_SCHEMA}"."code" c ON p.Key = c.Key
WHERE p.patient_Reference = ? AND p.status_Value = 'completed' AND c.value_Value = ?
"""


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class IrisContextSource:
    """Reads a patient's treatment context from the hospital FHIR repository in IRIS (read-only)."""

    def __init__(self, db: Database, now: Callable[[], datetime]) -> None:
        self._db = db
        self._now = now

    def get_context(self, patient_id: str) -> PatientContext | None:
        ref = f"Patient/{patient_id}"
        now = self._now()
        rows = self._db.query(_DOSES_SQL, [ref, ref, SACT_PROCEDURE_CODE])
        doses: list[tuple[datetime, str]] = []
        for key, raw_start in rows:
            when = _parse_iso(raw_start)
            if isinstance(key, str) and key and when is not None and when <= now:
                doses.append((when, key))
        if not doses:
            return None
        doses.sort(key=lambda item: (item[0], item[1]))
        last_dose_at = doses[-1][0]
        # Keep the references of the latest dose (and earlier ones in order, deduplicated).
        seen: dict[str, None] = {}
        for _, key in reversed(doses):
            seen.setdefault(key, None)
        return PatientContext(
            patient_id=patient_id,
            last_sact_dose_at=last_dose_at,
            regimen=None,
            immunocompromised=None,  # unknown in the dataset; never assumed False
            source_refs=list(seen)[:10],
            read_at=now,
        )
