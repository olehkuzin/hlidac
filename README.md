# Oncology Symptom Companion — Rakathon 2026 (team VI-KA)

> **Public copy for the Rakathon 2026 submission.** The hospital data lives only in InterSystems IRIS and is not part of this repository; server addresses, credentials and team-internal settings are left out (fill your own `.env` from `.env.example`).

**Chosen task:** `praha-03`, **Digitální hlídač nežádoucích účinků** (FN Motol a Homolka, Prague).

**Main goal:** win the **InterSystems prize** (€1,500 nationally) through meaningful IRIS use, innovation, impact and technical coherence. Winning the task itself is secondary.

An **accessible, mobile-first web app** built first for **patients with disabilities** (WCAG 2.2 AA+). It's **our own app**, with no messenger and no third parties, and its security is proven by tests. Patients describe how they feel **in Czech**, by text, voice or large buttons, answering a clinician-defined FHIR `Questionnaire`. It runs on **the real hospital data in IRIS**, which never leaves IRIS. A **local** speech model and an LLM turn that into validated findings, and the app asks only what the clinician's monitoring plan still needs. The LLM is the team's Azure OpenAI deployment in the demo (a local model by one setting) and sees only the pseudonymised message, never patient records. Each symptom gets a **provisional CTCAE grade** that a clinician confirms. A **deterministic, versioned rules engine** decides urgency. Patients get clinician-approved advice or urgent instructions, and nurses get explained, owned alerts with deadlines and fallback, **in the app and by email**. **InterSystems IRIS** holds the hospital data and ours: the interoperability production `Hlidac.Production` writes every check-in back as standard **FHIR R4**, while the app's outbox runner sends the emails (D40, D43), with full provenance and audit.

## Current status

The architecture is defined in [`architecture/`](architecture/): the [product spec](architecture/feature-description.md), the [accessibility requirements](architecture/accessibility.md), the [workflow diagram](architecture/workflow.drawio), the [system design](architecture/system-design.md) and the [roadmap](architecture/roadmap.md). Built and working on `main` (Sat 17 Oct 2026):

- **Core pure domain engine:** FHIR `Questionnaire` and plan loader (`WP1`, `plans.py`), CTCAE v5/v6 content and provisional grading (`WP2`/`WP3`, `ctcae.py`), deterministic rules engine with replay (`WP4`, `assessment.py`), bounded clarification (`WP5`, `clarification.py`), plain-language Czech advice cards (`WP6`, `advice.py`), deterministic red-flag phrase scan (`WP22`, `redflags.py`), and FHIR R4 transaction bundle mapping (`WP12`, `fhir_mapping.py`).
- **AI & security:** Model gateway with endpoint allow-list and Czech PII pseudonymisation (`WP7`, `models.py`), strict JSON-schema extraction with verbatim quote verification (`WP8`, `extraction.py`, verified on Azure OpenAI in `S3`), and passwordless email-code login, CSRF protection, and role/patient access control (`WP30`, `security/`, tested in `tests/security/`).
- **Accessible Czech web apps:** Patient check-in wizard (`WP9`, `ui/patient/`), clinician nurse queue, case view with provisional-to-confirmed CTCAE grade review and Unresolved list (`WP13`, `ui/clinician/`), in-app and email notifications (`WP16`, `notifications.py`, `ui/inbox.py`), alert deadlines with automatic fallback escalation and `/demo` panel (`WP18`, `alerts.py`, `ui/demo.py`), case audit trail with exact model inputs and deterministic replay check plus plans/rules viewer (`WP25`, `ui/clinician/`), and automated Playwright + axe WCAG 2.2 AA test suite (`WP15`, `tests/a11y/`).
- **InterSystems IRIS integration:** `IrisStore` on `Hlidac.*` with least-privilege `hlidac_app` account and locked `/fhir/r4` API (`WP10`, `iris/store.py`, `iris/setup.py`), `Hlidac.Production` interoperability production writing tagged FHIR R4 bundles in-process after re-running `fhir_guard` (`WP11`, `iris/cls/`, `iris/deploy.py`), real hospital treatment context (`IrisContextSource` reading `MedicationAdministration` and anticancer-therapy `Procedure` 42520 from IRIS search tables), pseudonymous demo patient seeding (`Pacient 01…05`), and safe demo reset (`WP14`, `iris/context.py`, `iris/demo_patients.py`, `iris/reset.py`, `scripts/reset_demo.sh`).
- **Voice (minimal):** "Namluvit" on the start and write pages; `mlx-whisper` transcribes inside the app on Apple Silicon, so audio never leaves the laptop. The patient checks and corrects the text before it is sent (`WP17`, `transcription.py`).
- **Audio mode (D59):** "Hlasový režim" in the chat runs the whole check-in hands-free: the device reads each step aloud with its own Czech voice (on-device only; macOS/iOS "Zuzana"), plays a short rising tone and listens until the patient stops talking (2.5 s of quiet, or "Hotovo"; a falling tone means "got it"), reads back what it understood and sends it on a spoken "ano"; the urgent screen is read aloud and ends audio mode. Use Chrome or Safari with the microphone allowed; tapping and typing keep working throughout (`static/audio.js`).
- **All three demo scenarios ran end to end on IRIS** (Sat 17 Oct). See "Running the demo" below.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer.

