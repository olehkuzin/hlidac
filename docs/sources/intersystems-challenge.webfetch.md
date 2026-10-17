# InterSystems Challenge at Rakathon 2026 — web-fetch paraphrase

> **Not the original text.** developer.intersystems.com returns HTTP 403 to scripted downloads, so this page was read with a web-fetch tool on 2026-10-16. It is a detailed paraphrase with short quotes. To verify, open https://developer.intersystems.com/intersystems-challenge-at-rakathon-2026/ in a browser.

## Challenge

- **Title:** "Best Use of InterSystems Technology for AI and Interoperability"
- **Task:** build an innovative Rakathon solution using InterSystems tools: IRIS for Health, the InterSystems FHIR server, GenAI (vector search), agentic approaches, machine learning, or any combination.
- **Eligibility:** "This challenge is open to all hackathon participants and can be combined with any other challenge of the hackathon."
- **Mentors:** "InterSystems tech mentors will be available 24/7, so feel free to reach out for help!" No named contacts.

## Technologies

1. **InterSystems FHIR server**
   - Suggested uses:
     - integrating with EHR-style data models;
     - normalising messy data;
     - exchanging patient, observation, medication or encounter records;
     - building hospital-ready, standards-based solutions.
   - IRIS has a "fully integrated FHIR Server": store, search and exchange resources through FHIRPath, SQL and REST APIs.
   - IRIS for Health converts HL7v2, CDA, DICOM, X12 and other formats into FHIR.
2. **IRIS vector search**
   - Suggested uses:
     - clinical AI assistants grounded in patient data;
     - semantic search over notes, PDFs and guidelines;
     - **meaning-based trial matching**;
     - RAG apps;
     - finding similar cases.
   - Works with Python, SQL, LangChain and LlamaIndex. "Embedding vectors are just another SQL datatype for us!"
   - IRIS also supports GenAI, tabular AutoML and interoperability.
3. **Agentic AI**
   - Kit tutorials:
     - AI agents: https://github.com/intersystems-community/FHIR-AI-Hackathon-Kit/blob/V2/Tutorials/5-ai/5.1-agents-and-tools.ipynb
     - MCP servers exposing IRIS data to agents: https://github.com/intersystems-community/FHIR-AI-Hackathon-Kit/blob/V2/Tutorials/5-ai/5.2-mcp-server.ipynb

**Kit:** https://github.com/intersystems-community/FHIR-AI-Hackathon-Kit

## Datasets

- Participating hospitals will supply datasets for their own challenges. These load into the IRIS kit.
- VZP will provide data and a tech stack in its Azure cloud. IRIS instances will run there too, set up with the Azure team, so VZP challenges can be combined with this one.
- Synthetic data:
  - Synthea: https://synthetichealth.github.io/synthea/
  - Kit guide: https://github.com/intersystems-community/FHIR-AI-Hackathon-Kit/blob/main/Additional-demos/Making-synthetic-fhir-data.md

## Judging criteria

The four criteria:
- **Innovation**
- **Real-world impact**
- **Use of InterSystems technology**
- **Technical coherence**

Judges also want to see:
- a clearly defined healthcare problem;
- smart use of AI and/or HL7® FHIR®;
- a sound data model and architecture;
- a realistic path to an MVP or startup;
- a clear explanation of impact.

## Prize

- **1st place: €1,500** for the winning team, for the most innovative solution using InterSystems technology.
- The Rakathon site adds that it's awarded on Sunday 18 Oct at the final in Prague.

## Learning materials

- **FHIR**
  - Kit walkthrough: https://youtu.be/_4qqv3vflRg
  - What is FHIR (1 min): https://www.youtube.com/shorts/YNV31yqVtbo
  - The concept of FHIR (23 min): https://youtu.be/fv2xTR_0QnA
  - The HL7 FHIR specification (15 min): https://www.youtube.com/watch?v=LQ9_itHr_Pc
- **AI**
  - What is RAG (11 min): https://youtu.be/u47GtXwePms
  - MCP explained (9 min): https://youtu.be/9ILgj4v992Q
  - AI for patient portals (11 min): https://youtu.be/diHha8GcjPA
  - Types of AI (8 min): https://youtu.be/QUncJdqwB8c
- **Docs**
  - IRIS for Health: https://docs.intersystems.com/irisforhealthlatest/csp/docbook/DocBook.UI.Page.cls
  - FHIR: https://docs.intersystems.com/irisforhealthlatest/csp/docbook/DocBook.UI.Page.cls?KEY=HXFHIR_fhir_components
  - Embedded Python: https://docs.intersystems.com/irislatest/csp/docbook/DocBook.UI.Page.cls?KEY=PAGE_epython
- **Community**
  - Discord: https://discord.com/channels/698987699406766170/702976447714820097
  - Forum: https://community.intersystems.com/
  - Open Exchange: https://openexchange.intersystems.com/
  - Docker images: https://hub.docker.com/u/intersystems
- **Startups:** InterSystems Ventures offers grants and investments, a 3-month Digital Health Incubator and a Growth Toolkit (https://startup.intersystems.com/).

## Not on the page

Submission requirements and deadlines.
