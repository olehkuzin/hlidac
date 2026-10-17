"""Hand-made test records. No patient data: every name, id and value here is invented."""

from datetime import datetime, timedelta
from typing import Any

from hlidac.clock import PRAGUE
from hlidac.schemas import (
    TAG_CODE,
    TAG_SYSTEM,
    Account,
    Alert,
    AnswerOption,
    AnswerSource,
    Assessment,
    AuditEntry,
    Checkin,
    CtcaeGrade,
    EmailJob,
    EnableWhen,
    FhirTransactionJob,
    Finding,
    MonitoringPlan,
    OutboxItem,
    PlanItem,
    QuoteSource,
    TriggeredRule,
)

T0 = datetime(2026, 10, 16, 8, 0, tzinfo=PRAGUE)
Q_URL = "urn:hlidac:questionnaire:demo"


def quoted(link_id: str = "nausea", state: str = "present", **kw: Any) -> Finding:
    return Finding(
        link_id=link_id,
        state=state,
        source=QuoteSource(kind="quote", message_id="m1", quote="je mi špatně od žaludku"),
        extractor_version="extract-v1",
        **kw,
    )


def answered(link_id: str = "fever", code: str = "yes", state: str = "present") -> Finding:
    return Finding(
        link_id=link_id,
        state=state,
        code=code,
        source=AnswerSource(kind="answer", message_id="m2", link_id=link_id, code=code),
    )


def grade(**kw: Any) -> CtcaeGrade:
    fields: dict[str, Any] = {
        "grade_id": "g1",
        "link_id": "nausea",
        "ctcae_term": "Nausea",
        "ctcae_version": "5.0",
        "grade": 1,
        "basis": ["DEMO criterion"],
        "mapping_id": "map-nausea",
        "mapping_version": "1",
    }
    return CtcaeGrade(**(fields | kw))


def urgent_rule() -> TriggeredRule:
    return TriggeredRule(
        rule_id="fever-sact",
        rule_version="1",
        outcome="urgent",
        inputs={"temperature_c": 38.4, "days_since_dose": 10},
        reason="Horečka do 56 dnů od léčby (DEMO)",
    )


def assessment(**kw: Any) -> Assessment:
    fields: dict[str, Any] = {
        "assessment_id": "a1",
        "checkin_id": "c1",
        "patient_id": "p1",
        "status": "no_escalation",
        "findings": [quoted()],
        "context": None,
        "grades": [grade()],
        "triggered_rules": [],
        "missing_link_ids": [],
        "questionnaire_url": Q_URL,
        "questionnaire_version": "1",
        "ruleset_version": "1",
        "mapping_version": "1",
        "assessed_at": T0,
        "engine_version": "0.1",
    }
    return Assessment(**(fields | kw))


def alert(**kw: Any) -> Alert:
    fields: dict[str, Any] = {
        "alert_id": "al1",
        "patient_id": "p1",
        "assessment_id": "a1",
        "priority": "urgent",
        "owner_team_id": "team-onko",
        "created_at": T0,
        "ack_deadline": T0 + timedelta(minutes=30),
    }
    return Alert(**(fields | kw))


def patient_account(**kw: Any) -> Account:
    fields: dict[str, Any] = {
        "account_id": "acc-p1",
        "email": "pacient1@demo.local",
        "role": "patient",
        "display_name": "Pacient 1",
        "patient_id": "p1",
    }
    return Account(**(fields | kw))


def checkin(**kw: Any) -> Checkin:
    fields: dict[str, Any] = {
        "checkin_id": "c1",
        "patient_id": "p1",
        "questionnaire_url": Q_URL,
        "questionnaire_version": "1",
        "reported_by": "patient",
        "started_at": T0,
        "updated_at": T0,
    }
    return Checkin(**(fields | kw))


def tagged(
    resource_type: str, system: str = "urn:hlidac:checkin", value: str = "c1", **fields: Any
) -> dict[str, Any]:
    """One of our resources, tagged and with our own identifier."""
    return {
        "resourceType": resource_type,
        "meta": {"tag": [{"system": TAG_SYSTEM, "code": TAG_CODE}]},
        "identifier": [{"system": system, "value": value}],
        **fields,
    }


def put(resource: dict[str, Any], query: str | None = None) -> dict[str, Any]:
    """A conditional PUT on the resource's own identifier (or on `query`, to test the guard)."""
    if query is None:
        own = resource["identifier"]
        own = own[0] if isinstance(own, list) else own
        query = f"identifier={own['system']}|{own['value']}"
    url = f"{resource['resourceType']}?{query}"
    return {"resource": resource, "request": {"method": "PUT", "url": url}}


def bundle(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"resourceType": "Bundle", "type": "transaction", "entry": list(entries)}


def fhir_item(outbox_id: str = "o1", key: str = "checkin:c1:v1") -> OutboxItem:
    job = FhirTransactionJob.of(bundle(put(tagged("QuestionnaireResponse"))))
    return OutboxItem(outbox_id=outbox_id, idempotency_key=key, created_at=T0, job=job)


def email_item(outbox_id: str = "o2", key: str = "notification:n1:email") -> OutboxItem:
    job = EmailJob(
        kind="email",
        to="sestra@demo.local",
        template_id="tpl-new-alert",
        template_version="1",
        subject="Nové upozornění",
        text_body="Máte nové upozornění. Otevřete aplikaci.",
        html_body="<p>Máte nové upozornění. Otevřete aplikaci.</p>",
    )
    return OutboxItem(outbox_id=outbox_id, idempotency_key=key, created_at=T0, job=job)


def audit(audit_id: str = "au1", target: str = "checkin:c1") -> AuditEntry:
    return AuditEntry(audit_id=audit_id, at=T0, actor="acc-n1", action="view_case", target=target)


def _options(*pairs: tuple[str, str]) -> list[AnswerOption]:
    return [AnswerOption(code=code, text_cs=text) for code, text in pairs]


def ui_plan() -> MonitoringPlan:
    """An invented plan for UI tests. Its texts say TEST: they are not approved content, and the
    app never ships them (WP1 brings the real Questionnaire)."""
    yes_no = _options(("yes", "TEST Ano"), ("no", "TEST Ne"), ("dont_know", "Nevím"))
    items = [
        PlanItem(
            link_id="fever",
            text_cs="TEST Otázka na horečku?",
            type="choice",
            required=True,
            red_flag=True,
            options=yes_no,
        ),
        PlanItem(
            link_id="temperature",
            text_cs="TEST Otázka na teplotu?",
            type="quantity",
            required=False,
            unit="Cel",
            enable_when=[EnableWhen(link_id="fever", answer_code="yes")],
        ),
        PlanItem(
            link_id="nausea",
            text_cs="TEST Otázka na nevolnost?",
            type="choice",
            required=True,
            options=_options(("no", "TEST Žádná"), ("mild", "TEST Mírná"), ("dont_know", "Nevím")),
        ),
        PlanItem(link_id="note", text_cs="TEST Poznámka", type="text", required=False),
    ]
    return MonitoringPlan(
        questionnaire_url=Q_URL,
        questionnaire_version="1",
        title_cs="TEST dotazník",
        items=items,
        rule_ids=[],
        mapping_ids=[],
        advice_card_ids=[],
        max_clarification_turns=3,
        source_ref="tests/factories.py (invented, TEST only)",
    )
