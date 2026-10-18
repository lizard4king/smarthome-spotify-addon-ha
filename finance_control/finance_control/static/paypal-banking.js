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
  const berlinCalendar = () => {
    const parts = new Intl.DateTimeFormat('en-CA', {timeZone: 'Europe/Berlin', year: 'numeric', month: '2-digit', day: '2-digit'}).formatToParts(new Date());
    const part = type => parts.find(item => item.type === type).value;
    const today = `${part('year')}-${part('month')}-${part('day')}`;
    return {today, monthStart: `${today.slice(0, 7)}-01`};
  };
  const safe = (value, max = 180) => typeof value === 'string' && value.length <= max ? value : '';
  const authorizationUrl = value => {
    if (typeof value !== 'string' || value.length > 4096 || /[\\\s\u0000-\u001f\u007f-\u009f]/u.test(value) || value.includes('#')) return null;
    const authorityMatch = value.match(/^https:\/\/([^/?#]*)/i);
    if (!authorityMatch || authorityMatch[1].includes('@')) return null;
    const authorityParts = authorityMatch[1].match(/^([^:]+)(?::([0-9]+))?$/);
    if (!authorityParts) return null;
    const host = authorityParts[1].toLowerCase();
    const allowedHosts = ['auth.enablebanking.com', 'tilisy.enablebanking.com'];
    if (!allowedHosts.includes(host) || (authorityParts[2] && authorityParts[2] !== '443')) return null;
    let parsed;
    try { parsed = new URL(value); } catch (_) { return null; }
    if (parsed.protocol !== 'https:' || !allowedHosts.includes(parsed.hostname)
      || parsed.username || parsed.password || (parsed.port && parsed.port !== '443') || parsed.hash) return null;
    return parsed;
  };
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
          const url = authorizationUrl(result?.authorization_url);
          if (!url) {
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

  function bankTransactionType(tx) {
    const code = tx?.bank_transaction_code;
    return [safe(code?.description, 120), safe(code?.code, 48), safe(code?.sub_code, 48)]
      .filter(Boolean).join(' · ').slice(0, 200);
  }

  function hasCreditTextSignal(tx) {
    const remittance = Array.isArray(tx?.remittance_information)
      ? tx.remittance_information.filter(value => typeof value === 'string').join(' ').slice(0, 240) : '';
    const description = safe(tx?.bank_transaction_code?.description, 120);
    const text = `${remittance} ${description}`;
    const signal = /\b(?:Ratenzahlung|Kreditrate|Tilgung|Darlehen|loan repayment|instalment repayment|installment repayment)\b/iu;
    const negated = /\b(?:kein(?:e[rsn]?)?|nicht|ohne)\s+(?:[\p{L}-]+\s+){0,2}(?:Ratenzahlung|Kreditrate|Tilgung|Darlehen|loan repayment|instalment repayment|installment repayment)\b/iu;
    return signal.test(text) && !negated.test(text);
  }

  function renderCreditInfo() {
    const details = node('details', '', 'pb-credit-info');
    details.append(node('summary', 'Ratenkredite'));
    details.append(node('p', 'Kreditplan ist nicht im Kontoabruf enthalten. Ein separat manuell geprüfter Stand wird darunter geführt.', 'pb-note'));
    const values = document.createElement('dl');
    for (const label of ['Restbetrag inkl. künftiger Zinsen', 'Offene Kreditsumme',
      'Zins- und Ratenplan', 'Nächster Termin']) {
      values.append(node('dt', label), node('dd', 'Nicht im Kontoabruf enthalten'));
    }
    details.append(values);
    const link = node('a', 'PayPal-Übersicht öffnen');
    link.href = 'https://www.paypal.com/myaccount/summary';
    link.target = '_blank'; link.rel = 'noopener noreferrer';
    details.append(link);
    return details;
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
        : [safe(balance?.name, 80), type ? `Saldoart ${type}` : 'Saldoart nicht ausgewiesen'].filter(Boolean).join(' · ');
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
      const bookingDate = safe(tx?.booking_date, 32) || safe(tx?.booking_date_time, 40) || safe(tx?.value_date, 32);
      const transactionDate = safe(tx?.transaction_date, 32);
      if (bookingDate) row.append(node('small', bookingDate));
      else if (transactionDate) row.append(node('small', `Transaktionsdatum: ${transactionDate}`));
      const amount = tx?.transaction_amount;
      const signed = signedAmount(amount?.amount, tx?.credit_debit_indicator);
      if (signed && safe(amount?.currency, 12)) {
        row.append(node('small', `${signed} ${amount.currency}`));
      }
      const remittance = Array.isArray(tx?.remittance_information)
        ? tx.remittance_information.filter(value => typeof value === 'string').join(' · ').slice(0, 240) : '';
      const description = remittance || safe(tx?.creditor?.name, 120) || safe(tx?.debtor?.name, 120);
      if (description) row.append(node('span', description));
      const bookingType = bankTransactionType(tx);
      if (bookingType) row.append(node('small', `Buchungsart: ${bookingType}`));
      if (hasCreditTextSignal(tx))
        row.append(node('small', 'Prüfhinweis: Kreditbezug im Text; nicht bestätigt.', 'pb-credit-hint'));
      target.append(row);
    }
  }

  async function renderBookings(host) {
    if (!host || typeof host.replaceChildren !== 'function') return;
    host.replaceChildren(); host.classList.add('pb-host');
    const root = node('div', '', 'pb-root');
    const status = node('p', 'PayPal-Konten werden geladen.', 'pb-status');
    status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const creditHost = node('div', '', 'pb-credit-host');
    root.append(status, renderCreditInfo(), creditHost); host.append(root);
    if (typeof window.PayPalCredit?.renderBookings === 'function')
      void window.PayPalCredit.renderBookings(creditHost);
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
    const calendar = berlinCalendar();
    const dateFrom = document.createElement('input'); dateFrom.type = 'date'; dateFrom.name = 'date_from'; dateFrom.value = calendar.monthStart; dateFrom.required = true;
    const dateTo = document.createElement('input'); dateTo.type = 'date'; dateTo.name = 'date_to'; dateTo.value = calendar.today; dateTo.required = true;
    for (const [text, input] of [['Von', dateFrom], ['Bis', dateTo]]) { const label = node('label', text); label.append(input); form.append(label); }
    const actionStatus = node('p', '', 'pb-action-status');
    const results = node('section', '', 'pb-results');
    const button = node('button', 'Leseprobe starten');
    form.append(button, actionStatus);
    const readDetails = node('details', '', 'pb-read-details');
    readDetails.append(node('summary', 'Erweiterte Leseprobe (ohne Übernahme)'), form, results);
    root.append(renderImport(host, root, accounts), readDetails);
    message(status, '');
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

  // Import is deliberately separate from the existing read-only sample.
  function renderImport(host, root, accounts) {
    const details = node('details', '', 'pb-import');
    details.append(node('summary', 'PayPal-Umsätze übernehmen'));
    const panel = node('section', '', 'pb-card'); details.append(panel);
    panel.append(node('p', 'Nach der ersten Übernahme führt der direkte PayPal-Abruf. Excel/CSV ergänzt dieses Konto dann nicht mehr. Bestehende ältere Buchungen bleiben erhalten.', 'pb-note'));
    const form = node('div', '', 'pb-form'); panel.append(form);
    const control = (text, input) => { const label = node('label', text); label.append(input); form.append(label); return input; };
    const account = control('Freigegebenes PayPal-Konto', document.createElement('select')); account.name = 'import_account_uid';
    const option = (select, text, value) => { const item = node('option', text); item.value = value; select.append(item); };
    accounts.forEach(item => option(account, `${safe(item.name, 160) || 'PayPal-Konto'} · ${safe(item.currency, 12)}`, item.uid));
    const target = control('Zielkonto im Finanzbestand', document.createElement('select')); target.name = 'import_account_id';
    option(target, 'Zielkonto ausdrücklich auswählen', '');
    const calendar = berlinCalendar();
    const from = control('Übernahme von', document.createElement('input')); from.type = 'date'; from.name = 'import_date_from'; from.value = calendar.monthStart;
    const to = control('Übernahme bis', document.createElement('input')); to.type = 'date'; to.name = 'import_date_to'; to.value = calendar.today;
    const preview = node('button', 'Übernahmevorschau laden'); preview.type = 'button'; form.append(preview);
    const status = node('p', '', 'pb-action-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite'); panel.append(status);
    const results = node('section', '', 'pb-results'); panel.append(results);
    const confirm = confirmation(panel, 'Ich bestätige die Quellumstellung dieses Kontos auf den direkten PayPal-Abruf.');
    const commit = node('button', 'Geprüfte Umsätze übernehmen'); commit.type = 'button'; panel.append(commit);
    let busy = false; let loaded = false; let revision = 0; let token = ''; let ready = false;
    const matches = new Map();
    const alive = () => host.isConnected && host.contains(root) && root.contains(details);
    const update = () => {
      panel.querySelectorAll('input,select,button').forEach(input => { input.disabled = busy; });
      preview.disabled = busy || !loaded;
      confirm.disabled = busy || !ready;
      commit.disabled = busy || !ready || !confirm.checked || !token;
    };
    const invalidate = (clear = true) => {
      revision++; token = ''; ready = false; confirm.checked = false;
      if (clear) { matches.clear(); results.replaceChildren(); preview.textContent = 'Übernahmevorschau laden'; }
      update();
    };
    [account, target, from, to].forEach(input => {
      input.addEventListener('input', () => invalidate());
      input.addEventListener('change', () => invalidate());
    });
    confirm.addEventListener('change', update);
    const count = value => Number.isSafeInteger(value) && value >= 0 ? String(value) : 'unbekannt';
    const draw = result => {
      results.replaceChildren();
      results.append(node('p', `Neu: ${count(result.inserted)} · Zu übernehmen: ${count(result.adopted)} · Unverändert: ${count(result.unchanged)}`, 'pb-note'));
      results.append(node('p', `Verbleibende Altbuchungen: ${count(result.unmatched_legacy)}. Diese werden nicht gelöscht.`, 'pb-note'));
      let unresolved = false;
      for (const item of result.items) {
        const row = node('article', '', 'pb-row');
        const raw = typeof item.amount === 'string' || typeof item.amount === 'number' ? String(item.amount) : '';
        const amount = /^[+-]?\d+(?:\.\d+)?$/.test(raw) ? (raw.startsWith('-') || raw.startsWith('+') ? raw : `+${raw}`) : 'Betrag nicht ausgewiesen';
        row.append(node('strong', `${safe(item.date, 32)} · ${amount} ${safe(item.currency, 12)}`), node('span', safe(item.counterparty, 512)), node('small', safe(item.description, 8000)));
        const labels = {review: 'Zuordnung erforderlich', new: 'Neue Buchung', adopted: 'Bestehende Buchung übernehmen', unchanged: 'Bereits übernommen'};
        row.append(node('small', labels[item.decision] || 'Unbekannte Entscheidung'));
        if (item.decision === 'review') {
          unresolved = true;
          const reference = safe(item.entry_reference, 512);
          if (!reference) throw new Error();
          const label = node('label', 'Bestehende Buchung zuordnen'); const select = document.createElement('select');
          select.setAttribute('aria-label', `Zuordnung ${reference}`);
          option(select, 'Bitte ausdrücklich entscheiden', '');
          const choices = new Map();
          (Array.isArray(item.candidates) ? item.candidates : []).forEach((candidate, index) => {
            const external = safe(candidate.external_id, 512);
            if (!external) return;
            const key = `candidate-${index}`; choices.set(key, external);
            option(select, `${safe(candidate.counterparty, 512)} · ${safe(candidate.description, 8000)} · ${external}`, key);
          });
          choices.set('additional', null); option(select, 'Bewusst als zusätzliche neue Buchung übernehmen', 'additional');
          select.addEventListener('change', () => {
            invalidate(false);
            if (choices.has(select.value)) matches.set(reference, choices.get(select.value)); else matches.delete(reference);
          });
          label.append(select); row.append(label);
        }
        results.append(row);
      }
      preview.textContent = unresolved ? 'Zuordnungen prüfen' : 'Übernahmevorschau aktualisieren';
      ready = result.ready === true && !unresolved;
      token = ready && /^[A-Za-z0-9_-]{43}$/.test(result.review_token || '') ? result.review_token : '';
      ready = ready && Boolean(token);
      confirm.checked = false;
    };
    details.addEventListener('toggle', async () => {
      if (!details.open || loaded || busy) return;
      busy = true; update(); message(status, 'Zielkonten werden geladen.');
      try {
        const result = await window.api('/api/administration/paypal-import-targets', {});
        if (!alive()) return;
        if (!Array.isArray(result?.targets)) throw new Error();
        for (const item of result.targets) {
          const id = safe(item.id, 256);
          if (id) option(target, `${safe(item.label, 180) || id} · ${safe(item.currency, 12)}`, id);
        }
        loaded = true; message(status, result.targets.length ? 'Wähle das Zielkonto und prüfe den Zeitraum.' : 'Kein geeignetes Zielkonto vorhanden.');
      } catch (error) { if (alive()) message(status, safe(error?.message, 240) || 'Zielkonten konnten nicht geladen werden. Schließe und öffne den Bereich erneut.', true); }
      finally { busy = false; if (alive()) update(); }
    });
    preview.addEventListener('click', async () => {
      if (busy) return;
      if (!target.value) { message(status, 'Bitte wähle das Zielkonto ausdrücklich aus.', true); return; }
      const days = (Date.parse(`${to.value}T00:00:00Z`) - Date.parse(`${from.value}T00:00:00Z`)) / 86400000 + 1;
      if (!validDate(from.value) || !validDate(to.value) || days < 1 || days > 90) { message(status, 'Bitte wähle einen gültigen Zeitraum mit höchstens 90 Tagen.', true); return; }
      invalidate(false); const generation = revision;
      busy = true; update(); message(status, 'Übernahmevorschau wird geprüft.');
      try {
        const result = await window.api('/api/administration/paypal-import-preview', {account_uid: account.value, account_id: target.value, date_from: from.value, date_to: to.value, matches: Object.fromEntries(matches)});
        if (!alive() || generation !== revision) return;
        if (!Array.isArray(result?.items)) throw new Error();
        draw(result); message(status, ready ? 'Vorschau geprüft. Bestätige die Quellumstellung für die Übernahme.' : 'Bitte kläre alle Zuordnungen und prüfe die Vorschau erneut.');
      } catch (error) { if (alive() && generation === revision) { invalidate(); message(status, safe(error?.message, 240) || 'Übernahmevorschau konnte nicht bestätigt werden. Bitte lade sie erneut.', true); } }
      finally { busy = false; if (alive()) update(); }
    });
    commit.addEventListener('click', async () => {
      if (busy || !ready || !token || !confirm.checked) return;
      const reviewToken = token; invalidate(false); busy = true; update(); message(status, 'Geprüfte Umsätze werden übernommen.');
      try {
        const result = await window.api('/api/administration/paypal-import-commit', {review_token: reviewToken, confirmed: true});
        if (!alive()) return;
        if (result?.imported !== true) throw new Error();
        results.replaceChildren(); matches.clear();
        message(status, `Übernahme abgeschlossen. Neu: ${count(result.inserted)} · Übernommen: ${count(result.adopted)} · Unverändert: ${count(result.unchanged)}.`);
        if (typeof window.financeRefreshAfterImport === 'function') {
          try { await window.financeRefreshAfterImport(); } catch (_) { if (alive()) message(status, 'Übernahme abgeschlossen. Die Ansicht konnte nicht aktualisiert werden; bitte lade die Seite neu.', true); }
        } else message(status, 'Übernahme abgeschlossen. Bitte lade die Seite neu, um den Finanzbestand zu aktualisieren.');
      } catch (error) { if (alive()) message(status, safe(error?.message, 240) || 'Die Übernahme konnte nicht bestätigt werden. Lade vor einem erneuten Versuch eine neue Vorschau.', true); }
      finally { busy = false; if (alive()) update(); }
    });
    update(); return details;
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
