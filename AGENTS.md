# AGENTS.md — rakhaton

The team's main working repo for Rakathon 2026 (team **VI-KA**). All code is written, tested and committed here. This file is for every coding agent (Claude Code, Codex, Junie, Copilot …) and every teammate.

**Read before you start, in this order:**
1. `CLAUDE.md`: the build brief and the **non-negotiable principles** (§3). Claude Code loads it automatically; other agents must open it.
2. `architecture/feature-description.md` + `architecture/workflow.drawio`: what the product does. **The top authority:** where it disagrees with `CLAUDE.md` or anything else, it wins; tell the team about the conflict.
3. `architecture/accessibility.md`: **required for any UI work.**
4. `architecture/system-design.md`: how we build it on IRIS (components, contracts, CTCAE grading, LLM guards, FHIR mapping, decisions).
5. `architecture/roadmap.md`: what to do next (spikes, work packages, acceptance tests, cut list).

**Challenge:** `praha-03` "Digitální hlídač nežádoucích účinků" (FN Motol a Homolka). **Main goal:** the InterSystems prize (`docs/intersystems-prize.md`), so make IRIS central in every component.

**The product is the Oncology Symptom Companion:**
- **our own accessible, mobile-first web app** (no messenger, no third parties), built first for **patients with disabilities**, with security proven by tests;
- **Czech only**; text, voice, large buttons (no images); check-ins against a FHIR `Questionnaire`;
- **real hospital data in IRIS** (not synthetic), which never leaves IRIS;
- local voice transcription and LLM extraction on pseudonymised text (Azure OpenAI in the demo, local by one setting), **provisional CTCAE grades** confirmed by clinicians, deterministic rules;
- in-app notifications mirrored to email;
- a nurse work queue on InterSystems IRIS.

The Python package and CLI are `hlidac`. Task, prize and event docs are in `docs/` (index `docs/README.md`).

**Deadline:** submission **Sat 17 Oct 2026 at 13:00** (decided); freeze at 11:00. **Delivery:** this private repo. See `docs/event.md`.

