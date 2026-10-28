'use strict';
// Presentation only. Proposal identity, evidence and every decision come from the server.
(() => {
  const mounts = new Set();
  let saving = false;
  const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  const button = (text, action) => { const n = node('button', 'secondary', text); n.type = 'button'; n.addEventListener('click', action); return n; };
  const date = value => value ? new Date(value + 'T12:00:00').toLocaleDateString('de-DE') : 'Termin offen';
  const money = item => new Intl.NumberFormat('de-DE', {style: 'currency', currency: item.currency}).format(item.amount);
  function liveMounts() {
    for (const mount of mounts) if (!mount.target.isConnected) mounts.delete(mount);
    return mounts;
  }
  function renderAll() { for (const mount of liveMounts()) mount.render(); }
  window.financePaymentReviewMount = (target, scope, {onChanged} = {}) => {
    for (const mount of mounts) if (mount.target === target) mounts.delete(mount);
    const query = {as_of: scope.as_of, until: scope.until, account_id: scope.account_id || ''};
    let proposals = [], series = [], catalogueAvailable = false, seriesSearch = '', limit = 5, loading = false, locked = true, sequence = 0, notice = '', problem = false;
    const mount = {target, render, reload}; mounts.add(mount);
    async function reload() {
      if (loading || saving) return;
      const request = ++sequence; loading = true; locked = true; render();
      try {
        const data = await api('/api/payment-review-view', query);
        if (request !== sequence) return;
        if (!Array.isArray(data.proposals)) throw new Error('Ungültige Vorschlagsliste.');
        proposals = data.proposals; series = Array.isArray(data.series) ? data.series : []; catalogueAvailable = Array.isArray(data.series); locked = false;
        if (problem) { notice = ''; problem = false; }
      } catch (error) {
        problem = true;
        notice = `${notice.startsWith('Entscheidung gespeichert') ? 'Entscheidung gespeichert. ' : ''}Vorschläge konnten nicht aktualisiert werden. Bitte neu laden. ${error.message}`;
      } finally { if (request === sequence) { loading = false; render(); } }
    }
    async function decide(item, decision) {
      if (saving || loading || locked) return;
      saving = true; notice = 'Entscheidung wird gespeichert …'; problem = false; renderAll();
      let saved = false;
      try {
        // Never retry a mutation automatically, including after an ambiguous response.
        await api('/api/payment-review-decide', {...query, id: item.id, token: item.token, decision, confirmed: true}, false);
        saved = true;
        proposals = proposals.filter(proposal => proposal.id !== item.id);
        notice = 'Entscheidung gespeichert.';
      } catch (error) {
        locked = true; problem = true;
        const stale = /stale|veraltet|versionskonflikt|geändert|changed|revision/i.test(error.message);
        notice = stale ? 'Vorschlag inzwischen geändert. Lade die Vorschläge neu und prüfe den aktuellen Stand.' : `Speichern nicht bestätigt. Lade den aktuellen Stand neu, bevor Du erneut entscheidest. ${error.message}`;
      } finally { saving = false; renderAll(); }
      if (saved) {
        await reload();
        document.dispatchEvent(new CustomEvent('finance-payments-changed', {detail: {...query, reviewOrigin: target}}));
        try { await onChanged?.(); }
        catch { if (target.isConnected) { notice = 'Entscheidung gespeichert. Die übrige Ansicht konnte nicht aktualisiert werden. Bitte neu laden.'; problem = true; render(); } }
      }
    }
    const seriesStatus = item => ({stable:'Geschätzt eingeplant',proposed:'Zur Bestätigung',needs_review:'Zu prüfen',covered:'Bereits abgedeckt',blocked:'Vertragsangaben prüfen',rejected:'Abgelehnt',confirmed:'Bestätigt',inactive:'Inaktiv',unclassified:'Noch nicht eingeordnet',unknown:'Rhythmus offen',transfer:'Interne Umbuchung'}[item.status] || 'Zu prüfen');
    function historyDetails(item) {
      const history = Array.isArray(item.history) ? item.history : [];
      const details = node('details', 'fc-payment-series-history');
      details.append(node('summary', '', `Buchungsreihe (${history.length})`));
      // Build the complete history only when requested; no evidence rows are cut off.
      let populated = false;
      details.addEventListener('toggle', () => {
        if (!details.open || populated) return;
        populated = true;
        const list = node('ol', 'fc-payment-series-rows');
        for (const row of history) {
          const entry = node('li');
          const heading = node('div', 'fc-payment-series-row-heading');
          heading.append(node('time', '', date(row.date)), node('strong', '', money({...item, ...row})));
          entry.append(heading);
          if (row.counterparty) entry.append(node('p', 'fc-caption', row.counterparty));
          if (row.description) entry.append(node('p', 'fc-caption', row.description));
          list.append(entry);
        }
        details.append(list);
        if (!history.length) details.append(node('p', 'fc-caption', 'Keine Buchungsnachweise vorhanden.'));
      });
      return details;
    }
    function forecastDetails(item) {
      const forecast = Array.isArray(item.forecast) ? item.forecast : [];
      const details = node('details', 'fc-payment-series-forecast');
      details.append(node('summary', '', `Fortsetzung (${forecast.length})`));
      if (item.is_transfer) details.append(node('p', 'fc-caption', 'Interne Umbuchung: Kontobewegung, keine zusätzliche Haushaltsausgabe.'));
      if (!forecast.length) details.append(node('p', 'fc-caption', item.forecast_empty_reason || 'Keine Fortsetzung im gewählten Zeitraum. Rhythmus und bisherigen Status prüfen.'));
      if (forecast.some(row => row.status === 'preview')) details.append(node('p', 'fc-caption', 'Geschätzte Fortsetzung, nicht bestätigt.'));
      const list = node('ol', 'fc-payment-series-rows');
      for (const row of forecast) {
        const entry = node('li');
        const heading = node('div', 'fc-payment-series-row-heading');
        heading.append(node('time', '', date(row.date)), node('strong', '', money({...item, ...row})));
        entry.append(heading, node('span', 'fc-caption', row.status === 'preview' ? 'Vorschau · geschätzt' : row.status === 'booked' ? 'Bereits gebucht' : row.status === 'expected' ? 'Erwartet laut bestehender Zahlungsregel' : 'Zu klären laut bestehender Zahlungsregel'));
        list.append(entry);
      }
      details.append(list);
      return details;
    }
    function renderCatalogue() {
      if (!catalogueAvailable) return;
      const section = node('section', 'fc-payment-series');
      section.append(node('h3', '', 'Alle Buchungsreihen'));
      const input = node('input', 'fc-payment-series-search');
      input.type = 'search'; input.value = seriesSearch; input.placeholder = 'Anbieter, Konto oder Buchung suchen'; input.setAttribute('aria-label', 'Alle Buchungsreihen durchsuchen');
      const count = node('p', 'fc-caption'); count.setAttribute('role', 'status');
      const list = node('div', 'fc-payment-series-list');
      const cards = series.map(item => {
        const card = node('article', 'fc-payment-series-card'); card.dataset.seriesId = item.id;
        card.append(node('strong', '', item.label), node('p', 'fc-caption', `${item.account_label || item.account_id || 'Konto offen'} · ${seriesStatus(item)}`));
        if (item.is_transfer) card.append(node('p', 'fc-payment-series-badge', 'Interne Umbuchung'));
        card.append(historyDetails(item), forecastDetails(item));
        if (item.warnings?.length) {
          const details = node('details', 'fc-payment-series-warnings'); details.append(node('summary', '', 'Hinweise'));
          for (const warning of item.warnings) details.append(node('p', 'fc-caption', warning));
          card.append(details);
        }
        list.append(card);
        // Search includes the full received history, even before its DOM is expanded.
        const searchText = [item.label, item.account_label, ...(item.history || []).flatMap(row => [row.counterparty, row.description, row.date, row.amount])].filter(Boolean).join(' ').toLocaleLowerCase('de-DE');
        return {card, searchText};
      });
      const filter = () => {
        const needle = seriesSearch.trim().toLocaleLowerCase('de-DE'); let visible = 0;
        for (const {card, searchText} of cards) { card.hidden = !!needle && !searchText.includes(needle); if (!card.hidden) visible++; }
        count.textContent = needle ? `${visible} von ${series.length} Buchungsreihen · Suchfilter aktiv` : `${series.length} Buchungsreihen · ohne Vorfilterung`;
      };
      input.addEventListener('input', () => { seriesSearch = input.value; filter(); });
      section.append(input, count, list); filter(); target.append(section);
    }
    function render() {
      target.classList.add('fc-payment-review');
      target.setAttribute('aria-busy', String(loading || saving));
      target.replaceChildren(node('h3', '', 'Zahlungen prüfen'));
      target.append(node('p', 'fc-caption', 'Vorschläge prüfen und entscheiden.'));
      const status = node('p', 'fc-payment-review-status', loading ? 'Vorschläge werden geladen …' : notice);
      status.setAttribute('role', problem ? 'alert' : 'status'); status.setAttribute('aria-live', 'polite'); target.append(status);
      const reloadButton = button('Vorschläge neu laden', () => void reload()); reloadButton.disabled = loading || saving; target.append(reloadButton);
      target.append(node('p', 'fc-caption', `${Math.min(limit, proposals.length)} von ${proposals.length} Vorschlägen angezeigt`));
      if (!proposals.length && !loading && !locked) target.append(node('p', 'muted', 'Keine offenen Vorschläge für diese Auswahl.'));
      const list = node('div', 'fc-payment-review-list');
      for (const item of proposals.slice(0, limit)) {
        const card = node('article', 'fc-payment-review-card'); card.dataset.proposalId = item.id;
        const heading = node('div', 'fc-payment-review-heading');
        heading.append(node('strong', '', item.label), node('strong', 'fc-payment-review-amount', money(item))); card.append(heading);
        if (!item.requires_review) card.append(node('p', 'fc-caption', `${date(item.expected_date)} · ${item.kind === 'match' ? 'Buchung zuordnen' : item.kind === 'amount' ? 'Prognosebetrag anpassen' : 'Regelmäßige Zahlung bestätigen'}`));
        if (item.requires_review) {
          const warning = node('div', 'fc-payment-review-warning');
          warning.append(node('span', '', item.can_accept === false ? 'Vertragsangaben fehlen.' : 'Historie unklar – bitte prüfen.'));
          card.append(warning);
          const cadence = item.interval_months === 1 ? 'Monatlich' : Number.isInteger(item.interval_months) && item.interval_months > 1 && item.interval_months <= 120 ? `Alle ${item.interval_months} Monate` : 'Rhythmus offen';
          card.append(node('p', 'fc-payment-review-cadence', item.can_accept === false
            ? `${cadence} · Vertragsdetails ergänzen.`
            : `${cadence} · künftig ab ${date(item.expected_date)}`));
        }
        if (item.forecast_included) card.append(node('p', 'fc-caption',
          'Geschätzt eingeplant · Ablehnen entfernt die Reihe.'));
        if (item.kind === 'amount') card.append(node('p', 'fc-payment-review-booking',
          `Bisher ${money({...item, amount:item.previous_amount})} · neuer Betrag ab ${date(item.expected_date)}`));
        if (item.booking) card.append(node('p', 'fc-payment-review-booking',
          `Gebucht: ${money({...item, ...item.booking})} · ${date(item.booking.date)} · ${item.booking.counterparty || 'Gegenpartei offen'}`));
        const details = node('details'); details.append(node('summary', '', 'Begründung & Belege'));
        if (item.requires_review) {
          for (const text of item.warnings || []) details.append(node('p', '', text));
          details.append(node('p', '', item.can_accept === false
            ? 'Für eine Bestätigung fehlen eindeutige Vertragsangaben. Bitte in den Vertragsdetails ergänzen.'
            : `Mit der Bestätigung übernimmst Du die Regel ab der nächsten künftigen Fälligkeit am ${date(item.expected_date)} für die Prognose. Vergangene Buchungen und frühere fehlende Perioden bleiben unverändert.`));
        }
        if (item.reason) details.append(node('p', '', item.reason));
        if (item.confidence != null) details.append(node('p', 'fc-caption', `Einschätzung: ${{high:'Gut passend',medium:'Bitte prüfen',low:'Unsicher'}[item.confidence] || 'Bitte prüfen'}`));
        if (item.booking) details.append(node('p', '', `Buchung: ${date(item.booking.date)} · ${money({...item, ...item.booking})} · ${item.booking.counterparty || 'Gegenpartei offen'}`));
        card.append(details);
        for (const source of series.filter(row => (row.proposal_ids || []).includes(item.id))) card.append(historyDetails(source), forecastDetails(source));
        const actions = node('div', 'fc-payment-review-actions');
        for (const [decision, label] of [['accept', item.requires_review ? 'Fortsetzung bestätigen' : 'Bestätigen'], ['reject', 'Ablehnen']]) {
          if (decision === 'accept' && item.can_accept === false) continue;
          const action = button(label, () => void decide(item, decision)); action.disabled = saving || loading || locked; actions.append(action);
        }
        card.append(actions); list.append(card);
      }
      target.append(list);
      if (proposals.length > limit) target.append(button(`Weitere anzeigen (${proposals.length - limit})`, () => { limit += 5; render(); }));
      renderCatalogue();
    }
    void reload(); return {reload, destroy: () => { sequence++; mounts.delete(mount); }};
  };
  function refreshViews(event) {
    if (document.visibilityState === 'hidden' || saving) return;
    for (const mount of liveMounts()) if (mount.target !== event?.detail?.reviewOrigin) void mount.reload();
  }
  window.addEventListener('focus', refreshViews);
  document.addEventListener('visibilitychange', refreshViews);
  document.addEventListener('finance-refreshed', refreshViews);
  document.addEventListener('finance-payments-changed', refreshViews);
})();
