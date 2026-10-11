'use strict';

(() => {
  const node = (tag, text = '', className = '') => {
    const element = document.createElement(tag);
    element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const routes = {
    state: '/api/administration/paypal-state',
    prepare: '/api/administration/paypal-prepare',
    configure: '/api/administration/paypal-configure',
    authorize: '/api/administration/paypal-authorize',
    disconnect: '/api/administration/paypal-disconnect',
    read: '/api/administration/paypal-read',
  };
  const phases = {
    UNCONFIGURED: 'Noch nicht eingerichtet',
    CERTIFICATE_READY: 'Zertifikat bereit',
    APPLICATION_READY: 'Anwendung gespeichert',
    AUTHORIZED: 'Freigabe erteilt',
  };
  const validDate = value => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const parsed = Date.parse(`${value}T00:00:00Z`);
    return Number.isFinite(parsed) && new Date(parsed).toISOString().slice(0, 10) === value;
  };
  const safe = (value, max = 180) => typeof value === 'string' && value.length <= max ? value : '';
  let dashboardMounted = false;
  let dashboardPending = false;

  function clearDashboardPreview() {
    dashboardMounted = false;
    const host = document.getElementById('dashboard-paypal-host');
    if (host) host.replaceChildren();
  }

  function mountDashboardPreview() {
    const host = document.getElementById('dashboard-paypal-host');
    if (!host || dashboardMounted) return;
    if (typeof state === 'undefined' || !state?.csrf) { dashboardPending = true; return; }
    dashboardPending = false;
    dashboardMounted = true;
    void renderBookings(host);
  }

  document.addEventListener('cockpit-area-changed', event => {
    if (event.detail?.id === 'dashboard') mountDashboardPreview();
    else { dashboardPending = false; clearDashboardPreview(); }
  });
  document.addEventListener('finance-refreshed', () => {
    if (dashboardPending && location.hash === '#dashboard') mountDashboardPreview();
  });
  document.querySelector('form.logout')?.addEventListener('submit', () => {
    dashboardPending = false;
    clearDashboardPreview();
  }, {once: true});
  if (location.hash === '#dashboard') {
    dashboardPending = true;
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mountDashboardPreview, {once: true});
    else mountDashboardPreview();
  }

  async function loadState(host, root, status) {
    try {
      const state = await window.api(routes.state, {});
      if (!host.isConnected || !host.contains(root)) return null;
      if (!state || typeof state !== 'object' || typeof state.supported !== 'boolean') throw new Error('invalid');
      return state;
    } catch (error) {
      if (host.isConnected && host.contains(root)) status.textContent = safe(error?.message, 240) || 'PayPal-Einrichtung konnte nicht geladen werden.';
      return null;
    }
  }

  function confirmation(parent, label) {
    const wrap = node('label', '', 'pb-check');
    const input = document.createElement('input');
    input.type = 'checkbox';
    wrap.append(input, node('span', label));
    parent.append(wrap);
    return input;
  }

  function message(target, text, error = false) {
    target.textContent = text;
    target.classList.toggle('pb-error', error);
  }

  async function perform(button, status, route, payload, success, onSuccess) {
    button.disabled = true;
    message(status, 'Bitte warten.');
    try {
      const result = await window.api(route, payload);
      if (success) success(result);
      else message(status, 'Änderung gespeichert.');
      if (onSuccess) await onSuccess(result);
    } catch (error) {
      message(status, safe(error?.message, 240) || 'Die Anfrage konnte nicht abgeschlossen werden. Bitte prüfe den Serverstatus.', true);
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  }

  function renderSetup(host, root, status, state) {
    if (state.supported !== true) {
      root.append(node('p', 'Diese Serverinstallation unterstützt die PayPal-Einrichtung nicht.', 'pb-note'));
      return;
    }
    const phase = phases[state.setup];
    if (!phase) {
      message(status, 'Der Einrichtungsstatus ist ungültig.', true);
      return;
    }
    const card = node('section', '', 'pb-card');
    card.append(node('h3', 'PayPal-Lesezugang', 'pb-heading'));
    card.append(node('p', `Status: ${phase}`, 'pb-note'));
    const detail = node('p', 'Die Einrichtung gilt nur für Dein eigenes Enable-Banking-Konto. PayPal-Zugangsdaten werden hier nicht eingegeben.', 'pb-note');
    card.append(detail);

    if (state.setup === 'UNCONFIGURED') {
      const button = node('button', '1. Zertifikat erzeugen');
      const check = confirmation(card, 'Ich bestätige, dass der Server ein öffentliches Zertifikat für meine eigene Einrichtung erzeugen darf.');
      const actionStatus = node('p', '', 'pb-action-status');
      button.addEventListener('click', () => {
        if (!check.checked) { message(actionStatus, 'Bitte bestätige zuerst die Zertifikatserzeugung.', true); return; }
        void perform(button, actionStatus, routes.prepare, {confirmed: true}, result => {
          const pem = safe(result?.certificate_pem, 20000);
          if (!pem || !pem.includes('BEGIN CERTIFICATE')) { message(actionStatus, 'Der Server hat kein gültiges öffentliches Zertifikat geliefert.', true); return; }
          const blob = new Blob([pem.endsWith('\n') ? pem : `${pem}\n`], {type: 'application/x-x509-ca-cert'});
          const url = URL.createObjectURL(blob);
          const link = node('a', 'Öffentliches Zertifikat herunterladen (.crt)');
          link.href = url;
          link.download = 'finance-control-paypal.crt';
          link.addEventListener('click', () => setTimeout(() => URL.revokeObjectURL(url), 1000), {once: true});
          card.append(link);
          message(actionStatus, 'Zertifikat erstellt. Lade es herunter und registriere damit Deine Anwendung bei Enable Banking.');
        }, () => loadAndRender());
      });
      card.append(button, actionStatus);
    }

    if (state.setup === 'CERTIFICATE_READY') {
      const certificate = safe(state.certificate_pem, 20000);
      if (certificate.includes('BEGIN CERTIFICATE')) {
        const blob = new Blob([certificate], {type: 'application/x-x509-ca-cert'});
        const link = node('a', 'Öffentliches Zertifikat herunterladen (.crt)');
        link.href = URL.createObjectURL(blob);
        link.download = 'finance-control-paypal.crt';
        card.append(link);
      }
      const providerLink = node('a', 'Enable Banking öffnen');
      providerLink.href = 'https://enablebanking.com/sign-in/';
      providerLink.target = '_blank';
      providerLink.rel = 'noopener noreferrer';
      card.append(providerLink);
      card.append(node('p', 'Melde Dich dort mit Deinem eigenen Entwicklerkonto an. Registriere eine Anwendung in PRODUCTION mit dem Dienst AIS, dem öffentlichen Zertifikat und der unten angezeigten Rückkehradresse. Verknüpfe anschließend Deine eigenen Konten mit dieser Anwendung; das ist Voraussetzung für die Freigabe.', 'pb-note'));
      card.append(node('p', 'Prüfe mögliche Kosten und Bedingungen direkt bei Enable Banking. Finance Control stimmt keinen kostenpflichtigen Bedingungen zu und aktiviert keine kostenpflichtigen Leistungen.', 'pb-note'));
      const form = node('form', '', 'pb-form');
      const label = node('label', 'Enable-Banking-App-ID');
      const input = document.createElement('input');
      input.name = 'application_id'; input.maxLength = 256; input.autocomplete = 'off'; input.required = true;
      label.append(input); form.append(label);
      form.append(node('p', `Rückkehradresse: ${safe(state.callback_url, 2048)}`, 'pb-note'));
      const check = confirmation(form, 'Ich bestätige, dass ich die App-ID meiner eigenen Enable-Banking-Anwendung speichere.');
      const button = node('button', '2. App-ID speichern');
      const actionStatus = node('p', '', 'pb-action-status');
      form.append(button, actionStatus);
      form.addEventListener('submit', event => {
        event.preventDefault();
        const applicationId = input.value.trim();
        if (!check.checked) { message(actionStatus, 'Bitte bestätige zuerst das Speichern.', true); return; }
        if (!applicationId) { message(actionStatus, 'Bitte gib die App-ID ein.', true); return; }
        input.value = '';
        void perform(button, actionStatus, routes.configure, {confirmed: true, application_id: applicationId}, null, () => loadAndRender());
      });
      card.append(form);
    }

    if (state.setup === 'APPLICATION_READY') {
      const form = node('form', '', 'pb-form');
      const label = node('label', 'Anbieterstandort');
      const select = document.createElement('select'); select.name = 'country';
      for (const [value, text] of [['LU', 'Luxemburg · PayPal Europe'], ['DE', 'Deutschland']]) {
        const option = node('option', text); option.value = value; select.append(option);
      }
      label.append(select); form.append(label);
      form.append(node('p', 'PayPal Europe wird im Anbieterverzeichnis unter Luxemburg geführt. Dein Wohnsitz ist davon unabhängig.', 'pb-muted'));
      const button = node('button', '3. PayPal-Freigabe öffnen');
      const actionStatus = node('p', '', 'pb-action-status');
      form.append(button, actionStatus);
      form.addEventListener('submit', event => {
        event.preventDefault();
        void perform(button, actionStatus, routes.authorize, {confirmed: true, country: select.value}, result => {
          let url;
          try { url = new URL(result?.authorization_url); } catch (_) { url = null; }
          if (!url || url.protocol !== 'https:' || url.hostname !== 'auth.enablebanking.com') {
            message(actionStatus, 'Der Server hat keinen gültigen Enable-Banking-Freigabelink geliefert.', true); return;
          }
          const link = node('a', 'Freigabe bei Enable Banking öffnen');
          link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
          card.append(link);
          const refresh = node('button', 'Freigabe-Status aktualisieren', 'pb-secondary');
          refresh.addEventListener('click', () => void loadAndRender());
          card.append(refresh);
          message(actionStatus, 'Öffne den offiziellen Freigabelink und bestätige dort den Zugriff.');
        });
      });
      card.append(form);
    }

    if (state.setup === 'AUTHORIZED') {
      card.append(node('p', `Freigegebene Konten: ${Array.isArray(state.accounts) ? state.accounts.length : 0}`, 'pb-note'));
      if (safe(state.expires_at, 80)) card.append(node('p', `Freigabe gültig bis: ${state.expires_at}`, 'pb-note'));
    }
    if (['CERTIFICATE_READY', 'APPLICATION_READY', 'AUTHORIZED'].includes(state.setup)) {
      const authorized = state.setup === 'AUTHORIZED';
      const check = confirmation(card, authorized
        ? 'Ich bestätige, dass ich die Freigabe für meine PayPal-Konten trennen möchte.'
        : 'Ich bestätige, dass ich die Einrichtung einschließlich Zertifikat und App-ID entfernen möchte.');
      const button = node('button', authorized ? 'Freigabe trennen' : 'Einrichtung entfernen', 'pb-danger');
      const actionStatus = node('p', '', 'pb-action-status');
      button.addEventListener('click', () => {
        if (!check.checked) { message(actionStatus, `Bitte bestätige zuerst ${authorized ? 'das Trennen der Freigabe' : 'das Entfernen der Einrichtung'}.`, true); return; }
        void perform(button, actionStatus, routes.disconnect, {confirmed: true}, null, () => loadAndRender());
      });
      card.append(button, actionStatus);
    }
    root.append(card);

    async function loadAndRender() {
      const updated = await loadState(host, root, status);
      if (updated) {
        root.replaceChildren();
        renderSetup(host, root, status, updated);
      }
    }
  }

  function transactionLabel(tx) {
    const status = safe(tx?.status, 32).toUpperCase();
    const labels = {BOOK: 'Gebucht', PDNG: 'Vorgemerkt', CNCL: 'Abgebrochen', HOLD: 'Gesperrt',
      RJCT: 'Abgelehnt', SCHD: 'Geplant', OTHR: 'Unbekannter Status'};
    return Object.hasOwn(labels, status) ? labels[status] : 'Status nicht ausgewiesen';
  }

  function signedAmount(amount, indicator) {
    const raw = typeof amount === 'string' || typeof amount === 'number' ? String(amount) : '';
    if (!/^[+-]?\d+(?:\.\d+)?$/.test(raw) || !['CRDT', 'DBIT'].includes(indicator)) return '';
    const magnitude = raw.replace(/^[+-]/, '');
    if (/^0+(?:\.0+)?$/.test(magnitude)) return magnitude;
    return indicator === 'DBIT' ? `-${magnitude}` : magnitude;
  }

  function showReadResults(result, target) {
    target.replaceChildren();
    if (!result || result.complete !== true || result.imported !== false) {
      target.append(node('p', 'Die Leseprobe ist unvollständig oder wurde nicht eindeutig als nicht importiert bestätigt.', 'pb-error'));
      return;
    }
    target.append(node('p', 'Leseprobe abgeschlossen. Es wurden keine Buchungen übernommen.', 'pb-note'));
    target.append(node('h4', 'Kontostände'));
    if (!Array.isArray(result.balances) || result.balances.length === 0) target.append(node('p', 'Keine Kontostände geliefert.', 'pb-note'));
    for (const balance of (Array.isArray(result.balances) ? result.balances : [])) {
      const amount = balance?.balance_amount?.amount;
      const currency = safe(balance?.balance_amount?.currency, 12);
      const type = safe(balance?.balance_type, 80);
      if ((typeof amount !== 'string' && typeof amount !== 'number') || !currency) continue;
      const row = node('article', '', 'pb-row');
      row.append(node('strong', `${String(amount)} ${currency}`));
      const types = {CLBD: 'Gebucht', ITBD: 'Gebucht', CLAV: 'Verfügbar', ITAV: 'Verfügbar'};
      const typeLabel = Object.hasOwn(types, type) ? `${types[type]} · ${type}`
        : safe(balance?.name, 80) || (type ? `Saldoart ${type}` : 'Saldoart nicht ausgewiesen');
      row.append(node('small', typeLabel));
      const referenceDate = safe(balance?.reference_date, 32);
      if (referenceDate) row.append(node('small', `Stichtag: ${referenceDate}`));
      const changedAt = safe(balance?.last_change_date_time, 40);
      if (changedAt) row.append(node('small', `Zuletzt geändert: ${changedAt}`));
      target.append(row);
    }
    target.append(node('h4', 'Buchungen'));
    if (!Array.isArray(result.transactions) || result.transactions.length === 0) target.append(node('p', 'Keine Buchungen im angefragten Zeitraum geliefert.', 'pb-note'));
    for (const tx of (Array.isArray(result.transactions) ? result.transactions : [])) {
      const row = node('article', '', 'pb-row');
      row.append(node('strong', transactionLabel(tx)));
      const date = safe(tx?.booking_date, 32) || safe(tx?.booking_date_time, 40) || safe(tx?.value_date, 32);
      if (date) row.append(node('small', date));
      const amount = tx?.transaction_amount;
      const signed = signedAmount(amount?.amount, tx?.credit_debit_indicator);
      if (signed && safe(amount?.currency, 12)) {
        row.append(node('small', `${signed} ${amount.currency}`));
      }
      const remittance = Array.isArray(tx?.remittance_information)
        ? tx.remittance_information.filter(value => typeof value === 'string').join(' · ').slice(0, 240) : '';
      const description = remittance || safe(tx?.creditor?.name, 120) || safe(tx?.debtor?.name, 120);
      if (description) row.append(node('span', description));
      target.append(row);
    }
  }

  async function renderBookings(host) {
    if (!host || typeof host.replaceChildren !== 'function') return;
    host.replaceChildren(); host.classList.add('pb-host');
    const root = node('div', '', 'pb-root');
    const status = node('p', 'PayPal-Konten werden geladen.', 'pb-status');
    status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    root.append(status); host.append(root);
    const state = await loadState(host, root, status);
    if (!state) return;
    if (state.supported !== true) { message(status, 'PayPal-Lesen wird auf dieser Serverinstallation nicht unterstützt.'); return; }
    if (state.setup !== 'AUTHORIZED') { message(status, 'Für PayPal gibt es noch keine aktive Freigabe. Richte sie zuerst in der Verwaltung ein.'); return; }
    const accounts = Array.isArray(state.accounts) ? state.accounts.filter(account => account && typeof account.uid === 'string' && account.uid.length <= 256) : [];
    if (!accounts.length) { message(status, 'Es wurde kein freigegebenes Konto gemeldet.'); return; }
    const form = node('form', '', 'pb-card pb-form pb-read-form');
    form.append(node('h3', 'PayPal-Leseprobe', 'pb-heading'));
    form.append(node('p', 'Die angezeigten Daten werden nur gelesen. Es findet kein Import statt.', 'pb-note'));
    const accountLabel = node('label', 'Konto');
    const accountSelect = document.createElement('select'); accountSelect.name = 'account_uid';
    accounts.forEach(account => {
      const name = safe(account.name, 160) || 'PayPal-Konto';
      const currency = safe(account.currency, 12);
      const option = node('option', currency ? `${name} · ${currency}` : name);
      option.value = account.uid; accountSelect.append(option);
    });
    accountLabel.append(accountSelect); form.append(accountLabel);
    const now = new Date();
    const today = new Date(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()));
    const start = new Date(Date.UTC(now.getFullYear(), now.getMonth(), 1));
    const dateFrom = document.createElement('input'); dateFrom.type = 'date'; dateFrom.name = 'date_from'; dateFrom.value = start.toISOString().slice(0, 10); dateFrom.required = true;
    const dateTo = document.createElement('input'); dateTo.type = 'date'; dateTo.name = 'date_to'; dateTo.value = today.toISOString().slice(0, 10); dateTo.required = true;
    for (const [text, input] of [['Von', dateFrom], ['Bis', dateTo]]) { const label = node('label', text); label.append(input); form.append(label); }
    const actionStatus = node('p', '', 'pb-action-status');
    const results = node('section', '', 'pb-results');
    const button = node('button', 'Leseprobe starten');
    form.append(button, actionStatus); root.append(form, results);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const from = dateFrom.value; const to = dateTo.value;
      if (!validDate(from) || !validDate(to) || from > to) { message(actionStatus, 'Bitte prüfe den Zeitraum.', true); return; }
      const days = (Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86400000 + 1;
      if (days > 90) { message(actionStatus, 'Der Zeitraum darf höchstens 90 Tage umfassen.', true); return; }
      button.disabled = true;
      message(actionStatus, 'Leseprobe läuft.'); results.replaceChildren();
      try {
        const result = await window.api(routes.read, {account_uid: accountSelect.value, date_from: from, date_to: to});
        showReadResults(result, results);
        message(actionStatus, result?.complete === true && result?.imported === false ? 'Leseprobe abgeschlossen.' : 'Leseprobe konnte nicht bestätigt werden.', !(result?.complete === true && result?.imported === false));
      } catch (_) { message(actionStatus, 'PayPal-Daten konnten nicht gelesen werden. Bitte prüfe den Serverstatus.', true); }
      finally { button.disabled = false; }
    });
  }

  async function renderAdministration(host) {
    if (!host || typeof host.replaceChildren !== 'function') return;
    host.replaceChildren(); host.classList.add('pb-host');
    const root = node('div', '', 'pb-root');
    const status = node('p', 'PayPal-Einrichtung wird geladen.', 'pb-status');
    status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    root.append(status); host.append(root);
    const state = await loadState(host, root, status);
    if (!state) return;
    message(status, ''); renderSetup(host, root, status, state);
  }

  window.PayPalBanking = {renderAdministration, renderBookings};
})();