**Status (Sat 17 Oct 2026, ~08:15):** the core, WP14 and WP22 are on `main` (see the roadmap's status column). **The final scope is decided (D43; `architecture/roadmap.md`, "Final scope"); work only inside it unless the user says otherwise:**
- **Must do, in order:** WP14 (done: treatment context read from the hospital data in IRIS, never set by us; pseudonymous labels only, never real names; a simple reset of our own writes) → **WP22 red-flag phrase scan** (done, P0) → one full run of the three scenarios on IRIS → WP2 review → pitch, README, backup video.
- **Only if time is left:** voice (S2, S4, WP17; done, minimal), then evaluation (WP21), then WP20, WP24, WP18 grouping, WP29, the WP15 manual log.
- **Scenario 3 by voice ran end to end** (Sat 17 Oct; in-memory store, a synthesized Czech recording): exact Whisper transcript in ~2 s → quoted extraction → red flags by buttons → only the open questions → routine review, the nurse alert as scripted, the audit with both in-process Whisper calls. Open: the live browser microphone; (fixed: transcription calls are labelled as such, and the audit page names the store it runs on). Details in `CLAUDE.md`, "Final scope".
- **Dropped:** WP27, WP28, the rest of S1 (port lockdown, password change), WP31, S6 (folded into WP14).
- **Kept as built:** emails go through the app's outbox runner; the IRIS production (no IoP, D40) writes FHIR only.
- **FN Motol's agreement (D51, reported by Nazar, Sat 17 Oct 2026):** FN Motol verified the approach and agreed that the team and its agents may **read and write the hospital data in IRIS** for the demo, and that the team may write clinical content (questions, red flags, CTCAE mappings, recommendations, demo clinical records) for the demo. Content stays labelled DEMO (not a clinical sign-off of each item). Still prefer counts and aggregates; patient data still stays out of git, and an IRIS wipe still needs the team's OK.
- **Illness groups (D50, D51):** 20 cancer groups by ICD-10, each with its own questionnaire, verified on the IRIS data (all 325 coded patients, 72 of 73 dose patients; `docs/illness-groups.md`).

**Contract changes since WP0 (read before WP9, WP10, WP11, WP13):** WP30 (decision D29 in `architecture/system-design.md`) added:
- `Store.update_outbox_item`: changes only an outbox row's status columns, plus one allowed job change, redacting a finished email. **WP10** implements it on IRIS.
- `EmailJob.redacted()`: a login-code email loses its body once its row is `done` or `failed`. **WP11's** production must do this too.
- **WP13's** Unresolved list shows failed email rows by template, status and error type only, never the body or address.
- New routes use `require_role(...)` and `ensure_patient_access()` (a test fails if a non-public route has no login check); setup and rules are in `README.md`, "Logging in locally".

## Workspace

This repo sits next to two sibling folders:
- `../VI-KA/` is the event-org repo. **It is not used for delivery** (team decision: deliver from this private repo). **Don't touch it** (no edits, commits or pushes) unless the user explicitly tells you to; every participant can see it.
- `../project resolving/` is a local, non-git archive of raw sources that only some teammates have. Anything the team needs belongs in `docs/` or `architecture/` here; never link product docs to it.

## Commands

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```sh
uv sync                 # create .venv and install dependencies
cp .env.example .env    # then fill in IRIS + model settings
uv run hlidac           # API at http://127.0.0.1:8000 (auto-reload)
uv run pytest           # tests, incl. the axe run in tests/a11y (needs Chrome, or: uv run playwright install chromium)
uv run ruff check .     # lint (line length 100)
```

## Layout

What exists and what is planned (planned modules belong to the work packages in `architecture/roadmap.md`):

```
src/hlidac/
  app.py, config.py, __init__.py   FastAPI app, settings from .env, main()        (exists)
  security/                        email-code login, CSRF, access checks (WP30, exists); headers dropped (WP31, D43)
  ui/__init__.py, ui/login.py      render() helper, login/logout routes, role homes (WP30, exists)
  ui/templates/, ui/static/        Jinja2 base layout + login pages, app.css (WP30, exists)
  audit.py, emails.py              audit-trail writer; emails from data/content/email/ (exists)
  outbox_runner.py, dev_seed.py    dev email runner (Mailpit); invented demo accounts (exists)
  schemas/                         shared Pydantic contracts, incl. the outbox     (exists, WP0)
  clock.py                         controllable demo clock                         (exists, WP0)
  store.py                         Store + ContextSource seam, in-memory versions  (exists, WP0)
  plans.py                         Questionnaire + patient plan loading            (exists, WP1)
  ctcae.py                         provisional CTCAE grading from the mapping      (exists, WP3)
  assessment.py                    pure rules engine → Assessment                  (exists, WP4)
  content.py                       shared YAML reader and DATA_DIR                 (exists, WP3)
  clarification.py                 next approved question, bounded turns           (exists, WP5)
  advice.py                        eligible advice-card selection                  (exists, WP6)
  models.py                        model gateway: allow-list, masking, call log    (exists, WP7)
  extraction.py                    LLM → ExtractionResult, quote check, retry      (exists, WP8)
  ui/patient/                      accessible Czech patient web app (exists, WP9); voice WP17, settings WP20 (only if time)
  wizard.py, pipeline.py, processing.py  button-path order and answers; live check-in pipeline (exists, WP9, WP18, D30)
  ui/clinician/, ui/inbox.py       nurse queue, case view, unresolved, audit, inbox (exists, WP13, WP16, WP25)
  ui/demo.py                       demo panel: demo clock, check-in invitations    (exists, D41)
  fhir_guard.py                    the FHIR write guard (stdlib only), also run by the production (exists, WP0)
  iris/schema.sql                  our IRIS SQL tables                             (exists, WP0)
  iris/db.py, store.py, setup.py   shared connection, IrisStore, tables + app account   (exists, WP10)
  iris/cls/, iris/deploy.py        outbox production Hlidac.Production, its deploy   (exists, WP11, D40)
  iris/context.py, demo_patients.py, reset.py  FHIR context source on hospital data, demo seeding, reset (exists, WP14)
  fhir_mapping.py                  check-in + assessment → transaction bundle      (exists, WP12)
  notifications.py                 in-app + email (via the outbox), templates      (exists, WP16)
  transcription.py                 mlx-whisper, inside the app                     (exists, WP17, D44)
  alerts.py                        deadlines, fallback contacts                    (exists, WP18, D41)
  redflags.py                      deterministic red-flag phrase scan              (exists, WP22)
data/questionnaires/              the demo FHIR Questionnaire(s) (exists, WP1)
data/plans/, data/content/         rules, response policy, advice cards, notification templates (versioned files; no patient data)
data/ctcae/                        CTCAE terms, verbatim grade definitions, UKONS ratings, mappings (WP2)
tests/factories.py, tests/fixtures/  hand-made test records with NO patient data (real data stays in IRIS)
scripts/reset_demo.sh              reset: removes only hlidac-tagged resources and our SQL rows (exists, WP14)
eval/                              evaluation CLI (WP21, only if time)
tests/, tests/a11y/, tests/security/  pytest; accessibility checks (WP15); security tests (WP30)
architecture/                      spec, accessibility, diagram, system design, roadmap
docs/                              brief, evidence, InterSystems prize + toolkit, event, sources
```

**Boundaries:**
- `schemas/`, `ctcae.py`, `assessment.py`, `clarification.py`, `advice.py`, `plans.py`, `fhir_mapping.py` and the validation in `extraction.py` are **pure**: no IRIS, network or clock imports.
- The app reaches IRIS only through `Store`, `ContextSource` and the outbox. The IRIS production (no IoP, D40) only moves ready FHIR jobs; it runs `fhir_guard.py` (standard library) and re-checks every bundle before posting. The app's outbox runner sends the email jobs.
- The UI is **server-rendered HTML with progressive enhancement**; no SPA framework.

## InterSystems IRIS (from the FHIR-AI-Hackathon-Kit)

The full map of the kit and its companion tools is in `docs/intersystems-toolkit.md`; the gotchas are in `CLAUDE.md` §5.

- **Ports and access:**
  - SQL / Python driver at `localhost:1972`, namespace **`FHIRSERVER`**;
  - FHIR at `http://localhost:52773/fhir/r4`;
  - portal at `http://localhost:52773/csp/sys/UtilHome.csp`;
  - dev login `SuperUser`/`SYS`.

  The team's IRIS runs on an Azure VM. These go in `.env` and `config.py`; never hardcode them.
- **Reset:**
  - re-running the kit's `start.sh` duplicates patients;
  - **`docker compose -f docker-iris-fhir/docker-compose.yml down -v` deletes the real hospital dataset: never run it or any IRIS wipe without the team's explicit approval.** Remove our own writes with the WP14 reset;
  - bundles placed in `docker-iris-fhir/data/fhir` are bulk-loaded on start.
- **Connections:** Community Edition allows 8, so always close cursors and connections.
- **The kit defaults to OpenAI for embeddings and agents.** Our data rule overrides that: patient text reaches a model only through our gateway (the approved Azure deployment or a local model, pseudonymised), and no agent or model queries patient records.
- **What we use from InterSystems (D28):** the FHIR R4 repository, IRIS SQL, one interoperability production of IRIS classes (`Hlidac.Production`, FHIR writes only; no IoP, D40) and the Management Portal. Not used: IoP, FHIR SQL Builder, vector search, MCP and agents, `iris-agentic-dev`, PyProd, the rule engine, the email adapter.

## How agents work here

- **Take one spike or work package** from `architecture/roadmap.md`. Stay inside its scope, meet its acceptance criteria, and set its status (`doing` → `done`).
- **Contracts first:** `src/hlidac/schemas/` is shared by every track. Change a schema only with its tests, and say so in the commit message.
- **Domain logic is pure and deterministic.** Inject the clock, plan, mapping and ruleset; never call `datetime.now()` in domain code.
- **Model calls go only through `models.py`.** Tests use recorded model outputs and never call a live model.
- **Patient-facing text comes only from approved content** (Questionnaire item texts, cards, notification templates, urgent instructions), in Czech. Never generate or rephrase it at runtime.
- **Clinical content is never invented.** Every rule value, CTCAE definition and mapping, red-flag phrase and advice text needs a `source_ref` and stays labelled "DEMO — čeká na schválení FN Motol". CTCAE definitions are copied verbatim with their version.
- **UI work follows `architecture/accessibility.md`** and isn't done until its §7 checks pass.
- **Record decisions** in `architecture/system-design.md` §11 (date and owner). If a work package takes far longer than planned, say so and use the roadmap's cut order.
- **Ask the user before:**
  - big architectural choices;
  - anything that weakens `CLAUDE.md` §3;
  - outward-facing steps (a public tunnel or HTTPS exposure for phones, real SMTP sending, deployment, `../VI-KA/`);
  - any IRIS wipe or reset beyond our tagged data, and any export of patient data.

## Safety checklist (before every commit that touches `src/`, `data/` or `eval/`)

Claude Code can run the `safety-reviewer` subagent (`.claude/agents/safety-reviewer.md`); other agents go through the list themselves.

1. **No path lets `not_mentioned`, `unknown`, a failure or a missed check-in end as `no_escalation`;** a missing attribute never lowers a CTCAE grade.
2. **The urgent path doesn't wait** for clarification, storage, email or a snooze; retries can't duplicate alerts.
3. **Patient-facing strings come only from approved content;** no model or model output builds them; **emails carry no clinical content.**
4. **Every model call goes through the gateway;** `PATIENT` text reaches only allow-listed endpoints (local or the approved Azure deployment) and is pseudonymised before any non-local call; audio reaches only local Whisper; no request carries records; tests cover all of it.
5. **CTCAE grades come only from the approved mapping,** stay `provisional` until a clinician confirms, and are never shown to patients.
6. **Every new finding, grade, assessment, alert or notification keeps its source** and its plan, mapping and rule versions.
7. **New structured boundaries use strict Pydantic** (`extra="forbid"`, `strict=True`).
8. **No secrets, patient data or hardcoded endpoints in the change** (code, fixtures, logs, docs); writes to IRIS are `hlidac`-tagged and never modify hospital resources.
9. **Security:** new routes and queries are scoped to the session's patient or role, forms carry CSRF tokens, nothing sensitive goes in URLs or logs, and a test covers it.
10. **`uv run pytest` and `uv run ruff check .` pass.**

## Accessibility checklist (before every commit that touches `ui/` or emails)

Claude Code can run the `accessibility-reviewer` subagent (`.claude/agents/accessibility-reviewer.md`). Full rules are in `architecture/accessibility.md`.

1. **Native, semantic HTML;** labels on every control; one `h1`; landmarks; `lang` set.
2. **Contrast:** text ≥ 7:1 in the patient flow (never < 4.5:1); UI parts ≥ 3:1; status never by colour alone.
3. **Targets ≥ 48 × 48 px with spacing;** no drag, swipe, long-press or multi-finger gestures.
4. **Fully keyboard-operable** with a visible, unobscured focus ring.
5. **No time limits;** progress saved after every answer; "Nevím" on every clinical question.
6. **Errors and status changes announced;** the urgent screen moves focus to its heading.
7. **Works at 200 % text and 400 % zoom / 320 px width.**
8. **Plain-language approved text;** no CTCAE grades or jargon on patient screens.
9. **Automated axe check passes;** the keyboard pass is done; a screen-reader pass is logged for flow changes.

## Git workflow

- `origin` is `github.com/nazarskiy/rakhaton` (private), and teammates commit here too.
- Keep `main` runnable. Before every commit, `uv run pytest` and `uv run ruff check .` must pass.
- Make small commits with clear messages, naming the work package (e.g. `WP3: …`). Commit or push only when the user asks.
- Before pushing, pull and rebase onto `origin/main`. Never force-push `main`.
- Never commit `.env`, keys, or any patient data (also not in fixtures, screenshots or eval output). The hospital data lives only in IRIS.

## Hard rules (full version in `CLAUDE.md` §3)

- **The LLM reads, the rules decide.** No model sets urgency or a CTCAE grade; a pure, versioned engine does.
- **Uncertainty never lowers urgency or a grade.** `absent` / `not_mentioned` / `unknown` stay distinct; failures stay visible.
- **Patients see only approved, versioned, plain-language Czech text;** emails carry no clinical content.
- **CTCAE grades are provisional until a clinician confirms them.**
- **Patient text goes only to approved model endpoints, minimised and pseudonymised; voice stays local** (D5), enforced by a gateway guard that raises and by tests.
- **Urgent never waits.**
- **The system never diagnoses or changes treatment;** a named person handles every alert.
- **Every patient screen meets WCAG 2.2 AA.**
- **Real patient data stays in IRIS;** agents may read and write it for the demo (FN Motol's agreement, D51), preferring aggregates; the app writes only tagged resources.
- **Security is proven by tests,** never just claimed.
- **Every finding, grade, assessment, alert and notification is traceable and audited.**
- **Ask before** big architectural choices or anything that weakens these rules.
