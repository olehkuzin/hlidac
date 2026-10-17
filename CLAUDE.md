# CLAUDE.md — Rakathon 2026: Oncology Symptom Companion (`praha-03`)

## Draft brief (Fri 16 Oct 2026, ~19:00), built on the team's architecture, the mentors' feedback and the team's decisions. Double-check assumptions with the user.

**`architecture/feature-description.md` is the top authority** (team decision D26). Where this file disagrees with it, the spec wins: fix this file and tell the team.

Repo commands, layout, git workflow, agent rules and the safety and accessibility checklists (including the hands-off rule for `../VI-KA/`):
@AGENTS.md

You are helping team **VI-KA** win **Rakathon 2026** (Prague, 16–18 October 2026), a national AI-for-oncology hackathon.

- **Challenge:** **`praha-03` Digitální hlídač nežádoucích účinků** (digital side-effect watchdog; owner FN Motol a Homolka). Regularly send a cancer patient a short side-effect questionnaire, advise them how to cope, and alert the care team on its own at warning levels. We only have the public summary (`docs/praha-03-brief.md`).
- **Main goal: the InterSystems prize** (€1,500, one team nationally). It's judged on innovation, real-world impact, use of InterSystems technology and technical coherence. Make IRIS central and visible in every component, not decorative. The challenge prize is secondary.
- **Time:** submission is **Sat 17 Oct at 13:00** (decided); freeze at 11:00. Optimise for a **working, honest demo**, not completeness. Keep `main` runnable at all times.
- **Delivery:** our private repo `nazarskiy/rakhaton` (decided). `../VI-KA/` stays hands-off.

> **Final scope (team decision D43, Sat 17 Oct ~03:00; full list in `architecture/roadmap.md`, "Final scope"). Don't work on anything outside it without the user's OK.**
> - **Must do, in order:** (1) WP14 (done): treatment context (last dose) **read from the hospital data in IRIS**, never set by us; demo patients are those with dose records; real names never shown, only "Pacient 01…"; a simple reset of our own writes. (2) **WP22 (done): the red-flag phrase scan** (critical states in free text; a hit only raises urgency; cited phrases). (3) One full run of all three scenarios on IRIS. (4) WP2 review. (5) Pitch, README, backup video.
> - **Only if time is left:** voice (S2, S4, WP17; done, minimal: see below), then evaluation (WP21), then WP20, WP24, WP18 grouping, WP29, the WP15 manual log.
> - **Scenario 3 by voice ran end to end (Sat 17 Oct, Nazar + Claude; in-memory store, demo patient A, a synthesized Czech recording uploaded like the browser does):** Whisper transcript word for word in ~2 s (warm) → Azure extraction quoted nausea and vomiting → red flags confirmed by buttons → only intake, episodes, diarrhoea asked → "Hlášení si prohlédne zdravotní sestra"; nurse queue: one amber UKONS rating, provisional Nausea ≥ 2, Vomiting ≥ 1; the case audit shows both Whisper calls in-process (audio size + hash only) and replays "Shoduje se". **Open:** the live browser microphone (not yet tried), (fixed: the audit timeline now labels transcription calls "přepis hlasu" and names the store it runs on).
> - **Dropped:** WP27 doctor recommendations, WP28 stretch, the rest of S1 (port lockdown, password change), WP31 headers and threat model, S6 (folded into WP14).
> - **Kept as built:** emails through the app's outbox runner; the IRIS production (no IoP, D40) writes FHIR only.
> - **FN Motol's agreement (D51, reported by Nazar, Sat 17 Oct 2026):** FN Motol verified the approach and agreed that the team and its agents may **read and write the hospital data in IRIS** for the demo, and that the team may write clinical content (questions, red flags, CTCAE mappings, recommendations, demo clinical records) for the demo. Content stays labelled DEMO (not a clinical sign-off of each item). Still prefer counts and aggregates; patient data still stays out of git, and an IRIS wipe still needs the team's OK.
> - **Illness groups (D50, D51):** 20 cancer groups by ICD-10, each with its own questionnaire, verified on the IRIS data (all 325 coded patients, 72 of 73 dose patients; `docs/illness-groups.md`).

## 1. What we build

**Oncology Symptom Companion.**
- **Our own accessible, mobile-first web app**, with no messenger and no third parties. It's built first for **patients with disabilities**, and we **prove it is secure**.
- Patients describe how they feel **in Czech**, by text, voice or large buttons, answering a clinician-defined **FHIR `Questionnaire`**, and the app asks only what is missing. No images in this version.
- It runs on **the real hospital data in IRIS**; records never leave IRIS, and a model sees only the patient's own message, minimised and pseudonymised.
- Each symptom gets a **provisional CTCAE grade** that a clinician confirms.
- Nurses see who needs attention, how severe it is and why, **in the app and by email**.

