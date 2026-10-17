# Accessibility: requirements for the patient web app

- **Why:** the mentors asked us to focus on **patients with disabilities**, with a very friendly frontend that follows all accessibility practices. Cancer treatment also causes temporary impairments, such as numb fingers from neuropathy, fatigue, and vision or concentration problems. Accessibility is therefore the core of the product, not a niche feature. (Cite a published source before putting prevalence numbers in the pitch.)
- **Scope:** every patient screen, every email, and the clinician screens (staff have disabilities too).
- **Rule for agents:** a UI change is done only when it meets §3–§5 and passes the §7 checks. When in doubt, choose the simpler, more native solution.

## 1. Standards

| Standard | How we use it |
|---|---|
| **WCAG 2.2, level AA** (W3C Recommendation, 2023) | **The minimum for every screen** |
| WCAG 2.2 AAA, selected criteria | Targets for the patient check-in flow: contrast 7:1 (1.4.6), touch targets ≥ 44 × 44 px (2.5.5), no timing (2.2.3), plain language (3.1.5) |
| EN 301 549 | The EU accessibility standard for ICT, which points to WCAG |
| Czech Act No. 99/2019 Sb. (accessibility of public-sector websites and mobile apps) | A production deployment at a public hospital such as FN Motol is likely to fall under it (*verify*). Useful as a real-world-impact point in the pitch. |

## 2. Who we design for

Each demo scenario must work for at least these people:

| Persona | Needs | Key requirements |
|---|---|---|
| **Blind patient** using VoiceOver (iOS) or TalkBack (Android) | Everything announced, in a logical order | Semantic HTML, labels, headings, live regions, no unlabelled icons |
| **Low vision**, often an older patient | Large text, strong contrast, zoom | 18 px+ base text in `rem`, reflow at 400 % zoom, contrast 7:1, no text in images |
| **Tremor or numb fingers** (neuropathy) | No precise taps | Targets ≥ 48 × 48 px with spacing; no drag, swipe, long-press or multi-finger gestures; voice input |
| **Deaf or hard of hearing** | No audio-only information; can't rely on a phone call | Everything in text; urgent instructions include a non-voice contact route (open decision D19) |
| **Cognitive fatigue** ("chemo brain"), low literacy, non-native speaker | Little to remember, little to read | One question per screen, plain language, progress shown, no time limits, partial answers kept |
| **Speech impairment** | Can't use voice | Voice is always optional; text and buttons always available |
| **Caregiver helping** | Reporting on someone's behalf | "Vyplňuji za pacienta" mode; the report records who reported |

## 3. Design rules (testable)

**Structure and semantics**
- Use native HTML elements first (`button`, `a`, `input`, `fieldset` + `legend`, `label`); ARIA only where HTML has no equivalent.
- One `h1` per page and a logical heading order; landmarks (`header`, `nav`, `main`, `footer`); a unique, descriptive `<title>`.
- Set `lang="cs"` on `<html>` (the app is Czech only); mark any foreign-language fragment with its own `lang`.
- Use server-rendered pages with progressive enhancement: the core check-in works as plain HTML forms, and JavaScript adds voice recording and live updates.

**Perceivable**
- **Contrast:**
  - text ≥ 7:1 in the patient flow (never below 4.5:1);
  - icons, borders and focus rings ≥ 3:1.
- **Zoom and reflow:**
  - base text ≥ 18 px, sized in `rem`;
  - no loss of content or function at 200 % text size or 400 % zoom, or at 320 CSS px width (1.4.4, 1.4.10);
  - user text spacing must not break the layout (1.4.12).
- **Never colour alone:** a status always has an icon, a word and a colour, for example "⚠ Naléhavé".
- **Images and icons** (the app has no photo reporting):
  - informative icons get alt text, and decorative ones `alt=""`;
  - no text inside images.
- **Read aloud** (optional, P1): a button reads the **approved** text on screen using the browser's speech synthesis (`cs-CZ`). It's never used for model-generated text, and voice quality varies by device.

**Operable**
- **Touch targets:** ≥ 48 × 48 CSS px with ≥ 8 px between targets (beyond WCAG 2.5.8 at 24 px and 2.5.5 at 44 px).
- **No complex gestures:**
  - no drag, swipe, long-press, multi-finger or path gestures (2.5.1, 2.5.7);
  - recording starts with one tap and stops with another (no press-and-hold).
- **Keyboard and switch access:**
  - everything is reachable and usable in a logical order;
  - a skip link;
  - a visible focus ring of at least 3 px, never hidden under sticky elements (2.4.7, 2.4.11).
- **No time limits:**
  - no session timeout during a check-in and no auto-advancing screens;
  - progress is saved after every answer (2.2.1, 2.2.3).
- **Motion:**
  - respect `prefers-reduced-motion`;
  - nothing flashes.

**Understandable**
- **Plain language:**
  - short sentences, everyday words and active voice;
  - medical terms are explained, or not used, on patient screens;
  - patients see severity in plain words (žádné, mírné, střední, silné, velmi silné), never as CTCAE grades. CTCAE grades are for clinicians.
