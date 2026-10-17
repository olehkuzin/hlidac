"""WP0: the shared contracts enforce the safety principles at the boundary."""

import inspect
import json
from datetime import timedelta

import pytest
from factories import (
    T0,
    alert,
    answered,
    assessment,
    bundle,
    checkin,
    email_item,
    fhir_item,
    grade,
    patient_account,
    put,
    quoted,
    tagged,
    urgent_rule,
)
from pydantic import ValidationError

from hlidac import schemas
from hlidac.fhir_guard import check_bundle, check_json
from hlidac.schemas import (
    DONT_KNOW,
    TYPED_VALUE,
    Account,
    AnswerSource,
    Assessment,
    AuditEntry,
    ButtonAnswer,
    Delivery,
    FhirTransactionJob,
    Finding,
    GradeReview,
    InboundMessage,
    MessageSource,
    MonitoringPlan,
    Notification,
    OutboxItem,
    PatientContext,
    PlanItem,
    Quantity,
    QuoteSource,
    Strict,
    Transcript,
    TriggeredRule,
)


def test_every_contract_is_strict_closed_and_frozen():
    models = [
        obj
        for obj in vars(schemas).values()
        if inspect.isclass(obj) and issubclass(obj, Strict) and obj is not Strict
    ]
    assert len(models) > 20
    for model in models:
        config = model.model_config
        assert (config["extra"], config["strict"], config["frozen"]) == ("forbid", True, True)


def test_unknown_fields_and_coercion_are_rejected():
    with pytest.raises(ValidationError):
        grade(surprise="x")
    with pytest.raises(ValidationError):
        grade(grade="2")  # no str -> int coercion
    with pytest.raises(ValidationError):
        grade(grade=6)


def test_four_finding_states_stay_distinct():
    assert quoted().state == "present"
    assert answered(code="no", state="absent").state == "absent"
    assert Finding(link_id="fever", state="not_mentioned").source is None
    assert answered(code=DONT_KNOW, state="unknown").state == "unknown"
    with pytest.raises(ValidationError):
        Finding(link_id="fever", state="maybe")


def test_present_or_absent_needs_evidence():
    for state in ("present", "absent", "unknown"):
        with pytest.raises(ValidationError):
            Finding(link_id="fever", state=state)


def test_not_mentioned_carries_nothing():
    source = QuoteSource(kind="quote", message_id="m1", quote="nic")
    with pytest.raises(ValidationError):
        Finding(link_id="fever", state="not_mentioned", source=source, extractor_version="v1")


def test_dont_know_always_means_unknown():
    with pytest.raises(ValidationError):
        answered(code=DONT_KNOW, state="absent")
    with pytest.raises(ValidationError):
        answered(code="no", state="unknown")


def test_button_answer_must_match_its_item():
    finding = answered()
    with pytest.raises(ValidationError):
        Finding(link_id="nausea", state="present", code="yes", source=finding.source)


def test_typed_value_travels_with_its_answer():
    value = Quantity(value=38.4, unit="Cel")
    source = AnswerSource(
        kind="answer", message_id="m3", link_id="temperature", code=TYPED_VALUE, value=value
    )
    finding = Finding(
        link_id="temperature", state="present", code=TYPED_VALUE, value=value, source=source
    )
    assert finding.value == value
    with pytest.raises(ValidationError):  # the finding's value differs from what was typed
        Finding(
            link_id="temperature",
            state="present",
            code=TYPED_VALUE,
            value=Quantity(value=36.6, unit="Cel"),
            source=source,
        )
    with pytest.raises(ValidationError):  # a typed answer without its value
        AnswerSource(kind="answer", message_id="m3", link_id="temperature", code=TYPED_VALUE)
    with pytest.raises(ValidationError):  # a button answer with a value
        ButtonAnswer(link_id="fever", code="yes", value=value)
    assert ButtonAnswer(link_id="temperature", code=TYPED_VALUE, value=value).value == value


def test_quoted_finding_records_extractor_version():
    source = QuoteSource(kind="quote", message_id="m1", quote="mám horečku")
    with pytest.raises(ValidationError):
        Finding(link_id="fever", state="present", source=source)


