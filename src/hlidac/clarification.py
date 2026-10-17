"""Targeted clarification (WP5, spec §4): after the patient wrote in their own words, ask only what
is still missing, one approved Questionnaire item at a time, with a bounded number of turns.

Pure: no store, network or clock. Without an extracted message the check-in is on the button path
and every enabled item is asked in order (`wizard.question_items`). With one, the questions are, in
this order:

0. **A reported red flag or number read from the patient's words** waits for its confirmation
   button before it can raise urgency (D49), so it is asked first; so is **the measurement a
   reported symptom waits for** (`measured_by`, D54: the temperature after "mám teplotu").
1. **Red-flag items** that aren't settled: not said, "don't know", or a denial or answer the
   patient hasn't confirmed with a button yet (G3, G5). Always asked; they never use up turns.
2. **Grading attributes and follow-ups** of what the patient reported: the items the CTCAE
   mapping reads to grade a present symptom (e.g. the number of vomiting episodes), and items
   enabled by an answer (`enableWhen`).
3. **Required items** nobody talked about.

**Patient history (D56):** symptoms the patient reported in their previous sent report
(`history`, item ids only, read from our own store; never sent to a model) join group 2, so the chat
asks how they are now even when today's message didn't mention them.

**At least `MIN_QUESTIONS` questions (D56):** the chat never ends after the patient's message
alone. When fewer items than that are asked, further open items are added in the same order
(follow-ups and history first), so the team gets a fuller picture before the result. Urgent still
never waits: the pipeline runs after every answer.

Groups 2 and 3 share the plan's `max_clarification_turns`: a turn is one such item the patient
answered with a button after writing. When the turns are used up, the rest stays missing and the
rules engine says `insufficient_information` (never `no_escalation`); the patient may still answer
the rest from the summary. A quoted number the patient only has to confirm (G5) is asked without
using a turn. Optional items that grade nothing are not asked. Urgent never waits: the pipeline
runs after every answer, whatever is still open.
"""

from collections.abc import Mapping

from hlidac import wizard
from hlidac.ctcae import CtcaeMapping, condition_links
from hlidac.schemas import Checkin, MonitoringPlan, PlanItem, QuoteSource

GradingAttributes = Mapping[str, frozenset[str]]  # symptom item -> the items its grade reads

# D56, DEMO — čeká na schválení FN Motol: the fewest questions the chat asks after free text.
MIN_QUESTIONS = 3


def grading_attributes(mapping: CtcaeMapping) -> dict[str, frozenset[str]]:
    """For each mapped symptom item, the other items its CTCAE proposals read."""
    return {
        term.link_id: frozenset(link for p in term.proposals for link in condition_links(p.when))
        - {term.link_id}
        for term in mapping.mappings
    }


def is_clarifying(checkin: Checkin) -> bool:
    """Whether the patient's own words were extracted, so only the missing items are asked."""
    return bool(checkin.extracted_message_ids)


def _answered_after_text(plan: MonitoringPlan, checkin: Checkin) -> list[str]:
    """Items answered with a button after the first extracted message, in order, no repeats."""
    first = next(
        (
            n
            for n, m in enumerate(checkin.messages)
            if m.message_id in checkin.extracted_message_ids
        ),
        None,
    )
    if first is None:
        return []
    known = {item.link_id for item in plan.items}
    answered: list[str] = []
    for message in checkin.messages[first + 1 :]:
        link = message.answer.link_id if message.answer is not None else None
        if link in known and link not in answered:
            answered.append(link)
    return answered


def turns_used(plan: MonitoringPlan, checkin: Checkin) -> int:
    """Non-red-flag items the patient answered with a button after writing."""
    by_id = {item.link_id: item for item in plan.items}
    return sum(not by_id[link].red_flag for link in _answered_after_text(plan, checkin))


def _confirm_only(item: PlanItem, checkin: Checkin) -> bool:
    """A quoted statement the patient only has to confirm with a button (G5)."""
    finding = next((f for f in checkin.findings if f.link_id == item.link_id), None)
    return (
        finding is not None
        and finding.state in ("present", "absent")
        and wizard.needs_confirmation(item, finding)
    )


def _confirmed_quote(item: PlanItem, checkin: Checkin) -> bool:
    """A quoted statement the patient confirmed on the confirmation screen (G5)."""
    finding = next((f for f in checkin.findings if f.link_id == item.link_id), None)
    return (
        finding is not None
        and isinstance(finding.source, QuoteSource)
        and finding.confirmed
        and (item.red_flag or item.type == "quantity")
    )