- **One question per screen:**
  - "Otázka 2 ze 4" is shown as text;
  - Back always works;
  - the layout and the position of the Help link are the same on every screen (3.2.3, 3.2.6).
- **"Nevím" (I don't know) is always an option.** It records the finding as `unknown`, never as absent, so the UI keeps the system's safety rule.
- **Confirm consequential answers:** red-flag answers (fever, breathing) and values transcribed from voice are shown back for confirmation ("Rozuměli jsme: 38,4 °C. Je to správně?"), with a way to correct them (3.3.4; guard G5 in `system-design.md`).
- **Errors:**
  - described in text next to the field, focus moves to them, and they're announced (3.3.1, 3.3.3);
  - nothing the patient already gave is asked again (3.3.7).
- **Passwordless, accessible login:**
  - a link or one-time code by email;
  - pasting is allowed;
  - no CAPTCHA, puzzles or memory tests (3.3.8).

**Robust**
- Every control has a correct name, role and value (4.1.2).
- **Status messages** use live regions (4.1.3):
  - `aria-live="polite"` for new notifications and "saved";
  - the urgent screen moves focus to its heading instead of announcing over the user.
- Test with real assistive technology (§7). Automated checks find only part of the problems.

## 4. Component patterns

| Component | Pattern |
|---|---|
| **Check-in start** | A large heading "Jak se dnes cítíte?" with three equal, large choices: **Napsat** (text area with a visible label), **Namluvit** (voice), **Odpovídat na otázky** (button-only path) |
| **Choice question** | `fieldset` + `legend`; large radio buttons as full-width cards with icon and text; always "Nevím"; no sliders |
| **Severity** | Five large labelled options with one-line plain descriptions; never a slider or a number-only scale |
| **Voice recording** | A large toggle button with a visible and announced state ("Nahrávám… klepněte pro ukončení"); no hard time limit; afterwards "Rozuměli jsme: …" with **Ano** / **Opravit** / **Napsat místo toho** |
| **Result: advice** | The approved card text in plain language, with a short "what to do next" list and a source link; read-aloud button; "Tým byl / nebyl informován" stated explicitly |
| **Result: urgent** | A full-screen page with a red icon + heading "Zavolejte hned", focus on the heading, a large `tel:` link to the approved number (155 when it applies), **a non-voice alternative for patients who can't call (D19, open)**, and whether the team has been notified. Never auto-dismissed. |
| **Notifications** | The unread count in text ("3 nová oznámení"); each notification is a link to its screen; mirrored to email (§6) |
| **Settings** | Text size, high contrast, language, voice-first mode, simplified mode, caregiver mode. Saved in the patient's profile and visible to the care team as accessibility needs. |
| **Clinician screens** | WCAG 2.2 AA; statuses and CTCAE grades always as text plus icon plus colour; tables with proper headers; keyboard shortcuts optional and never required |

## 5. Content rules

- All patient-facing text is **approved content** (`data/content/`), never generated at runtime. That includes question wording, button labels on clinical questions, advice cards and urgent instructions.
- Write for a reading age of about 12; one idea per sentence. Provide an easy-to-read version of each advice card where possible.
- Never use abbreviations or English terms on Czech patient screens without an explanation.
- Links say where they go ("Otevřít dotazník"), not "klikněte zde".

## 6. Email

- Plain-text part plus simple, semantic HTML; readable without images; descriptive link text; `lang` set.
- **No clinical content** in emails: no symptoms, grades or advice. Only "Máte nový dotazník / novou zprávu od týmu" and a link back to the app, which requires login.

## 7. Testing protocol (definition of done for UI changes)

1. **Automated:** run axe-core on every changed page (for example Playwright with an axe plugin, or the browser's Lighthouse accessibility audit). No violations may remain.
2. **Keyboard:** complete the changed flow with Tab, Shift+Tab, Enter and Space only; focus always visible and in a logical order.
3. **Screen reader:** complete the flow with VoiceOver (macOS or iOS) or TalkBack. Every control is announced with its name and state, and errors and status messages are announced.
4. **Zoom:** 200 % text and 400 % zoom (or 320 px width) without horizontal scrolling or lost content.
5. **Contrast:** every new colour pair is checked against §3.
6. **Persona walk-through before the demo:** scenario 1 with a screen reader, scenario 3 voice-only, scenario 2 at 200 % text size.

Record each manual pass in the work package's status in `roadmap.md` (what, who, when, assistive technology used).

## 8. Technical constraints to know

- **Recording the microphone needs a secure context:** HTTPS or `localhost`. The demo runs on the laptop at `localhost` in mobile view (D17). A phone opening `http://<laptop-ip>` can't record; phone HTTPS is out of scope in this version.
- **Recording formats differ:** Safari records `audio/mp4`, Chrome `audio/webm`. Convert with ffmpeg before Whisper.
- **Web Push** works on iOS only for web apps added to the home screen, so in-app plus email is the reliable path (Web Push is P2).
- **Fonts:** a highly legible font such as Atkinson Hyperlegible, with system fonts as fallback. The layout must work if web fonts don't load.
