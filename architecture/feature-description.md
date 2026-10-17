# Feature description: Oncology Symptom Companion

**This file is the top authority for the product.** Where it and `../PRINCIPLES.md` (or any other doc) disagree, this file wins and the other doc must be corrected; agents flag such a conflict to the team instead of choosing silently.

See the [workflow diagram](workflow.drawio) for the end-to-end process, [`system-design.md`](system-design.md) for how it is built on InterSystems IRIS, and [`accessibility.md`](accessibility.md) for the frontend requirements.

> **Mentor feedback, Fri 16 Oct 2026:**
> 1. No messenger: our own mobile-friendly web app.
> 2. Severity grades according to CTCAE.
> 3. In-app notifications, mirrored to email.
> 4. Recommendations from the doctor: **open point**.
> 5. Focus on patients with disabilities, with a very friendly frontend following all accessibility practices.
>
> **Team decisions, Fri 16 Oct ~19:00:**
> - Our own web app, and we **prove it is secure** (§15).
> - **Czech only.**
> - **No images** in this version (a possible future feature).
> - The monitoring template is a **FHIR `Questionnaire`**.
> - **Real data from IRIS**, not synthetic (§14).
> - Final delivery in **our private repo**.
> - Submission **Sat 17 Oct, 13:00**.
>
> **Team decision, Fri 16 Oct (evening), on an InterSystems mentor's advice:**
> - **Text extraction uses the team's Azure OpenAI deployment** on minimised, pseudonymised message text, with a local model as a configuration switch (the path for a hospital deployment).
> - **Voice is transcribed locally;** audio never goes to a hosted service.
> - No model ever receives patient records or queries IRIS (§3, §15).
>
> **Team decision, Fri 16 Oct (later): a simplified stack** (details in `system-design.md` §3, D28). The app decides and shows the result at once; IRIS holds the hospital data and ours, and an IRIS Interoperability production writes every check-in back to FHIR and sends every email. In this version check-ins start from the demo panel (no scheduler), and the demo runs on a laptop.

An accessible, secure, mobile-first web app with a conversational assistant that helps oncology patients, **including patients with disabilities**, report symptoms between appointments in Czech, and helps their care team identify concerning changes early.

**Patients describe how they feel in their own words, by voice, text or large buttons. The assistant asks only what is missing. Nurses see who needs attention, how severe it is on the CTCAE scale, and why.**

The system supports patients with an existing medical history and an active treatment plan, taken from the hospital data in InterSystems IRIS. Clinicians approve what the assistant monitors, which advice it can provide, how reported symptoms map to CTCAE grades, and when it must escalate a report.

## 1. Clinician-approved monitoring plan (FHIR Questionnaire)

The doctor defines a shared monitoring template for a patient group with the same illness, **as a FHIR `Questionnaire`** stored in IRIS. Treatment-specific variants are separate, versioned Questionnaires. The Questionnaire holds:
- the clinical fields (items), each linked to its CTCAE term;
- the required questions, in approved Czech wording;
- the answer options, including "Nevím";
- the follow-up questions (conditional items).

The rules, the CTCAE grading mapping and the advice cards reference the Questionnaire's items and version. The doctor does not need to write Pydantic code.

Each patient is assigned a group Questionnaire. The system then combines it with clinician-reviewed patient context from the hospital record in IRIS:
- relevant medical history;
- current treatment;
- baseline symptoms;
- previous adverse events, with their CTCAE grades;
- applicable individual restrictions;
- **accessibility needs and preferences**, for example screen reader, large text, voice-first input, simplified language, or a caregiver who helps with reporting.

The clinician confirms this assignment and any necessary overrides before activation. There are no separate per-patient care-plan resources; a questionnaire does not need to be designed from scratch for every patient.

The plan defines:

- Relevant treatment, treatment dates, and baseline symptoms.
- Symptoms and medication-use information to collect, each linked to its CTCAE term.
- Check-in frequency and required questions.
- Follow-up questions for incomplete reports.
- Rules for urgent alerts, routine review, and missed check-ins.
- Approved patient advice and contact instructions.
- The team responsible for responding, and its notification settings (in app and email).

