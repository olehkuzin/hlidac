"""Demo accounts with mock emails for real patients in IRIS (WP14, D43).

    uv run python -m hlidac.iris.demo_patients            # 5 patients with SACT doses
    uv run python -m hlidac.iris.demo_patients --count 10
    uv run python -m hlidac.iris.demo_patients --per-group 2   # up to 2 patients in every group

Selects the first N patients in the hospital FHIR data who have completed anticancer-therapy dose
records (`MedicationAdministration` or `Procedure` 42520 "APLIKACE PROTINÁDOROVÉ TERAPIE"), ordered
so those with a dose within the SACT window before `AS_OF` come first (ids only, kept in memory,
never printed or stored anywhere but our own tables in IRIS). Gives each a patient account
`pacient-NN@demo.local` ("Pacient NN", never a real name) and a monitoring plan in the demo team,
plus the nurse accounts (`sestra@demo.local`, `vedouci@demo.local`, `sestra-zaloha@demo.local`),
and enqueues the demo `Questionnaire` into the outbox.

Illness groups (D50): each patient's plan uses the questionnaire of their cancer group
(`data/plans/groups-demo-v1.yaml`), chosen from the ICD-10 codes of their hospital `Condition`s
(read-only, kept in memory, never printed); with no matching code, the general questionnaire.
Every group questionnaire is enqueued too.

The address carries a number, never the patient id, because it shows in Mailpit, mail headers,
the login form and the audit log; the link to the patient lives only in Hlidac.account and
Hlidac.patient_plan. Idempotent: the same patients, accounts and plans on every run, and an
account keeps an email address and a display name changed by hand. Prints counts only.
"""

import argparse
from datetime import datetime

from hlidac import groups as groups_mod
from hlidac import plans as plan_loader
from hlidac.clock import DemoClock
from hlidac.config import Settings
from hlidac.content import DATA_DIR
from hlidac.dev_seed import ACCOUNTS as DEV_ACCOUNTS
from hlidac.dev_seed import DEMO_QUESTIONNAIRE, DEMO_TEAM
from hlidac.iris.context import (
    FHIR_SEARCH_SCHEMA,
    PROC_CODE_SCHEMA,
    SACT_PROCEDURE_CODE,
    _parse_iso,
)
from hlidac.iris.db import Database
from hlidac.iris.setup import AdminSettings, admin_database
from hlidac.iris.store import IrisStore
from hlidac.schemas import Account, PatientPlan

BACKUP_NURSE = Account(
    account_id="acc-demo-nurse-backup",
    email="sestra-zaloha@demo.local",
    role="nurse",
    display_name="Sestra Záloha",
    team_id=DEMO_TEAM,
)
NURSES = [*(a for a in DEV_ACCOUNTS if a.role == "nurse"), BACKUP_NURSE]
SACT_WINDOW_DAYS = 56
CONDITION_CODE_SCHEMA = "HSFHIR_X0001_S_Condition"
ICD10_SYSTEM = "http://hl7.org/fhir/sid/icd-10"


def demo_records(
    patient_ids: list[str],
    now: datetime,
    codes: dict[str, list[str]] | None = None,
    groups: groups_mod.Groups | None = None,
) -> tuple[list[Account], list[PatientPlan]]:
    """Accounts and plans; with `codes` and `groups`, each plan uses the patient's group
    questionnaire (the general one if no code matches)."""
    accounts, plans = [], []
    for n, patient_id in enumerate(patient_ids, start=1):
        group = (
            groups_mod.group_for_codes(groups, (codes or {}).get(patient_id, []))
            if groups is not None
            else None
        )
        accounts.append(
            Account(
                account_id=f"acc-pacient-{n:02d}",
                email=f"pacient-{n:02d}@demo.local",
                role="patient",
                display_name=f"Pacient {n:02d}",
                patient_id=patient_id,
            )
        )
        plans.append(
            PatientPlan(
                patient_id=patient_id,
                questionnaire_url=group.questionnaire_url if group else DEMO_QUESTIONNAIRE,
                questionnaire_version="1",
                care_team_id=DEMO_TEAM,
                active_from=now,
            )
        )
    return [*accounts, *NURSES], plans


def condition_codes(db: Database) -> dict[str, list[str]]:
    """Patient id → the ICD-10 codes of their hospital `Condition`s (read-only, in memory)."""
    rows = db.query(
        f"""
        SELECT cond.patient_Reference, c.value_Value
        FROM "{FHIR_SEARCH_SCHEMA}"."Condition" cond
        JOIN "{CONDITION_CODE_SCHEMA}"."code" c ON cond.Key = c.Key
        WHERE c.value_System = ?
        """,
        [ICD10_SYSTEM],
    )
    codes: dict[str, list[str]] = {}
    for ref, code in rows:
        if isinstance(ref, str) and ref.startswith("Patient/") and isinstance(code, str):
            codes.setdefault(ref.removeprefix("Patient/"), []).append(code)
    return codes


