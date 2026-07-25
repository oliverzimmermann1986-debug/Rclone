"use strict";

window.RcloneUI = Object.freeze({
  storedChoice(key, allowed, fallback) {
    try {
      const value = window.localStorage.getItem(key);
      return allowed.includes(value) ? value : fallback;
    } catch (_) {
      return fallback;
    }
  },
  store(key, value) {
    try { window.localStorage.setItem(key, value); } catch (_) { /* optional */ }
  },
  prefersCompact() {
    return window.matchMedia?.('(max-width: 680px)').matches || false;
  },
  focusMain() {
    window.requestAnimationFrame(() => document.getElementById('main-content')?.focus({ preventScroll: true }));
  },
});
