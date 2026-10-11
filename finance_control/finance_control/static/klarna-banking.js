'use strict';

(() => {
  const node = (tag, text = '', className = '') => {
    const element = document.createElement(tag);
    element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const routes = {
    state: '/api/administration/klarna-state', prepare: '/api/administration/klarna-prepare',
    configure: '/api/administration/klarna-configure', authorize: '/api/administration/klarna-authorize',
    disconnect: '/api/administration/klarna-disconnect', read: '/api/administration/klarna-read',
  };
  const phases = {UNCONFIGURED: 'Noch nicht eingerichtet', CERTIFICATE_READY: 'Zertifikat bereit',
    APPLICATION_READY: 'Anwendung gespeichert', AUTHORIZED: 'Freigabe erteilt'};
  const safe = (value, max = 180) => typeof value === 'string' && value.length <= max ? value : '';
  const validDate = value => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const parsed = Date.parse(`${value}T00:00:00Z`);
    return Number.isFinite(parsed) && new Date(parsed).toISOString().slice(0, 10) === value;
  };
  const providerUrl = value => {
    if (typeof value !== 'string' || value.length > 4096 || /[\\\s\u0000-\u001f\u007f-\u009f]/u.test(value) || value.includes('#')) return null;
    const match = value.match(/^https:\/\/([^/?#]*)/i);
    if (!match || match[1].includes('@')) return null;
    const parts = match[1].match(/^([^:]+)(?::([0-9]+))?$/);
    const hosts = ['auth.enablebanking.com', 'tilisy.enablebanking.com'];
    if (!parts || !hosts.includes(parts[1].toLowerCase()) || (parts[2] && parts[2] !== '443')) return null;
    try {
      const url = new URL(value);
      return url.protocol === 'https:' && hosts.includes(url.hostname) && !url.username && !url.password
        && (!url.port || url.port === '443') && !url.hash ? url : null;
    } catch (_) { return null; }
  };
  const renderGenerations = new WeakMap();
  function nextGeneration(host) {
    const generation = (renderGenerations.get(host) || 0) + 1;
    renderGenerations.set(host, generation);
    return generation;
  }
  let dashboardMounted = false;
  let dashboardPending = false;
  let dashboardController = null;
  function clearDashboardPreview() {
    dashboardMounted = false;
    dashboardController?.abort(); dashboardController = null;
    document.getElementById('dashboard-klarna-host')?.replaceChildren();
  }
  function mountDashboardPreview() {
    const host = document.getElementById('dashboard-klarna-host');
    if (!host || dashboardMounted) return;
    if (typeof state === 'undefined' || !state?.csrf) { dashboardPending = true; return; }
    dashboardPending = false; dashboardMounted = true;
    dashboardController = new AbortController();
    void renderBookings(host, dashboardController.signal);
  }
  document.addEventListener('cockpit-area-changed', event => {
    if (event.detail?.id === 'dashboard') mountDashboardPreview();
    else { dashboardPending = false; clearDashboardPreview(); }
  });
  document.addEventListener('finance-refreshed', () => {
    if (dashboardPending && location.hash === '#dashboard') mountDashboardPreview();
  });
  document.querySelector('form.logout')?.addEventListener('submit', () => {
    dashboardPending = false; clearDashboardPreview();
  }, {once: true});
  if (location.hash === '#dashboard') {
    dashboardPending = true;
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mountDashboardPreview, {once: true});
    else mountDashboardPreview();
  }

  const current = (host, root, generation, signal) => !signal?.aborted && generation === renderGenerations.get(host)
    && host.isConnected && host.contains(root);
  async function loadState(host, root, status, generation, signal) {
    try {
      const state = await window.api(routes.state, {});
      if (!current(host, root, generation, signal)) return null;
      if (!state || typeof state !== 'object' || typeof state.supported !== 'boolean') throw new Error('invalid');
      return state;
    } catch (error) {
      if (current(host, root, generation, signal)) status.textContent = safe(error?.message, 240) || 'Klarna-Einrichtung konnte nicht geladen werden.';
      return null;
    }
  }
  function message(target, text, error = false) { target.textContent = text; target.classList.toggle('kb-error', error); }
  function confirmation(parent, label) {
    const wrap = node('label', '', 'kb-check'); const input = document.createElement('input');
    input.type = 'checkbox'; wrap.append(input, node('span', label)); parent.append(wrap); return input;
  }
  async function perform(button, status, route, payload, generation, host, root, signal, onSuccess) {
    button.disabled = true; message(status, 'Bitte warten.');
    try {
      const result = await window.api(route, payload);
      if (!current(host, root, generation, signal)) return;
      await onSuccess?.(result);
    } catch (error) {
      if (current(host, root, generation, signal)) message(status, safe(error?.message, 240) || 'Die Anfrage konnte nicht abgeschlossen werden. Bitte prüfe den Serverstatus.', true);
    } finally { if (button.isConnected && !signal?.aborted) button.disabled = false; }
  }
  async function refresh(host, root, status, generation, signal, draw) {
    const updated = await loadState(host, root, status, generation, signal);
    if (updated && current(host, root, generation, signal)) { root.replaceChildren(); message(status, ''); draw(updated); }
  }
  function renderAdministration(host) {
    if (!host || typeof host.replaceChildren !== 'function') return;
    const generation = nextGeneration(host);
    host.replaceChildren(); host.classList.add('kb-host');
    const root = node('div', '', 'kb-root');
    const status = node('p', 'Klarna-Einrichtung wird geladen.', 'kb-status');
    status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite'); root.append(status); host.append(root);
    void (async () => {
      const state = await loadState(host, root, status, generation);
      if (state && current(host, root, generation)) { message(status, ''); renderSetup(host, root, status, state, generation); }
    })();
  }
  function renderSetup(host, root, status, state, generation) {
    if (!state.supported) { root.append(node('p', 'Diese Serverinstallation unterstützt die Klarna-Einrichtung nicht.', 'kb-note')); return; }
    const phase = phases[state.setup];
    if (!phase) { message(status, 'Der Einrichtungsstatus ist ungültig.', true); return; }
    const card = node('section', '', 'kb-card'); card.append(node('h3', 'Klarna-Kontozugriff', 'kb-heading'));
    card.append(node('p', `Status: ${phase}`, 'kb-note'));
    card.append(node('p', 'Die Freigabe erfolgt bei Klarna. Gib hier keine Klarna-Nutzerkennung und kein Passwort ein.', 'kb-note'));
    card.append(node('p', 'Dieser Kontenabruf zeigt keine vollständige Einkaufs- oder Ratenübersicht. Fälligkeiten und Ratenfelder werden nicht aus Buchungstexten abgeleitet. Der unterstützte automatische Datenweg für diese Angaben ist noch ungeklärt.', 'kb-info'));
    if (state.setup === 'UNCONFIGURED') {
      const check = confirmation(card, 'Ich bestätige, dass der Server ein öffentliches Zertifikat für meine eigene Einrichtung erzeugen darf.');
      const button = node('button', '1. Zertifikat erzeugen'); const action = node('p', '', 'kb-action-status');
      button.addEventListener('click', () => {
        if (!check.checked) { message(action, 'Bitte bestätige zuerst die Zertifikatserzeugung.', true); return; }
        void perform(button, action, routes.prepare, {confirmed: true}, generation, host, root, null, async result => {
          if (!safe(result?.certificate_pem, 20000).includes('BEGIN CERTIFICATE')) { message(action, 'Der Server hat kein gültiges öffentliches Zertifikat geliefert.', true); return; }
          message(action, 'Zertifikat erstellt. Lade es herunter und registriere die Anwendung bei Enable Banking.');
          await refresh(host, root, status, generation, null, updated => renderSetup(host, root, status, updated, generation));
        });
      }); card.append(button, action);
    }
    if (state.setup === 'CERTIFICATE_READY') {
      const certificate = safe(state.certificate_pem, 20000);
      if (certificate.includes('BEGIN CERTIFICATE')) {
        const link = node('a', 'Öffentliches Zertifikat herunterladen (.crt)');
        link.href = URL.createObjectURL(new Blob([certificate.endsWith('\n') ? certificate : `${certificate}\n`], {type: 'application/x-x509-ca-cert'}));
        link.download = 'finance-control-klarna.crt'; link.addEventListener('click', () => setTimeout(() => URL.revokeObjectURL(link.href), 1000), {once: true}); card.append(link);
      }
      const provider = node('a', 'Enable Banking öffnen'); provider.href = 'https://enablebanking.com/sign-in/'; provider.target = '_blank'; provider.rel = 'noopener noreferrer'; card.append(provider);
      card.append(node('p', 'Registriere eine eigene Anwendung in PRODUCTION mit dem Dienst AIS, dem öffentlichen Zertifikat und der angezeigten Rückkehradresse. Finance Control verwendet ausschließlich diese eigene Anwendung.', 'kb-note'));
      card.append(node('p', 'Verknüpfe anschließend Deine eigenen Klarna-Konten im Enable-Banking-Portal mit genau dieser Anwendung. Das ist ein eigener Einrichtungsschritt und Voraussetzung dafür, dass die Freigabe hier funktioniert.', 'kb-note'));
      card.append(node('p', `Rückkehradresse: ${safe(state.callback_url, 2048) || 'Nicht ausgewiesen'}`, 'kb-note'));
      card.append(node('p', 'Prüfe Bedingungen und mögliche Kosten direkt bei Enable Banking. Finance Control stimmt keinen kostenpflichtigen Bedingungen zu.', 'kb-note'));
      const form = node('form', '', 'kb-form'); const label = node('label', 'Enable-Banking-App-ID'); const input = document.createElement('input');
      input.name = 'application_id'; input.maxLength = 256; input.autocomplete = 'off'; input.required = true; label.append(input); form.append(label);
      const check = confirmation(form, 'Ich bestätige, dass ich die App-ID meiner eigenen Enable-Banking-Anwendung speichere.');
      const button = node('button', '2. App-ID speichern'); const action = node('p', '', 'kb-action-status'); form.append(button, action);
      form.addEventListener('submit', event => { event.preventDefault(); const id = input.value.trim();
        if (!check.checked) { message(action, 'Bitte bestätige zuerst das Speichern.', true); return; }
        if (!id) { message(action, 'Bitte gib die App-ID ein.', true); return; } input.value = '';
        void perform(button, action, routes.configure, {confirmed: true, application_id: id}, generation, host, root, null,
          () => refresh(host, root, status, generation, null, updated => renderSetup(host, root, status, updated, generation)));
      }); card.append(form);
    }
    if (state.setup === 'APPLICATION_READY') {
      card.append(node('p', 'Anbieterland: Deutschland (DE). Klarna-Freigaben werden für Deutschland angefragt.', 'kb-note'));
      const button = node('button', '3. Klarna-Freigabe öffnen'); const action = node('p', '', 'kb-action-status');
      button.addEventListener('click', () => void perform(button, action, routes.authorize, {confirmed: true, country: 'DE'}, generation, host, root, null, result => {
        const url = providerUrl(result?.authorization_url);
        if (!url) { message(action, 'Der Server hat keinen gültigen Enable-Banking-Freigabelink geliefert.', true); return; }
        const link = node('a', 'Freigabe bei Enable Banking öffnen'); link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; card.append(link);
        const update = node('button', 'Freigabestatus aktualisieren', 'kb-secondary'); update.addEventListener('click', () => void refresh(host, root, status, generation, null, fresh => renderSetup(host, root, status, fresh, generation))); card.append(update);
        message(action, 'Öffne den offiziellen Freigabelink und bestätige dort den Kontozugriff.');
      })); card.append(button, action);
    }
    if (state.setup === 'AUTHORIZED') {
      card.append(node('p', `Freigegebene Konten: ${Array.isArray(state.accounts) ? state.accounts.length : 0}`, 'kb-note'));
      if (safe(state.expires_at, 80)) card.append(node('p', `Freigabe gültig bis: ${state.expires_at}`, 'kb-note'));
      if (state.last_read) card.append(node('p', `Letzter Abruf: ${safe(state.last_read, 80) || 'Zeitpunkt nicht ausgewiesen'}`, 'kb-note'));
    }
    if (['CERTIFICATE_READY', 'APPLICATION_READY', 'AUTHORIZED'].includes(state.setup)) {
      const active = state.setup === 'AUTHORIZED'; const check = confirmation(card, active ? 'Ich bestätige, dass ich die Klarna-Freigabe trennen möchte.' : 'Ich bestätige, dass ich die Einrichtung einschließlich Zertifikat und App-ID entfernen möchte.');
      const button = node('button', active ? 'Freigabe trennen' : 'Einrichtung entfernen', 'kb-danger'); const action = node('p', '', 'kb-action-status');
      button.addEventListener('click', () => { if (!check.checked) { message(action, 'Bitte bestätige zuerst das Trennen beziehungsweise Entfernen.', true); return; }
        void perform(button, action, routes.disconnect, {confirmed: true}, generation, host, root, null,
          () => refresh(host, root, status, generation, null, updated => renderSetup(host, root, status, updated, generation))); }); card.append(button, action);
    }
    root.append(card);
  }
  function amountText(amount, currency) {
    const raw = typeof amount === 'string' || typeof amount === 'number' ? String(amount) : '';
    if (!/^[+-]?\d+(?:\.\d+)?$/.test(raw)) return 'Betrag nicht ausgewiesen';
    if (!/^[A-Z]{3}$/.test(safe(currency, 3))) return `${raw} · Währung nicht ausgewiesen`;
    return `${raw} ${currency}`;
  }
  function signedAmount(amount, indicator) {
    const raw = typeof amount === 'string' || typeof amount === 'number' ? String(amount) : '';
    if (!/^[+-]?\d+(?:\.\d+)?$/.test(raw) || !['CRDT', 'DBIT'].includes(indicator)) return '';
    const magnitude = raw.replace(/^[+-]/, ''); return /^0+(?:\.0+)?$/.test(magnitude) ? magnitude : indicator === 'DBIT' ? `-${magnitude}` : magnitude;
  }
  function renderResults(result, target) {
    target.replaceChildren();
    if (!result || result.complete !== true || result.imported !== false) { target.append(node('p', 'Der Abruf ist unvollständig oder nicht eindeutig als nicht importiert bestätigt.', 'kb-error')); return false; }
    const from = safe(result.date_from, 32), to = safe(result.date_to, 32);
    if (from && to) target.append(node('p', `Abgefragter Zeitraum: ${from} bis ${to}`, 'kb-note'));
    const retrieved = safe(result.retrieved_at, 64);
    if (retrieved) target.append(node('p', `Abgerufen am: ${retrieved}`, 'kb-note'));
    target.append(node('h4', 'Kontostände'));
    if (!Array.isArray(result.balances) || !result.balances.length) target.append(node('p', 'Keine Kontostände geliefert.', 'kb-note'));
    for (const balance of Array.isArray(result.balances) ? result.balances : []) {
      const amount = balance?.amount ?? balance?.balance_amount;
      const row = node('article', '', 'kb-row'); row.append(node('strong', amountText(amount?.amount, amount?.currency)));
      row.append(node('small', safe(balance?.balance_type, 80) || 'Saldoart nicht ausgewiesen'));
      if (safe(balance?.reference_date, 32)) row.append(node('small', `Stichtag: ${balance.reference_date}`)); target.append(row);
    }
    target.append(node('h4', 'Buchungen'));
    if (!Array.isArray(result.transactions) || !result.transactions.length) target.append(node('p', 'Keine Buchungen im angefragten Zeitraum geliefert.', 'kb-note'));
    const labels = {BOOK: 'Gebucht', PDNG: 'Vorgemerkt', CNCL: 'Abgebrochen', HOLD: 'Gesperrt', RJCT: 'Abgelehnt', SCHD: 'Geplant'};
    for (const tx of Array.isArray(result.transactions) ? result.transactions : []) {
      const row = node('article', '', 'kb-row'); const code = safe(tx?.status, 32).toUpperCase(); row.append(node('strong', labels[code] || (code ? `Status: ${code}` : 'Status nicht ausgewiesen')));
      const bookingDate = safe(tx?.booking_date, 32) || safe(tx?.booking_date_time, 40);
      const valueDate = safe(tx?.value_date, 32);
      const transactionDate = safe(tx?.transaction_date, 32);
      row.append(node('small', bookingDate ? `Buchungstag: ${bookingDate}` : valueDate ? `Valuta: ${valueDate}`
        : transactionDate ? `Transaktionsdatum (Ersatz): ${transactionDate}` : 'Datum nicht ausgewiesen'));
      const value = signedAmount(tx?.transaction_amount?.amount, tx?.credit_debit_indicator);
      const knownDirection = ['CRDT', 'DBIT'].includes(tx?.credit_debit_indicator);
      const displayedAmount = value ? amountText(value, tx?.transaction_amount?.currency)
        : `${knownDirection ? '' : 'Richtung nicht ausgewiesen · '}${amountText(tx?.transaction_amount?.amount, tx?.transaction_amount?.currency)}`;
      row.append(node('small', displayedAmount));
      const lines = Array.isArray(tx?.remittance_information) ? tx.remittance_information.filter(v => typeof v === 'string').slice(0, 8).join(' · ').slice(0, 240) : '';
      const merchant = tx?.credit_debit_indicator === 'DBIT' ? safe(tx?.creditor?.name, 120)
        : tx?.credit_debit_indicator === 'CRDT' ? safe(tx?.debtor?.name, 120)
          : safe(tx?.creditor?.name, 120) || safe(tx?.debtor?.name, 120);
      if (merchant || lines) row.append(node('span', [merchant, lines].filter(Boolean).join(' · ')));
      target.append(row);
    }
    return true;
  }
  async function renderBookings(host, externalSignal) {
    if (!host || typeof host.replaceChildren !== 'function') return;
    const generation = nextGeneration(host); const controller = new AbortController();
    externalSignal?.addEventListener('abort', () => controller.abort(), {once: true}); const signal = controller.signal;
    host.replaceChildren(); host.classList.add('kb-host');
    const root = node('div', '', 'kb-root'); const status = node('p', 'Klarna-Konten werden geladen.', 'kb-status');
    status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    root.append(status, node('p', 'Klarna: Vorschau der Kontostände und Umsätze. Die Ansicht deckt keine vollständige Einkaufs- und Ratenübersicht ab.', 'kb-compact-hint'));
    const limits = node('details', '', 'kb-limits');
    limits.append(node('summary', 'Umfang und Grenzen des Abrufs'));
    limits.append(node('p', 'Der Kontenabruf zeigt keine vollständige Einkaufs- und Ratenübersicht. Fälligkeiten und Ratenfelder werden nicht aus Buchungstexten abgeleitet. Der unterstützte automatische Datenweg für diese Angaben ist noch ungeklärt.', 'kb-info'));
    root.append(limits);
    host.append(root);
    const state = await loadState(host, root, status, generation, signal);
    if (!state || !current(host, root, generation, signal)) return;
    if (!state.supported) { message(status, 'Klarna-Lesen wird auf dieser Serverinstallation nicht unterstützt.'); return; }
    if (state.setup !== 'AUTHORIZED') { message(status, 'Für Klarna liegt noch keine aktive Freigabe vor. Richte sie zuerst in der Verwaltung ein.'); return; }
    const accounts = Array.isArray(state.accounts) ? state.accounts.filter(a => a && typeof a.uid === 'string' && a.uid.length <= 256) : [];
    if (!accounts.length) { message(status, 'Es wurde kein freigegebenes Konto gemeldet.'); return; }
    const form = node('form', '', 'kb-card kb-form kb-read-form'); form.append(node('h3', 'Klarna-Leseprobe', 'kb-heading'));
    form.append(node('p', 'Die angezeigten Daten werden nur gelesen; es findet kein Import statt.', 'kb-note'));
    const accountLabel = node('label', 'Konto'); const select = document.createElement('select'); select.name = 'account_uid'; select.required = true;
    const placeholder = node('option', 'Konto auswählen'); placeholder.value = ''; placeholder.disabled = true; placeholder.selected = true; select.append(placeholder);
    for (const account of accounts) { const option = node('option', `${safe(account.name, 160) || 'Klarna-Konto'} · ${safe(account.currency, 3) || 'Währung nicht ausgewiesen'}`); option.value = account.uid; select.append(option); }
    accountLabel.append(select); form.append(accountLabel);
    const today = new Date(); const end = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate())); const start = new Date(end); start.setUTCDate(start.getUTCDate() - 89);
    const dateFrom = document.createElement('input'); dateFrom.type = 'date'; dateFrom.name = 'date_from'; dateFrom.value = start.toISOString().slice(0, 10); dateFrom.required = true;
    const dateTo = document.createElement('input'); dateTo.type = 'date'; dateTo.name = 'date_to'; dateTo.value = end.toISOString().slice(0, 10); dateTo.required = true;
    for (const [text, input] of [['Von', dateFrom], ['Bis', dateTo]]) { const label = node('label', text); label.append(input); form.append(label); }
    const action = node('p', '', 'kb-action-status'); const button = node('button', 'Leseprobe starten'); form.append(button, action); root.append(form);
    const results = node('section', '', 'kb-results'); root.append(results); message(status, '');
    form.addEventListener('submit', async event => {
      event.preventDefault(); const from = dateFrom.value, to = dateTo.value;
      if (!select.value) { message(action, 'Bitte wähle zuerst ein Konto aus.', true); return; }
      if (!validDate(from) || !validDate(to) || from > to || (Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86400000 >= 90) { message(action, 'Bitte prüfe den Zeitraum. Er darf höchstens 90 Tage umfassen.', true); return; }
      button.disabled = true; message(action, 'Leseprobe läuft.'); results.replaceChildren();
      try {
        const result = await window.api(routes.read, {account_uid: select.value, date_from: from, date_to: to});
        if (!current(host, root, generation, signal)) return;
        message(action, renderResults(result, results) ? 'Leseprobe abgeschlossen.' : 'Leseprobe konnte nicht bestätigt werden.', !(result?.complete === true && result?.imported === false));
      } catch (_) { if (current(host, root, generation, signal)) message(action, 'Klarna-Daten konnten nicht gelesen werden. Bitte prüfe den Serverstatus.', true); }
      finally { if (button.isConnected && !signal.aborted) button.disabled = false; }
    });
  }
  window.KlarnaBanking = {renderAdministration, renderBookings};
})();