| Source | What it holds |
|---|---|
| `architecture/feature-description.md` | Product behaviour (team-owned; **top authority**; mentor feedback and team decisions applied) |
| `architecture/accessibility.md` | Frontend requirements: WCAG 2.2 AA minimum, personas, design rules, component patterns, testing protocol |
| `architecture/workflow.drawio` | The workflow diagram (editable draw.io source; the PNG is an older export) |
| `architecture/system-design.md` | How we build it on IRIS: components, contracts, CTCAE grading, LLM guards, FHIR mapping, security, **decisions** |
| `architecture/roadmap.md` | Milestones, spikes, work packages with acceptance tests, cut list, demo script, mentor questions |
| `docs/` | The brief, medical evidence, InterSystems prize and toolkit, event facts, sources |

**Our angle against existing ePRO products** (Noona, Kaiku and others, in `docs/deep-dive-praha-03.md` §7):
- **usable by patients with disabilities;**
- **patient records never reach a model or a third party** (no messenger; the extraction model sees only the pseudonymised message text, and voice is transcribed locally);
- **conversation instead of forms;**
- **the LLM reads, the rules decide;**
- standard CTCAE severity for clinicians;
- fewer, explained, grouped alerts with a closed loop;
- standard FHIR inside InterSystems IRIS.

**Evidence, one line each** (sources in the deep dive):
- ePRO monitoring cut ER visits (34 % vs 41 %) and improved quality of life (STAR, Basch 2016).
- The largest trial, PRO-TECT (Nature Medicine 2025), found **no survival difference**. **Never claim a survival benefit.**
- In PRO-TECT about **1 in 3 surveys triggered an alert, and 47 % of nurses said there were too many.** That's the problem we attack.

**Nobody on the team has medical education.**
- Never invent thresholds, CTCAE mappings, red-flag phrases or advice.
- Every clinical value and every patient-facing text needs a cited source and is labelled "demo default, needs Motol approval".
- CTCAE grade definitions are copied **verbatim** from the official NCI file, with the version.

## 2. What the judges told us they want (from a judge; applies to all teams)

- **Must-haves:**
  - data minimisation;
  - full data governance, with every source known;
  - **a human in the loop** (the judges distrust AI);
  - traceability.
- **Strong pluses:**
  - AI outputs as deterministic as possible;
  - model flexibility (local or cloud).

Never trade these away for a feature.

## 3. Non-negotiable principles

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

## 4. Architecture in one screen

Simplified stack (D28): the app decides, IRIS stores and delivers.

```
Demo laptop: browser at localhost ─► FastAPI app (one process; server-rendered, login, CSRF)
  ├ pure core: plans · ctcae (provisional grades) · assessment (rules) · clarification · advice
  ├ model gateway ─► Azure OpenAI (text, pseudonymised) │ mlx-whisper inside the app (voice, only if time)
  ├ Store ─► IRIS SQL schema Hlidac (InMemoryStore without IRIS)
  └ ContextSource ─► hospital FHIR data, read only (hand-made fixtures without IRIS)
  result at once: urgent screen + alert row │ next question │ advice card │ nurse queue
  then: outbox rows (FHIR transaction bundles, emails), idempotent; the app's runner sends the emails
IRIS on the team's Azure VM (through an SSH tunnel):
  FHIR R4: hospital data (read only) + ours, tagged: Questionnaire, QuestionnaireResponse,
           Observation (CTCAE grades, preliminary → final), Provenance, Task
  Production "Hlidac.Production" (IRIS classes, no IoP; D40): OutboxService ─► OutboxProcess ─► FhirWriter
Clinician web app (Czech): nurse queue · case view (confirm CTCAE grade) · unresolved · plans/rules · audit · demo
```

- Domain logic is pure Python in `src/hlidac/` with no IRIS imports; the app and the tests call the same functions. The production only moves ready FHIR jobs from the outbox; it runs `hlidac/fhir_guard.py` (stored in IRIS) again before every FHIR write. Email jobs are sent by the app's outbox runner (`OUTBOX_RUNNER=app`, D40).
- The outbox (`src/hlidac/schemas/outbox.py`, table `Hlidac.outbox`) is the only interface between the app and the production. It accepts only tagged writes of our five resource types on our own identifiers, and emails rendered from approved templates.
- Content (Questionnaire, CTCAE mappings, rules, advice cards, templates) is versioned files in `data/`; every assessment records the versions it used.
- Full mapping of every diagram node to its module, IRIS feature and priority: `architecture/system-design.md` §3–§4.

## 5. IRIS and platform facts that bite

IRIS details are in `docs/intersystems-toolkit.md`.

- **Kit:** https://github.com/intersystems-community/FHIR-AI-Hackathon-Kit.
  - Namespace `FHIRSERVER`, FHIR at `http://localhost:52773/fhir/r4`, SQL on 1972.
  - Re-running `start.sh` duplicates patients.
  - **`docker compose … down -v` would delete the real hospital dataset. Never run it, or any IRIS wipe, without the team's explicit approval.** Our own demo writes are removed with the WP14 reset (our SQL rows and tagged resources only).