def questions(
    plan: MonitoringPlan,
    checkin: Checkin,
    attributes: GradingAttributes | None = None,
    history: frozenset[str] = frozenset(),
) -> list[PlanItem]:
    """The items this check-in asks, answered ones included, in the order they are asked.

    Asked items (answered with a button, or a quote confirmed) come first, in the order the
    patient saw them; the open ones follow. So the position only grows and "Zpět" goes to the
    question the patient saw before, even when an answer opens a follow-up that sorts earlier."""
    enabled = wizard.question_items(plan, checkin)
    if not is_clarifying(checkin):
        return enabled
    present = {f.link_id for f in checkin.findings if f.state == "present"}
    wanted = {link for symptom in present for link in (attributes or {}).get(symptom, ())}

    found = {f.link_id: f for f in checkin.findings}
    awaited = {
        plan.measured_by[link]
        for link in plan.measured_by
        if wizard.awaits_measurement(plan, checkin, link)
    }
    # A reported symptom's measurement is asked right after the symptom, and keeps that place
    # once answered, so progress and "Zpět" stay steady (D54).
    measured = {
        measure: symptom
        for symptom, measure in plan.measured_by.items()
        if symptom in present
    }

    def group(item: PlanItem) -> int:
        if (f := found.get(item.link_id)) is not None and wizard.awaits_confirmation(item, f):
            return 0  # its "Je to správně?" decides whether urgent shows now (D49)
        if item.red_flag:
            return 1
        if item.enable_when or item.link_id in wanted or item.link_id in history:
            return 2
        return 3 if item.required else 4

    order = {item.link_id: n for n, item in enumerate(plan.items)}
    answered = _answered_after_text(plan, checkin)
    budget = plan.max_clarification_turns - turns_used(plan, checkin)
    asked: list[PlanItem] = []
    still_open: list[PlanItem] = []
    by_id = {item.link_id: item for item in plan.items}

    def place(item: PlanItem) -> tuple[int, int, int]:
        if (symptom := measured.get(item.link_id)) is not None:
            return (group(by_id[symptom]), order[symptom], 1)
        return (group(item), order[item.link_id], 0)

    for item in sorted(enabled, key=place):
        done = wizard.is_done(item, checkin)
        if not done and wizard.answered_by_reading(plan, checkin, item.link_id):
            continue  # a confirmed high reading already answers it (D55)
        asked_anyway = (
            item.red_flag or _confirm_only(item, checkin) or item.link_id in awaited
        )  # never use up turns
        if item.link_id in answered or (done and _confirmed_quote(item, checkin)):
            asked.append(item)
        elif not done and asked_anyway:
            still_open.append(item)
        elif not done and group(item) in (2, 3) and budget > 0:
            still_open.append(item)
            budget -= 1
    # Button answers take their slots among the asked items in the order they were given; a
    # confirmed quote has no message of its own and keeps its place (it was next when asked).
    by_time = iter(sorted(
        (i for i in asked if i.link_id in answered), key=lambda i: answered.index(i.link_id)
    ))  # fmt: skip
    asked = [next(by_time) if i.link_id in answered else i for i in asked]
    # D56: at least MIN_QUESTIONS questions, the next open items in the same order.
    chosen = {i.link_id for i in (*asked, *still_open)}
    for item in sorted(enabled, key=place):
        if len(asked) + len(still_open) >= MIN_QUESTIONS:
            break
        if (
            item.link_id not in chosen
            and not wizard.is_done(item, checkin)
            and not wizard.answered_by_reading(plan, checkin, item.link_id)
        ):
            still_open.append(item)
    return [*asked, *still_open]


def next_item(
    plan: MonitoringPlan,
    checkin: Checkin,
    attributes: GradingAttributes | None = None,
    history: frozenset[str] = frozenset(),
) -> PlanItem | None:
    """The next item to ask, or None when nothing more is asked."""
    items = questions(plan, checkin, attributes, history)
    return next((i for i in items if not wizard.is_done(i, checkin)), None)


def previous_item(
    plan: MonitoringPlan,
    checkin: Checkin,
    link_id: str,
    attributes: GradingAttributes | None = None,
    history: frozenset[str] = frozenset(),
) -> PlanItem | None:
    items = questions(plan, checkin, attributes, history)
    index = next((n for n, item in enumerate(items) if item.link_id == link_id), len(items))
    return items[index - 1] if index > 0 else None


def reported_before(plan: MonitoringPlan, earlier: list[Checkin]) -> frozenset[str]:
    """D56: the plan's question items the patient reported (`present`) in their latest sent
    report before this one, from our own store. Ids only; nothing of it reaches a model."""
    sent = [c for c in earlier if c.status == "completed"]
    if not sent:
        return frozenset()
    last = max(sent, key=lambda c: c.updated_at)
    asked = {i.link_id for i in plan.items if i.type != "text"}
    return frozenset(f.link_id for f in last.findings if f.state == "present") & asked