```sh
uv sync                 # create .venv and install dependencies
cp .env.example .env    # fill in IRIS + model settings; never commit .env
uv run hlidac           # app at http://127.0.0.1:8000
uv run pytest           # tests, incl. the axe run (needs Chrome, or: uv run playwright install chromium)
uv run ruff check .     # lint
```

Health check: `GET /api/health`. The API docs (`/docs`, `/openapi.json`) are switched off on purpose (D29).

### Logging in locally (no IRIS needed)

With `STORE_BACKEND=memory` (the default) the app runs on an in-memory store with three invented demo accounts. Nothing survives a restart.

1. **Set `SECRET_KEY`** in `.env` (it signs the session cookie and keys the login-code hashes):
   `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. Without it, every restart logs everyone out.
2. **Start the mail catcher** (Docker), bound to the laptop only:
   ```sh
   docker run -d --name mailpit -p 127.0.0.1:1025:1025 -p 127.0.0.1:8025:8025 axllent/mailpit:v1.27
   ```
   Login codes arrive in the mailbox at http://localhost:8025. The app's outbox runner sends them and retries every few seconds (`OUTBOX_RUNNER=app`). Keep `app` in every real setup, the IRIS production too: it writes only the FHIR rows (D40), so the app's runner is the only email sender. `OUTBOX_RUNNER=off` sends no email at all and is for tests only.
3. **Open http://localhost:8000/prihlaseni** and log in with one of:

   | Email | Role | Lands on |
   |---|---|---|
   | `pacient.a@demo.local` | patient (team `team-demo`) | `/pacient` |
   | `pacient.b@demo.local` | patient (team `team-demo`) | `/pacient` |
   | `sestra@demo.local` | nurse (team `team-demo`) | `/sestra` |
   | `vedouci@demo.local` | nurse, the team's fallback contact (told only when an alert passes its deadline untaken) | `/sestra` |

   With `DEMO_PANEL=true` in `.env`, nurses also get the **Demo panel** (`/demo`): move the demo clock forward (an untaken alert past its deadline then goes to the fallback contact, in the app and by email) and invite a patient to a check-in.

**Rules that bite:**
- A code is 6 digits, valid **10 minutes**, usable once; 5 wrong tries lock it; at most 3 codes per account per 15 minutes. These timers use the real clock, not the demo clock (D29).
- The session cookie is `Secure`: use **Chrome or Firefox** on `http://localhost`. Safari drops it there; for a local Safari run only, set `SESSION_HTTPS_ONLY=false`.
- **Every new route** must depend on `require_role(...)` or `current_account` (a test fails otherwise), and must call `ensure_patient_access(...)` before reading a patient's data; add an "A can't read B" test for it. Forms include the hidden `csrf_token` field (`render()` provides it); scripts send the `X-CSRF-Token` header.
- **Contract changes for WP10, WP11 and WP13:** `Store.update_outbox_item` (status columns only, plus redacting a finished email) must also be implemented on IRIS; the production redacts a login-code email (`EmailJob.redacted()`) once its row is `done` or `failed`; Unresolved shows only template and status for failed email rows, never the body.
- "Kód nepřišel?" offers **Poslat nový kód**, which reuses the email from the session (no retyping).
- Known demo limits are listed in D29 (`architecture/system-design.md` §11).

