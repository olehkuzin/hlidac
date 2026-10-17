// The urgent screen moves focus to its heading (accessibility.md §3, 4.1.3). `autofocus` on the
// heading is the fallback; some Safari and VoiceOver versions ignore it after a redirect.
// An external file, so a strict CSP without inline script still allows it (WP31).
document.getElementById("result-heading")?.focus();
