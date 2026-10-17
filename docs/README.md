# Docs

Background for the challenge and the medical evidence behind our design. The product spec, the system design and the accessibility requirements are in [`../architecture/`](../architecture/README.md). The approach is summarised in the [main README](../README.md).

| File | Contents |
|---|---|
| [`praha-03-brief.md`](praha-03-brief.md) | The official `praha-03` brief (FN Motol a Homolka), verbatim in Czech and translated |
| [`deep-dive-praha-03.md`](deep-dive-praha-03.md) | Medical evidence with sources (STAR, PRO-TECT, UKONS, CTCAE), the questionnaire, innovation features, the IRIS mapping, competition and risks |
| [`illness-groups.md`](illness-groups.md) | The 20 cancer illness groups by ICD-10, each with its own FHIR Questionnaire, verified against the hospital data in IRIS (counts only) |
| [`images/`](images/) | Screenshots and diagrams used in the README |

**Ground rules:** nobody on the team has medical training. Every clinical number and advice text cites a source and is labelled DEMO until FN Motol approves it. The prototype is not clinically validated. No patient data is in this repository: the hospital data stays in InterSystems IRIS.