def test_an_unread_message_is_only_ever_a_present_finding():
    source = MessageSource(kind="message", message_id="m1")
    assert Finding(link_id="other", state="present", source=source).source == source
    for bad in (
        {"state": "absent"},
        {"state": "unknown", "code": DONT_KNOW},
        {"state": "present", "code": "yes"},
        {"state": "present", "extractor_version": "extract-v1"},
        {"state": "present", "confirmed": True},
    ):
        with pytest.raises(ValidationError):
            Finding(link_id="other", source=source, **bad)


def test_findings_round_trip_through_json():
    for finding in (quoted(attributes={"episodes_per_day": 3}), answered()):
        assert Finding.model_validate_json(finding.model_dump_json()) == finding


def test_grade_is_exact_or_at_least():
    with pytest.raises(ValidationError):
        grade(at_least=2)  # both set
    with pytest.raises(ValidationError):
        grade(grade=None)  # neither set
    with pytest.raises(ValidationError):
        grade(grade=None, at_least=2)  # at least without clinical assessment
    assert grade(grade=None, at_least=2, needs_clinical_assessment=True).at_least == 2


def test_grade_is_provisional_until_a_clinician_confirms():
    assert grade().status == "provisional"
    with pytest.raises(ValidationError):
        grade(confirmed_by="acc-n1", confirmed_at=T0)  # provisional with a reviewer
    with pytest.raises(ValidationError):
        grade(status="confirmed")  # no reviewer
    with pytest.raises(ValidationError):
        grade(
            status="confirmed",
            grade=None,
            at_least=2,
            needs_clinical_assessment=True,
            confirmed_by="acc-n1",
            confirmed_at=T0,
        )
    assert grade(status="confirmed", confirmed_by="acc-n1", confirmed_at=T0).grade == 1


def test_missing_items_never_end_as_no_escalation():
    with pytest.raises(ValidationError):
        assessment(missing_link_ids=["fever"])
    assert assessment(status="insufficient_information", missing_link_ids=["fever"])


def test_urgent_names_its_rule_and_overrides_advice():
    with pytest.raises(ValidationError):
        assessment(status="urgent")
    with pytest.raises(ValidationError):
        assessment(
            status="urgent",
            triggered_rules=[urgent_rule()],
            advice_card_id="card-nausea",
            advice_card_version="1",
        )
    assert assessment(status="urgent", triggered_rules=[urgent_rule()]).status == "urgent"


def test_escalating_rules_and_open_grades_never_end_as_no_escalation():
    routine = TriggeredRule(
        rule_id="amber", rule_version="1", outcome="routine_review", inputs={}, reason="DEMO"
    )
    with pytest.raises(ValidationError):
        assessment(triggered_rules=[urgent_rule()])  # urgent rule, status no_escalation
    with pytest.raises(ValidationError):
        assessment(triggered_rules=[routine])
    open_grade = grade(grade=None, at_least=2, needs_clinical_assessment=True)
    with pytest.raises(ValidationError):
        assessment(grades=[open_grade])
    assert assessment(status="routine_review", triggered_rules=[routine], grades=[open_grade])


def test_advice_only_follows_no_escalation():
    card = {"advice_card_id": "card-nausea", "advice_card_version": "1"}
    assert assessment(**card).advice_card_id == "card-nausea"
    with pytest.raises(ValidationError):
        assessment(status="insufficient_information", missing_link_ids=["fever"], **card)


def test_assessment_snapshots_what_the_engine_saw():
    context = PatientContext(
        patient_id="p1",
        source_refs=["MedicationAdministration/x"],
        read_at=T0,
        last_sact_dose_at=T0 - timedelta(days=10),
    )
    replay = assessment(context=context)
    assert Assessment.model_validate_json(replay.model_dump_json()) == replay
    with pytest.raises(ValidationError):
        assessment(context=context.evolve(patient_id="p2"))


def test_grade_review_holds_a_reviewed_grade():
    reviewed = grade(status="corrected", grade=2, confirmed_by="acc-n1", confirmed_at=T0)
    assert GradeReview(review_id="r1", assessment_id="a1", grade=reviewed, note="DEMO")
    with pytest.raises(ValidationError):
        GradeReview(review_id="r1", assessment_id="a1", grade=reviewed)  # no reason
    with pytest.raises(ValidationError):
        GradeReview(review_id="r1", assessment_id="a1", grade=grade())  # still provisional


def test_engine_never_outputs_confirmed_grades():
    confirmed = grade(status="confirmed", confirmed_by="acc-n1", confirmed_at=T0)
    with pytest.raises(ValidationError):
        assessment(grades=[confirmed])