- **IRIS runs on the team's Azure VM** (its address only in `.env`); the app reaches it through an SSH tunnel. The port lockdown is dropped (D43); the pitch names it as an open gap. Docker isn't installed on Oleh's laptop.
- **No IoP** (D40): the production is IRIS classes in `src/hlidac/iris/cls/`, deployed by `python -m hlidac.iris.deploy` (re-run after every change or restart). It writes FHIR only; the app's runner sends emails.
- **The hospital dataset** has almost no structured regimens: doses show up as `MedicationAdministration` or the `Procedure` "APLIKACE PROTINÁDOROVÉ TERAPIE". Dates are shifted per patient (some reach 2027), so set `AS_OF` relative to the demo patients' records.
- **No FHIR Subscriptions:** the production polls the `Hlidac.outbox` table (D7).
- **Community Edition allows 8 connections,** and FHIR REST calls count. Use one shared connection and close cursors.
- **FHIR R4:**
  - we write only `Questionnaire`, `QuestionnaireResponse`, `Observation` (CTCAE grades), `Provenance` and `Task`, each tagged `urn:hlidac:tag|hlidac` and written by a conditional PUT on our own identifier;
  - a CTCAE grade is an `Observation` that goes `preliminary` → `final` on clinician confirmation;
  - IRIS validates each write; a rejected write leaves the outbox row `failed`, visible in Unresolved.
- **CTCAE:** v6.0 (NCI, July 2025) is current; UKONS v3 (January 2025) grades on CTCAE v5. Which version to use is open (D13).
- **The browser microphone needs HTTPS or `localhost`.** The demo runs on the laptop at `localhost` (D17). A phone on `http://<laptop-ip>` can't record, and phone HTTPS is out of scope (outward-facing, so ask first).
- **Pseudonymise** patient text in logs shown outside the audit screen: names, Czech birth numbers (`\d{6}/?\d{3,4}`), addresses, phone numbers.

## 6. Demo (build toward this; full script in `architecture/roadmap.md`)

The three scenarios from the spec, on real patient context from IRIS (pseudonymous labels, never real names), each showing an accessibility mode:
1. **Mild report with VoiceOver on:** approved, plain-language advice; no interruption.
2. **Concerning report at 200 % text:** fever on day 10 after a dose → the urgent screen at once; the alert in the clinician app and the demo mailbox. The nurse confirms the provisional CTCAE grade; unacknowledged → fallback.
3. **Ambiguous report, by voice** (WP17; checked with a recorded file, the live microphone still to try; text is the fallback): one targeted question instead of guessing.

Then the **IRIS portal**:
- the production's Visual Trace of the outbox rows (FHIR write-back);
- the FHIR resources written back, with a CTCAE grade going `preliminary` → `final`;
- `Provenance`;
- the SQL tables: the replayable assessment and the audit row with exactly what the model saw.

Close with security (records never left IRIS, the model saw only pseudonymised text; access-control, CSRF and login tests; the audit trail), the test suite and the WCAG checks. Evaluation numbers only if WP21 gets done.

## 7. How to work with us

- **Pick a work package or spike from `architecture/roadmap.md`;** stay inside its scope and update its status.
  - Follow the final scope (D43, top of this file): the must-do list first, extras only after it.
  - Without IRIS, build on `InMemoryStore` and `StaticContextSource` (`src/hlidac/store.py`) with hand-made fixtures.
  - Don't start P1 work while P0 in your track is open.
- **Contracts first:** `src/hlidac/schemas/` is shared by every track. Change a schema only together with its tests, and tell the team.
- **Build the demo path end to end first, then deepen.** Prefer simple, readable code; no speculative frameworks; server-rendered HTML for the UI.
- **Read the kit's notebooks before writing IRIS or FHIR code;** reuse their connection and query patterns. Don't guess connection details.
- **Tests protect the principles** (§3). Model-dependent tests use recorded model outputs, not a live model. UI changes pass `accessibility.md` §7.
- **Record decisions** in `architecture/system-design.md` §11 (date and owner). When something about IRIS is uncertain, check the docs, fall back to the simplest working option, and note it there.
- **Ask before:**
  - big architectural choices;
  - anything that weakens §3;
  - anything outward-facing: a public tunnel or HTTPS exposure for phones, real SMTP sending, deployment, or pushes to `../VI-KA/`;
  - any IRIS wipe or reset beyond our tagged data, and any export of patient data.

  Flag anything that takes much longer than planned so we can cut scope ("Final scope" in the roadmap).
- **Code and the final delivery live in** `git@github.com:nazarskiy/rakhaton.git` (`origin`, private; D24). The event org `hackathon-green-snail-77` (where `../VI-KA/` lives) is visible to every participant and deleted on 19 Oct; nothing goes there without the user's explicit OK.
- **Secrets only in `.env`;** never commit them.

## 8. Open questions

The lists for FN Motol, the InterSystems mentors and the organisers are at the end of `architecture/roadmap.md`. The biggest open items:
- the dataset's terms of use (D23);
- the CTCAE version and grade-mapping approval (D13);
- an urgent route for patients who can't call (D19);
- the patient group and thresholds (D9);
- the Azure OpenAI region and data-retention terms (D5);
- ~~saving the full chat history~~ of every check-in: done (D46, `Checkin.history`, on the case audit trail).
