# Safety principles

The rules Hlídač is built on. The code, the content files in `data/` and the tests refer to them by number ("principle 2"). Nobody on the team has medical training, and the prototype is not clinically validated: every clinical value and patient-facing text is labelled DEMO and waits for approval by FN Motol.


1. **The LLM reads, the rules decide.** The LLM (Azure or local, behind the gateway; principle 5) only fills the plan's strict schema. A pure, versioned grading and rules engine sets grades and status; no LLM sets urgency or a grade. Guards G1–G8 are in `architecture/system-design.md` §6.
2. **Uncertainty never lowers urgency.**
   - `absent`, `not_mentioned` and `unknown` are distinct. Only an explicit, quoted or button-confirmed denial is `absent`, and "Nevím" means `unknown`.
   - A missing red-flag answer can't lead to `no_escalation`, and a missing attribute never lowers a CTCAE grade.
   - A missed check-in, failed processing or partial report stays visible.
   - Free text, model output and phrase hits can only raise urgency.
3. **Patients see only approved text:**
   - versioned advice cards, Questionnaire item texts, notification templates and urgent instructions, in plain Czech;
   - no model writes patient-facing text, and doctor recommendations (open, D15) are always authored or approved by a named clinician;
   - urgent overrides self-care advice and includes the approved contact path (155 when it applies, plus a non-voice route; D19);
   - the app says whether the team was notified and never implies continuous monitoring;
   - **emails carry no clinical content.**
4. **The system never diagnoses or changes treatment.**
   - Statuses describe workflow urgency.
   - **CTCAE grades are provisional until a clinician confirms or corrects them,** never presented as a clinician's assessment, and never shown to patients.
   - A named person handles every alert.
   - Patient-reported medication use is stored separately from prescriptions.
5. **Patient text goes only to approved model endpoints, minimised and pseudonymised; voice stays local** (D5, revised Fri 16 Oct on an InterSystems mentor's advice).
   - **Voice:** audio is transcribed only by local Whisper on our own infrastructure and never goes to a hosted API.
   - **Text extraction:** the team's Azure OpenAI deployment in the demo, or a local LLM (Ollama) by a config switch. A hospital deployment runs locally (on-prem).
   - **The model never sees records:** it gets only the current message text and the Questionnaire items, with no patient ID, dates or treatment context, and it never queries IRIS.
   - **Before any non-local call** the gateway masks the patient's name, birth numbers, phone numbers, email and street addresses.
   - The model gateway **raises** on `PATIENT` text to any endpoint not on its allow-list (local, or the approved Azure deployment) and on audio to any non-local endpoint; tests cover both. Never OpenAI's public API or any other cloud service.
   - The Azure region and data-retention terms (abuse monitoring) are checked and stated in the pitch.
6. **Urgent never waits** for clarification, storage, email, a snooze or a final FHIR write. Retries are idempotent and never duplicate alerts.
7. **Everything is traceable:**
   - every finding → the patient's statement or button answer (the quote or answer in the check-in record; `Provenance` links each grade to its `QuestionnaireResponse`);
   - every grade → mapping version and basis;
   - every assessment → plan, context and rule versions, replayable;
   - every alert → owner, delivery per channel, acknowledgement, resolution.
8. **Everything is audited,** including exactly what each model saw.
9. **One controllable clock.** All scheduling, deadlines and "days since last dose" use the demo clock (`AS_OF` in config). Never call `datetime.now()` in domain code.
10. **Every structured boundary is strictly validated** (Pydantic, `extra="forbid"`, `strict=True`). Failure leads to a retry, a clarification or a visible error, never success. Schema validity is not clinical correctness.
11. **Accessibility is not optional.**
    - **Every patient screen meets WCAG 2.2 AA**, and the check-in flow meets the AAA targets in `architecture/accessibility.md`.
    - Every answer can be given by text, voice or buttons.
    - No time limits; status never shown by colour alone.
    - A UI change isn't done until it passes `accessibility.md` §7.
12. **Real patient data stays in IRIS** (D23).
    - The app reads the hospital data (read-only) and writes only `hlidac`-tagged resources and its own SQL rows.
    - Patient data never goes into git, files, logs, URLs, screenshots outside the demo, chats or any cloud tool (the only exception is the model gateway's pseudonymised message text, principle 5). For the demo, FN Motol agreed that the team's agents may read and write the hospital data in IRIS (D51); prefer counts and aggregates.
    - Tests use hand-made fixtures with no patient data.
    - The demo shows the minimum, with names masked where possible.
13. **Security is proven, not claimed.**
    - Passwordless login, per-patient and per-role access control, CSRF protection, least-privilege IRIS account, full audit (strict headers dropped with WP31, D43: never claim them).
    - Each control has a test (`architecture/system-design.md` §9); we claim nothing we haven't tested.


## Data handling and the IRIS gotchas

- **Pseudonymise** patient text before any non-local model call and in logs shown outside the audit screen: names, Czech birth numbers (`\d{6}/?\d{3,4}`), addresses, phone numbers and emails.
- The app writes only `hlidac`-tagged FHIR resources (conditional PUT on our own identifiers) and its own `Hlidac.*` SQL rows; hospital resources are read-only.
- Community Edition allows 8 connections, and FHIR REST calls count: one shared connection, cursors always closed.
- IRIS for Health stores `Subscription` resources but does not send notifications, so the production polls the `Hlidac.outbox` table.
