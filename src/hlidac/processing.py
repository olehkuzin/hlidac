"""The live `Pipeline.process` (WP18 part 1): assess a check-in, store it, raise the alert row and
notify (WP16).

`make_process(store, context_source, base_url=...)` returns the function the patient app calls
after every saved answer and when the report is sent (`hlidac.pipeline`). Per call:

1. Read the treatment context from the hospital record (`ContextSource`, read only). If that fails
   or returns another patient's context, the engine gets `context=None` and applies the stricter
   "on SACT" row; the failure is audited, never read as "no escalation".
2. `assessment.assess(...)` (WP4) decides the status; nothing here changes it.
3. While the report is still being filled in, only an urgent assessment is stored and returned
   (urgent never waits); anything else returns None, because the patient hasn't finished and the
   next answer is assessed again. Once the report is sent, every assessment is stored.
4. Advice (WP6, `select_card`) is chosen only for `no_escalation`; urgent overrides advice.
5. An `urgent` assessment gets an urgent alert row before this returns. A `routine_review` on a sent
   report gets a routine alert. `insufficient_information` gets no alert: the nurse's Unresolved
   list shows it ("Chybí informace"), and `no_escalation` needs nobody.
6. Right after an alert row is saved and audited, `notifications.notify_alert` notifies the owning
   team's nurses (in app; email too when urgent) and the patient (in app). The alert row comes
   first, so a failed notification never hides it: the failure is audited (`notification_failed`,
   the error's type only) and the patient still gets the result. A retry notifies an existing open
   alert again, which completes an interrupted notification and duplicates nothing.

Idempotent: the assessment id is a hash of what the engine saw (check-in, findings, context without
its read time, plan, ruleset, mapping and engine versions), so a retry finds the stored assessment
instead of adding a second one. An alert is created at most once per check-in, unless a later
assessment of the same check-in is worse (`group_with_open`); a retry never duplicates an alert.
Deadlines and fallback are in `alerts.py`.

Grouping across check-ins (spec §9, WP18): a report of the same episode as an unresolved alert of
an earlier check-in (same symptoms, same or lower grade and status, no newly triggered rule) joins
that alert instead of raising a new one; anything worse raises a new alert, audited with the alert
it renews and why. Safety: an urgent report joins only an alert nobody has taken over yet (`open`);
once a nurse has acknowledged, a new urgent report is a new alert. A grouped report is audited
(`alert_grouped`, target the alert it joined, `assessment` = the report's assessment), so the nurse
sees it in the case (`grouped_ids`) and the patient is told the team knows (`alert_of`).

Deadlines are demo defaults (DEMO — čeká na schválení FN Motol): an urgent alert must be
acknowledged within `URGENT_ACK_MINUTES`; a routine one within the triggered rule's
`review_within_hours`, else `ROUTINE_ACK_HOURS`.
"""

import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from hlidac import alerts, audit, extraction, notifications, redflags
from hlidac import assessment as engine
from hlidac.ctcae import CtcaeMapping, read_mapping
from hlidac.pipeline import Process
from hlidac.schemas import Alert, Assessment, Checkin, MonitoringPlan, PatientContext, QuoteSource
from hlidac.store import ContextSource, Store

# DEMO — čeká na schválení FN Motol. Workflow deadlines, not clinical values: 30 minutes is the
# team's demo default for acknowledging an urgent alert; 24 hours follows UKONS v3 p. 10
# ("1 Amber = review within 24 hours") and is used when a routine rule names no deadline.
URGENT_ACK_MINUTES = 30
ROUTINE_ACK_HOURS = 24
SYSTEM = "system"
GROUPED = "alert_grouped"

# (assessment, plan) -> (card_id, card_version), or None when no card is eligible (WP6).
SelectCard = Callable[[Assessment, MonitoringPlan], tuple[str, str] | None]


def check_plans(
    plans: Iterable[MonitoringPlan], mapping: CtcaeMapping, rules: engine.Ruleset
) -> None:
    """Raise ValueError unless every plan fits the mapping and ruleset and has a free-text item
    for unread messages (run at app start)."""
    redflags.load_files()
    for plan in plans:
        try:
            engine.check_content(plan, mapping, rules)
            extraction.free_text_item(plan)
        except ValueError as error:
            raise ValueError(f"{plan.questionnaire_url}|{plan.questionnaire_version}: {error}")


def _checkin_text(checkin: Checkin) -> str:
    """All free-text messages and extracted quotes on this check-in for the red-flag scan."""
    parts = [m.text for m in checkin.messages if m.kind == "text" and m.text]
    parts.extend(f.source.quote for f in checkin.findings if isinstance(f.source, QuoteSource))
    return "\n".join(parts)


