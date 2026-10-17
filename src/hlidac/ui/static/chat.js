// The chat (patient check-in): after each reply the page reloads (or audio mode swaps its
// content) with the next question as its heading. Bring it into view and move focus to it, so a
// screen reader reads the new question; on the urgent screen focus goes to its heading.
(() => {
  "use strict";
  const focusPrompt = () => {
    const urgent = document.getElementById("result-heading");
    if (urgent) {
      urgent.focus();
      return;
    }
    const prompt = document.getElementById("prompt");
    if (!prompt) return;
    const error = document.getElementById("text-error");
    if (error) {
      document.getElementById("text")?.focus();
      return;
    }
    prompt.closest(".msg-current")?.scrollIntoView({ block: "center" });
    if (document.querySelector(".chat-log")) prompt.focus({ preventScroll: true });
  };
  focusPrompt();
  document.addEventListener("hlidac:swap", focusPrompt);
})();