For the first version, the agent fills the approved schema and supports clarification; it does not author monitoring plans, clinical rules, thresholds or CTCAE mappings. Patient history is supplied as reviewed context rather than allowing the model to independently reinterpret the entire record. Questionnaires and context are versioned so that each report can be traced to the configuration used at that time. Treatment changes or relevant history updates trigger a review of the patient's plan.

## 2. Scheduled check-ins in our own accessible web app

**The patient channel is our own mobile-first web app**, installable to the home screen. No messenger or third-party channel is involved, so no messaging company ever sees a patient report. Everything patients see is in **Czech**. The app follows [`accessibility.md`](accessibility.md) (WCAG 2.2 AA as the minimum, AAA where practical for the main flows).

Patients can:

- Record a voice message in the app.
- Describe symptoms in text.
- Answer structured questions using large buttons.
- Report a new concern at any time, without waiting for the next scheduled check-in.
- Have the screen read aloud (screen reader, or the app's read-aloud of approved text) and enlarge text without losing content.
- Let a caregiver help; the report records who reported.

The check-in starts naturally, one question per screen, for example:

> „Jak se dnes cítíte? Popište nám případné potíže a změny v užívání léků.“

The assistant uses information already provided to avoid repeating questions.

### Notifications: in app and by email

Check-in reminders, clarification requests, messages from the care team and the status of a report ("the team has been notified", "a nurse is reviewing your report") appear in the app's notification centre and are **mirrored to email**. Emails contain no clinical details, only a short notice and a link back to the app. Care-team alerts work the same way: in the clinician app and by email (§9).

### Check-in schedule and rescheduling

The doctor defines the default interval for each group, with patient-specific overrides where necessary. Daily reporting is the proposed demo setting, not a fixed requirement for every patient. Patients can select a preferred time within the approved schedule.

The plan also defines reminder timing, the maximum number of reminders, and what happens when a report remains incomplete or is missed. A completed report suppresses reminders for that check-in. An unsolicited report only satisfies a scheduled check-in when it covers the required fields within the configured reporting window. The app has no time limits: a patient can pause and continue later, and partial answers are kept.

Rescheduling follows explicit rules: a patient may request an allowed time change or snooze, while changes to reporting frequency require clinician approval or a previously approved scheduling rule. Treatment changes can trigger schedule review. The language model does not independently postpone monitoring, and urgent alerts are never delayed by a snooze.

## 3. Speech transcription and structured extraction

Voice messages recorded in the app are transcribed with a **locally run** Whisper model; audio never goes to a hosted service. Voice input is a first-class accessibility feature, not an extra, and every voice step has a text and button alternative.

A language model extracts information into a defined schema. In the demo it is the team's Azure OpenAI deployment; one configuration setting switches it to a locally deployed model, which is the path for a hospital deployment. The model receives only the patient's current message, pseudonymised, and the Questionnaire items: never patient records, identifiers or treatment context (§15). It extracts:

- Reported symptoms and their timing.
- Frequency, severity, and effect on daily activities, where provided (the attributes needed for CTCAE grading).
- Relevant measurements reported by the patient.
- Medication use and deviations from the known treatment plan.
- Uncertainty, contradictions, and missing information.

Each extracted finding retains a reference to the supporting patient statement.

The system explicitly distinguishes **a symptom being absent, not mentioned, or unknown**. It does not invent missing values or present patient reports as verified clinical findings.

**Images are not part of this version.** Photo reporting (with clinician review and a retention policy) is a possible future feature.

### Pydantic validation throughout the data flow

Every structured boundary uses strict Pydantic schemas:
- incoming request metadata and transcription results;
- extracted findings and CTCAE grade proposals;
- clarification state and monitoring configuration;
- assessment results and advice selections;
- notifications and alert payloads;
- the internal records mapped into FHIR.

Audio uploads additionally require file-type, size and decoding checks; their contents are not validated by a Pydantic schema alone.

Schemas enforce types, required fields, allowed values, units, explicit unknown states, source references, and cross-field constraints. Unexpected fields and invalid outputs are rejected rather than silently accepted. Validation failure triggers a bounded retry, targeted clarification, or a visible processing-error state. It never becomes a reassuring result or a successful completion. Unprocessed reports requiring review remain visible to the responsible team.

**Schema validity is not clinical correctness.** A valid object can still contain an incorrect transcription or extraction. Source evidence, consequential-field confirmation, clinical rules, and review remain necessary. FHIR output also requires validation against the chosen FHIR version and profiles.

## 4. Targeted clarification

The assistant compares the extracted information with the Questionnaire and asks only the questions needed to complete the report, determine the CTCAE grade, or decide the next action.

For example:

> Patient: „Dnes jsem zvracela a skoro nic nepila.“  
> Assistant: „Kolikrát jste dnes zvracela?“

Consequential ambiguities, such as an unclear medication dose, trigger a targeted confirmation. If the patient cannot answer, the field remains unresolved.

The system avoids prolonged questioning: questions come one at a time, in plain language, with large answer buttons. A report can remain incomplete, and urgent escalation does not wait for unrelated questions to be answered.

## 5. Medication-use check

The assistant starts from the patient’s recorded medication plan in the hospital record rather than asking them to repeat their entire medication list daily.

Where required by the monitoring plan, it asks about:

- Missed or changed doses.
- Additional medicines taken.
- Actual use of prescribed supportive medication.
- Unclear drug names or amounts.

Patient-reported intake is stored separately from prescribed treatment. The assistant does not change prescriptions or independently recommend dose adjustments.

## 6. CTCAE severity grading

Each reported symptom is expressed on the **NCI CTCAE** scale (grade 1 mild to grade 5 death related to the adverse event), using the version the clinical team chooses. CTCAE v6.0 (2025) is current; the UKONS v3 triage toolkit is built on CTCAE v5.

- The Questionnaire links each symptom to its CTCAE term, and a **clinician-approved mapping** links patient-reportable attributes (for example frequency, severity, effect on eating or daily activities, measured temperature) to a CTCAE grade. The mapping uses the official CTCAE grade definitions word for word, with their source.
- The system proposes a **provisional, patient-reported CTCAE grade** for each symptom, with the evidence used. Where a grade criterion is not patient-reportable (for example "hospitalisation indicated"), the system can only propose "at least grade N, needs clinical assessment".
- **A clinician confirms or corrects every grade.** The confirmed grade is the clinical record; the provisional grade is never presented as a clinician's assessment.
- Missing or unknown attributes never lower a proposed grade.

## 7. Rule-based assessment and escalation

A separate rules engine evaluates the available information against the clinician-approved monitoring plan. The language model supports extraction and conversation; it does not independently define clinical thresholds or grades.

The assessment can consider:

- Current reported symptoms and their provisional CTCAE grades.
- Change from the patient’s baseline.
- Persistence or worsening across reports.
- Relevant treatment context from the hospital record.
- Missing information required for assessment.

Each report receives an operational status:

| Status | System response |
|---|---|
| No escalation criteria met, with required assessment information available | Record the report and provide matching approved advice if available |
| Routine review required | Add the case to the care team’s review queue |
| Urgent criteria met | Immediately initiate the alert workflow and show patient instructions |
| Insufficient information | Ask targeted questions and retain an unresolved status |

These statuses describe workflow urgency. They are not diagnoses; CTCAE grades accompany them as the severity vocabulary, and only a clinician confirms a grade.

## 8. Approved patient guidance and doctor recommendations

The clinical team authors or approves a versioned library of advice cards, using its clinical protocols and relevant reference material. Each Questionnaire references the cards it permits. Patient history and individual restrictions can further limit which cards are eligible.

Each advice card contains:

- An identifier, version, approver, and review date.
- Approved Czech wording, written in plain language (an easy-to-read version where available).
- Explicit applicability conditions, including the symptom, its CTCAE grade and relevant reported attributes.
- Exclusions, required information, and circumstances requiring clinical contact instead.
- Any approved follow-up instructions.

A deterministic selection rule chooses eligible cards using validated report data and the patient's plan. The app delivers the approved wording; it does not invent advice or rephrase medical instructions at runtime. Urgent pathways take priority over routine self-care guidance. If no card matches, or required information is missing, the app does not manufacture a recommendation: it asks for clarification or follows the configured review/contact pathway. Absence of an alert alone is not sufficient to select advice.

Guidance may include:

- Advice for managing reported symptoms.
- Instructions to contact the treating clinic.
- Emergency instructions, including calling 155 when the approved emergency pathway applies, **and an accessible alternative for patients who cannot make a voice call** (to be agreed with the clinical team).
- Clear information about whether the care team has been notified and whether a response should be expected.

The assistant does not generate unrestricted treatment recommendations. Patients are told that the service is not continuously monitored unless the care team actually provides that coverage.

**Open point: recommendations from the doctor.** The mentors asked for doctor recommendations; what that means is not decided yet. Candidates:
- (a) a doctor writes a personal recommendation to the patient from the case view, delivered in the app and announced by email, clearly labelled with the author;
- (b) a doctor chooses and personalises an approved template;
- (c) standing per-patient recommendations stored with the patient's plan.

In every option a named clinician authors or approves the text; a model never writes it. Clarify with the mentors and FN Motol before building.

## 9. Actionable alerts, notifications and clinician work queue

Healthcare professionals receive notifications for cases that meet configured escalation criteria, **in the clinician app and by email** (the email carries no clinical details, only a link). Notifications link to a compact case view containing:

- Patient identity and relevant treatment context, including accessibility needs relevant for contacting the patient.
- The reported concern, its provisional CTCAE grade, and when it occurred.
- The exact rule that triggered escalation.
- Relevant changes from previous reports.
- Missing or uncertain information.
- The patient’s original statement and extracted summary.

Clinicians can acknowledge, assign, confirm or correct the CTCAE grade, and resolve the case, recording the action taken.

Repeated reports about the same unresolved episode are grouped to reduce noise. Worsening symptoms can trigger renewed escalation according to the plan.

## 10. Alert ownership and delivery tracking

Every escalation is routed to a designated team.

The workflow tracks:

- Whether the notification was delivered (in app and by email, separately).
- Whether someone acknowledged responsibility.
- Whether the case was resolved.
- Whether a configured fallback is needed because delivery failed or acknowledgement is overdue.

Coverage hours and fallback contacts are configured in advance. A sent notification or email is never treated as confirmation that the patient has received care.

## 11. Missed check-in handling

If a patient does not respond, the assistant follows the approved outreach policy.

This may include an in-app and email reminder or a task for the care team. Missing reports remain visible and are never interpreted as an absence of symptoms.

## 12. Longitudinal records in IRIS FHIR

Each check-in is stored as a FHIR `QuestionnaireResponse` against the patient's Questionnaire (in progress while partial, completed when done), with its CTCAE grades as linked `Observation`s and, where needed, an alert `Task`, in the IRIS FHIR repository next to the patient's hospital record. The rest of the record (findings with their quotes, assessments, notifications and the audit trail) is kept in IRIS SQL.

The record preserves:

- Patient-reported findings and reporting times, and who reported (patient or caregiver).
- Provisional and clinician-confirmed CTCAE grades.
- Relevant medication-use statements.
- Confirmation and uncertainty status.
- Applicable Questionnaire, mapping and rule versions.
- Advice provided, doctor recommendations and escalation decisions.
- Notifications sent and their delivery status.
- Clinician acknowledgement and resolution history.

The clinician interface provides a simple symptom timeline, by CTCAE grade, to support review of persistence and change.

## 13. Accessibility for patients with disabilities

**Patients with disabilities are a primary audience**, and cancer treatment itself often causes temporary impairments (for example numb fingers from neuropathy, fatigue, vision or concentration problems). Each of these patients can complete a check-in on their own or with a caregiver:
- a blind patient using a screen reader;
- a patient with tremor or numb fingers;
- a patient with low vision;
- a deaf patient;
- a patient with cognitive fatigue.

Requirements, patterns and the testing protocol are in [`accessibility.md`](accessibility.md). In short:
- WCAG 2.2 AA as the minimum;
- semantic HTML, with full screen-reader and keyboard support;
- large touch targets and no time limits;
- one question per screen in plain Czech;
- voice, text and button input for every answer;
- no information by colour alone;
- passwordless login;
- urgent instructions that work without a voice call.

## 14. Real hospital data in IRIS

The app works with **the real patient data held in InterSystems IRIS** (the hospital dataset), not synthetic data.

- **Patient records never leave IRIS.** They are never committed to git, copied into files or documents, pasted into chats, shown in screenshots outside the demo, or sent to any cloud service, including the model. The only patient data a cloud service sees is the patient's own message text, minimised and pseudonymised, sent through the model gateway to the team's approved Azure OpenAI deployment (§15).
- **Coding agents and cloud AI tools never read patient records:** schema and counts only. In the running app, only the model gateway sends patient text to a model, under the rules in §15.
- Everything the app writes (questionnaire responses, findings, grades, tasks, notifications) is tagged, so it can be told apart from the hospital record and removed without touching the original data.
- The demo shows the minimum necessary, with names masked where possible. The terms of use of the dataset are confirmed with FN Motol.
- Automated tests use small hand-made fixtures that contain no patient data, because tests live in git.

## 15. Security, privacy, access and traceability: and how we prove it

We claim the app is secure only as far as we can **demonstrate**:
- **No unnecessary third parties:** our own web app instead of a messenger; records stay in IRIS; voice is transcribed locally.
- **Model calls are minimised and pseudonymised:**
  - the extraction model gets only the current message text and the Questionnaire items, never records, identifiers or treatment context, and it never queries IRIS;
  - before any non-local call the gateway masks the patient's name, birth numbers, phone numbers, email and street addresses;
  - patient text goes only to allow-listed endpoints (the team's Azure OpenAI deployment or a local model) and audio only to local Whisper; anything else raises (enforced in code and tested);
  - the Azure region and data-retention terms are stated in the pitch, and a hospital deployment switches to a local model with one setting.
- **Access:**
  - each login is linked to the enrolled patient (and any authorised caregiver);
  - passwordless and accessible (no puzzles or memory tests), with short-lived single-use links or codes;
  - a patient can see only their own data, and staff only what their role allows (tested);
  - the app connects to IRIS with a least-privilege account, not the administrator.
- **Transport and sessions:** HTTPS; secure, HttpOnly, SameSite session cookies; CSRF protection; strict security headers (CSP and others); no patient data in URLs or logs.
- **Data minimisation:** emails contain no clinical content, and the app reads only the hospital data a rule needs.
- **Audit:** every data access, model call, grading, rule evaluation, notification and clinician action is recorded in IRIS. Retention of recordings and transcripts is explicitly configured.
- **Proof:** a short threat model, automated security tests (access control, CSRF, headers, the model-endpoint guard and pseudonymisation) and the audit trail shown in the demo. We do not claim certifications or penetration tests we have not done.

## Hackathon MVP

The first version demonstrates one complete workflow for a selected treatment group:

- One doctor-defined group Questionnaire with clinician-reviewed patient-history context from IRIS, CTCAE mapping and individual overrides.
- Our own accessible, mobile-first web app as the sole patient channel, in Czech.
- Czech text and voice reporting, and large-button answers.
- Check-ins started from the demo panel (doctor-defined intervals, reminders and rescheduling are later extensions).
- Structured extraction and targeted clarification with strict Pydantic validation at every structured boundary.
- Provisional CTCAE grades per symptom, confirmed by a clinician in the case view.
- A small set of explicit escalation rules.
- A small library of clinician-approved Czech advice cards with explicit selection rules.
- In-app notifications mirrored to email, for patients and the care team.
- A nurse work queue with acknowledgement.
- Check-ins stored as `QuestionnaireResponse` with CTCAE grade `Observation`s in IRIS FHIR, next to the real hospital data, written by an IRIS Interoperability production.
- Accessibility verified with automated checks and manual screen-reader and keyboard tests.
- Security demonstrated with access-control and header tests and the audit trail.

The demo includes three scenarios: a mild report without interruption, a concerning report with an explainable alert, and an ambiguous report that prompts clarification rather than guessing. At least one scenario is performed with a screen reader or voice-only input.

Images, English and other languages, scheduled check-ins and reminders, caregiver mode, medication-use questions, video analysis, broad treatment coverage, additional channels, and AI-assisted drafting of monitoring plans are later extensions. Doctor recommendations remain an open point until clarified.

**Success is measured by whether the system:**
- collects the required information;
- catches the agreed escalation scenarios;
- avoids unnecessary alerts;
- gives a nurse enough context to act;
- can be used independently by patients with disabilities;
- keeps patient records inside the hospital's systems and sends a model only pseudonymised message text.
