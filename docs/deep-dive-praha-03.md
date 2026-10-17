# Deep dive: `praha-03` Digitální hlídač nežádoucích účinků (chosen)

- **Status:** the team chose this challenge on Fri 16 Oct 2026, ~15:40. The build brief is `../CLAUDE.md`; this file holds the evidence, the medical background, the innovation plan and the pitch.
- **Product design moved on (Fri ~17:30):** the team's architecture is an accessible, mobile-first web app for patients with disabilities (our own secure app, Czech only; text, voice, buttons; no messenger, no images; real hospital data in IRIS; per the mentors and the team) with LLM extraction on pseudonymised text (the team's Azure OpenAI deployment in the demo, a local model by one setting; voice transcribed locally), provisional CTCAE grades confirmed by clinicians, in-app and email notifications, and deterministic rules: `../architecture/feature-description.md` and `../architecture/system-design.md`. The evidence, I1–I8 rationale and safety points here still apply; where this file describes a fixed web questionnaire, the architecture docs take precedence.
- **Main goal:** the InterSystems prize. ePRO monitoring is established practice; sections 4 and 5 explain how context-aware triage, closed-loop follow-up and deep IRIS integration differentiate our solution.
- **Sources:** a research pass on Fri 16 Oct (URLs in section 11). Lines marked **UNVERIFIED** rest on a snippet or weren't confirmed. **Every threshold and every piece of advice text needs sign-off from the FN Motol mentors** — nobody on the team has medical education.

## 1. The brief, and what it leaves open

Public summary (`praha-03-brief.md`):
- regularly send the patient a **short side-effect questionnaire**;
- **advise** them how to cope;
- **alert the care team on its own** when answers reach warning levels.

Not stated: the questionnaire, the patient group, the channel (app, SMS, web), who receives alerts, the MVP and the evaluation criteria. These are the first questions for Motol (section 9).

## 2. The evidence (what we can say in the pitch)

| Claim | Numbers | Source |
|---|---|---|
| **ePRO monitoring cut ER visits and improved quality of life** (STAR, MSKCC, single-centre RCT, n=766) | ER visits at 1 year 34 % vs 41 %; QoL improved in 34 % vs 18 %; hospitalisation 45 % vs 49 % (not significant) | Basch, J Clin Oncol 2016 |
| **…and patients lived longer** (post hoc, 7-year follow-up) | Median OS 31.2 vs 26.0 months | Basch, JAMA 2017 |
| **Lung cancer web follow-up** (Sentinel, later Moovcare) | Median OS 22.5 vs 14.9 months | Denis, JAMA 2019 |
| **The big multi-centre trial found no survival gain** (PRO-TECT, 52 US community practices, n=1,191) | OS HR 0.99; but **fewer ER visits** (HR 0.84), **slower decline** of physical function, symptoms and QoL; 91.5 % survey adherence | Basch, Nature Medicine 2025 |
| **ESMO recommends it** | "Digital symptom monitoring with PROMs in routine clinical care during systemic cancer treatment is recommended" [I, A] (quote from a snippet; full text returned 403) | Di Maio, Ann Oncol 2022 |
| **Alert fatigue is a real problem** | In PRO-TECT about **1 in 3 surveys triggered an alert**, and **47 % of nurses said there were "too many"** | PRO-TECT design and implementation paper (PMC7768331) |
| **Alerts weren't watched after hours** | STAR's nurse e-mails were not monitored after hours, and no action protocol was mandated | Basch 2016 |

**How to use it:**
- The brief's own claim ("prodlužuje přežití v řádu měsíců") comes from the single-centre trials. **Don't lead with survival.** PRO-TECT, the largest trial, found none.
- Lead with **fewer emergency visits and better quality of life**, which every trial supports.
- Showing the judges we know PRO-TECT's null survival result is itself a credibility point. Clinicians on the jury may know it.
- **The problem we fix is not "nobody does ePRO".** It's that ePRO done naively floods nurses with alerts, treats every patient the same and leaves alerts unanswered. That's sourced (the last two rows) and it's our innovation story.

## 3. The questionnaire

