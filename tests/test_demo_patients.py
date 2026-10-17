"""WP14 (part): the demo accounts for real patients never carry the patient id in the address or
the name. Invented ids only."""

from factories import T0

from hlidac.dev_seed import DEMO_TEAM
from hlidac.iris.demo_patients import BACKUP_NURSE, demo_records


def test_numbered_mock_emails_and_a_plan_per_patient():
    accounts, plans = demo_records(["invented-x1", "invented-x2"], T0)
    patients = [a for a in accounts if a.role == "patient"]
    assert [a.email for a in patients] == ["pacient-01@demo.local", "pacient-02@demo.local"]
    assert [a.patient_id for a in patients] == ["invented-x1", "invented-x2"]
    assert all("invented" not in a.email + a.display_name + a.account_id for a in patients)
    assert [(p.patient_id, p.care_team_id) for p in plans] == [
        ("invented-x1", DEMO_TEAM),
        ("invented-x2", DEMO_TEAM),
    ]
    assert BACKUP_NURSE in accounts and BACKUP_NURSE.team_id == DEMO_TEAM
