'use strict';
// Shared preference only. Run before CSS to avoid an incorrect first paint.
(() => {
  const key = 'finance-preview-theme';
  const root = document.documentElement;
  let current = 'dark';
  const normalize = value => value === 'light' ? 'light' : 'dark';

  function apply(value) {
    const next = normalize(value), changed = current !== next;
    current = next;
    root.dataset.theme = next;
    if (document.body) document.body.classList.toggle('light', next === 'light');
    if (changed) window.dispatchEvent(new Event('finance-theme-change'));
  }

  function set(value) {
    if (value !== 'light' && value !== 'dark') return false;
    apply(value);
    try { localStorage.setItem(key, value); } catch { /* Preference remains usable in this tab. */ }
    return true;
  }

  try { apply(localStorage.getItem(key)); } catch { apply('dark'); }
  window.addEventListener('storage', event => {
    if (event.key === key || event.key === null) apply(event.newValue);
  });
  document.addEventListener('DOMContentLoaded', () => apply(current), {once: true});
  window.financeTheme = Object.freeze({get: () => current, set, applyCurrent: () => apply(current)});
})();
