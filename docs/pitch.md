# Pitch: Oncology Symptom Companion (praha-03 + InterSystems prize)

Working outline for Saturday's local judging (14:00) and Sunday's final. Evidence and sources:
`deep-dive-praha-03.md` §2, §7, §8. Demo steps: `../README.md`, "Running the demo". Every number
here has a source; don't add claims that aren't in this file.

## One-liner

> **CZ:** „Hlídač pozná zhoršení mezi kontrolami, poradí pacientovi schváleným textem a sestře pošle
> jen to, co opravdu musí řešit — s termínem, eskalací a plnou dohledatelností, nad standardem FHIR
> v InterSystems IRIS."

**EN:** it catches deterioration between check-ups, gives the patient approved advice, and sends the
nurse only what needs action, with a deadline, escalation and a full audit trail, on FHIR in
InterSystems IRIS.

## Structure (about 4 minutes)

| Time | Part | Say | Show |
|---|---|---|---|
| 0:00–0:30 | **Problem** | Side effects show up weeks before the next visit. Symptom monitoring cuts ER visits and improves quality of life (STAR; PRO-TECT, n=1,191). Done naively it floods nurses: **1 in 3 surveys alerted, 47 % of nurses said "too many"** (PRO-TECT). And digital tools often shut out patients with disabilities. | Title slide |
| 0:30–1:30 | **Patient** | Our own accessible Czech web app, no messenger, no third parties. Text, voice or large buttons; one question per screen; "Nevím" everywhere; no time limits. | Scenario 1 with VoiceOver (advice card with its source). Scenario 3: one sentence in, only the missing questions asked, red flags first. |
| 1:30–2:30 | **Nurse** | The queue shows only what needs action, each with *why*: the patient's quote, the rule and its version, days since the dose **read from the hospital's data in IRIS**, a provisional CTCAE grade that the nurse confirms. Untaken alerts escalate. | Scenario 2 at 200 % text: urgent screen at once → alert in the queue and in the mailbox → confirm the grade → move the clock → fallback contact notified. |
| 2:30–3:30 | **InterSystems** | IRIS is the whole data layer: the hospital's FHIR data, our report written back as standard FHIR next to it, our SQL tables, and the production that delivers every write. | Portal: Visual Trace of `Hlidac.Production`; the `QuestionnaireResponse` next to the hospital's record; grade `Observation` `preliminary` → `final`; `Task`, `Provenance`; `Hlidac.*` tables. App: audit trail with exactly what the model saw, replay "Shoduje se". |
| 3:30–4:00 | **Trust and path** | The LLM only reads; deterministic, versioned rules decide; a person handles every alert. Records never leave IRIS; the model sees only the pseudonymised message. Security and accessibility proven by tests. Path: CE class IIa (MDR Rule 11), any FHIR hospital. | Architecture slide; the test run. |

## Architecture slide (one picture)

Every box names the IRIS feature it uses:

```
Patient (phone / laptop, screen reader, voice)        Nurse (queue, case, audit)
                 │                                             │
                 ▼                                             ▼
   FastAPI app: pages, login, CSRF · pure rules engine · CTCAE grading · model gateway
     │ text, pseudonymised ─► Azure OpenAI (EU) │ or local model (one setting)
     │ voice ─► Whisper inside the app (audio never leaves the laptop)
     │
     ├─ IRIS SQL  (Hlidac.*: check-ins, assessments, alerts, outbox, audit)
     ├─ IRIS FHIR R4 (read): the hospital's MedicationAdministration / Procedure → days since dose
     └─ outbox ─► IRIS Interoperability: Hlidac.Production (re-checks every bundle)
                    └─► IRIS FHIR R4 (write, tagged): QuestionnaireResponse, Observation
                         (CTCAE grade, preliminary → final), Task, Provenance
```

## InterSystems criteria → what we show

| Criterion | Evidence |
|---|---|
| Innovation | Accessibility-first ePRO on a standard FHIR `Questionnaire`; the LLM reads and the rules decide; provisional CTCAE grades with evidence, confirmed by a clinician; only missing questions are asked |
| Real-world impact | Fewer ER visits and better QoL (STAR, PRO-TECT); fewer, explained alerts; includes patients other tools exclude; runs on the hospital's own data |
| Use of InterSystems | FHIR R4 repository (read hospital data + write standard resources), IRIS SQL for every app table, an interoperability production as the only FHIR writer, Management Portal trace |
| Technical coherence | One app decides, one production delivers, one outbox contract between them; strict schemas; replayable, versioned assessments; audit end to end |

## Numbers we can say

- Our project: about 780 automated tests, including rules, red flags, "uncertainty never lowers urgency", access control ("patient A can't read B"), CSRF, the model gateway's endpoint and pseudonymisation guard, and an axe WCAG 2.2 AA run over the patient and nurse pages.
- **All three scenarios ran end to end on IRIS** (Sat 17 Oct): production 6/6 FHIR writes, 9/9 emails, 0 errors; grade `preliminary` → `final`; fallback after +1 h.
- Evidence numbers: only those in `deep-dive-praha-03.md` §2, with their source.

## Q&A

- **"Isn't this Noona / Kaiku?"** We are the hospital-side triage and integration layer on its own data platform: standard FHIR, rules the hospital controls, closed-loop follow-up. The front end can be anyone's.
- **"Does it extend survival?"** Honestly: the single-centre trials said yes; PRO-TECT, the largest, found no survival gain but fewer ER visits and slower decline. We lead with those.
- **"Is patient data sent to an AI?"** Only the patient's own message, pseudonymised, to the EU Azure OpenAI deployment; one setting switches to a local model. Never records. Voice is transcribed on the laptop. The audit screen shows exactly what was sent.
- **"Who set the thresholds?"** Published sources (UKONS v3, CTCAE v5.0), versioned, each with a `source_ref` and labelled "DEMO — čeká na schválení FN Motol". Motol decides.
- **"What if the patient doesn't answer / says Nevím?"** Missing or unknown never counts as "no problem": it ends as "insufficient information" for the nurse, never as no escalation.
- **"What if the AI is wrong?"** It only extracts quotes; a quote not found verbatim in the message is dropped. Red flags are scanned deterministically and asked with buttons; the rules decide; the nurse confirms every grade.
- **"Regulation?"** Software informing clinical decisions is a medical device, likely class IIa under MDR Rule 11 (SÚKL). The deterministic, versioned, audited design is built for that path.

## Say honestly (known gaps)

- Not clinically validated; nobody on the team has medical training; every rule and text awaits Motol's approval.
- Demo security scope: no strict security headers (WP31 dropped), IRIS ports not locked down and the default admin password unchanged on the demo VM (S1 dropped). Never claim these.
- Voice is shown only on the demo Mac (Apple Silicon); phones need HTTPS, which is future work.
- No evaluation numbers (WP21 not done): don't claim "X % fewer alerts".

## Claims to avoid

- "Extends survival."
- "Reduces alerts by X %."
- "PRO-CTCAE" for our questions (we wrote our own wording).
- Anything that sounds like diagnosis or treatment advice.

## Backup video (if the live demo fails)

Record before the 11:00 freeze, about 4 minutes, following the table above. Patients appear only as "Pacient 01…"; the browser and portal must not show real names or record data beyond the tagged write-back. Scenes:
1. Scenario 1 with VoiceOver audible.
2. Scenario 3 by text (or voice).
3. Scenario 2 at 200 %: urgent screen → nurse queue → Mailpit → confirm grade → clock → fallback email.
4. Portal: Visual Trace, the FHIR resources, the SQL tables.
5. The audit trail with the model input and the replay check.
