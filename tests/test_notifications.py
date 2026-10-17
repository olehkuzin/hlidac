"""WP16: in-app notifications and the urgent email to the care team (approved, non-clinical).
Hand-made fixtures only; every account and id is invented."""

import json
import re
from datetime import timedelta

import pytest
from demo_helpers import NOW, PLAN, context, findings, quoted_text
from factories import alert, checkin, patient_account
from pydantic import ValidationError

from hlidac import emails, notifications
from hlidac.notifications import mark_read, notify_alert, text_for, unread_count
from hlidac.processing import make_process
from hlidac.schemas import Account, Notification, PatientPlan
from hlidac.store import InMemoryStore, StaticContextSource

BASE = "http://localhost:8000"
QUEUE = f"{BASE}/sestra"
TEAM = "t1"


def nurse(account_id="acc-n1", email="sestra1@demo.local", team_id=TEAM) -> Account:
    return Account(
        account_id=account_id, email=email, role="nurse", display_name="Sestra Jedna",
        team_id=team_id,
    )  # fmt: skip


def seeded(*nurses: Account) -> InMemoryStore:
    store = InMemoryStore()
    for account in nurses or (nurse(),):
        store.save_account(account)
    store.save_account(patient_account())  # acc-p1 for patient p1
    store.save_patient_plan(
        PatientPlan(
            patient_id="p1",
            questionnaire_url=PLAN.questionnaire_url,
            questionnaire_version=PLAN.questionnaire_version,
            care_team_id=TEAM,
            active_from=NOW,
        )
    )
    return store


def emails_in(store):
    return [o for o in store.list_outbox() if o.job.kind == "email"]


def audited(store, action):
    return [e for e in store.list_audit() if e.action == action]


# --- notify_alert ----------------------------------------------------------------------------


def test_an_urgent_alert_notifies_every_nurse_in_app_and_by_one_email_each():
    store = seeded(nurse(), nurse("acc-n2", "sestra2@demo.local"), nurse("acc-x", "x@demo.local",
                                                                         team_id="other"))  # fmt: skip
    urgent = alert(owner_team_id=TEAM)
    notify_alert(store, urgent, NOW, base_url=BASE)
    mails = emails_in(store)
    assert sorted(m.job.to for m in mails) == ["sestra1@demo.local", "sestra2@demo.local"]
    for account_id in ("acc-n1", "acc-n2"):
        [note] = store.list_notifications(account_id)
        assert note.kind == "urgent_alert" and note.link == f"/sestra/pripad/{urgent.alert_id}"
        assert text_for(note) == "Čeká nové naléhavé hlášení."
        channels = {d.channel: d for d in note.deliveries}
        assert set(channels) == {"in_app", "email"}
        assert channels["email"].outbox_id in {m.outbox_id for m in mails}
    assert store.list_notifications("acc-x") == []


def test_notifying_twice_duplicates_nothing_and_keeps_read_at():
    store = seeded()
    urgent = alert(owner_team_id=TEAM)
    notify_alert(store, urgent, NOW, base_url=BASE)
    [note] = store.list_notifications("acc-n1")
    read = mark_read(store, note, NOW + timedelta(minutes=1))
    notify_alert(store, urgent, NOW + timedelta(minutes=2), base_url=BASE)
    assert store.list_notifications("acc-n1") == [read]
    assert len(emails_in(store)) == 1
    assert len(store.list_notifications("acc-p1")) == 1
    assert len(audited(store, "notification_created")) == 2  # the nurse's and the patient's


def test_a_routine_alert_notifies_in_app_only():
    store = seeded()
    notify_alert(store, alert(priority="routine", owner_team_id=TEAM), NOW, base_url=BASE)
    [note] = store.list_notifications("acc-n1")
    assert note.kind == "report_received" and [d.channel for d in note.deliveries] == ["in_app"]
    assert text_for(note) == "Čeká nové hlášení k prohlédnutí."
    assert emails_in(store) == []


@pytest.mark.parametrize("priority", ["urgent", "routine"])
def test_the_patient_is_told_the_team_was_notified_in_app_only(priority):
    store = seeded()
    notify_alert(store, alert(priority=priority, owner_team_id=TEAM), NOW, base_url=BASE)
    [note] = store.list_notifications("acc-p1")
    assert (note.kind, note.link) == ("team_notified", "/pacient")
    assert [d.channel for d in note.deliveries] == ["in_app"]
    assert text_for(note) == "Váš tým jsme o hlášení informovali."
    assert all(m.job.to != "pacient1@demo.local" for m in emails_in(store))


def test_a_team_without_nurses_fails_visibly_after_telling_the_patient():
    store = InMemoryStore()
    store.save_account(patient_account())
    with pytest.raises(LookupError):
        notify_alert(store, alert(owner_team_id=TEAM), NOW, base_url=BASE)
    assert [n.kind for n in store.list_notifications("acc-p1")] == ["team_notified"]