### Running on IRIS (WP10)

With `STORE_BACKEND=iris` the app keeps everything in our SQL tables in IRIS (`Hlidac.*` in the `FHIRSERVER` namespace) as the least-privilege account `hlidac_app` (D35).

1. **Fill the `IRIS_*` values in `.env`** (see `.env.example`). Ask the teammate who runs IRIS for the `hlidac_app` password.
2. **Only after a server restart** (the Azure instance loses its data then) or a password change, someone with the admin account runs the setup again. It creates the tables, the app's role and account, and with the flag the three invented demo accounts above:
   ```sh
   uv run python -m hlidac.iris.setup --seed-dev-accounts
   ```
   It is idempotent and touches only our own schema, role and account, plus one server setting: the FHIR API (`/fhir/r4`) requires the `FhirApi` resource and a login. Accounts with `%All` (`_SYSTEM`, `SuperUser`) and the role `FhirApiAccess` can use it; `hlidac_app` can't (D35). IRIS answers a refused FHIR request with 404.
3. **Deploy the outbox production** (WP11, D40) after the setup, and again after changing `src/hlidac/iris/cls/` or `fhir_guard.py`. It loads and compiles our classes over the admin connection, with no IoP and nothing installed in the container, then (re)starts `Hlidac.Production`:
   ```sh
   uv run python -m hlidac.iris.deploy          # --stop stops it
   ```
   The production writes every queued FHIR row (check-ins, grades, alerts) into the FHIR repository, oldest first. Watch it in the portal: Interoperability → Production Configuration → `Hlidac.Production`, and the Visual Trace of each message.
4. **Seed the 5 real SACT demo patients** (WP14), which creates pseudonymous patient accounts (`Pacient 01…05`, never real names), nurse accounts, `patient_plan` rows, and enqueues the demo `Questionnaire`:
   ```sh
   uv run python -m hlidac.iris.demo_patients
   ```
5. **Start the app** with `STORE_BACKEND=iris` (in `.env` or the environment) and log in as above. To reset only our SQL rows and `hlidac`-tagged FHIR resources (verifying hospital resource counts before and after) and re-seed the demo accounts:
   ```sh
   bash scripts/reset_demo.sh                   # or: uv run python -m hlidac.iris.reset
   ```

**IRIS store tests** (a teammate runs them; never CI): `HLIDAC_IRIS_TESTS=1 uv run pytest tests/test_store.py tests/test_outbox_runner.py tests/test_iris_store.py` (about 2 minutes). They run every store test on IRIS in a separate `HlidacTest` schema that is dropped afterwards, and prove the app account's privileges: no UPDATE on the append-only tables, no DELETE anywhere, nothing outside our schema, no global access or code execution, no FHIR API. They need the admin values in `.env`. With the production deployed, `HLIDAC_IRIS_TESTS=1 uv run pytest tests/test_iris_production.py` also checks it end to end: a tagged self-test `QuestionnaireResponse` is written once even when sent twice, a tampered bundle is refused, and the test removes what it wrote.
- Pages are rendered with `hlidac.ui.render(...)` from `src/hlidac/ui/templates/` (Jinja2, autoescaped). `create_app()` loads the demo `Questionnaire`, CTCAE mapping, ruleset, red-flag phrases, advice cards, and context source (`IrisContextSource` when `STORE_BACKEND=iris`, or demo fixtures in memory) automatically.