def test_message_payload_matches_its_kind():
    fields = {
        "message_id": "m1",
        "checkin_id": "c1",
        "patient_id": "p1",
        "session_account_id": "acc-p1",
        "reported_by": "patient",
        "received_at": T0,
    }
    assert InboundMessage(kind="text", text="bolí mě hlava", **fields)
    with pytest.raises(ValidationError):
        InboundMessage(kind="voice", text="bolí mě hlava", **fields)
    with pytest.raises(ValidationError):
        InboundMessage(kind="text", text="x", audio_ref="a1", **fields)


def test_transcript_correction_is_explicit():
    with pytest.raises(ValidationError):
        Transcript(message_id="m1", text="x", model="whisper", review="corrected")
    assert Transcript(message_id="m1", text="x", model="whisper").review == "pending"


def _choice(link_id: str, codes: list[str], **kw) -> PlanItem:
    options = [{"code": code, "text_cs": code} for code in codes]
    return PlanItem.model_validate(
        {
            "link_id": link_id,
            "text_cs": "Otázka",
            "type": "choice",
            "required": True,
            "options": options,
        }
        | kw
    )


def test_every_choice_item_offers_dont_know():
    assert _choice("fever", ["yes", "no", DONT_KNOW])
    with pytest.raises(ValidationError):
        _choice("fever", ["yes", "no"])


def test_red_flags_are_button_items():
    with pytest.raises(ValidationError):
        PlanItem(
            link_id="temp",
            text_cs="Teplota",
            type="quantity",
            required=True,
            unit="Cel",
            red_flag=True,
        )


def test_plan_items_and_conditions_are_consistent():
    fever = _choice("fever", ["yes", "no", DONT_KNOW])
    chills = _choice(
        "chills", ["yes", "no", DONT_KNOW], enable_when=[{"link_id": "fever", "answer_code": "yes"}]
    )
    plan = {
        "questionnaire_url": "urn:q",
        "questionnaire_version": "1",
        "title_cs": "Demo",
        "rule_ids": [],
        "mapping_ids": [],
        "advice_card_ids": [],
        "max_clarification_turns": 3,
        "source_ref": "DEMO",
    }
    assert MonitoringPlan(items=[fever, chills], **plan).item("chills") == chills
    with pytest.raises(ValidationError):
        MonitoringPlan(items=[fever, fever], **plan)
    with pytest.raises(ValidationError):
        MonitoringPlan(items=[chills], **plan)  # depends on a missing item


def test_checkin_failure_stays_visible():
    with pytest.raises(ValidationError):
        checkin(status="processing_failed")
    assert checkin(status="processing_failed", failure="model output invalid").failure
    with pytest.raises(ValidationError):
        checkin(findings=[quoted(), quoted()])


def test_only_the_checkins_own_text_messages_are_extracted():
    def message(kind, **payload):
        return InboundMessage(
            message_id=f"m-{kind}", checkin_id="c1", patient_id="p1", session_account_id="a1",
            reported_by="patient", received_at=T0, kind=kind, **payload,
        )  # fmt: skip

    written = message("text", text="TEST")
    button = message("answer", answer=ButtonAnswer(link_id="fever", code="yes"))
    assert checkin(messages=[written], extracted_message_ids=["m-text"]).extracted_message_ids
    with pytest.raises(ValidationError):
        checkin(messages=[button], extracted_message_ids=["m-answer"])  # not a text message
    with pytest.raises(ValidationError):
        checkin(extracted_message_ids=["m-other"])  # not in this check-in
    with pytest.raises(ValidationError):
        checkin(messages=[written], extracted_message_ids=["m-text", "m-text"])


def test_alert_lifecycle_fields_match_status():
    assert alert().status == "open"
    with pytest.raises(ValidationError):
        alert(status="acknowledged")
    acked = {"status": "acknowledged", "acknowledged_by": "acc-n1", "acknowledged_at": T0}
    assert alert(**acked)
    with pytest.raises(ValidationError):
        alert(status="resolved", acknowledged_by="acc-n1", acknowledged_at=T0)
    with pytest.raises(ValidationError):
        alert(ack_deadline=T0 - timedelta(minutes=1))


