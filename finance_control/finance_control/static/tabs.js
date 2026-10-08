'use strict';

const cockpitNav = document.querySelector('nav[aria-label="Bereiche"]');
const cockpitAnnouncement = document.getElementById('active-area-announcement');
const cockpitAreas = {
  dashboard: 'Buchungen', 'category-outflows': 'Abgänge nach Kategorie', 'plan-actual': 'Plan & Ist', classification: 'Buchungen bearbeiten', planning: 'Planung',
  scenarios: 'Planvarianten', accounts: 'Konten', finanzguru: 'Finanzguru-Import',
  intake: 'Vorbereiteter Import', documents: 'Belege', overview: 'Überblick',
  wealth: 'Vermögen', monthly: 'Monatsauswertung', approvals: 'Freigaben', analytics: 'Auswertung',
};
let cockpitPrimary = 'plan-actual';
let cockpitAuxiliary = false;
let cockpitReturnFocus = null;

function cockpitTabs() {
  return [...cockpitNav.querySelectorAll('a[href^="#"]')];
}

function cockpitShowTab(id, {focus = false} = {}) {
  const area = Object.hasOwn(cockpitAreas, id) ? id : 'plan-actual';
  const tabs = cockpitTabs();
  const primary = tabs.some(tab => tab.hash === '#' + area);
  const returning = cockpitAuxiliary && primary;
  if (!primary && !cockpitAuxiliary)
    cockpitReturnFocus = document.activeElement === document.body ? null : document.activeElement;
  if (primary) cockpitPrimary = area;
  cockpitAuxiliary = !primary;
  for (const [key] of Object.entries(cockpitAreas)) {
    const panel = document.getElementById(key);
    if (panel) panel.hidden = key !== area;
  }
  let selected;
  for (const tab of tabs) {
    const active = tab.hash === '#' + cockpitPrimary;
    tab.setAttribute('role', 'tab'); tab.id = 'tab-' + tab.hash.slice(1);
    tab.setAttribute('aria-controls', tab.hash.slice(1));
    tab.setAttribute('aria-selected', String(active));
    if (active) { tab.setAttribute('aria-current', 'page'); selected = tab; }
    else tab.removeAttribute('aria-current');
    tab.tabIndex = active ? 0 : -1;
    const panel = document.getElementById(tab.hash.slice(1));
    if (panel) { panel.setAttribute('role', 'tabpanel'); panel.setAttribute('aria-labelledby', tab.id); }
  }
  const auxiliary = document.getElementById('cockpit-auxiliary');
  if (auxiliary) auxiliary.hidden = primary;
  const title = document.getElementById('cockpit-auxiliary-title');
  if (title) title.textContent = ['planning', 'scenarios'].includes(area) ? 'Planung'
    : ['finanzguru', 'intake'].includes(area) ? 'Import' : cockpitAreas[area];
  const returnButton = document.getElementById('cockpit-return');
  if (returnButton) returnButton.textContent = `Zurück zu ${cockpitAreas[cockpitPrimary]}`;
  for (const [group, ids] of [['cockpit-planning-tools', ['planning', 'scenarios']],
    ['cockpit-import-tools', ['finanzguru', 'intake']]]) {
    const controls = document.getElementById(group);
    if (controls) controls.hidden = !ids.includes(area);
  }
  for (const button of document.querySelectorAll('[data-cockpit-target]'))
    button.setAttribute('aria-pressed', String(button.dataset.cockpitTarget === area));
  if (cockpitAnnouncement) {
    const text = `${primary ? 'Aktueller Bereich' : 'Werkzeug'}: ${cockpitAreas[area]}.`;
    if (cockpitAnnouncement.textContent !== text) cockpitAnnouncement.textContent = text;
  }
  if (returning && cockpitReturnFocus?.isConnected && !cockpitReturnFocus.closest('[hidden]'))
    cockpitReturnFocus.focus();
  else if (focus || returning) (primary ? selected : returnButton)?.focus();
  if (returning) cockpitReturnFocus = null;
  document.dispatchEvent(new CustomEvent('cockpit-area-changed', {detail: {id: area}}));
}

function cockpitNavigate(id, {focus = false} = {}) {
  const area = Object.hasOwn(cockpitAreas, id) ? id : 'plan-actual';
  if (location.hash !== '#' + area) location.hash = '#' + area;
  cockpitShowTab(area, {focus});
}

function cockpitReturn() {
  cockpitNavigate(cockpitPrimary, {focus: true});
}

cockpitNav.setAttribute('role', 'tablist');
cockpitNav.setAttribute('aria-orientation', 'horizontal');
cockpitNav.addEventListener('keydown', event => {
  const tabs = cockpitTabs(), index = tabs.indexOf(document.activeElement);
  if (index < 0) return;
  let next;
  if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
  else if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
  else if (event.key === 'Home') next = 0;
  else if (event.key === 'End') next = tabs.length - 1;
  else return;
  event.preventDefault(); cockpitNavigate(tabs[next].hash.slice(1), {focus: true});
});
for (const button of document.querySelectorAll('[data-cockpit-target]'))
  button.addEventListener('click', () => cockpitNavigate(button.dataset.cockpitTarget, {focus: true}));
document.getElementById('cockpit-return')?.addEventListener('click', cockpitReturn);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && cockpitAuxiliary && !document.querySelector('dialog[open]')) {
    event.preventDefault(); cockpitReturn();
  }
});
window.addEventListener('hashchange', () => cockpitShowTab(location.hash.slice(1)));
if (location.hash === '#classification') history.replaceState(null, '', '#dashboard');
else if (!Object.hasOwn(cockpitAreas, location.hash.slice(1))) location.hash = '#plan-actual';
cockpitShowTab(location.hash.slice(1));