def assessment_id(
    checkin: Checkin,
    plan: MonitoringPlan,
    context: PatientContext | None,
    mapping: CtcaeMapping,
    rules: engine.Ruleset,
) -> str:
    """The same inputs always give the same id, so a retry finds the stored assessment."""
    seen: dict[str, object] = {
        "checkin": checkin.checkin_id,
        "findings": [f.model_dump(mode="json") for f in checkin.findings],
        "context": context.model_dump(mode="json", exclude={"read_at"}) if context else None,
        "plan": [plan.questionnaire_url, plan.questionnaire_version],
        "versions": [rules.version, mapping.version, engine.ENGINE_VERSION],
    }
    if (match := redflags.check(_checkin_text(checkin))) is not None:
        seen["redflag"] = [redflags.load_files().version, match.phrase, match.level]
    digest = hashlib.sha256(json.dumps(seen, sort_keys=True).encode()).hexdigest()
    return f"as-{digest[:40]}"


def ack_deadline(result: Assessment, now: datetime) -> datetime:
    if result.status == "urgent":
        return now + timedelta(minutes=URGENT_ACK_MINUTES)
    hours = [
        rule.inputs["review_within_hours"]
        for rule in result.triggered_rules
        if isinstance(rule.inputs.get("review_within_hours"), int)
    ]
    return now + timedelta(hours=min(hours, default=ROUTINE_ACK_HOURS))


def _read_context(
    store: Store, source: ContextSource, patient_id: str, now: datetime
) -> PatientContext | None:
    target = f"patient:{patient_id}"
    try:
        context = source.get_context(patient_id)
    except Exception as error:  # noqa: BLE001 — unknown context means the stricter rule
        audit.record(store, now, SYSTEM, "context_read_failed", target=target,
                     error=type(error).__name__)  # fmt: skip
        return None
    if context is not None and context.patient_id != patient_id:
        audit.record(store, now, SYSTEM, "context_read_failed", target=target,
                     error="another patient's context")  # fmt: skip
        return None
    return context


def _with_card(
    result: Assessment, plan: MonitoringPlan, select_card: SelectCard | None
) -> Assessment:
    if select_card is None or result.status != "no_escalation":
        return result  # urgent (and any review) overrides advice
    chosen = select_card(result, plan)
    if chosen is None:
        return result
    card_id, version = chosen
    if card_id not in plan.advice_card_ids:
        raise ValueError(f"advice card {card_id} is not in the patient's plan")
    return result.evolve(advice_card_id=card_id, advice_card_version=version)


def _notify(
    store: Store, alert: Alert, now: datetime, base_url: str, escalation: alerts.Escalation
) -> None:
    """Never raises: the alert row is stored already and shows in the nurse queue. A failure is
    audited and listed in Unresolved until a retry completes the notification. The team's fallback
    contacts are not notified first; they hear only if the deadline passes (`alerts.py`)."""
    target = f"alert:{alert.alert_id}"
    failed_before = notifications.failure_open(store, alert.alert_id)
    skip = escalation.fallback_emails(alert.owner_team_id)
    try:
        notifications.notify_alert(store, alert, now, base_url=base_url, skip_emails=skip)
    except Exception as error:  # noqa: BLE001 — a failure stays visible (audit, Unresolved)
        audit.record(store, now, SYSTEM, notifications.FAILED, target=target,
                     error=type(error).__name__)  # fmt: skip
        return
    if failed_before:
        audit.record(store, now, SYSTEM, notifications.COMPLETED, target=target)


def grouped_ids(store: Store, alert: Alert) -> list[str]:
    """The assessments that joined this alert (WP18 grouping), oldest first."""
    found = [
        e.details.get("assessment")
        for e in store.list_audit(target=f"alert:{alert.alert_id}")
        if e.action == GROUPED
    ]
    return list(dict.fromkeys(a for a in found if isinstance(a, str)))


def alert_of(store: Store, assessment: Assessment) -> Alert | None:
    """The alert that carries this assessment: its own, or the one it was grouped into."""
    own = store.get_alert(f"al-{assessment.assessment_id}")
    if own is not None:
        return own
    for alert in store.list_alerts(patient_id=assessment.patient_id):
        if assessment.assessment_id in grouped_ids(store, alert):
            return alert
    return None