- **The standard is NCI PRO-CTCAE:**
  - 124 items covering 78 symptomatic adverse events;
  - each item rates one attribute (frequency, severity, interference, amount, or presence/absence), 1–3 attributes per adverse event;
  - scored 0–4, or 0/1 for presence; recall period is the past 7 days.
  - Severity labels: None, Mild, Moderate, Severe, Very severe. Frequency: Never … Almost constantly. Interference: Not at all … Very much.
- **An official, linguistically validated Czech version exists** (NCI certificate). We don't have its text; it has to be requested from NCI.
- **Terms of use** (NCI):
  - free, registration expected;
  - wording may not be changed;
  - may not be built into products for sale;
  - stated scope: "not intended for diagnostic, prognostic or therapeutic purposes". Whether an alerting tool fits that scope is **UNVERIFIED** (ask NCI or Motol).
- **Composite grading** (Basch, Clin Trials 2021): maps the 1–3 attribute scores of one symptom to a single 0–3 grade. All 187 combinations were mapped by clinicians and validated.
- **No LOINC codes or official FHIR Questionnaire for PRO-CTCAE were found.** We use our own code system (`urn:…/symptom`) and say so; mapping to a standard terminology is future work.
- **Which symptoms:** PRO-TECT used 8 (pain, nausea, vomiting, constipation, diarrhoea, dyspnoea, insomnia, depression), plus patient-rated ECOG, falls and financial toxicity. ESMO's core list is the same 8 plus physical function. STAR used 12, including fatigue, cough, neuropathy, appetite loss and fever-adjacent items.