## Running the demo (on IRIS)

The pitch runs on the demo Mac at `localhost`, against the team's IRIS through the SSH tunnel. Full script and timing: [`docs/pitch.md`](docs/pitch.md) and `architecture/roadmap.md`, "Demo script".

**Before (about 10 minutes):**
1. **Open the SSH tunnel** to the IRIS VM (ports 1972 and 52773 forwarded to `localhost`; the address is only in the team's `.env`). Check it: the portal opens at `http://localhost:52773/csp/sys/UtilHome.csp`.
2. **`.env`:** `STORE_BACKEND=iris`, `DEMO_PANEL=true`, `OUTBOX_RUNNER=app`, `SECRET_KEY` set, the Azure OpenAI values filled in, and `AS_OF` set relative to the demo patients' dose dates (they are shifted per patient). `WHISPER_MODEL` only if voice is shown; fetch the model beforehand (see `.env.example`).
3. **Mailpit** running (see "Logging in locally"); the mailbox is at http://localhost:8025.
4. **Production running:** `uv run python -m hlidac.iris.deploy`. In the portal: Interoperability → Production Configuration → `Hlidac.Production` is green.
5. **Clean start:** `bash scripts/reset_demo.sh`. It removes only our SQL rows and `hlidac`-tagged FHIR resources, checks that the hospital resource counts didn't change, and seeds the demo accounts again.
6. **Start the app:** `uv run hlidac`, then open http://localhost:8000/prihlaseni in **Chrome** (Safari drops the Secure cookie on `http://localhost`). Use a second browser profile for the nurse.

**Accounts on IRIS** (codes arrive in Mailpit):

| Email | Who |
|---|---|
| `pacient-01@demo.local` … `pacient-05@demo.local` | "Pacient 01…05": real hospital patients with dose records, pseudonymous labels only |
| `sestra@demo.local` | nurse: queue, case view, Demo panel (`/demo`) |
| `vedouci@demo.local`, `sestra-zaloha@demo.local` | the team's fallback contacts (told when an alert passes its deadline untaken) |

**The three scenarios:**
1. **Mild, with VoiceOver on (⌘F5):** start a report → "Odpovídat na otázky" → nausea "Ano", intake "Jím a piju jako obvykle", temperature "Nevím", every other question "Ne" (each red-flag "Ne" confirmed) → send → an approved advice card with its source; no nurse task.
2. **Concerning, at 200 % text (⌘+):** write "Mám horečku 38,4 a zimnici." → the urgent screen at once (focus on its heading). Nurse: the alert in the queue and in Mailpit; the case shows the days since the dose read from IRIS, the rule, the quote and the provisional grade → confirm the grade. Don't acknowledge; on `/demo` move the clock past the deadline → the fallback contact gets it, in the app and by email.
3. **Ambiguous, by text (or "Namluvit"):** write "Dnes jsem zvracela a je mi špatně od žaludku." → red flags first, each denial confirmed by a button → only the open items: intake "Jím a piju trochu méně než obvykle", vomiting "3 až 5krát", diarrhoea → "Hlášení si prohlédne zdravotní sestra" (routine review). Don't answer "skoro nic nepila": UKONS rates no intake red, so it becomes urgent.

**Then in IRIS:** the Visual Trace of the FHIR rows; the `QuestionnaireResponse` next to the hospital's record; the grade `Observation` going `preliminary` → `final`; `Task` and `Provenance`; the `Hlidac.*` SQL tables. In the app: the case's audit trail ("Záznam případu a co viděl model") shows exactly what the model saw and the replay check "Shoduje se".

**After:** `bash scripts/reset_demo.sh`. Never run `docker compose … down -v` or any other IRIS wipe: it deletes the real hospital dataset.

### Releases (WP32)

`azure-pipelines.yml` runs **only when a `release-*` tag is pushed**, never on branch pushes or pull requests:

```sh
git tag release-2026-10-17 && git push origin release-2026-10-17
```

It runs lint and tests, builds the backend image (`Dockerfile`; the app also renders the pages, and a frontend image follows in WP33) and pushes it to Azure Container Registry as `hlidac-backend:<tag>`, then deploys it to Azure Container Instances. The container group (`deploy/aci_spec.py`) adds Caddy, which gets a certificate for `https://<DNS_LABEL>.<LOCATION>.azurecontainer.io`; only ports 80 and 443 are public, and the app runs with `STORE_BACKEND=iris` and Secure cookies. `DEMO_SKIP_LOGIN` is always off there. Voice in the image needs `VOICE_BACKEND=elevenlabs` (opt-in cloud, D52) or `server` (our own Whisper server); `mlx` needs a Mac, so without either there is no voice button.

**One-time setup** (someone with rights on `<resource-group>`):
1. **A registry:** done, `<acr-name>.azurecr.io` (Basic, admin user on, so ACI can pull; `ACR_NAME=<acr-name>`). Check that the image builds without the pipeline: `az acr build --registry <acr-name> --image hlidac-backend:build-check .`
2. **Azure DevOps:** a project with a pipeline from this repo's `azure-pipelines.yml` (GitHub connection), and two service connections: a Docker Registry connection to the ACR named `rakhaton-acr`, and an Azure Resource Manager connection scoped to `<resource-group>` named `rakhaton-azure`.
3. **Library → variable group `rakhaton-release`:** `ACR_NAME`, `RESOURCE_GROUP` (`<resource-group>`), `ACI_NAME`, `LOCATION` (`swedencentral`), `DNS_LABEL`, the app settings from `.env.example` that apply (`AS_OF`, `IRIS_*`, `FHIR_BASE_URL`, `AZURE_OPENAI_*`, `SMTP_*`, `MAIL_FROM`, `DEMO_PANEL`, `VOICE_BACKEND`, `WHISPER_SERVER_URL`, `ELEVENLABS_STT_MODEL`, `LOCAL_*`), and as **secret** variables `SECRET_KEY`, `IRIS_PASSWORD`, `AZURE_OPENAI_API_KEY`, `SMTP_PASSWORD`, `ELEVENLABS_API_KEY`, `WHISPER_SERVER_API_KEY`. A release without `SECRET_KEY` or `IRIS_PASSWORD` stops.
4. Optionally an approval check on the environment `rakhaton-release`, so every deploy waits for a person.

## Project context

- [Build brief](CLAUDE.md): what we build, the non-negotiable safety principles, IRIS gotchas, demo.
- [Agent instructions](AGENTS.md): commands, layout, how agents pick up work, safety checklist.
- [Architecture](architecture/README.md): product spec, diagram, system design, roadmap.
- [Docs index](docs/README.md): everything about the task, the prize and the event.
- [Praha 03 brief](docs/praha-03-brief.md): the official brief, verbatim and translated.
- [Praha 03 deep dive](docs/deep-dive-praha-03.md): medical evidence, sources, innovation and pitch.
- [Pitch](docs/pitch.md): the 4-minute structure, architecture slide, Q&A and backup video plan.
- [InterSystems prize](docs/intersystems-prize.md): judging criteria and technologies on offer.
- [InterSystems toolkit](docs/intersystems-toolkit.md): the IRIS software for the watchdog, setup and unverified capabilities.
- [Event facts](docs/event.md): schedule, registration and constraints.

Submission: **Sat 17 Oct 2026 at 13:00** (decided), delivered from this private repo.

**Safety requirements for the build:** real patient data stays in IRIS (never in git, logs or cloud tools; agents never read records); clinician-approved, sourced thresholds and advice; the LLM only extracts, and deterministic rules decide; human follow-up; patient text only to approved model endpoints, pseudonymised, and voice only to a local model; no diagnosis or treatment changes. Nobody on the team has medical education, and the prototype is not clinically validated.

All product work belongs here in `rakhaton/`. The sibling `VI-KA/` event-org repo is not used for delivery and is hands-off without explicit authorisation. Do not commit, push or deploy unless the user asks.
