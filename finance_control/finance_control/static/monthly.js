'use strict';

let monthlyRequest = 0;
let monthlyTransactions = [];
const monthlyKinds = {CHECKING:'Girokonto', SAVINGS:'Sparkonto', CREDIT_CARD:'Kreditkarte', DEPOT:'Depot'};

function monthlyPeriod() { return $('monthly-period').value; }
function monthlySetError(text) { $('monthly-error').textContent = text || ''; }
function monthlySetStatus(text) { $('monthly-status').textContent = text || ''; }
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
  $('monthly-preview').hidden = true;
  $('monthly-revision').textContent = 'Noch keine Vorschau';
  monthlySetError('');
  monthlySetStatus('Bitte die Vorschau für den ausgewählten Monat laden.');
}
function monthlyRender(snapshot) {
  $('monthly-preview').hidden = false;
  $('monthly-revision').textContent = snapshot.period + ' · Vorschau';
  $('monthly-account-rows').replaceChildren();
  for (const account of snapshot.accounts || []) {
    const row = document.createElement('tr');
    cell(row, account.account_label || accountDisplayById(account.id)); cell(row, monthlyKinds[account.kind] || account.kind);
    for (const key of ['opening','income','expenses','transfers','closing']) cell(row, amount(account[key]), true);
    $('monthly-account-rows').append(row);
  }
  monthlySetTransactions(snapshot.transactions);
  $('monthly-opening').textContent = eur(snapshot.liquidity.opening);
  $('monthly-income').textContent = eur(snapshot.liquidity.income);
  $('monthly-expenses').textContent = eur(snapshot.liquidity.expenses);
  $('monthly-transfers').textContent = eur(snapshot.liquidity.transfers);
  $('monthly-closing').textContent = eur(snapshot.liquidity.closing);
  $('monthly-count').textContent = String(snapshot.transaction_count);
  $('monthly-equation').textContent = `${eur(snapshot.liquidity.opening)} + ${eur(snapshot.liquidity.income)} − ${eur(snapshot.liquidity.expenses)} + (${eur(snapshot.liquidity.transfers)}) = ${eur(snapshot.liquidity.closing)} · Zwei Monatsendpunkte, keine Tageskurve.`;
  chart('monthly-chart', [{name:'Liquidität', values:[snapshot.liquidity.opening, snapshot.liquidity.closing]}], ['Anfang', 'Ende']);
  monthlySetStatus(`${snapshot.period} · Vorschau geladen.`);
}
async function monthlyPreview() {
  const period = monthlyPeriod();
  if (!/^\d{4}-\d{2}$/.test(period)) throw new Error('Bitte einen Kalendermonat auswählen.');
  monthlyReset();
  const request = ++monthlyRequest;
  monthlySetStatus(`Vorschau für ${period} wird geladen …`);
  try {
    const snapshot = await api('/api/monthly-preview', {period});
    if (request !== monthlyRequest || period !== monthlyPeriod()) return;
    monthlyRender(snapshot);
  } catch (error) {
    if (request !== monthlyRequest || period !== monthlyPeriod()) return;
    monthlySetError(error.message);
    monthlySetStatus(`Vorschau für ${period} nicht geladen. Bitte erneut „Vorschau laden“ wählen.`);
    throw error;
  }
}

$('monthly-period').addEventListener('change', monthlyReset);
$('monthly-transaction-account').addEventListener('change', monthlyRenderTransactions);
$('monthly-transaction-status').addEventListener('change', monthlyRenderTransactions);
$('monthly-transaction-query').addEventListener('input', monthlyRenderTransactions);
$('monthly-preview-button').addEventListener('click', () => run(async () => {
  try { await monthlyPreview(); } catch (error) { monthlySetError(error.message); throw error; }
}));