**For the demo** (now the plan's required fields and approved question bank, asked conversationally; see `../architecture/system-design.md` §5): a **short Czech questionnaire we write ourselves**, using PRO-CTCAE's structure (7-day recall, 0–4 scales) but not its wording, clearly labelled "inspired by PRO-CTCAE". The pitch says production would license the official Czech PRO-CTCAE. If Motol already uses the Czech PRO-CTCAE, use theirs.

## 4. The innovation plan

Each item has a stated source or is a mechanism with rules supplied by clinicians. Numbered **I1–I8** for the build brief.

| # | Feature | Why it matters | Evidence or source |
|---|---|---|---|
| **I1** | **Context-aware triage.** Rules read the patient's FHIR record (regimen, date of last dose, latest neutrophils), not only the answers. Example: fever within 6–8 weeks of systemic anti-cancer therapy → red, neutropenic-sepsis pathway. | The same answer means different things on day 2 and day 10 after chemo. | UKONS v3 fever rule; neutrophil nadir 7–14 days (Cancer Research UK) |
| **I2** | **Fewer, better alerts.** Red / amber / green with UKONS's combination rule (2+ amber → red). A persisting symptom at the same grade, already handled by a nurse this week, isn't re-alerted (mechanism; needs Motol's agreement). Show the alert rate vs a naive PRO-TECT-style rule on the synthetic data, with zero missed red flags. | Alert fatigue: 1 in 3 surveys alerted; 47 % of nurses said too many. | PRO-TECT; UKONS v3 |
| **I3** | **Closed loop with deadlines.** Every alert becomes a FHIR `Task` with a deadline (red: immediately; amber: within 24 h per UKONS). Not acknowledged in time → escalates to the on-call doctor. The call is recorded as a `Communication`. Time-to-response is on the dashboard. | STAR's alert e-mails weren't monitored after hours. | Basch 2016; UKONS |
| **I4** | **Silence is a signal.** No answer → reminder at 24 h, nurse call at 72 h. | PRO-TECT used exactly this protocol and reached 91.5 % adherence. | PRO-TECT |
| **I5** | **Rules owned by clinicians.** Thresholds are versioned data, editable in the IRIS portal; every alert can be replayed from (answers, rule version). | Hospitals differ; Motol must be able to set its own thresholds. | — |
| **I6** | **Plugs into the hospital.** Standard FHIR `Questionnaire`/`QuestionnaireResponse`, so any FHIR-capable patient app can render it (e.g. an existing hospital app). Stretch: treatment events from the hospital system arrive as HL7v2 and are converted to FHIR. | No new data silo. | — |
| **I7** | **Free text, safely.** "Anything else?" is matched to symptom terms with **local** embeddings and IRIS vector search. A deterministic Czech red-flag phrase list escalates. Free text can only raise urgency, never lower it; a nurse confirms every match. | Patients describe symptoms in their own words. | — |
| **I8** | **Pre-visit summary for the doctor.** Each symptom's trend across treatment cycles, computed deterministically from the stored observations. | The doctor sees weeks of symptoms in one screen at the check-up. | — |

**Minimum for the innovation story:** I1, I2, I3 and I4. They are cheap (rules plus a production), they are sourced, and they speak directly to the known failure modes. I7 and the HL7v2 part of I6 are stretch goals.

## 5. Where InterSystems IRIS goes

The current mapping of every component to IRIS is in `../architecture/system-design.md` §2–§4, and the build order is in `../architecture/roadmap.md`. Setup, sources and capability caveats are in `intersystems-toolkit.md`.

| Need | IRIS technology |
|---|---|
| Patients, treatments, labs, questionnaires, answers, symptom grades | **FHIR R4 server**: `Patient`, `MedicationAdministration`/`MedicationRequest`, `Observation`, `Questionnaire`, `QuestionnaireResponse` |
| Schedule, reminders, scoring, alert routing, escalation timers | **Interoperability production** built in Python with IoP: a scheduler service, a scoring process, an alert operation, an escalation timer. Its message trace in the portal is the demo's InterSystems moment. |
| Thresholds editable by clinicians | **Versioned IRIS SQL rule table** first; native **business rules** as a stretch if IoP can call them |
| Alerts, follow-up, traceability | **FHIR write-back**: `Flag`, `Task` (with deadline), `Communication`, `Provenance` (answers + rule version → alert), `AuditEvent` |
| Dashboards and trends | **FHIR SQL Builder** projections queried with **IRIS SQL** |
| Free-text matching | **Vector search** with local embeddings (stretch, I7) |
| Assistant access ("who needs a call today?") | **MCP** (stretch) |
| Treatment events from the hospital | **HL7v2 → FHIR** (stretch, I6) |

## 6. How deep we need to go medically

**Level we need:** understand the vocabulary below and apply clinician-supplied rules. **Level we must not pretend to:** deciding thresholds or writing advice ourselves.

| Term | Plain meaning |
|---|---|
| Adverse event / nežádoucí účinek | A harmful side effect of treatment |
| CTCAE | The NCI scale doctors use to grade side effects, from 1 (mild) to 5 (death) |
| PRO / ePRO | Patient-reported outcome: the patient rates their own symptoms; "e" = electronically |
| PRO-CTCAE | The patient-facing version of CTCAE: questions the patient answers, scored 0–4 |
| SACT | Systemic anti-cancer therapy: chemotherapy, immunotherapy, targeted drugs |
| Cycle | One round of treatment, e.g. a drug every 21 days |
| Nadir | The lowest point of white blood cells after chemo, usually 7–14 days after a dose |
| Neutrophils / ANC | The white blood cells that fight bacteria; ANC = absolute neutrophil count |
| Febrile neutropenia | Fever with very low neutrophils. An emergency: infection can turn into sepsis quickly. |
| Triage / RAG | Sorting by urgency: red (now), amber (within 24 h), green (self-care) |
| ECOG | A 0–5 score of how much the patient can do day to day (0 = fully active) |
| irAE | Immune-related adverse event: side effects of immunotherapy, e.g. colitis or pneumonitis |

**Fever thresholds differ by source**, which shows why Motol must choose:
- UKONS v3 triage: temperature **≥ 37.5 °C or < 36.0 °C** within 6–8 weeks of SACT → urgent review.
- ESMO 2016 definition of febrile neutropenia: **> 38.3 °C once, or > 38.0 °C twice over 2 h**, plus ANC < 0.5 × 10⁹/L.
- Demo default: UKONS, because it's a triage tool and the lower threshold is the safer side. Say so on screen.

**Advice text** must come from published Czech patient material, quoted and cited, and be marked "demo — requires Motol approval":
- MOÚ: brochure "Nežádoucí účinky chemoterapie a jak je zvládnout"; web pages on the digestive tract, nervous system, skin, immunotherapy;
- Linkos.cz (Česká onkologická společnost): chemotherapy side effects by symptom (about 8 years old);
- Liga proti rakovině Praha: brochure "Chemoterapie nádorových onemocnění".

Reuse permission for these texts is **UNVERIFIED**. For the demo, short paraphrases with a source link are safer than copying.

**Red-answer text** always tells the patient to call the oncology line now, or **155** (emergency medical service) if severe. The app is not an emergency service, and the screen says so.

## 7. Competition and regulation

- **Products already doing ePRO in Europe:** Noona (Siemens Healthineers, via Varian), Elekta Kaiku (Kaiku Health), Resilience PRO (France, CE class IIa, PRO-CTCAE-based), Cureety (France), Moovcare (Sivan; France dropped reimbursement in Feb 2025).
- **In Czechia:** MOÚ's "Mou Meddi" app (2021, built with MEDDI hub) has smart questionnaires on chemotherapy effects that doctors see as a chart, and is explicitly not for acute problems. No FN Motol system was found.
- **Our angle against them:** we don't compete as yet another app. We're the **integration and triage layer inside the hospital's own data platform**: standard FHIR, rules the hospital controls, closed-loop follow-up, any app as the front end.
- **Regulation:** software that informs diagnostic or therapeutic decisions is a medical device, class IIa or higher under EU MDR Rule 11 (supervised in Czechia by SÚKL). A Commission proposal of Dec 2025 would relax Rule 11, but it isn't adopted. Startup path: CE marking as class IIa; the deterministic, versioned, audited design makes that easier. The exact class depends on the intended-purpose wording (needs a regulatory expert).

## 8. Pitch

**One-liner (Czech, for the jury):** *„Hlídač pozná zhoršení mezi kontrolami, poradí pacientovi schváleným textem a sestře pošle jen to, co opravdu musí řešit — s termínem, eskalací a plnou dohledatelností, nad standardem FHIR v InterSystems IRIS."*

English: it catches deterioration between check-ups, gives the patient approved advice, and sends the nurse only what needs action — with a deadline, escalation and a full audit trail, on FHIR in InterSystems IRIS.

**4-minute structure:**
1. **Problem (30 s):** side effects show up weeks before the check-up. Monitoring works (fewer ER visits, better QoL), but done naively it floods nurses: 1 in 3 surveys alerted in PRO-TECT, and half the nurses said too many.
2. **Patient (45 s):** in the accessible web app (screen reader, voice or large buttons), the patient writes or speaks in their own words; mild nausea → approved advice; an ambiguous report → one targeted question; a voice note with fever on day 10 after chemo → urgent instructions at once.
3. **Nurse (60 s):** the work queue shows only urgent and routine-review cases, each with why (the patient's quote, rule and version, treatment context) and an acknowledgement deadline. An unacknowledged urgent alert falls back live.
4. **InterSystems (60 s):** the production's message trace in the portal, the rule a clinician can edit, the FHIR resources written back (`Flag`, `Task`, `Provenance`, `AuditEvent`), and the SQL projection behind the dashboard.
5. **Impact and path (45 s):** the alert-burden number on the team's labelled test reports, zero missed red flags, MDR class IIa path, and integration with any FHIR app.

**Q&A to prepare:**
- "Isn't this Noona/Kaiku?" → section 7: we're the hospital-side triage and integration layer; the front end can be anyone's.
- "Survival?" → the honest PRO-TECT answer from section 2.
- "Who wrote the thresholds?" → published sources (UKONS, STAR, PRO-TECT), editable and versioned; Motol decides.
- "What if the patient doesn't answer?" → I4.
- "Is patient data sent to an AI?" → only the patient's own message text, pseudonymised, to the team's Azure OpenAI deployment (one setting switches to a local model); never records, and voice is transcribed locally.

**Claims to avoid:**
- "Extends survival" (contested).
- "Reduces alerts by X %" as a real-world claim (we only have the team's test reports, not a clinical study).
- "PRO-CTCAE" for our own questions (its wording can't be changed).
- Anything that sounds like diagnosis or treatment advice.

## 9. Questions to ask today

**FN Motol (challenge owner):**
1. Is there a full brief with an MVP and evaluation criteria?
2. Which patients: one cancer or treatment type (e.g. chemotherapy, immunotherapy)?
3. Do you use PRO-CTCAE (the official Czech version) or another questionnaire?
4. Who receives alerts, by which channel, and how fast must red and amber be handled?
5. Which thresholds do you use today? Is the UKONS triage tool or something similar in use?
6. Which patient-education materials can we cite for advice text?
7. Is there an existing patient app or portal we should plug into?

**InterSystems mentors:**
1. Can an IoP (Python) production call the IRIS business-rule engine, or should rules live in our own table?
2. The best way to make a FHIR write (a new `QuestionnaireResponse`) trigger the production.
3. Is the prize judged across all teams or only Sunday's finalists?
4. Is HL7v2 → FHIR conversion usable in the Community Edition image?

**Organisers:** can we keep working after Sat 13:00 for the Sunday final, or is the submission frozen?

## 10. Risks

| Risk | Mitigation |
|---|---|
| Scope: accessible web app, voice, LLM extraction, CTCAE grading, notifications, nurse queue | Timeboxed spikes first, text and buttons before voice, WCAG AA baseline never cut; cut order in `../architecture/roadmap.md`. |
| Seen as "just another ePRO app" | Lead with I1–I4 and the alert-fatigue evidence; show the production trace live. |
| Medical-advice safety | Approved, cited text only; "call now" for red; on-screen disclaimer; no generated advice. |
| Thresholds wrong | Sourced defaults, labelled as such, confirmed by Motol; editable rules. |
| Existing ePRO products and other teams on `praha-03` | Differentiate on integration, closed loop and the InterSystems depth. |
| IRIS setup time (Docker not on Oleh's laptop) | One machine runs IRIS for the team; set it up first. |

## 11. Sources

- Basch 2016, J Clin Oncol (STAR): https://ascopubs.org/doi/10.1200/JCO.2015.63.0830 · https://pmc.ncbi.nlm.nih.gov/articles/PMC4872028/
- Basch 2017, JAMA (OS): https://pmc.ncbi.nlm.nih.gov/articles/PMC5817466/
- PRO-TECT design and alert rules: https://pmc.ncbi.nlm.nih.gov/articles/PMC7768331/
- PRO-TECT 2022, JAMA: https://jamanetwork.com/journals/jama/fullarticle/2793279
- PRO-TECT 2025, Nature Medicine: https://www.nature.com/articles/s41591-025-03507-y
- Denis 2017, JNCI: https://pubmed.ncbi.nlm.nih.gov/28423407/ · Denis 2019, JAMA: https://jamanetwork.com/journals/jama/fullarticle/2721170
- ESMO PROM guideline 2022: https://www.annalsofoncology.org/article/S0923-7534(22)00691-3/fulltext
- PRO-CTCAE overview: https://healthcaredelivery.cancer.gov/pro-ctcae/overview.html · terms of use: https://healthcaredelivery.cancer.gov/pro-ctcae/terms_of_use.html · Czech certificate: https://healthcaredelivery.cancer.gov/pro-ctcae/certificates/pro-ctcae/pro-ctcae_cert_czech.pdf
- PRO-CTCAE composite grading: https://pmc.ncbi.nlm.nih.gov/articles/PMC7878323/
- UKONS 24-hour triage v3: https://www.ukacuteoncology.co.uk/information-hub/ao-guidelines/ukons-guidelines/ukons-24-hour-triage
- ESMO febrile neutropenia 2016: https://www.annalsofoncology.org/article/S0923-7534(19)31643-6/fulltext
- Nadir timing: https://www.cancerresearchuk.org/about-cancer/coping/physically/fever/causes/during-or-after-treatment
- Mou Meddi (MOÚ): https://www.zdravezpravy.cz/2021/07/26/v-masarykove-onkologickem-ustavu-maji-chytrou-aplikaci/
- EU MDR: https://eur-lex.europa.eu/eli/reg/2017/745/oj · MDCG guidance: https://health.ec.europa.eu/medical-devices-sector/new-regulations/guidance-mdcg-endorsed-documents-and-other-guidance_en
- Czech patient material: https://www.mou.cz/document/file/3805?dl=0 · https://www.linkos.cz/pacient-a-rodina/pece-o-pacienta/nezadouci-ucinky-lecby-chemo/nezadouci-ucinky-chemoterapie-1/ · https://www.lpr.cz/prevence-a-lecba/edukacni-a-poradenske-publikace
