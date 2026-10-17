# InterSystems toolkit for `praha-03`

- **Purpose:** the IRIS pieces we actually use, and how to use them. Trimmed to the simplified stack (D28 in `../architecture/system-design.md`); the InterSystems prize is our main goal, so each piece we keep is visible in the demo.
- **Checked:** Fri 16 Oct 2026. These are documentation findings, **not capabilities we have run and verified** (spike S1 verifies them).
- **Sources:**
  - [FHIR-AI-Hackathon-Kit](https://github.com/intersystems-community/FHIR-AI-Hackathon-Kit), commit `3ebaee2` (2026-10-14), including its environment skill and Docker Compose configuration;
  - the [IoP](https://github.com/grongierisc/interoperability-embedded-python) README;
  - [IRIS for Health documentation](https://docs.intersystems.com/irisforhealthlatest/csp/docbook/DocBook.UI.Page.cls);
  - the InterSystems prize page, recorded in `sources/intersystems-challenge.webfetch.md`.
- **Build brief:** `../CLAUDE.md` §4–§5. Architecture: `../architecture/system-design.md` §3, §7, §8.

## What we use

| Piece | What it does for us | Demo proof |
|---|---|---|
| **FHIR R4 repository** | Holds the real hospital data (read only for us) and our tagged writes: `Questionnaire`, `QuestionnaireResponse`, CTCAE grade `Observation`s, `Provenance`, `Task` | Our `QuestionnaireResponse` next to the hospital's record; a grade going `preliminary` → `final` |
| **IRIS SQL** (schema `Hlidac`) | Our app's data: accounts, login codes, plans, check-ins, assessments, alerts, notifications, the outbox, the audit log (`src/hlidac/iris/schema.sql`) | The replayable assessment and the audit row with exactly what the model saw |
| **One Interoperability production in Python (IoP)** | `Hlidac.Production` polls `Hlidac.outbox`, posts FHIR transaction bundles and sends emails (`smtplib` to Mailpit), with retries and a final `failed` state | The Visual Trace of a check-in's outbox rows |
| **Management Portal** | Visual Trace, FHIR resources, SQL queries | Shown live in the demo |

From the kit we use only the IRIS container (already running on the team's Azure VM) and the code patterns in tutorials 2 (SQL from Python) and 3.1–3.4 (FHIR reads and writes, transaction bundles, `OperationOutcome` handling). The kit's notebooks default to OpenAI for embeddings and agents; we don't use those paths (D5).

## Connection facts

From the kit's `skills/iris-environment/SKILL.md` and `docker-compose.yml`. Put actual settings in `.env`; never hardcode credentials or endpoints. The server is on Azure, so the app reaches it through an SSH tunnel and the host below stays `localhost`.

| Thing | Kit default |
|---|---|
| Container | `iris-fhir` (Compose service `iris`); image `intersystems/irishealth-community:latest-cd` |
| SQL / Python driver (`intersystems-irispython`) | `localhost:1972`, namespace **`FHIRSERVER`** |
| FHIR base URL | `http://localhost:52773/fhir/r4`; capability statement at `/metadata` |
| Management Portal | `http://localhost:52773/csp/sys/UtilHome.csp` |
| Login (development default only; change it on the server) | `SuperUser` / `SYS` |
| FHIR storage | JSON strategy `HS.FHIRServer.Storage.Json.InteractionsStrategy`, R4 `hl7.fhir.r4.core@4.0.1` |
| Persistence | Named volume `iris-data` |

- Re-running `start.sh` on the same volume **duplicates patients**.
- `docker compose -f docker-iris-fhir/docker-compose.yml down -v` **deletes the real hospital dataset**: never run it, or any IRIS wipe, without the team's explicit approval. Our own writes are removed with `scripts/reset_demo.sh` (tagged resources and our SQL rows only).
- On Fri evening the server's portal answered from outside but `/fhir/r4/metadata` returned 404: spike S1 checks where the kit's FHIR endpoint is.

## IoP notes (unverified until spike S1)

- A `Production` object in `settings.py` is registered via `PRODUCTIONS = [prod]`.
- Components: `PollingBusinessService.on_poll` (our outbox poller), `BusinessProcess.on_request` / `on_response`, `BusinessOperation.on_message` (the FHIR writer and the email sender).
- Use `on_init`, not `__init__`; messages are dataclasses; IoP messages show in the Message Viewer and Visual Trace as JSON. Restart the production after code changes.
- CLI from the docs: `iop --init`, `iop --migrate settings.py`, `iop --start Hlidac.Production --detach`; `-S` stop, `-x` status, `-L` logs.
- Proposed install inside the kit container: `python3 -m pip install --target /durable/mgr/python iris-pex-embedded-python`, then `irispython -m iop --init` with `IRISNAMESPACE=FHIRSERVER`. **Not yet proven;** ask the mentors if it fails.
- Our production code imports only `src/hlidac/fhir_guard.py` (standard library only) and runs it on each bundle before posting; nothing else of ours has to be installed into IRIS.
- **Fallback** if IoP doesn't run within 2 hours: the app processes the same outbox rows itself (FHIR REST + `smtplib`).

## Limits and capabilities that matter

- **Community Edition:** 8 connections (FHIR REST calls count too), 20 cores, 10 GB of data. The app uses one shared SQL connection and closes cursors.
- **No FHIR Subscription notifications:** IRIS for Health stores `Subscription` resources but doesn't send them, so the production polls our outbox table.
- **FHIR support:** `Questionnaire` and `QuestionnaireResponse` are supported; no `$populate` / `$extract`, `_total` or `_filter` in the documented capabilities. Whether transactions accept conditional PUTs by identifier is checked in S1.

## Considered and not used (D28)

FHIR SQL Builder and `iris-fhirsql` (the timeline reads our own assessments; a projection is P2 stretch), vector search, agents and MCP, `iris-agentic-dev`, PyProd, the native rule engine and Alert Manager, the IRIS email adapter, HL7v2 → FHIR conversion, IntegratedML, `iris-disguise` (D27, open; pitch line only unless it runs), and the kit's synthetic data (we use the real hospital data, D23). Reasons: each adds setup risk without changing the demo path, and several default to cloud models.
