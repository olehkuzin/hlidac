# Praha 3 — Digitální hlídač nežádoucích účinků (Digital side-effect watchdog)

| Field | Value |
|---|---|
| Track | Praha, challenge 3 of 9 |
| Available to our team | **Yes** |
| Owner (zadavatel) | Fakultní nemocnice Motol a Homolka |
| Challenge-specific prize | None listed |
| Brief we have | Public summary only (rakathon.cz `#rv-praha-4`). No full brief. |
| Team | VI-KA |
| Our solution | [`../README.md`](../README.md) |
| Deep dive | `deep-dive-praha-03.md`: evidence with sources, IRIS design, innovation, pitch |

## Brief (verbatim, Czech)

> Pacienti na onkologické léčbě často trpí nežádoucími účinky týdny předtím, než je zachytí lékař při kontrole. Přitom studie ukazují, že pravidelné elektronické hlášení symptomů pacienty prodlužuje přežití v řádu měsíců a snižuje počet hospitalizací. Navrhněte systém, který pacientovi pravidelně pošle krátký dotazník na nežádoucí účinky, poradí mu, jak se s nimi vypořádat, a při varovných hodnotách sám upozorní ošetřující tým.

## Brief (English, our translation)

Patients on cancer treatment often suffer side effects for weeks before a doctor picks them up at a check-up. Yet studies show that regular electronic symptom reporting extends patients' survival by months and reduces hospitalisations. Design a system that regularly sends the patient a short side-effect questionnaire, advises them how to cope, and alerts the care team on its own when values reach warning levels.

## What the brief asks for

- **A recurring short questionnaire** about side effects, sent to the patient.
- **Self-management advice** to the patient, based on the answers.
- **Automatic alerts** to the care team when answers cross warning thresholds.

**Not in the public summary:**
- MVP and evaluation criteria;
- which questionnaire to use;
- the channel (app, SMS, web).

## Analysis *(inference, not from the brief)*

- **Standard:** this is ePRO monitoring.
  - The standard instrument is NCI **PRO-CTCAE**.
  - The survival claim refers to trials such as Basch et al.
- **IRIS fit: high.**
  - FHIR `Questionnaire` and `QuestionnaireResponse`;
  - `Observation` for symptom grades;
  - `Flag` or `DetectedIssue` for alerts, and `Communication` to the care team;
  - an Interoperability production for scheduling and alert routing.
- **Advice to the patient:**
  - should come from **approved, versioned content** keyed to the symptom grade, not free-form LLM text;
  - this keeps it safe and deterministic, which the judges want.
- **Data:** synthetic patients and responses are easy to generate. No real data is needed.
- **Team fit: medium.** The rules and pipeline are easy; the patient-facing UI is most of the work.
- **Demo in one sentence:** a patient fills in a 1-minute questionnaire on a phone, gets approved advice, and a fever during recent treatment triggers a RED nurse Task with a reason, deadline and audit trail (demo rules need Motol approval).
- **Risks:**
  - established ePRO products: differentiate through context-aware triage and closed-loop follow-up inside IRIS;
  - medical-advice safety;
  - UI-heavy.
- **Questions for Motol:**
  - Which symptoms and thresholds matter?
  - Who receives the alerts, and how?
  - Should we use PRO-CTCAE?
  - Is there an existing patient app to integrate with?
