# Docs — `praha-03` and the InterSystems prize

Everything the team needs to know about the task, the InterSystems prize and the event, kept in the repo so every teammate has it. The build brief and safety principles are in [`../CLAUDE.md`](../CLAUDE.md). The product spec, system design and roadmap are in [`../architecture/`](../architecture/README.md).

**Task:** `praha-03` **Digitální hlídač nežádoucích účinků** (digital side-effect watchdog, FN Motol a Homolka). Regularly send a cancer patient a short side-effect questionnaire, advise them how to cope, and alert the care team on its own when answers reach warning levels.

**Main goal:** the **InterSystems prize** (€1,500, one team nationally), judged on innovation, real-world impact, use of InterSystems technology and technical coherence. Winning the task itself is secondary.

| File | Contents |
|---|---|
| [`praha-03-brief.md`](praha-03-brief.md) | The official brief, verbatim in Czech and translated; what it asks for and leaves open |
| [`deep-dive-praha-03.md`](deep-dive-praha-03.md) | Medical evidence with sources, the questionnaire, innovation features I1–I8, the IRIS mapping, glossary, competition, pitch, questions for mentors, risks |
| [`illness-groups.md`](illness-groups.md) | The 20 illness groups (D50, D51) and their verification on the IRIS data; FN Motol's agreement for the demo |
| [`pitch.md`](pitch.md) | The pitch: one-liner, 4-minute structure, architecture slide, InterSystems criteria, Q&A, known gaps, backup video |
| [`intersystems-prize.md`](intersystems-prize.md) | The InterSystems prize: brief, judging criteria, technologies on offer, how to score |
| [`intersystems-toolkit.md`](intersystems-toolkit.md) | The InterSystems pieces we use (FHIR repository, IRIS SQL, one IoP production, the portal), connection facts, IoP notes, limits, and what we considered and dropped (D28) |
| [`event.md`](event.md) | Schedule and deadlines, registration, prizes, judges' asks, data and infrastructure |
| [`sources.md`](sources.md) | Where each fact comes from, and its caveats |
| [`sources/`](sources/) | The InterSystems challenge page, as a web-fetch paraphrase |

## Ground rules for these docs

- **Nobody on the team has medical education.** Never invent clinical rules, thresholds or advice. Every clinical number and advice text needs a cited source and Motol approval, and the prototype is never presented as clinically validated.
- Keep the official Czech brief **verbatim**. Mark interpretation as *inference*, and mark IRIS capabilities we haven't run ourselves as unverified.
- Never put secrets or patient data here. The real hospital data stays in IRIS (`../architecture/feature-description.md` §14).
- Our team, **VI-KA**, is registered for `praha-03`. Submission deadline: **Sat 17 Oct, 13:00** (decided by the team), delivered from our private repo.