def test_accounts_are_scoped():
    assert patient_account().patient_id == "p1"
    with pytest.raises(ValidationError):
        patient_account(patient_id=None)
    with pytest.raises(ValidationError):
        patient_account(email="Pacient1@demo.local")
    with pytest.raises(ValidationError):
        Account(
            account_id="acc-n1",
            email="n@demo.local",
            role="nurse",
            display_name="Sestra",
            patient_id="p1",
            team_id="team-onko",
        )


def test_notification_links_carry_no_query_string():
    fields = {
        "notification_id": "n1",
        "recipient_account_id": "acc-p1",
        "kind": "team_notified",
        "template_id": "tpl",
        "template_version": "1",
        "created_at": T0,
    }
    assert Notification(link="/vysledek/c1", **fields)
    with pytest.raises(ValidationError):
        Notification(link="/vysledek?patient=p1", **fields)
    with pytest.raises(ValidationError):
        Notification(link="//evil.example/x", **fields)  # protocol-relative: another site
    with pytest.raises(ValidationError):
        Delivery(channel="email", at=T0)


def test_model_call_is_audited_and_voice_stays_local():
    call = {
        "data_class": "PATIENT",
        "endpoint_host": "localhost",
        "model": "whisper",
        "request_text": "audio:a1",
        "response_text": "text",
        "duration_ms": 10,
        "ok": True,
    }
    with pytest.raises(ValidationError):
        AuditEntry(
            audit_id="au1",
            at=T0,
            actor="system",
            action="model_call",
            model_call={"purpose": "transcription", "backend": "azure", **call},
        )
    with pytest.raises(ValidationError):
        AuditEntry(audit_id="au1", at=T0, actor="system", action="model_call")
    entry = AuditEntry(
        audit_id="au1",
        at=T0,
        actor="system",
        action="model_call",
        model_call={"purpose": "transcription", "backend": "local", **call},
    )
    assert AuditEntry.model_validate_json(entry.model_dump_json()) == entry


def test_outbox_round_trips_both_job_kinds():
    for item in (fhir_item(), email_item()):
        assert OutboxItem.model_validate_json(item.model_dump_json()) == item


def _job(*entries) -> FhirTransactionJob:
    return FhirTransactionJob.of(bundle(*entries))


def test_outbox_writes_only_tagged_resources_of_our_types():
    untagged = tagged("Observation")
    untagged["meta"] = {}
    with pytest.raises(ValidationError):
        _job(put(untagged))
    with pytest.raises(ValidationError):
        _job(put(tagged("Patient")))  # hospital resource type
    with pytest.raises(ValidationError):
        _job()  # empty bundle
    with pytest.raises(ValidationError):
        FhirTransactionJob(kind="fhir_transaction", bundle_json="not json")


SAFE_JSON = fhir_item().job.bundle_json


@pytest.mark.parametrize(
    "ambiguous",
    [
        # Python keeps the last value, other parsers may keep the first: the guard would check
        # one bundle and IRIS could post another.
        '{"type": "batch", ' + SAFE_JSON[1:],
        SAFE_JSON.replace('"resourceType": "Q', '"resourceType": "Patient", "resourceType": "Q'),
        SAFE_JSON.replace('"code": "hlidac"', '"code": "other", "code": "hlidac"'),
    ],
    ids=["bundle-type", "resource-type", "tag-code"],
)
def test_guard_refuses_duplicate_keys(ambiguous):
    check_bundle(json.loads(ambiguous))  # the last value wins: safe without the duplicate check
    with pytest.raises(ValueError, match="repeats a key"):
        check_json(ambiguous)
    with pytest.raises(ValidationError):
        FhirTransactionJob(kind="fhir_transaction", bundle_json=ambiguous)


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_guard_refuses_non_finite_numbers(number):
    extension = f'"extension": [{{"valueDecimal": {number}}}], '
    text = SAFE_JSON.replace('"identifier": [', extension + '"identifier": [')
    check_bundle(json.loads(text))  # Python reads them; JSON has no such numbers
    with pytest.raises(ValueError):
        check_json(text)


def _with(**fields) -> str:
    return json.dumps(bundle(put(tagged("Observation") | fields)))


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        b"{}",
        3,
        ["{}"],
        "[" * 100_000,  # deeper than the parser recurses
        _with(meta="hlidac"),
        _with(meta=["hlidac"]),
        _with(meta={"tag": 3}),
        _with(meta={"tag": {"system": "urn:hlidac:tag", "code": "hlidac"}}),
        _with(resourceType=["Observation"]),
        _with(resourceType={"Observation": 1}),
    ],
    ids=["none", "bytes", "int", "list", "deep", "meta-str", "meta-list", "tag-int", "tag-dict",
         "type-list", "type-dict"],
)
def test_guard_raises_only_value_error(malformed):
    """Its promise: Pydantic turns only ValueError into a ValidationError; any other error would
    escape the job's validation."""
    with pytest.raises(ValueError):
        check_json(malformed)


