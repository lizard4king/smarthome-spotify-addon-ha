'use strict';

let monthlySnapshot = null;
let monthlyRequest = 0;
// Only user selections/actions advance this value; refresh() also resets requests.
let monthlyView = 0;
let monthlyAutoLoaded = false;
let monthlyTransactions = [];
const monthlyKinds = {CHECKING:'Girokonto', SAVINGS:'Sparkonto', CREDIT_CARD:'Kreditkarte', DEPOT:'Depot'};

function monthlyPeriod() { return $('monthly-period').value; }
function monthlySetError(text) { $('monthly-error').textContent = text || ''; }
function monthlySetStatus(text) { $('monthly-status').textContent = text || ''; }
function monthlySetSaveStatus(text, error = false) {
  const target = $('monthly-save-status');
  target.textContent = text || ''; target.className = error ? 'error' : '';
}
function monthlyChecklistItem(label, text, status) {
  const item = document.createElement('div');
  item.className = `monthly-check-item ${status || ''}`;
  const title = document.createElement('strong'); title.textContent = label; item.append(title);
  const detail = document.createElement('span'); detail.textContent = text; item.append(detail);
  return item;
}
function monthlyRenderChecklist(checklist) {
  const target = $('monthly-checklist'); target.replaceChildren();
  if (!checklist) {
    target.append(monthlyChecklistItem('Prüfhinweise', 'Für diesen gespeicherten Stand liegt noch keine Checkliste vor.', 'attention'));
    return;
  }
  const scope = checklist.scope || {};
  const period = checklist.period || {};
  const imports = checklist.imports || {};
  const treatment = checklist.account_treatment || {};
  target.append(monthlyChecklistItem('Zeitraum', `${period.start || '—'} bis ${period.end || '—'} · vollständiger Kalendermonat`, 'ok'));
  target.append(monthlyChecklistItem('Buchungsumfang', `${scope.account_count || 0} Konten · ${scope.transaction_count || 0} Buchungen. Die Quelle kann fehlende Bankauszüge nicht erkennen.`, 'attention'));
  const importStatus = imports.status === 'ok' ? 'ok' : (imports.status === 'attention' ? 'attention' : 'review-required');
  target.append(monthlyChecklistItem('Importprüfung', imports.note || 'Importe müssen geprüft werden.', importStatus));
  const cards = (treatment.credit_cards || {}).account_ids || [];
  const depots = (treatment.depots || {}).account_ids || [];
  target.append(monthlyChecklistItem('Kontenbehandlung', `Kreditkarten: ${cards.length ? cards.join(', ') : 'keine'} · Depots: ${depots.length ? depots.join(', ') : 'keine'}.`, 'ok'));
}
function monthlyRenderTransactions() {
  const account = $('monthly-transaction-account').value;
  const status = $('monthly-transaction-status').value;
  const query = $('monthly-transaction-query').value.trim().toLocaleLowerCase('de');
  const visible = monthlyTransactions.filter(tx => {
    if (account && tx.account_id !== account) return false;
    if (status === 'open' && (tx.confirmed || tx.is_transfer)) return false;
    if (status === 'confirmed' && (!tx.confirmed || tx.is_transfer)) return false;
    if (status === 'transfer' && !tx.is_transfer) return false;
    if (status === 'linked' && !(tx.links || []).length) return false;
    return !query || `${tx.counterparty || ''} ${tx.description || ''}`.toLocaleLowerCase('de').includes(query);
  });
  $('monthly-transaction-scope').textContent = `${visible.length} von ${monthlyTransactions.length} Buchungen`;
  const body = $('monthly-transaction-rows'); body.replaceChildren();
  if (!visible.length) {
    const row = document.createElement('tr'), value = cell(row, monthlyTransactions.length ? 'Keine Buchung passt zum Filter.' : 'Für diese Revision sind keine einzelnen Buchungen gespeichert.');
    value.colSpan = 7; body.append(row); return;
  }
  for (const tx of visible) {
    const row = document.createElement('tr');
    cell(row, tx.date); cell(row, tx.account_label || accountDisplayById(tx.account_id));
    const description = cell(row, '');
    const name = document.createElement('strong'); name.textContent = tx.transfer_display || tx.counterparty || 'Keine Gegenpartei';
    const purpose = document.createElement('p'); purpose.className = tx.source_context_complete === false ? 'notice' : 'muted'; purpose.textContent = tx.is_transfer ? 'Interner Geldweg; die Händlerausgabe steht als eigene Buchung beim Zahlungskonto.' : (tx.source_context_complete === false ? 'Quelldetails fehlen: Empfänger und Verwendungszweck sind nicht enthalten.' : (tx.description || 'Kein Verwendungszweck'));
    description.append(name, purpose);
    cell(row, amount(tx.amount), true);
    cell(row, tx.is_transfer ? 'Umbuchung' : (tx.category || 'Noch offen'));
    cell(row, (tx.links || []).length ? tx.links.map(link => `#${link.id} ${link.title}`).join(' · ') : 'Kein Beleg zugeordnet');
    const actions = cell(row, '');
    if (!tx.is_transfer) button(actions, tx.confirmed ? 'Kategorie ansehen' : 'Kategorie prüfen', () => classificationOpenTransaction(tx));
    if ((tx.links || []).length) button(actions, 'Beleg und Buchung', () => classificationOpenComparison(tx.links[0].id, tx));
    body.append(row);
  }
}
function monthlySetTransactions(rows) {
  monthlyTransactions = rows || [];
  const select = $('monthly-transaction-account'), chosen = select.value;
  select.replaceChildren(new Option('Alle Konten', ''));
  for (const id of [...new Set(monthlyTransactions.map(tx => tx.account_id))].sort()) select.append(new Option(accountDisplayById(id), id));
  select.value = [...select.options].some(option => option.value === chosen) ? chosen : '';
  monthlyRenderTransactions();
}
function monthlyReset() {
  monthlyRequest += 1;
  monthlySnapshot = null;
  $('monthly-preview').hidden = true;
  $('monthly-complete').checked = false;
  $('monthly-save').disabled = true;
  $('monthly-revision').textContent = 'Noch keine Vorschau';
  monthlySetError('');
  monthlySetStatus('Bitte die Vorschau für den ausgewählten Monat laden.');
}
function monthlyRender(snapshot, revisionLabel) {
  monthlySnapshot = snapshot;
  $('monthly-preview').hidden = false;
  $('monthly-revision').textContent = snapshot.period + ' · ' + (revisionLabel || 'Vorschau');
  $('monthly-account-rows').replaceChildren();
  for (const account of snapshot.accounts || []) {
    const row = document.createElement('tr');
    cell(row, account.account_label || accountDisplayById(account.id)); cell(row, monthlyKinds[account.kind] || account.kind);
    for (const key of ['opening','income','expenses','transfers','closing']) cell(row, amount(account[key]), true);
    $('monthly-account-rows').append(row);
  }
  monthlyRenderChecklist(snapshot.checklist);
  monthlySetTransactions(snapshot.transactions);
  $('monthly-opening').textContent = eur(snapshot.liquidity.opening);
  $('monthly-income').textContent = eur(snapshot.liquidity.income);
  $('monthly-expenses').textContent = eur(snapshot.liquidity.expenses);
  $('monthly-transfers').textContent = eur(snapshot.liquidity.transfers);
  $('monthly-closing').textContent = eur(snapshot.liquidity.closing);
  $('monthly-count').textContent = String(snapshot.transaction_count);
  $('monthly-equation').textContent = `${eur(snapshot.liquidity.opening)} + ${eur(snapshot.liquidity.income)} − ${eur(snapshot.liquidity.expenses)} + (${eur(snapshot.liquidity.transfers)}) = ${eur(snapshot.liquidity.closing)} · Zwei Monatsendpunkte, keine Tageskurve.`;
  chart('monthly-chart', [{name:'Liquidität', values:[snapshot.liquidity.opening, snapshot.liquidity.closing]}], ['Anfang', 'Ende']);
  $('monthly-save').disabled = Boolean(snapshot.id) || !$('monthly-complete').checked;
  monthlySetStatus(`${snapshot.period} · ${revisionLabel || 'Vorschau'} geladen.`);
}
function monthlySavedRows() {
  const rows = $('monthly-saved-rows'); rows.replaceChildren();
  for (const saved of (state.monthly_reviews || [])) {
    const row = document.createElement('tr');
    cell(row, saved.period); cell(row, String(saved.revision)); cell(row, saved.created_at);
    const actions = cell(row, '');
    button(actions, 'Ansehen', async () => {
      monthlyView += 1;
      monthlySetError('');
      const request = ++monthlyRequest;
      const snapshot = await api('/api/monthly-load', {id: Number(saved.id)});
      if (request !== monthlyRequest) return;
      if (snapshot.period !== monthlyPeriod()) $('monthly-period').value = snapshot.period;
      monthlyRender(snapshot, `Gespeicherte Revision ${snapshot.revision}`);
      $('monthly-complete').checked = false; $('monthly-save').disabled = true;
    });
    button(actions, 'CSV', async () => download((await api('/api/monthly-export', {id: Number(saved.id)})).download_url));
    button(actions, 'JSON', async () => download((await api('/api/monthly-export', {id: Number(saved.id)})).snapshot_url));
    rows.append(row);
  }
}
function monthlyRefresh() {
  monthlyReset();
  monthlySavedRows();
  if (!$('monthly-period').value && state && state.as_of) $('monthly-period').value = state.as_of.slice(0, 7);
  if (!monthlyAutoLoaded && monthlyPeriod()) {
    monthlyAutoLoaded = true;
    run(async () => {
      try { await monthlyPreview(); } catch (error) { monthlySetError(error.message); throw error; }
    });
  }
}
async function monthlyPreview() {
  const period = monthlyPeriod();
  if (!/^\d{4}-\d{2}$/.test(period)) throw new Error('Bitte einen Kalendermonat auswählen.');
  monthlyReset();
  monthlyView += 1;
  const request = ++monthlyRequest;
  monthlySetStatus(`Vorschau für ${period} wird geladen …`);
  try {
    const snapshot = await api('/api/monthly-preview', {period});
    if (request !== monthlyRequest || period !== monthlyPeriod()) return;
    monthlyRender(snapshot, 'Vorschau · noch nicht gespeichert');
  } catch (error) {
    if (request !== monthlyRequest || period !== monthlyPeriod()) return;
    monthlySetError(error.message);
    monthlySetStatus(`Vorschau für ${period} nicht geladen. Bitte erneut „Vorschau laden“ wählen.`);
    throw error;
  }
}

