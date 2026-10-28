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
    let proposals = [], limit = 5, loading = false, locked = true, sequence = 0, notice = '', problem = false;
    const mount = {target, render, reload}; mounts.add(mount);
    async function reload() {
      if (loading || saving) return;
      const request = ++sequence; loading = true; locked = true; render();
      try {
        const data = await api('/api/payment-review-view', query);
        if (request !== sequence) return;
        if (!Array.isArray(data.proposals)) throw new Error('Ungültige Vorschlagsliste.');
        proposals = data.proposals; locked = false;
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
    function render() {
      target.classList.add('fc-payment-review');
      target.setAttribute('aria-busy', String(loading || saving));
      target.replaceChildren(node('h3', '', 'Zahlungen prüfen'));
      target.append(node('p', 'fc-caption', 'Bestätigen übernimmt den Vorschlag. Ablehnen merkt Deine Entscheidung.'));
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
        card.append(node('p', 'fc-caption', `${date(item.expected_date)} · ${item.kind === 'match' ? 'Buchung zuordnen' : item.kind === 'amount' ? 'Prognosebetrag anpassen' : 'Regelmäßige Zahlung bestätigen'}`));
        if (item.forecast_included) card.append(node('p', 'fc-caption',
          'Bereits als Schätzung eingeplant. Ablehnen entfernt diese Reihe aus der Prognose.'));
        if (item.kind === 'amount') card.append(node('p', 'fc-payment-review-booking',
          `Bisher ${money({...item, amount:item.previous_amount})} · neuer Betrag ab ${date(item.expected_date)}`));
        if (item.booking) card.append(node('p', 'fc-payment-review-booking',
          `Gebucht: ${money({...item, ...item.booking})} · ${date(item.booking.date)} · ${item.booking.counterparty || 'Gegenpartei offen'}`));
        const details = node('details'); details.append(node('summary', '', 'Begründung & Belege'));
        if (item.reason) details.append(node('p', '', item.reason));
        if (item.confidence != null) details.append(node('p', 'fc-caption', `Einschätzung: ${{high:'Gut passend',medium:'Bitte prüfen',low:'Unsicher'}[item.confidence] || 'Bitte prüfen'}`));
        if (item.booking) details.append(node('p', '', `Buchung: ${date(item.booking.date)} · ${money({...item, ...item.booking})} · ${item.booking.counterparty || 'Gegenpartei offen'}`));
        card.append(details);
        const actions = node('div', 'fc-payment-review-actions');
        for (const [decision, label] of [['accept', 'Bestätigen'], ['reject', 'Ablehnen']]) {
          const action = button(label, () => void decide(item, decision)); action.disabled = saving || loading || locked; actions.append(action);
        }
        card.append(actions); list.append(card);
      }
      target.append(list);
      if (proposals.length > limit) target.append(button(`Weitere anzeigen (${proposals.length - limit})`, () => { limit += 5; render(); }));
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
