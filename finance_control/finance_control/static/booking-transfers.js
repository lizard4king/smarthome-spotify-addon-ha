'use strict';
// Explicit single-leg evidence only; no inferred partner or bank action.
(() => {
  let active = null;
  let sequence = 0;
  const node = (tag, text) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = String(text);
    return element;
  };
  const button = (text, handler) => {
    const element = node('button', text); element.type = 'button';
    element.addEventListener('click', handler); return element;
  };
  const current = session => active === session && session.dialog.isConnected;
  const message = error => ({
    stale_single_leg_preview: 'Die Buchung wurde inzwischen geändert. Lade die Vorschau neu und prüfe sie erneut.',
    effective_transfer_already_set: 'Diese Buchung ist bereits als Umbuchung bestätigt.',
    unknown_transaction: 'Diese Buchung ist nicht mehr aktiv.',
    single_leg_not_set: 'Diese einzelne Umbuchungsmarkierung besteht nicht mehr. Lade die Buchung neu.',
    single_leg_overlapping_transfer: 'Diese Buchung ist zusätzlich durch Import oder ein Umbuchungspaar als Umbuchung belegt. Diese Zuordnung kann hier nicht aufgehoben werden.',
    invalid_single_leg_transfer: 'Bitte eine Begründung mit höchstens 120 Zeichen eingeben und ausdrücklich bestätigen.',
  })[error?.message] || 'Die Änderung konnte nicht gespeichert werden. Lade die Vorschau erneut.';
  function close(session) {
    if (active === session) active = null;
    session.dialog.close(); session.dialog.remove();
  }
  async function open(host, accountId, externalId, mode = 'confirm') {
    if (!host?.append || typeof accountId !== 'string' || typeof externalId !== 'string') return false;
    if (!['confirm', 'revoke'].includes(mode)) return false;
    if (active) close(active);
    const dialog = node('dialog'); dialog.className = 'bt-dialog';
    const session = {dialog, key: {account_id: accountId, external_id: externalId}, mode, request: 0, busy: false};
    active = session;
    const titleId = 'bt-title-' + (++sequence);
    dialog.setAttribute('aria-labelledby', titleId);
    dialog.addEventListener('close', () => {if (active === session) active = null; dialog.remove();});
    host.append(dialog); dialog.showModal();
    await load(session, titleId); return true;
  }
  async function load(session, titleId) {
    const request = ++session.request;
    session.dialog.replaceChildren(node('p', 'Buchung wird geprüft …'), button('Schließen', () => close(session)));
    try {
      const result = await api('/api/transfer-correction-single-leg-preview',
        {...session.key, ...(session.mode === 'revoke' ? {mode: 'revoke'} : {})});
      if (!current(session) || request !== session.request) return;
      const tx = result?.transaction;
      if (!tx || tx.account_id !== session.key.account_id || tx.external_id !== session.key.external_id
          || !/^[0-9a-f]{64}$/.test(result.fingerprint || '')
          || (session.mode === 'revoke' && typeof result.single_leg?.reason !== 'string')) throw Error('invalid_preview');
      render(session, titleId, result);
    } catch (error) {
      if (!current(session) || request !== session.request) return;
      session.dialog.replaceChildren(node('p', message(error)),
        button('Neu laden', () => void load(session, titleId)), button('Schließen', () => close(session)));
    }
  }
  function render(session, titleId, preview) {
    const {dialog} = session, tx = preview.transaction, revoke = session.mode === 'revoke';
    const title = node('h2', revoke ? 'Umbuchung aufheben' : 'Einzelne Umbuchung bestätigen'); title.id = titleId;
    const form = node('form');
    const sourceCase = preview.basis === 'source_category_without_counterpart';
    const original = node('p', `Original: ${tx.date} · ${tx.amount} ${tx.currency} · Konto ${tx.account_label} (${tx.account_id})`);
    original.className = 'bt-original';
    form.append(original, node('p', tx.counterparty || ''), node('p', tx.description || ''),
      node('p', revoke && sourceCase
        ? 'Die Einzelmarkierung wird aufgehoben. Die Buchung bleibt wegen der Quellkategorie Umbuchung neutral gerechnet; der Hinweis auf die fehlende Gegenbuchung erscheint wieder.'
        : revoke
        ? 'Die eigene Einzelmarkierung wird aufgehoben. Die Originalbuchung bleibt erhalten und zählt wieder als Haushaltsausgabe oder Einnahme. Importierte Umbuchungen und Umbuchungspaare bleiben geschützt.'
        : 'Bestätige nur eine belegte Bewegung zwischen eigenen Konten. Die Gegenbuchung fehlt im Buchungsbuch. Es wird keine Gegenbuchung erzeugt.'));
    if (sourceCase && !revoke) form.append(node('p', 'Die Quelle führt diese Buchung als Umbuchung, eine Gegenbuchung fehlt. Du bestätigst sie als bewusst einseitig (zum Beispiel Konto außerhalb). Summen ändern sich nicht; der Hinweis entfällt.'));
    if (revoke) form.append(node('p', 'Bisheriger Nachweis: ' + (preview.single_leg?.reason || 'nicht ausgewiesen')));
    const label = node('label', revoke ? 'Grund der Aufhebung' : 'Begründung / Nachweis');
    const reason = node('textarea'); reason.name = 'reason'; reason.required = true; reason.maxLength = 120;
    label.append(reason); form.append(label);
    const confirmation = node('label');
    const confirmed = node('input'); confirmed.type = 'checkbox'; confirmed.name = 'confirmed';
    confirmation.append(confirmed, node('span', revoke
      ? 'Ich bestätige: Nur diese einzelne Umbuchungsmarkierung soll aufgehoben werden.'
      : 'Ich bestätige anhand des Nachweises: eigene Umbuchung, keine Haushaltsausgabe oder Einnahme.'));
    form.append(confirmation);
    const status = node('p'); status.setAttribute('role', 'status');
    const save = node('button', revoke ? 'Umbuchung aufheben' : 'Als Umbuchung bestätigen'); save.type = 'submit'; save.disabled = true;
    const reload = button('Neu laden', () => void load(session, titleId));
    const dismiss = button('Schließen', () => close(session));
    const update = () => {save.disabled = session.busy || !confirmed.checked || !reason.value.trim() || reason.value.trim().length > 120;};
    reason.addEventListener('input', update); confirmed.addEventListener('change', update);
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (save.disabled || session.busy || !current(session)) return;
      session.busy = true; reload.disabled = true; update(); status.textContent = 'Bestätigung wird gespeichert …';
      try {
        await api('/api/transfer-correction-single-leg-' + (revoke ? 'revoke' : 'save'), {...session.key, reason: reason.value.trim(),
          confirmed: confirmed.checked === true, fingerprint: preview.fingerprint});
        if (!current(session)) return;
        document.dispatchEvent(new CustomEvent('finance-booking-transfer-changed', {detail: {...session.key}}));
        close(session);
      } catch (error) {
        if (!current(session)) return;
        status.textContent = message(error);
        // A failed request always requires a new preview and explicit consent.
        confirmed.checked = false; reason.disabled = true; confirmed.disabled = true;
      } finally {
        if (current(session)) {session.busy = false; reload.disabled = false; update();}
      }
    });
    form.append(status, save, reload, dismiss); dialog.replaceChildren(title, form);
  }
  window.BookingTransfers = {open};
})();
