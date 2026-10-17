# Architecture — Oncology Symptom Companion

| File | What it is | Owner |
|---|---|---|
| [`feature-description.md`](feature-description.md) | Product behaviour: the FHIR Questionnaire plan, check-ins in our own accessible Czech web app, transcription and extraction, clarification, medication check, CTCAE grading, rules, advice and doctor recommendations (open), notifications, alerts, records, accessibility, real hospital data, security, MVP. **The top authority:** it wins over `../CLAUDE.md` and every other doc. Mentor feedback and team decisions applied. | Team |
| [`accessibility.md`](accessibility.md) | Frontend requirements for patients with disabilities: standards (WCAG 2.2 AA+), personas, design rules, component patterns, content and email rules, testing protocol | Agents + team |
| [`workflow.drawio`](workflow.drawio) | Editable diagram source, with labels and tooltips updated for the mentors' feedback and the team's decisions (no messenger, Czech only, no images, real data). Open in [draw.io](https://app.diagrams.net). | Team |
| [`workflow.drawio.png`](workflow.drawio.png) | The team's original export (**outdated:** still shows WhatsApp, English, images, synthetic data). Re-export from `workflow.drawio` with "Include a copy of my diagram" (roadmap WP29). | Team |
| `embed.html` | The original export wrapped in an `<img>` tag (same PNG) | Team |
| [`system-design.md`](system-design.md) | How we build it on InterSystems IRIS: runtime architecture, diagram → module → IRIS mapping, Pydantic contracts, CTCAE grading, assessment order and LLM guards, FHIR R4 mapping (Questionnaire/QuestionnaireResponse), SQL tables, **security and how we prove it**, screens, **decisions** | Agents + team |
| [`roadmap.md`](roadmap.md) | Next steps: milestones, timeboxed spikes, work packages with acceptance tests, cut list, demo script, mentor questions | Agents + team |

When the spec and another doc disagree, **the spec wins** (team decision D26): fix the other doc, record the decision in `system-design.md` §11, and tell the team.
