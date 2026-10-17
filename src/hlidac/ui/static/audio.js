// Audio mode (D59, accessibility.md §3 "read aloud", §5 "voice-first"): the check-in by voice,
// hands-free. One tap on "Hlasový režim" starts it (that tap lets the page speak and listen);
// then, for each step: the device reads the step aloud, listens until the patient stops talking,
// has it transcribed (voice-core.js, the app's own /prepis), reads back what was understood and
// asks "Mám to odeslat?"; a spoken yes sends it, a no asks again (G5). The next step loads in
// place (no page reload, which would make the browser block speech), so it continues by itself.
//
// Rules kept:
// - The device says only approved texts (each step's `data-audio-say`, built by the server from
//   the texts on screen), the fixed audio texts (chat-v1.json "audio") and the patient's own
//   transcribed words. Never model output.
// - Speech output only with an on-device Czech voice (`localService`); a network voice would send
//   the read-back of the patient's words to a third party. Without one, the steps stay on screen
//   and only listening works.
// - Yes and no to a confirmation are matched on the first word against fixed lists; anything
//   else is asked again. No model decides a confirmation.
// - The urgent screen is read aloud and audio mode ends there; it never calls anyone.
// - Sound cues (made here, no files): a rising two-note "ding" right before listening starts
//   ("speak now"), a falling one when the recording ends and transcription starts ("got it").
//   The cue ends before the microphone records, so it is never transcribed. The same moments
//   are always shown in words too (status line, "Hotovo"), so no one depends on the sound.
// - The voice screen (like a phone's voice assistant, in our design): a full-screen dialog over
//   the chat with an orb that breathes while Hlídač speaks, follows the voice level while it
//   listens and shimmers while it transcribes; the state in words under it; a large caption of
//   what is said or asked (nothing is audio-only); "Ano" / "Ne" buttons whenever a yes or no is
//   asked; "Hotovo" (ends the patient's turn) and "Ukončit" (closes it). The chat behind it is
//   inert and keeps updating; closing shows it again. The urgent screen closes it.
// - Everything stays tappable and typeable; a tap on any other control leaves audio mode paused
//   (the next page offers "Pokračovat hlasem"). No time limits: before any speech it listens
//   until the patient speaks or taps "Hotovo".
window.HlidacAudio = (() => {
  "use strict";

  const core = window.HlidacVoice;
  const synth = window.speechSynthesis;
  const KEY = "hlidac-audio";
  const $ = (id) => document.getElementById(id);

  const fold = (s) =>
    s.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "").replace(/[^a-z0-9,.\s]/g, " ");

  // "yes" | "no" | null from what the patient said: the first word only, against fixed lists.
  const classify = (text, texts) => {
    const first = fold(text).replace(/[,.]/g, " ").trim().split(/\s+/)[0] || "";
    if ((texts.yes_words || []).some((w) => fold(w).trim() === first)) return "yes";
    if ((texts.no_words || []).some((w) => fold(w).trim() === first)) return "no";
    return null;
  };

  // A spoken answer the app takes like a tap: exactly an option, or a plain number.
  const exactAnswer = (text, options, quantity) => {
    const said = fold(text).replace(/[.!]+$/, "").trim();
    if (options.some((o) => fold(o).trim() === said)) return true;
    return quantity && /^\d+([.,]\d+)?\s*(°\s*c|°|stupnu|stupne|stupen|c)?$/.test(said);
  };

  if (!core || !core.supported) return { classify, exactAnswer };

  class Stopped extends Error {}

  let on = false;
  let run = 0; // bumped on every stop, so a running loop notices and ends
  let stream = null;
  let voice = null;
  let stopRecording = null;

  const texts = () => {
    try {
      return JSON.parse($("audio-mode")?.dataset.texts || "{}");
    } catch (error) {
      return {};
    }
  };

  const pickVoice = () => {
    const voices = synth ? synth.getVoices() : [];
    voice = voices.find((v) => /^cs/i.test(v.lang) && v.localService) || null;
  };
  if (synth) {
    pickVoice();
    synth.addEventListener?.("voiceschanged", pickVoice);
  }

  // Two short sine notes; resolves when they have finished playing.
  let cues = null;
  const cue = (kind) => {
    const Context = window.AudioContext || window.webkitAudioContext;
    try {
      cues = cues && cues.state !== "closed" ? cues : new Context();
      if (cues.state === "suspended") cues.resume();
    } catch (error) {
      return Promise.resolve();
    }
    const notes = { listen: [660, 880], done: [880, 587], warm: [] }[kind] || [];
    const start = cues.currentTime + 0.02;
    notes.forEach((frequency, i) => {
      const at = start + i * 0.13;
      const osc = cues.createOscillator();
      const gain = cues.createGain();
      osc.type = "sine";
      osc.frequency.value = frequency;
      gain.gain.setValueAtTime(0.0001, at);
      gain.gain.exponentialRampToValueAtTime(0.25, at + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.12);
      osc.connect(gain).connect(cues.destination);
      osc.start(at);
      osc.stop(at + 0.13);
    });
    // The notes, plus a short gap so the microphone doesn't catch their tail.
    return new Promise((resolve) => setTimeout(resolve, notes.length * 130 + 120));
  };

  // --- The voice screen ------------------------------------------------------------------
  const icon = (name) =>
    `<svg class="icon" aria-hidden="true" focusable="false"><use href="#i-${name}"/></svg>`;
  const escape = (value) =>
    String(value).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  const behind = () =>
    [...document.body.children].filter((el) => el.id !== "voice-screen" && el.tagName !== "SCRIPT");

  const screen = () => {
    let el = $("voice-screen");
    if (el) return el;
    const t = texts();
    el = document.createElement("div");
    el.id = "voice-screen";
    el.className = "voice-screen";
    el.hidden = true;
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-modal", "true");
    el.setAttribute("aria-labelledby", "voice-screen-title");
    el.dataset.state = "on";
    el.innerHTML = `
      <div class="voice-top">
        <img class="logo" src="/static/logo.svg" alt="" width="40" height="40">
        <h2 id="voice-screen-title" tabindex="-1">${escape(t.screen_title)}</h2>
      </div>
      <div class="voice-stage">
        <div class="orb" aria-hidden="true"><span class="orb-cloud"></span><span class="orb-shine"></span></div>
        <p class="voice-state" id="voice-state"></p>
      </div>
      <p class="voice-caption" id="voice-caption" role="status" aria-live="polite"></p>
      <div class="voice-answers" id="voice-answers" hidden>
        <button type="button" class="chip chip-primary voice-answer" data-tap="yes">${escape(t.yes)}</button>
        <button type="button" class="chip voice-answer" data-tap="no">${escape(t.no)}</button>
      </div>
      <div class="voice-controls">
        <div class="voice-slot">
          <button type="button" class="round" id="audio-stop" hidden>${icon("mic")}<span>${escape(t.stop_listening)}</span></button>
        </div>
        <div class="voice-slot">
          <button type="button" class="round round-close" id="audio-close">${icon("close")}<span>${escape(t.close)}<span class="visually-hidden"> ${escape(t.screen_title.toLowerCase())}</span></span></button>
        </div>
      </div>`;
    document.body.appendChild(el);
    return el;
  };

  const openScreen = () => {
    const el = screen();
    el.hidden = false;
    behind().forEach((node) => (node.inert = true));
    document.body.classList.add("voice-open");
    $("voice-screen-title")?.focus();
  };

  const closeScreen = () => {
    const el = $("voice-screen");
    if (!el || el.hidden) return;
    el.hidden = true;
    behind().forEach((node) => (node.inert = false));
    document.body.classList.remove("voice-open");
    ($("result-heading") || $("audio-toggle"))?.focus();
  };

  // The yes / no buttons: shown while a yes or no is asked; a tap counts like the word.
  let tapped = null;
  const answers = (shown) => {
    tapped = null;
    const el = $("voice-answers");
    if (el) el.hidden = !shown;
  };

  const status = (message) => {
    const el = $("audio-status");
    if (el) el.textContent = message;
    const caption = $("voice-caption");
    if (caption) caption.textContent = message;
  };

  const check = (mine) => {
    if (!on || mine !== run) throw new Stopped();
  };

  // Speak sentence by sentence (some browsers cut long utterances). Resolves when done.
  const speak = async (text, mine) => {
    check(mine);
    const clean = (text || "").replace(/\s+/g, " ").trim();
    if (!clean || !voice || window.HLIDAC_AUDIO_SILENT) return;
    setState("speaking");
    for (const part of clean.split(/(?<=[.?!:])\s+/)) {
      if (tapped) break; // a tap on Ano / Ne doesn't wait for the rest
      check(mine);
      await new Promise((resolve) => {
        const utterance = new SpeechSynthesisUtterance(part);
        utterance.voice = voice;
        utterance.lang = voice.lang;
        utterance.rate = 0.95;
        utterance.onend = resolve;
        utterance.onerror = resolve;
        synth.speak(utterance);
      });
    }
    setState("on");
    check(mine);
  };

  const panel = () => $("audio-mode");

  const show = () => {
    const box = panel();
    if (!box) return;
    const t = texts();
    const stored = sessionStorage.getItem(KEY) === "on";
    const onResult = !!$("audio-result");
    box.hidden = onResult && !on && !stored && !$("audio-status")?.textContent;
    box.dataset.state = on ? box.dataset.state || "on" : "off";
    const label = $("audio-label");
    if (label) label.textContent = on ? t.turn_off : stored ? t.resume : t.turn_on;
    $("audio-toggle")?.setAttribute("aria-pressed", on ? "true" : "false");
    document.body.classList.toggle("audio-on", on);
    const hint = $("audio-hint");
    if (hint) hint.hidden = on;
  };

  const setState = (state) => {
    const box = panel();
    if (box) box.dataset.state = state;
    const el = $("voice-screen");
    if (el) {
      el.dataset.state = state;
      if (state !== "listening") el.style.setProperty("--level", "0");
    }
    const stop = $("audio-stop");
    if (stop) stop.hidden = state !== "listening";
    const t = texts();
    const word = { speaking: t.state_speaking, listening: t.state_listening, working: t.state_working };
    const label = $("voice-state");
    if (label) label.textContent = word[state] || "";
  };

  // Record until the speech is over (or "Hotovo"); returns the blob, or null when stopped.
  // `question`: what is being asked now, kept on screen while listening (not only spoken).
  const listen = async (mine, question) => {
    check(mine);
    if (tapped) return null;
    await cue("listen");
    if (tapped) return null;
    return record(mine, question);
  };

  const record = (mine, question) =>
    new Promise((resolve, reject) => {
      check(mine);
      const chunks = [];
      const recorder = new MediaRecorder(stream);
      const bars = [...(panel()?.querySelectorAll(".wave i") || [])];
      let meter = null;
      try {
        meter = core.meter(stream, bars);
      } catch (error) {
        meter = null;
      }
      const ended = core.silence({ quietMs: 2500 });
      const orb = $("voice-screen");
      const timer = setInterval(() => {
        if (!meter) return;
        const level = meter.level();
        if (orb) orb.style.setProperty("--level", Math.min(1, level * 12).toFixed(2));
        if (ended(level, performance.now())) finish();
      }, 50);
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        clearInterval(timer);
        if (recorder.state !== "inactive") recorder.stop();
      };
      stopRecording = finish;
      recorder.addEventListener("dataavailable", (event) => {
        if (event.data && event.data.size) chunks.push(event.data);
      });
      recorder.addEventListener("stop", () => {
        if (meter) meter.stop();
        stopRecording = null;
        setState("on");
        if (!on || mine !== run) reject(new Stopped());
        else resolve(new Blob(chunks, { type: recorder.mimeType }));
      });
      recorder.start();
      setState("listening");
      // The caption keeps what is being asked; "Poslouchám" is under the orb. In the chat's
      // own status line (outside the voice screen) both are written out.
      const caption = $("voice-caption");
      status(question ? `${question} ${texts().listening_short}` : texts().listening);
      if (caption && question) caption.textContent = question;
    });

  // Listen and transcribe until something was heard; returns the text.
  const hear = async (mine, question) => {
    for (;;) {
      const blob = await listen(mine, question);
      if (tapped) return { tap: tapped };
      cue("done"); // plays while the upload starts; nothing records now
      setState("working");
      status(texts().working);
      const box = panel();
      const reply = await core.transcribe(box.dataset.url, box.dataset.csrf, blob);
      check(mine);
      setState("on");
      if (reply.text) return reply.text;
      status(reply.error);
      await speak(reply.error, mine);
    }
  };

  const yesOrNo = async (mine, question) => {
    let asking = question;
    answers(true);
    for (;;) {
      const heard = await hear(mine, asking);
      const answer = heard.tap || classify(heard, texts());
      if (answer) {
        answers(false);
        return answer;
      }
      asking = texts().unclear;
      status(asking);
      await speak(asking, mine);
    }
  };

  // Load the next step in place. False when the answer left the patient pages (e.g. logged out).
  const swap = async (response, mine) => {
    const html = await response.text();
    check(mine);
    const doc = new DOMParser().parseFromString(html, "text/html");
    const next = doc.querySelector("main#obsah");
    const url = new URL(response.url, location.href);
    if (!next || !url.pathname.startsWith("/pacient")) {
      location.assign(response.url);
      return false;
    }
    document.querySelector("main#obsah").innerHTML = next.innerHTML;
    document.title = doc.title;
    history.replaceState(null, "", url.pathname + url.search);
    show();
    document.dispatchEvent(new CustomEvent("hlidac:swap"));
    return true;
  };

  const post = async (form, extra, mine) => {
    const data = new FormData(form);
    Object.entries(extra || {}).forEach(([key, value]) => data.set(key, value));
    const response = await fetch(form.action, {
      method: "POST",
      body: data,
      credentials: "same-origin",
    });
    return swap(response, mine);
  };

  const get = async (href, mine) =>
    swap(await fetch(href, { credentials: "same-origin" }), mine);

  // A spoken answer to the step on screen: read back and confirmed (G5), unless it is exactly an
  // option or a number for a question; those are confirmed with all the others in the closing
  // summary "Rozuměl jsem správně?" (D58), which is read aloud in full before anything is sent.
  const answer = async (step, mine, question = step.dataset.audioSay) => {
    const t = texts();
    const options = JSON.parse(step.dataset.audioOptions || "[]");
    const quantity = step.dataset.audioQuantity === "1";
    for (;;) {
      answers(false);
      const said = await hear(mine, question);
      const direct = step.dataset.audioDirect === "1" && exactAnswer(said, options, quantity);
      if (!direct) {
        const words = said.replace(/[\s.!?…]+$/, "");
        const prompt = `${t.read_back} „${words}“. ${t.send_question}`;
        status(prompt);
        await speak(prompt, mine);
        if ((await yesOrNo(mine, prompt)) === "no") {
          status(t.again);
          await speak(t.again, mine);
          continue;
        }
      }
      const form = document.querySelector('form[data-audio="message"]');
      return post(form, { text: said, voice: "1" }, mine);
    }
  };

  const loop = async (mine) => {
    for (;;) {
      check(mine);
      const step = document.querySelector("[data-audio-kind]");
      if (!step) return stop();
      const kind = step.dataset.audioKind;
      const t = texts();
      if (!voice) status(t.no_voice);
      status(step.dataset.audioSay); // the caption: what is being said, always shown
      await speak(step.dataset.audioSay, mine);
      if (kind === "urgent") return stop(t.stopped_urgent); // shows the urgent screen behind
      if (kind === "result") return stop(t.off);
      let moved = true;
      if (kind === "confirm") {
        moved =
          (await yesOrNo(mine, step.dataset.audioSay)) === "yes"
            ? await post(document.querySelector('form[data-audio="yes"]'), {}, mine)
            : await get(document.querySelector('a[data-audio="no"]').href, mine);
      } else if (kind === "summary") {
        if ((await yesOrNo(mine, step.dataset.audioSay)) === "yes") {
          moved = await post(document.querySelector('form[data-audio="send"]'), {}, mine);
        } else {
          status(t.add_more);
          await speak(t.add_more, mine);
          moved = await answer(step, mine, t.add_more);
        }
      } else {
        moved = await answer(step, mine);
      }
      if (!moved) return;
    }
  };

  const start = async () => {
    const t = texts();
    pickVoice();
    cue("warm"); // inside the tap: lets the page play the cues from now on (silent: no notes)
    if (synth) synth.cancel();
    try {
      stream = stream || (await navigator.mediaDevices.getUserMedia({ audio: true }));
    } catch (error) {
      status(t.no_mic);
      return;
    }
    on = true;
    const mine = ++run;
    sessionStorage.setItem(KEY, "on");
    show();
    openScreen();
    setState("on");
    status(t.on);
    loop(mine).catch((error) => {
      if (!(error instanceof Stopped)) stop(t.off);
    });
  };

  const stop = (message) => {
    on = false;
    if (cues) cues.close().catch(() => {});
    cues = null;
    run += 1;
    if (synth) synth.cancel();
    if (stopRecording) stopRecording();
    if (stream) stream.getTracks().forEach((track) => track.stop());
    stream = null;
    sessionStorage.removeItem(KEY);
    answers(false);
    setState("off");
    if (message) status(message);
    show();
    closeScreen();
  };

  // One handler for the page's lifetime: the panel's buttons are replaced on every swap.
  document.addEventListener("click", (event) => {
    if (event.target.closest("#audio-toggle")) {
      if (on) stop(texts().off);
      else start();
    } else if (event.target.closest("#audio-stop")) {
      if (stopRecording) stopRecording();
    } else if (event.target.closest("#audio-close")) {
      stop(texts().off);
    } else if (event.target.closest("[data-tap]")) {
      tapped = event.target.closest("[data-tap]").dataset.tap;
      if (synth) synth.cancel();
      if (stopRecording) stopRecording();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && on) stop(texts().off);
  });
  window.addEventListener("pagehide", () => {
    if (synth) synth.cancel();
  });

  show();
  return { classify, exactAnswer, stop, get on() { return on; } };
})();