def pick_per_group(
    ordered: list[str], codes: dict[str, list[str]], groups: groups_mod.Groups, per_group: int
) -> list[str]:
    """Up to `per_group` patients of every group, keeping the order of `ordered`."""
    taken: dict[str | None, int] = {}
    picked = []
    for pid in ordered:
        group = groups_mod.group_for_codes(groups, codes.get(pid, []))
        key = group.group_id if group else None
        if key is not None and taken.get(key, 0) < per_group:
            taken[key] = taken.get(key, 0) + 1
            picked.append(pid)
    return picked


def dose_patient_ids(db: Database, now: datetime, count: int | None) -> list[str]:
    """First `count` Patient ids that have completed SACT dose records in the hospital data
    (`MedicationAdministration` or `Procedure` 42520), prioritising patients with a dose within
    the SACT window (`<= now`, within 56 days) so the demo scenarios run on real treatment context."""
    rows = db.query(
        f"""
        SELECT patient_Reference, effectiveTime_StartRaw
        FROM "{FHIR_SEARCH_SCHEMA}"."MedicationAdministration"
        WHERE status_Value = 'completed'
        UNION ALL
        SELECT p.patient_Reference, p.date_StartRaw
        FROM "{FHIR_SEARCH_SCHEMA}"."Procedure" p
        JOIN "{PROC_CODE_SCHEMA}"."code" c ON p.Key = c.Key
        WHERE p.status_Value = 'completed' AND c.value_Value = ?
        """,
        [SACT_PROCEDURE_CODE],
    )
    by_patient: dict[str, list[datetime]] = {}
    for ref, raw in rows:
        when = _parse_iso(raw)
        if isinstance(ref, str) and ref.startswith("Patient/") and when is not None:
            pid = ref.removeprefix("Patient/")
            by_patient.setdefault(pid, []).append(when)

    def sort_key(pid: str) -> tuple[int, int, str]:
        past = [dt for dt in by_patient[pid] if dt <= now]
        if not past:
            return (2, 9999, pid)
        days = (now - max(past)).days
        in_window = 0 if days <= SACT_WINDOW_DAYS else 1
        # Closest to day 10 after dose first (demo scenario 2), then deterministic by pid.
        return (in_window, abs(days - 10), pid)

    ordered = sorted(by_patient, key=sort_key)
    if count is None:
        return ordered
    ordered = ordered[:count]
    if len(ordered) < count:
        raise SystemExit(
            f"only {len(ordered)} patients with SACT dose records in the FHIR data, {count} wanted"
        )
    return ordered


def questionnaire_files(groups: groups_mod.Groups | None) -> list[str]:
    names = ["symptoms-demo-v1.json"]
    if groups is not None:
        names += [f"{g.file_stem}.json" for g in groups.groups]
    return names


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument(
        "--per-group",
        type=int,
        default=None,
        help="instead of --count: up to N patients with dose records in every illness group",
    )
    parser.add_argument(
        "--no-groups", action="store_true", help="give everyone the general questionnaire"
    )
    args = parser.parse_args()
    groups = None if args.no_groups else groups_mod.default_groups()

    config = Settings()
    now = DemoClock.from_as_of(config.as_of).now()
    admin_db = admin_database(config, AdminSettings())
    try:
        codes = condition_codes(admin_db) if groups is not None else {}
        if args.per_group is not None and groups is not None:
            all_ids = dose_patient_ids(admin_db, now, None)
            patient_ids = pick_per_group(all_ids, codes, groups, args.per_group)
        else:
            patient_ids = dose_patient_ids(admin_db, now, args.count)
    finally:
        admin_db.close()
    accounts, plans = demo_records(patient_ids, now, codes, groups)
    db = Database.from_settings(config)
    try:
        store = IrisStore(db)
        for account in accounts:
            existing = store.get_account(account.account_id)
            if existing is not None:  # keep an address and a name changed by hand (a teammate's)
                account = account.evolve(email=existing.email, display_name=existing.display_name)
            store.save_account(account)
        for plan in plans:
            store.save_patient_plan(plan)
        for name in questionnaire_files(groups):
            q = plan_loader.read_questionnaire(DATA_DIR / "questionnaires" / name)
            plan_loader.enqueue_questionnaire(store, q, now=now)
    finally:
        db.close()
    by_url: dict[str, int] = {}
    for plan in plans:
        by_url[plan.questionnaire_url] = by_url.get(plan.questionnaire_url, 0) + 1
    for url, n in sorted(by_url.items()):
        group = groups.by_url(url) if groups else None
        print(f"  {n:3d} × {group.name_cs if group else 'obecný dotazník'}")
    print(
        f"{len(plans)} patient accounts (pacient-01 … pacient-{len(plans):02d}@demo.local) with SACT"
        f" dose records and plans in {DEMO_TEAM}, {len(NURSES)} nurses, and the demo Questionnaire"
        " enqueued; patient ids stay in IRIS"
    )


if __name__ == "__main__":
    main()