def test_notifications_are_audited_by_id_only():
    store = seeded()
    notify_alert(store, alert(owner_team_id=TEAM), NOW, base_url=BASE)
    for entry in audited(store, "notification_created"):
        assert entry.target.startswith("notification:nt-")
        assert set(entry.details) == {"kind", "recipient", "alert", "email_outbox"}
        dumped = entry.model_dump_json()
        assert "Čeká" not in dumped and "@" not in dumped


def test_a_bad_base_url_is_refused():
    for bad in ("javascript:alert(1)", "http://localhost:8000/x?id=al1", "ftp://host"):
        with pytest.raises(ValueError):
            notifications.queue_link(bad)
    assert notifications.queue_link(BASE + "/") == QUEUE


# --- mark_read and unread_count --------------------------------------------------------------


def test_mark_read_sets_read_at_once_and_unread_count_follows():
    store = seeded()
    notify_alert(store, alert(owner_team_id=TEAM), NOW, base_url=BASE)
    notify_alert(store, alert(alert_id="al2", owner_team_id=TEAM), NOW, base_url=BASE)
    assert unread_count(store, "acc-n1") == 2
    first, _ = store.list_notifications("acc-n1")
    read = mark_read(store, first, NOW + timedelta(minutes=1))
    again = mark_read(store, first, NOW + timedelta(minutes=9))  # the stale copy, read again
    assert read.read_at == again.read_at == NOW + timedelta(minutes=1)
    assert unread_count(store, "acc-n1") == 1
    assert unread_count(store, "acc-nobody") == 0
    assert len(audited(store, "notification_read")) == 1


# --- approved content ------------------------------------------------------------------------


def test_text_for_covers_every_template_and_refuses_unknown_ones():
    content = notifications.read_content()
    assert content.approval == "DEMO — čeká na schválení FN Motol" and content.source_ref
    assert {t.kind for t in content.templates} >= {"urgent_alert", "report_received",
                                                    "team_notified"}  # fmt: skip
    for template in content.templates:
        note = Notification(
            notification_id="n1", recipient_account_id="acc-n1", kind=template.kind,
            template_id=template.template_id, template_version=content.version,
            link="/sestra", created_at=NOW,
        )  # fmt: skip
        assert text_for(note) == template.text
    base = {"notification_id": "n1", "recipient_account_id": "acc-n1", "kind": "urgent_alert",
            "link": "/sestra", "created_at": NOW}  # fmt: skip
    with pytest.raises(LookupError):
        text_for(Notification(**base, template_id="nope", template_version="1"))
    with pytest.raises(LookupError):
        text_for(Notification(**base, template_id="urgent-alert", template_version="99"))
    with pytest.raises(ValueError):
        text_for(Notification(**base | {"kind": "team_notified"}, template_id="urgent-alert",
                              template_version="1"))  # fmt: skip


def test_every_content_version_validates_strictly():
    assert notifications.content_versions() == ["1", "2"]
    for version in ("1", "2"):
        raw = json.loads((notifications.CONTENT_DIR / f"notifications-v{version}.json").read_text())
        with pytest.raises(ValidationError):
            notifications.NotificationContent.model_validate({**raw, "extra": 1})
    # v2 only adds texts: every v1 text is unchanged, so old notifications read the same
    old, new = notifications.read_content("1"), notifications.read_content("2")
    assert set(old.templates) <= set(new.templates)
    patient_text = json.loads(
        (notifications.CONTENT_DIR.parent / "patient" / "results-v1.json").read_text()
    )["team_notified"]
    assert text_for_kind("team_notified") == patient_text


def text_for_kind(kind):
    return next(t.text for t in notifications.read_content().templates if t.kind == kind)


def test_the_new_alert_email_takes_only_the_queue_link():
    job = emails.render("new-alert", "sestra1@demo.local", link=QUEUE)
    assert QUEUE in job.text_body and QUEUE in job.html_body
    for bad in (
        f"{QUEUE}?alert=al1",
        f"{BASE}/sestra/pripad/al1",
        f"{BASE}/pacient",
        f"{QUEUE}#al1",
        "javascript:alert(1)",
        f"{QUEUE}/",
    ):
        with pytest.raises(ValueError):
            emails.render("new-alert", "sestra1@demo.local", link=bad)
    with pytest.raises(ValueError):
        emails.render("new-alert", "sestra1@demo.local", link=QUEUE, code="123456x")


# --- through the live process ----------------------------------------------------------------


def process_for(store, **kw):
    source = StaticContextSource({"p1": context()})
    return make_process(store, source, plans=[PLAN], base_url=BASE, **kw)


def sent(found, **kw):
    return checkin(findings=found, status="completed", **kw)


