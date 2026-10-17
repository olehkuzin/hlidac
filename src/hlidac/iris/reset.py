"""Reset our demo writes in IRIS (WP14, D43).

    uv run python -m hlidac.iris.reset
    bash scripts/reset_demo.sh

Removes ONLY:
1. Our own SQL rows in `Hlidac`: `checkin`, `assessment`, `grade_review`, `alert`,
   `notification`, `outbox`, `audit_log` and `login_code` (keeping `account` and `patient_plan`,
   unless `--include-accounts` is given).
2. Our own `urn:hlidac:tag|hlidac`-tagged FHIR resources of `WRITABLE_TYPES`
   (`Questionnaire`, `QuestionnaireResponse`, `Observation`, `Provenance`, `Task`), deleted one
   by one only after verifying in SQL (`HSFHIR_X0001_S_Rsrc._tag`) that the resource carries our
   tag and its type is in `WRITABLE_TYPES`.
3. Re-enqueues the demo `Questionnaire` so the FHIR repository keeps it after the reset.

Counts all hospital FHIR resources (all non-deleted resources in `HSFHIR_X0001_R.Rsrc` that are
not tagged `urn:hlidac:tag|hlidac`) before and after, and raises `RuntimeError` if a single count
differs. Prints counts only, never patient ids or values.
"""

import argparse
from datetime import UTC, datetime

import httpx

from hlidac import plans as plan_loader
from hlidac.config import Settings
from hlidac.content import DATA_DIR
from hlidac.fhir_guard import TAG_CODE, TAG_SYSTEM, WRITABLE_TYPES
from hlidac.iris.db import Database
from hlidac.iris.setup import AdminSettings, admin_database
from hlidac.iris.store import SCHEMA, IrisStore

DEMO_WORKFLOW_TABLES: tuple[str, ...] = (
    "checkin",
    "assessment",
    "grade_review",
    "alert",
    "notification",
    "outbox",
    "audit_log",
    "login_code",
)
ACCOUNT_TABLES: tuple[str, ...] = ("account", "patient_plan")


COUNTED_FHIR_TABLES: tuple[str, ...] = (
    "Rsrc",
    "Patient",
    "MedicationAdministration",
    "Procedure",
    *sorted(WRITABLE_TYPES),
)


def hospital_resource_counts(db: Database) -> dict[str, int]:
    """Count non-deleted hospital FHIR resources (excluding our hlidac-tagged writes)."""
    tagged = tagged_fhir_keys(db)
    tagged_by_type: dict[str, int] = {}
    for key in tagged:
        rtype, _, _ = key.partition("/")
        tagged_by_type[rtype] = tagged_by_type.get(rtype, 0) + 1

    union_sql = " UNION ALL ".join(
        f'SELECT \'{table}\', COUNT(*) FROM "HSFHIR_X0001_S"."{table}"'
        for table in COUNTED_FHIR_TABLES
    )
    counts: dict[str, int] = {}
    for table, total in db.query(union_sql):
        name = str(table)
        subtract = len(tagged) if name == "Rsrc" else tagged_by_type.get(name, 0)
        counts[name] = int(total) - subtract
    return counts


def tagged_fhir_keys(db: Database) -> list[str]:
    """Keys (`<ResourceType>/<id>`) of non-deleted resources tagged `urn:hlidac:tag|hlidac`."""
    rows = db.query(
        """
        SELECT DISTINCT t.Key
        FROM HSFHIR_X0001_S_Rsrc._tag t
        JOIN HSFHIR_X0001_R.Rsrc r ON t.Key = r.Key
        WHERE r.Deleted = 0 AND t.value_System = ? AND t.value_Value = ?
        ORDER BY t.Key
        """,
        [TAG_SYSTEM, TAG_CODE],
    )
    keys: list[str] = []
    for (key,) in rows:
        if not isinstance(key, str) or "/" not in key:
            raise RuntimeError(f"unexpected FHIR key: {key!r}")
        rtype, _, _ = key.partition("/")
        if rtype not in WRITABLE_TYPES:
            raise RuntimeError(f"refusing to delete tagged resource of type {rtype!r}")
        keys.append(key)
    return keys


def delete_tagged_fhir(config: Settings, admin: AdminSettings, db: Database) -> int:
    """Delete our hlidac-tagged FHIR resources via the FHIR REST API. Returns the count deleted."""
    if not config.fhir_base_url:
        raise RuntimeError("set FHIR_BASE_URL in .env")
    if admin.user is None or admin.password is None:
        raise RuntimeError("set IRIS_ADMIN_USER and IRIS_ADMIN_PASSWORD in .env")
    keys = tagged_fhir_keys(db)
    auth = (admin.user, admin.password.get_secret_value())
    base = config.fhir_base_url.rstrip("/")
    for key in keys:
        response = httpx.delete(f"{base}/{key}", auth=auth, timeout=60)
        if response.status_code not in {200, 204, 404}:
            raise RuntimeError(f"DELETE {key} returned HTTP {response.status_code}")
    return len(keys)


def clear_demo_sql(
    db: Database, schema: str = SCHEMA, *, include_accounts: bool = False
) -> dict[str, int]:
    """Delete rows from our SQL tables in `schema` (never hospital tables). Returns deleted counts."""
    tables = DEMO_WORKFLOW_TABLES + (ACCOUNT_TABLES if include_accounts else ())
    removed: dict[str, int] = {}
    for table in tables:
        [(count,)] = db.query(f"SELECT COUNT(*) FROM {schema}.{table}")
        if count:
            db.execute(f"DELETE FROM {schema}.{table}")
        removed[table] = int(count)
    return removed


def reset_demo(
    config: Settings | None = None,
    admin: AdminSettings | None = None,
    *,
    include_accounts: bool = False,
) -> tuple[dict[str, int], int, dict[str, int]]:
    """Run the full WP14 reset and verify hospital resource counts before and after."""
    config = config or Settings()
    admin = admin or AdminSettings()
    admin_db = admin_database(config, admin)
    try:
        before = hospital_resource_counts(admin_db)
        sql_removed = clear_demo_sql(admin_db, include_accounts=include_accounts)
        fhir_removed = delete_tagged_fhir(config, admin, admin_db)
        after = hospital_resource_counts(admin_db)
        if before != after:
            raise RuntimeError(f"hospital resource counts changed: before={before}, after={after}")
    finally:
        admin_db.close()

    if not include_accounts and config.iris_user and config.iris_password:
        app_db = Database.from_settings(config)
        try:
            from hlidac.groups import default_groups
            from hlidac.iris.demo_patients import questionnaire_files

            for name in questionnaire_files(default_groups()):
                q = plan_loader.read_questionnaire(DATA_DIR / "questionnaires" / name)
                plan_loader.enqueue_questionnaire(IrisStore(app_db), q, now=datetime.now(UTC))
        finally:
            app_db.close()
    return sql_removed, fhir_removed, after


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--include-accounts",
        action="store_true",
        help="also delete Hlidac.account and Hlidac.patient_plan rows",
    )
    args = parser.parse_args()

    sql_removed, fhir_removed, hospital = reset_demo(include_accounts=args.include_accounts)
    total_sql = sum(sql_removed.values())
    total_hospital = hospital.get("Rsrc", 0)
    print(
        f"reset done: {total_sql} Hlidac SQL rows and {fhir_removed} hlidac-tagged FHIR resources"
        f" removed; hospital FHIR data untouched ({total_hospital} resources)"
    )


if __name__ == "__main__":
    main()
