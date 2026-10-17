// Voice input (WP17, accessibility.md §3): one tap starts recording, another stops it (no
// press-and-hold, no time limit). The recording becomes a 16 kHz WAV and is transcribed by the app
// (voice-core.js); the text lands in the text box, where the patient checks and corrects it before
// sending (G5). Nothing is sent without that. Without microphone support the voice controls stay
// hidden and the text form works as is. On a number question (`data-target="number"`) the first
// number heard goes into the number field instead, again only to check and send with Pokračovat.
// While recording, the bars show the live input level (none with reduced motion); the state is
// always in the button's text and the status. Re-binds when audio mode swaps the page content.
(() => {
  "use strict";

  const core = window.HlidacVoice;
  if (!core || !core.supported) return;

  const bind = () => {
    const box = document.getElementById("voice");
    if (!box || box.dataset.bound) return;
    box.dataset.bound = "1";
    const toggle = document.getElementById("voice-toggle");
    const label = document.getElementById("voice-label");
    const status = document.getElementById("voice-status");
    const text = document.getElementById("text") || document.createElement("textarea");
    const used = document.getElementById("voice-used");
    const badge = document.getElementById("voice-badge");
    const number = document.getElementById("number");
    const forNumber = box.dataset.target === "number" && !!number;
    const bars = [...box.querySelectorAll(".wave i")];
    box.hidden = false;

    let state = "idle"; // idle | recording | working
    let recorder = null;
    let chunks = [];
    let level = null;

    const say = (message) => {
      if (status) status.textContent = message;
    };

    const setState = (next) => {
      state = next;
      box.dataset.state = next;
      label.textContent = next === "recording" ? "Ukončit nahrávání" : "Namluvit";
      toggle.setAttribute("aria-disabled", next === "working" ? "true" : "false");
    };

    const send = async (blob) => {
      setState("working");
      say("Přepisuji nahrávku, chvíli strpení…");
      try {
        const reply = await core.transcribe(box.dataset.url, box.dataset.csrf, blob);
        if (reply.error) {
          say(reply.error);
          return;
        }
        if (forNumber) {
          const found = reply.text.match(/\d+(?:[.,]\d+)?/);
          if (!found) {
            say(`Rozuměli jsme: „${reply.text}“. Číslo jsme v tom nenašli. Napište ho prosím.`);
            number.focus();
            return;
          }
          number.value = found[0].replace(".", ",");
          say(`Rozuměli jsme: ${number.value}. Zkontrolujte číslo a klepněte na Pokračovat.`);
          number.focus();
          return;
        }
        text.value = text.value.trim() ? `${text.value.trim()} ${reply.text}` : reply.text;
        if (used) used.value = "1";
        if (badge) badge.hidden = false;
        // The text itself is in the box, which gets focus and is read out; no need to repeat it.
        say("Rozuměli jsme. Zkontrolujte text, případně ho opravte, a pak ho odešlete.");
        text.focus();
      } finally {
        setState("idle");
      }
    };

    const start = async () => {
      let stream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch (error) {
        say("Mikrofon není povolený. Povolte ho v prohlížeči, nebo text napište.");
        return;
      }
      chunks = [];
      recorder = new MediaRecorder(stream);
      recorder.addEventListener("dataavailable", (event) => {
        if (event.data && event.data.size) chunks.push(event.data);
      });
      recorder.addEventListener("stop", () => {
        if (level) level.stop();
        level = null;
        stream.getTracks().forEach((track) => track.stop());
        send(new Blob(chunks, { type: recorder.mimeType }));
      });
      recorder.start();
      try {
        level = core.meter(stream, bars);
      } catch (error) {
        level = null;
      }
      setState("recording");
      say("Nahrávám. Až domluvíte, klepněte na Ukončit nahrávání.");
    };

    toggle.addEventListener("click", () => {
      if (state === "idle") start();
      else if (state === "recording" && recorder) recorder.stop();
    });
  };

  bind();
  document.addEventListener("hlidac:swap", bind);
})();
