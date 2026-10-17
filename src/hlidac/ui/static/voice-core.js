// Shared voice helpers (WP17, D59), used by voice.js (the "Namluvit" button) and audio.js (the
// hands-free audio mode). Nothing here sends audio anywhere except the app's own `/prepis`
// endpoint, which transcribes it with the configured transcriber (D44, D52) and keeps nothing.
//
// - toWav: the browser decodes its own recording (webm in Chrome, mp4 in Safari) and resamples it
//   to a 16 kHz mono 16-bit WAV, the one format the app accepts (`transcription.read_wav`).
// - transcribe: uploads the WAV with the CSRF header; returns { text } or { error } (approved
//   texts from the server).
// - silence: a pure end-of-speech detector on input levels (unit-tested in the browser run): the
//   recording ends after speech followed by `quietMs` of quiet; before any speech it never ends
//   (no time limit; the patient can always stop it by hand).
window.HlidacVoice = (() => {
  "use strict";

  const RATE = 16000;
  const Context = window.AudioContext || window.webkitAudioContext;

  const supported =
    !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia) &&
    typeof window.MediaRecorder === "function" &&
    typeof window.OfflineAudioContext === "function" &&
    typeof Context === "function";

  const wav = (samples) => {
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);
    const ascii = (offset, s) => [...s].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
    ascii(0, "RIFF");
    view.setUint32(4, 36 + samples.length * 2, true);
    ascii(8, "WAVE");
    ascii(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true); // PCM
    view.setUint16(22, 1, true); // mono
    view.setUint32(24, RATE, true);
    view.setUint32(28, RATE * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    ascii(36, "data");
    view.setUint32(40, samples.length * 2, true);
    for (let i = 0; i < samples.length; i++) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return new Blob([buffer], { type: "audio/wav" });
  };

  const toWav = async (blob) => {
    const context = new Context();
    try {
      const decoded = await context.decodeAudioData(await blob.arrayBuffer());
      const length = Math.max(1, Math.ceil(decoded.duration * RATE));
      const offline = new OfflineAudioContext(1, length, RATE);
      const source = offline.createBufferSource();
      source.buffer = decoded; // a mono destination mixes every channel down
      source.connect(offline.destination);
      source.start();
      const rendered = await offline.startRendering();
      return wav(rendered.getChannelData(0));
    } finally {
      context.close();
    }
  };

  const FAILED = "Přepis se nepovedl. Zkuste to prosím znovu, nebo text napište.";

  const transcribe = async (url, csrf, blob) => {
    try {
      const audio = await toWav(blob);
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "audio/wav", "X-CSRF-Token": csrf },
        body: audio,
        credentials: "same-origin",
      });
      const reply = await response.json().catch(() => ({}));
      if (!response.ok || !reply.text) return { error: reply.error || FAILED };
      return { text: reply.text };
    } catch (error) {
      return { error: "Nahrávku se nepodařilo zpracovat. Zkuste to prosím znovu, nebo text napište." };
    }
  };

  // Live input level of a stream: `level()` gives the RMS (0..1) of the latest frame; `bars`
  // (optional) are animated unless the patient prefers reduced motion.
  const meter = (stream, bars = []) => {
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const context = new Context();
    const analyser = context.createAnalyser();
    analyser.fftSize = 1024;
    context.createMediaStreamSource(stream).connect(analyser);
    const wave = new Float32Array(analyser.fftSize);
    const freq = new Uint8Array(analyser.frequencyBinCount);
    const step = Math.max(1, Math.floor(freq.length / 16 / (bars.length + 1)));
    let frame = 0;
    const draw = () => {
      analyser.getByteFrequencyData(freq);
      bars.forEach((bar, i) => {
        const value = freq[(i + 1) * step] / 255;
        bar.style.transform = `scaleY(${Math.max(0.15, value).toFixed(2)})`;
      });
      frame = requestAnimationFrame(draw);
    };
    if (bars.length && !still) draw();
    return {
      context,
      level() {
        analyser.getFloatTimeDomainData(wave);
        let sum = 0;
        for (const v of wave) sum += v * v;
        return Math.sqrt(sum / wave.length);
      },
      stop() {
        cancelAnimationFrame(frame);
        bars.forEach((bar) => (bar.style.transform = ""));
        context.close();
      },
    };
  };

  // End-of-speech detector. Feed it (level, nowMs); it returns true once the speech is over.
  // The threshold adapts to the room: speech is a level well above the quietest level seen.
  const silence = ({ quietMs = 2500, minSpeechMs = 300, floor = 0.01 } = {}) => {
    let noise = null;
    let speechMs = 0;
    let last = null;
    let quietSince = null;
    return (level, now) => {
      const dt = last === null ? 0 : now - last;
      last = now;
      noise = noise === null ? level : Math.min(noise * 1.002 + 0.0001, Math.max(level, 0));
      const loud = level > Math.max(floor, noise * 3);
      if (loud) {
        speechMs += dt;
        quietSince = null;
        return false;
      }
      if (speechMs < minSpeechMs) return false; // nothing said yet: keep listening, no limit
      if (quietSince === null) quietSince = now;
      return now - quietSince >= quietMs;
    };
  };

  return { supported, toWav, transcribe, meter, silence, RATE };
})();