def _group(store: Store, result: Assessment, now: datetime) -> tuple[bool, dict[str, str | None]]:
    """(grouped, details for the new alert's audit). Same check-in first (a later assessment of
    one report), then the unresolved alerts of earlier check-ins (spec §9)."""
    same: list[tuple[Alert, Assessment]] = []
    earlier: list[tuple[Alert, Assessment]] = []
    for alert in store.list_alerts(patient_id=result.patient_id):
        alerted = store.get_assessment(alert.assessment_id)
        if alerted is None:
            continue
        if alerted.checkin_id == result.checkin_id:
            same.append((alert, alerted))
        elif alert.status == "open" or (
            alert.status == "acknowledged" and result.status != "urgent"
        ):  # an urgent report never joins an alert someone has already taken over
            earlier.append((alert, alerted))
    for candidates in (same, earlier):
        if not candidates:
            continue
        # Latest first: join the first unresolved alert this report is no worse than.
        for alert, alerted in sorted(candidates, key=lambda c: c[1].assessed_at, reverse=True):
            if engine.group_with_open(result, [alerted]).kind == "grouped":
                audit.record(store, now, SYSTEM, GROUPED, target=f"alert:{alert.alert_id}",
                             assessment=result.assessment_id, status=result.status,
                             checkin=result.checkin_id)  # fmt: skip
                return True, {}
        latest_alert, latest = max(candidates, key=lambda c: c[1].assessed_at)
        worse = engine.group_with_open(result, [latest])
        return False, {"renews": latest_alert.alert_id, "worse": "; ".join(worse.reasons)}
    return False, {}


def _raise_alert(
    store: Store,
    result: Assessment,
    now: datetime,
    base_url: str,
    escalation: alerts.Escalation,
) -> Alert | None:
    """At most one alert per check-in, unless this assessment is worse than the alerted one; a
    report of an episode with an unresolved alert joins that alert (module docstring)."""
    priority = {"urgent": "urgent", "routine_review": "routine"}.get(result.status)
    if priority is None:
        return None
    alert_id = f"al-{result.assessment_id}"
    existing = store.get_alert(alert_id)
    if existing is not None:  # a retry: this assessment's alert exists
        if existing.status == "open":
            _notify(store, existing, now, base_url, escalation)  # completes an interrupted notification
        return None
    if alert_of(store, result) is not None:  # a retry: grouped already
        return None
    grouped, renewal = _group(store, result, now)
    if grouped:
        return None
    plan = store.get_patient_plan(result.patient_id)
    if plan is None:  # nobody owns this patient: fail visibly (the urgent screen still shows)
        raise LookupError("the patient has no care team to own the alert")
    alert = Alert(
        alert_id=alert_id,
        patient_id=result.patient_id,
        assessment_id=result.assessment_id,
        priority=priority,
        owner_team_id=plan.care_team_id,
        created_at=now,
        ack_deadline=ack_deadline(result, now),
    )
    store.save_alert(alert)
    audit.record(store, now, SYSTEM, "alert_created", target=f"alert:{alert.alert_id}",
                 priority=priority, assessment=result.assessment_id, **renewal)  # fmt: skip
    _notify(store, alert, now, base_url, escalation)
    return alert


def make_process(
    store: Store,
    context_source: ContextSource,
    *,
    plans: Iterable[MonitoringPlan] = (),
    mapping: CtcaeMapping | None = None,
    rules: engine.Ruleset | None = None,
    select_card: SelectCard | None = None,
    escalation: alerts.Escalation | None = None,
    base_url: str,
) -> Process:
    """The `Pipeline.process` for the app. Reads the mapping and ruleset once and checks them
    against `plans` (and `base_url` against the email link rule) now, so bad content or settings
    fail at start-up, not on a patient's report. `base_url` is where the app is reached; urgent
    emails link to its nurse queue."""
    mapping = mapping if mapping is not None else read_mapping()
    rules = rules if rules is not None else engine.read_rules()
    escalation = escalation if escalation is not None else alerts.read_escalation()
    check_plans(plans, mapping, rules)
    notifications.queue_link(base_url)

    def process(checkin: Checkin, plan: MonitoringPlan, now: datetime) -> Assessment | None:
        context = _read_context(store, context_source, checkin.patient_id, now)
        wanted_id = assessment_id(checkin, plan, context, mapping, rules)
        stored = store.get_assessment(wanted_id)
        if stored is not None:  # a retry: same inputs, same verdict
            _raise_alert(store, stored, now, base_url, escalation)
            return stored
        result = engine.assess(
            assessment_id=wanted_id,
            checkin_id=checkin.checkin_id,
            patient_id=checkin.patient_id,
            findings=checkin.findings,
            plan=plan,
            mapping=mapping,
            rules=rules,
            context=context,
            now=now,
        )
        result = redflags.raise_on_hit(result, _checkin_text(checkin))
        if checkin.status == "in_progress" and result.status != "urgent":
            return None  # not finished yet; the next answer is assessed again
        result = _with_card(result, plan, select_card)
        store.add_assessment(result)
        audit.record(store, now, SYSTEM, "assessment_created",
                     target=f"assessment:{result.assessment_id}", status=result.status,
                     checkin=checkin.checkin_id, ruleset=result.ruleset_version,
                     mapping=result.mapping_version, engine=result.engine_version)  # fmt: skip
        _raise_alert(store, result, now, base_url, escalation)
        return result

    return process