$('monthly-period').addEventListener('change', () => { monthlyView += 1; monthlyReset(); });
$('monthly-transaction-account').addEventListener('change', monthlyRenderTransactions);
$('monthly-transaction-status').addEventListener('change', monthlyRenderTransactions);
$('monthly-transaction-query').addEventListener('input', monthlyRenderTransactions);
$('monthly-complete').addEventListener('change', () => { if (monthlySnapshot && !monthlySnapshot.id) $('monthly-save').disabled = !$('monthly-complete').checked; });
$('monthly-save').addEventListener('click', () => run(async () => {
  if (!monthlySnapshot || monthlySnapshot.id || !$('monthly-complete').checked) throw new Error('Bitte zuerst die Vorschau prüfen und die Vollständigkeit bestätigen.');
  monthlyRequest += 1;
  const view = monthlyView, period = monthlySnapshot.period;
  const current = () => view === monthlyView && period === monthlyPeriod();
  $('monthly-save').disabled = true;
  monthlySetSaveStatus(`Monatsprüfung für ${period} wird gespeichert …`);
  monthlySetStatus(`Monatsprüfung für ${period} wird gespeichert …`);
  let saved = null;
  try {
    const result = await api('/api/monthly-save', {period: monthlySnapshot.period, review_token: monthlySnapshot.review_token, confirmed: true});
    saved = result.snapshot || result;
    monthlySetSaveStatus(`${period} · Revision ${saved.revision} gespeichert.`);
    if (current()) monthlySetStatus(`${period} · Revision ${saved.revision} gespeichert. Listen werden aktualisiert …`);
    await refresh();
    if (!current()) return;
    monthlyRender(saved, `Gespeicherte Revision ${saved.revision}`);
    $('monthly-complete').checked = false; $('monthly-save').disabled = true;
  } catch (error) {
    const feedback = saved
      ? `${period} · Revision ${saved.revision} gespeichert. Die Listen konnten nicht aktualisiert werden. Bitte die Seite neu laden.`
      : `Speichern für ${period} nicht bestätigt. Bitte die Seite neu laden und die gespeicherten Monatsprüfungen prüfen.`;
    monthlySetSaveStatus(`${feedback} ${error.message}`, true);
    if (current()) { monthlySetError(error.message); monthlySetStatus(feedback); }
    throw new Error(`${feedback} ${error.message}`, {cause:error});
  }
}));
$('monthly-preview-button').addEventListener('click', () => run(async () => {
  try { await monthlyPreview(); } catch (error) { monthlySetError(error.message); throw error; }
}));

if (typeof state !== 'undefined' && state) monthlyRefresh();