@pytest.mark.parametrize(
    "query",
    [
        "identifier=http://hospital.example|123",  # a hospital identifier system
        "identifier=urn:hlidac:checkin|c1,http://hospital.example|9",  # comma = OR
        "identifier=urn:hlidac:checkin|c1%2Chttp://hospital.example|9",  # encoded comma
        "identifier=",  # empty: would create on every retry
        "identifier=urn:hlidac:checkin|c1&_id=hospital-1",  # extra parameter
        "identifier=urn:hlidac:checkin|c1&identifier=urn:hlidac:checkin|c2",  # two identifiers
        "identifier=urn:hlidac:checkin|c2",  # not the resource's own identifier
        "_id=hospital-1",
    ],
)
def test_conditional_writes_match_only_our_own_identifier(query):
    with pytest.raises(ValidationError):
        _job(put(tagged("Observation"), query=query))
    create = {
        "resource": tagged("Task"),
        "request": {"method": "POST", "url": "Task", "ifNoneExist": query},
    }
    with pytest.raises(ValidationError):
        _job(create)


def test_outbox_writes_never_delete_patch_or_hit_hospital_ids():
    resource = tagged("Task")
    for request in (
        {"method": "PUT", "url": "Task/123"},  # not our id
        {"method": "POST", "url": "Task"},  # unconditional create
        {"method": "DELETE", "url": "Task?identifier=urn:hlidac:checkin|c1"},
        {"method": "PATCH", "url": "Task?identifier=urn:hlidac:checkin|c1"},
        {"method": "PUT", "url": "Flag?identifier=urn:hlidac:checkin|c1"},  # other type
    ):
        with pytest.raises(ValidationError):
            _job({"resource": resource, "request": request})
    with pytest.raises(ValidationError):
        _job(put(tagged("Observation", id="hospital-obs-1")))
    create = {"method": "POST", "url": "Task", "ifNoneExist": "identifier=urn:hlidac:checkin|c1"}
    assert _job({"resource": resource, "request": create})
    assert _job(put(tagged("QuestionnaireResponse") | {"identifier": resource["identifier"][0]}))


def test_provenance_is_written_by_our_own_id():
    provenance = tagged("Provenance", id="hlidac-prov-c1")
    del provenance["identifier"]
    put_by_id = {"method": "PUT", "url": "Provenance/hlidac-prov-c1"}
    assert _job({"resource": provenance, "request": put_by_id})
    with pytest.raises(ValidationError):
        _job({"resource": provenance, "request": {"method": "PUT", "url": "Provenance/other"}})


def test_checked_bundle_cannot_be_changed_afterwards():
    job = fhir_item().job
    copy = job.bundle()
    copy["entry"].append({"request": {"method": "DELETE", "url": "Patient/1"}})
    assert len(job.bundle()["entry"]) == 1
    with pytest.raises(ValidationError):
        job.bundle_json = "{}"  # frozen


def test_outbox_status_fields_are_consistent():
    item = fhir_item()
    with pytest.raises(ValidationError):
        OutboxItem.model_validate(item.model_dump() | {"status": "done"})
    with pytest.raises(ValidationError):
        OutboxItem.model_validate(item.model_dump() | {"status": "failed", "processed_at": T0})


def test_evolve_revalidates_changes():
    acked = alert().evolve(status="acknowledged", acknowledged_by="acc-n1", acknowledged_at=T0)
    assert acked.status == "acknowledged"
    with pytest.raises(ValidationError):
        alert().evolve(status="acknowledged")  # model_copy(update=...) would have allowed this


def test_patient_context_immunocompromised_is_unknown_unless_the_record_says():
    from hlidac.schemas import PatientContext

    base = {"patient_id": "p1", "source_refs": ["Condition/x"], "read_at": T0}
    assert PatientContext(**base).immunocompromised is None
    assert PatientContext(**base, immunocompromised=False).immunocompromised is False
    with pytest.raises(ValidationError):
        PatientContext(**base, immunocompromised="no")