def test_an_urgent_report_queues_exactly_one_email_and_a_retry_adds_none():
    store = seeded()  # the demo team has one nurse
    process = process_for(store)
    report = sent(findings(fever="yes", temperature=38.4))
    result = process(report, PLAN, NOW)
    assert result.status == "urgent"
    assert len(emails_in(store)) == 1
    process(report, PLAN, NOW + timedelta(minutes=5))
    assert len(emails_in(store)) == 1
    assert len(store.list_notifications("acc-n1")) == 1
    assert len(store.list_notifications("acc-p1")) == 1


def test_a_retry_completes_an_interrupted_notification():
    store = seeded()
    store.list_team_accounts = lambda team_id: (_ for _ in ()).throw(ConnectionError("down"))
    process = process_for(store)
    report = sent(findings(fever="yes"))
    result = process(report, PLAN, NOW)
    assert result.status == "urgent" and len(store.list_alerts()) == 1
    [failure] = audited(store, "notification_failed")
    assert failure.details == {"error": "ConnectionError"}
    [alert] = store.list_alerts()
    assert notifications.failure_open(store, alert.alert_id)  # listed in Unresolved
    del store.list_team_accounts  # IRIS is back
    process(report, PLAN, NOW + timedelta(minutes=1))
    assert len(store.list_notifications("acc-n1")) == 1 and len(emails_in(store)) == 1
    assert not notifications.failure_open(store, alert.alert_id)
    assert len(audited(store, "notification_completed")) == 1
    process(report, PLAN, NOW + timedelta(minutes=2))
    assert len(audited(store, "notification_completed")) == 1  # only after a failure


def test_a_failing_notification_never_hides_the_urgent_result():
    store = seeded()
    store.save_notification = lambda n: (_ for _ in ()).throw(RuntimeError("boom"))
    result = process_for(store)(sent(findings(fever="yes")), PLAN, NOW)
    assert result.status == "urgent"
    [stored] = store.list_alerts()
    assert stored.assessment_id == result.assessment_id
    [failure] = audited(store, "notification_failed")
    assert failure.target == f"alert:{stored.alert_id}" and failure.details["error"] == "RuntimeError"


def test_a_routine_report_notifies_without_email():
    store = seeded()
    result = process_for(store)(
        sent(findings(vomiting="yes", vomiting_episodes="episodes_3_5")), PLAN, NOW
    )
    assert result.status == "routine_review"
    assert [n.kind for n in store.list_notifications("acc-n1")] == ["report_received"]
    assert emails_in(store) == []


def test_make_process_refuses_a_base_url_that_makes_a_bad_link():
    with pytest.raises(ValueError):
        make_process(InMemoryStore(), StaticContextSource({}), plans=[PLAN],
                     base_url="http://host/?x=1")  # fmt: skip


# --- emails carry no clinical content --------------------------------------------------------

SCENARIOS = [
    findings(fever="yes", temperature=38.4, chills="yes", chills_severity="shivering"),
    findings(fever="yes", vomiting="yes", vomiting_episodes="episodes_3_5", nausea="yes"),
    findings(fever="yes", diarrhoea="yes", diarrhoea_blood="yes") + [quoted_text()],
]


def _forbidden(store, result, found):
    """Every clinical or identifying string of this report and its assessment."""
    words = {result.patient_id, result.assessment_id, result.checkin_id, "p1", "acc-p1", "Pacient 1"}
    words |= {a.alert_id for a in store.list_alerts()}
    for f in found:
        words |= {f.link_id, *(str(v) for v in f.attributes.values())}
        if f.code:
            words.add(f.code)
        if f.value is not None:
            words.add(str(f.value.value))
        quote = getattr(f.source, "quote", None)
        if quote:
            words.add(quote)
        item = next((i for i in PLAN.items if i.link_id == f.link_id), None)
        if item is not None:
            words.add(item.text_cs)
            words |= {o.text_cs for o in item.options if o.code == f.code}
            if item.ctcae_term:
                words.add(item.ctcae_term)
    for g in result.grades:
        words |= {g.ctcae_term, g.link_id, *g.basis}
        words |= {f"grade {g.grade}", f"stupeň {g.grade}", f"Grade {g.grade}"}
    for rule in result.triggered_rules:
        words.add(rule.rule_id)
    words |= {"CTCAE", "UKONS", "teplota", "horečka", "průjem", "zvracení"}
    return {w for w in words if w}


@pytest.mark.parametrize("found", SCENARIOS)
def test_email_bodies_contain_no_clinical_content(found):
    store = seeded()
    result = process_for(store)(sent(found), PLAN, NOW)
    assert result.status == "urgent"
    [mail] = emails_in(store)
    job = mail.job
    for part in (job.subject, job.text_body, job.html_body):
        for word in _forbidden(store, result, found):
            pattern = rf"(?<![\w-]){re.escape(word)}(?![\w-])"
            assert not re.search(pattern, part, re.IGNORECASE), f"{word!r} is in the email"
        links = re.findall(r"[a-z]+:[^\s<\"']+", part)
        assert set(links) <= {QUEUE}
    assert QUEUE in job.text_body
