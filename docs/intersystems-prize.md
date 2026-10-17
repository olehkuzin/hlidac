# All cities 4 — Nejlepší využití technologií InterSystems pro AI a interoperabilitu (Best use of InterSystems technology)

| Field | Value |
|---|---|
| Track | Shared by Praha, Brno and Ostrava. **It's an overlay prize, not a standalone challenge:** you enter it alongside any other challenge. |
| Available to our team | **Yes, entered alongside `praha-03`** |
| Owner (zadavatel) | InterSystems |
| Prize | **€1,500 for one team nationally**, awarded at the Sunday final in Prague. **This is the team's priority prize.** |
| Brief we have | The public summary (rakathon.cz `#rv-spolecne-4`) and a paraphrase of the InterSystems challenge page (`sources/intersystems-challenge.webfetch.md`) |
| Team status | Entering it with `praha-03`. This is the main goal. |

## Brief (verbatim, Czech)

> Oceníme tým, který při řešení kterékoli z ostatních výzev Rakathonu nejlépe využije technologie InterSystems k zapojení umělé inteligence, propojení zdravotnických dat a systémů (zejména prostřednictvím standardu HL7® FHIR®) nebo kombinaci obojího. Podrobnosti, dostupné nástroje a návody najdete na stránce výzvy InterSystems.

## Brief (English, our translation)

We will reward the team that, while solving any of the other Rakathon challenges, makes the best use of InterSystems technology to bring in AI, to connect health data and systems (especially through the HL7® FHIR® standard), or a combination of both. Details, the available tools and tutorials are on the InterSystems challenge page.

## From the InterSystems page (paraphrased)

- **Judging criteria:**
  1. Innovation
  2. Real-world impact
  3. Use of InterSystems technology
  4. Technical coherence
- **Judges also want:**
  - a clearly defined healthcare problem;
  - smart use of AI and/or FHIR;
  - a sound data model and architecture;
  - a realistic path to an MVP or startup;
  - a clear explanation of impact.
- **Technologies on offer:**
  - **FHIR server:** FHIRPath, SQL and REST access; converts HL7v2, CDA, DICOM and X12 into FHIR.
  - **Vector search:** vectors are an SQL datatype. The listed uses include RAG, semantic search over notes and PDFs, finding similar cases and meaning-based matching. For us this is the free-text symptom matching (I7).
  - **GenAI, AutoML (IntegratedML) and Interoperability.**
  - **Agents and MCP:** kit tutorials 5.1 and 5.2.
- **Kit:** https://github.com/intersystems-community/FHIR-AI-Hackathon-Kit. The InterSystems page links its V2 branch.
- **Data:**
  - hospitals supply datasets for their own challenges;
  - **VZP hosts its data and IRIS instances in its Azure cloud;**
  - for synthetic data, use Synthea and the kit's guide.
- **Mentors:** InterSystems tech mentors are available 24/7.

## How to score on it *(inference)*

- Make IRIS **central, not decorative**. Each architecture box should map to an IRIS feature: the FHIR repository, SQL over the FHIR data, vector search in the same database, Interoperability productions, Embedded Python and an MCP server.
- Use FHIR as the **data model** (real resources: `Observation`, `DocumentReference`, `Task`, `Provenance`, `AuditEvent` …), not just as an import format.
- For "real-world impact", show a path into a hospital: HL7v2 or FHIR ingestion and local code mapping (e.g. NČLP → LOINC).
- For "technical coherence", show one clean architecture slide and a demo that runs end to end.

## Fit with `praha-03` *(inference)*

- The judges reward **AI, interoperability, or both**. Our design does both, on IRIS: guarded LLM extraction and local voice transcription feed deterministic rules; a patient's report from the accessible web app comes out as standard FHIR R4 resources (a `QuestionnaireResponse`, CTCAE-coded grade observations, `Provenance`, `Task`) next to the hospital's data; and an IRIS Interoperability production delivers every FHIR write and email from our outbox (simplified stack, D28).
- Don't let it read as just a chatbot. Put the weight on the deterministic rules, the FHIR write-back next to the hospital's data and the production's Visual Trace.
- The criterion-by-criterion story and the IRIS mapping are in `../architecture/system-design.md` §2 and §4. The build brief is `../CLAUDE.md`.
