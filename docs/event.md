# Rakathon 2026 — event facts

Facts that apply to every challenge. Sources are listed in `sources.md`; "CZ site" means rakathon.cz as of 2026-10-16.

## Basics

- **What:** "Největší AI hackathon proti rakovině na světě" ("the world's largest AI hackathon against cancer"). The CZ site expects 500+ participants.
- **When:** 16–18 October 2026.
- **Where:** run simultaneously in three cities.

  | City | Host hospital | Academic partners |
  |---|---|---|
  | **Praha** (our city) | FN Motol a Homolka | 2. LF UK, ČVUT |
  | Brno | Masarykův onkologický ústav (MOU) | MUNI MED, VUT |
  | Ostrava | FN Ostrava | VŠB-TUO |

- **Organiser:** Insane Business Ideas s.r.o.
  - Contacts: info@rakathon.cz (CZ site) and info@ceehacks.com (team notes).
  - Main partner: VZP.
  - Funded by the City of Prague.
  - Under the auspices of the Prime Minister, the Minister of Health and the Government AI Envoy.

## Schedule

Times come from the CZ site, which is the newer source. The English site still shows an older schedule: hacking from 12:00, submission **Sat 12:00**, local pitching 13:00, and the local TOP 3 per city advancing. **The team works to Sat 13:00 (decided Fri 16 Oct), with the build frozen at 11:00.**

| When | What |
|---|---|
| Thu 15 Oct | Online session where experts present tools and data sources (already happened) |
| Fri 16 Oct, 9:30 | Opening in all cities: registration, challenge presentations, meeting the mentors |
| Fri 16 Oct, 14:00 | Hacking starts and runs into the next day |
| **Fri 16 Oct, 19:00** | **Deadline for choosing a challenge** on https://submit.rakathon.cz/vyzvy. The choice can be changed until then. |
| **Sat 17 Oct, 13:00** | **Project submission deadline** |
| Sat 17 Oct, 14:00 | Local judging in each city |
| Sat 17 Oct, 17:00 | Local challenge winners announced; they advance to Sunday's final |
| Sun 18 Oct, 14:00 | National final in Prague (Kampus Plzeňská, 2. LF UK). The best teams from all three cities pitch again to the main jury. |
| Sun 18 Oct, 17:00 | Overall winners announced |

**What this means for us:** the build has to be finished by Saturday 11:00 (freeze) for the 13:00 submission. Sunday is only for pitching, if we reach the final.

## Registering our challenge

- **Our status: registered.** Our team, **VI-KA**, chose `praha-03` on Fri 16 Oct, ~15:40, and the registration is confirmed. At 15:45 the board showed 2 Prague teams on `praha-03`: us and one direct rival.
- **Where:**
  - the captain submits one form per team at https://submit.rakathon.cz/vyzvy/vybrat;
  - the live board is at https://submit.rakathon.cz/vyzvy.
- **Rules:**
  - teams have 2–4 people, captain included;
  - the choice **can be changed until Fri 19:00**, using the link sent after submitting.
- **The board** shows how many teams have each challenge. Red challenges have no team yet, and the organisers say those "need one most right now".
- **Not stated anywhere:**
  - whether each challenge's local winner advances to the final, or only the city's top 3;
  - whether a team may take several challenges.

## Prizes

| Prize | Award | Who can win it |
|---|---|---|
| 1st / 2nd / 3rd place | 100,000 / 50,000 / 25,000 CZK | Overall winners chosen by the main jury at the Sunday final |
| **InterSystems** | **€1,500**, awarded at the Sunday final | **One team nationally**, on any challenge (see `intersystems-prize.md`) |
| MEDDI ONCO Award | A possible product workshop with MEDDI and proof-of-concept talks | The team with the greatest practical potential according to MEDDI. It's unclear whether this is limited to MEDDI's own challenge. |
| Asseco CE | Mentoring from Asseco (IT and business), plus access to healthcare contacts and resources | The most innovative solution with real-world potential. It's unclear whether this is limited to Asseco's own challenge. |
| Caelestinus | A wild card into the incubator's 2027 Batch (top 25 startups worldwide) | One selected team |
| Hackers' prize | A PitchPerfect.cz pitching masterclass, worth 30,000 CZK | Voted by the participants |

## InterSystems prize judging

These come from the InterSystems page; the full detail is in `intersystems-prize.md`.

- **The four criteria:** innovation, real-world impact, use of InterSystems technology, technical coherence.
- **Judges also look for:**
  - a clear healthcare problem;
  - smart use of AI and/or FHIR;
  - a sound data model and architecture;
  - a realistic path to an MVP or startup;
  - a clearly explained impact.

## What a judge told the team (applies to every challenge)

This is a team note (see `../CLAUDE.md` §2), not an official rule.

- **Must-haves:**
  - data minimisation: patient data isn't fetched or sent unless it's needed at that moment;
  - full data governance, with every data source known;
  - a human in the loop, because the judges distrust AI;
  - traceability.
- **Pluses:**
  - AI outputs that are as deterministic as possible;
  - model flexibility, so the system can run on a local model or a cloud API.

## Data and infrastructure

- **Hospital challenges:** "Participating hospitals will supply datasets for their own challenges." These load into the IRIS kit (per the InterSystems page). Ask the FN Motol mentors whether `praha-03` comes with data.
- **Our data:** the team uses the real hospital data in IRIS, not synthetic data (team decision D23; rules in `../architecture/feature-description.md` §14). Synthea and the kit's synthetic-data guide exist but aren't used.
- **InterSystems kit:** https://github.com/intersystems-community/FHIR-AI-Hackathon-Kit, which runs IRIS for Health with FHIR in Docker.
  - The tutorials cover CSV/SQL, FHIR, synthetic data, vector search, agents and MCP.
  - Kit V2 dropped local Ollama models as too slow (team notes).
- **Azure OpenAI from the organisers:** available to team captains from Fri 13:00, with a login separate from Rakathon's. Martin Lahučký coordinates it (team notes).
  - The team uses it for text extraction on pseudonymised patient messages (D5, revised on an InterSystems mentor's advice); voice stays local.
- **Event GitHub org `hackathon-green-snail-77`:** it provides the Copilot licence. Every participant can see its contents, and it's deleted on 19 Oct (team notes).
