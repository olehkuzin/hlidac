# Architecture

| File | What it is |
|---|---|
| [`feature-description.md`](feature-description.md) | Product behaviour: the FHIR Questionnaire plan, check-ins in our accessible Czech web app, transcription and extraction, clarification, CTCAE grading, rules, advice, notifications, alerts, records, accessibility, real hospital data, security. The top authority among these files. |
| [`system-design.md`](system-design.md) | How it is built on InterSystems IRIS: runtime architecture, components, Pydantic contracts, CTCAE grading, the order of the assessment and the LLM guards, FHIR R4 mapping, SQL tables, security and how we prove it, screens, and the decision log (D1…D59) |
| [`accessibility.md`](accessibility.md) | Frontend requirements for patients with disabilities: WCAG 2.2 AA+, personas, design rules, component patterns, testing protocol |
| [`workflow.drawio`](workflow.drawio) | The editable workflow diagram (open in [draw.io](https://app.diagrams.net)) |
| [`workflow.drawio.png`](workflow.drawio.png) | The team's first whiteboard export of the workflow. Kept for history: it still shows ideas we later dropped (a messenger, English, images, synthetic data) |

Rendered diagrams of the current design are in [`../docs/images/`](../docs/images/) (`diagram-*.png`).
