'use strict';

let wealthRevision = null;
let wealthLatestRevision = 0;
let wealthReview = null;
let wealthHistory = [];
let wealthRequest = 0;
let wealthEditVersion = 0;
let wealthInitialized = false;
let wealthPositionNumber = 0;

function wealthSetStatus(text, error=false) {
  const node = $('wealth-status');
  node.textContent = text;
  node.className = error ? 'error' : '';
}

function wealthCurrency(value) {
  return value === null || value === undefined ? '—' : eur(value);
}

function wealthField(labelText, control) {
  const label = document.createElement('label');
  label.textContent = labelText;
  label.append(control);
  return label;
}

function wealthInput(name, value='', type='text') {
  const input = document.createElement('input');
  input.name = name;
  input.type = type;
  input.value = value ?? '';
  input.required = true;
  return input;
}

function wealthOwnerSelect(value='') {
  const select = document.createElement('select');
  select.name = 'owner';
  select.required = true;
  select.append(new Option('Bitte zuordnen', ''));
  for (const person of profilePeople()) select.append(new Option(person.label, person.id));
  if (profilePeople().length > 1) select.append(new Option('Gemeinsam', 'JOINT'));
  select.value = value || '';
  return select;
}

function wealthPosition(position={}) {
  const item = document.createElement('article');
  item.className = 'wealth-position';
  item.dataset.position = String(++wealthPositionNumber);
  const heading = document.createElement('div');
  heading.className = 'wealth-position-head';
  const title = document.createElement('strong');
  title.textContent = position.label || 'Neue Position';
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'secondary';
  remove.textContent = 'Entfernen';
  remove.addEventListener('click', () => {item.remove(); wealthInvalidate();});
  heading.append(title, remove);

  const fields = document.createElement('div');
  fields.className = 'wealth-position-fields';
  const id = wealthInput('id', position.id || ''); id.maxLength = 64; id.pattern = '[A-Za-z0-9][A-Za-z0-9_.-]{0,63}';
  const label = wealthInput('label', position.label || ''); label.maxLength = 120;
  label.addEventListener('input', () => {title.textContent = label.value.trim() || 'Neue Position';});
  const kind = document.createElement('select'); kind.name = 'kind'; kind.required = true;
  for (const [value, text] of [['asset','Vermögenswert'],['liability','Verbindlichkeit'],['investment','Anlage']]) kind.append(new Option(text, value));
  kind.value = position.kind || 'asset';
  const amountInput = wealthInput('amount', position.amount || '', 'text');
  amountInput.inputMode = 'decimal'; amountInput.placeholder = '0,00';
  const currency = wealthInput('currency', 'EUR'); currency.readOnly = true;
  const valuedOn = wealthInput('valued_on', position.valued_on || $('wealth-as-of').value, 'date');
  const owner = wealthOwnerSelect(position.owner || '');
  fields.append(
    wealthField('Kennung', id), wealthField('Bezeichnung', label),
    wealthField('Art', kind), wealthField('Wert EUR', amountInput),
    wealthField('Währung', currency), wealthField('Bewertet am', valuedOn),
    wealthField('Zuordnung', owner),
  );
  item.append(heading, fields);
  item.addEventListener('input', wealthInvalidate);
  item.addEventListener('change', wealthInvalidate);
  return item;
}

function wealthRenderPositions(positions) {
  const list = $('wealth-positions');
  list.replaceChildren();
  for (const position of positions || []) list.append(wealthPosition(position));
  if (!list.children.length) {
    const empty = document.createElement('p');
    empty.className = 'muted wealth-empty';
    empty.textContent = 'Noch keine manuelle Position eingetragen.';
    list.append(empty);
  }
}

function wealthRenderGoals(progress=[]) {
  const saved = new Map((progress || []).map(item => [item.goal_id, item]));
  const list = $('wealth-goals');
  list.replaceChildren();
  const goals = Array.isArray(state?.profile_goals) ? state.profile_goals : [];
  for (const goal of goals) {
    const value = saved.get(goal.id) || {};
    const item = document.createElement('article');
    item.className = 'wealth-goal';
    item.dataset.goalId = goal.id;
    const heading = document.createElement('div');
    const label = document.createElement('strong'); label.textContent = goal.label;
    const target = document.createElement('span');
    target.textContent = `Ziel ${formatProfileGoalAmount(goal.target_amount, goal.currency)}`;
    heading.append(label, target);
    const fields = document.createElement('div'); fields.className = 'wealth-goal-fields';
    const current = wealthInput('current_amount', value.current_amount || '', 'text');
    current.required = false; current.inputMode = 'decimal'; current.placeholder = 'Noch nicht eingetragen';
    const valuedOn = wealthInput('valued_on', value.valued_on || '', 'date'); valuedOn.required = false;
    fields.append(wealthField(`Manueller Zielstand ${goal.currency}`, current), wealthField('Bewertet am', valuedOn));
    item.append(heading, fields);
    item.addEventListener('input', wealthInvalidate);
    item.addEventListener('change', wealthInvalidate);
    list.append(item);
  }
  if (!goals.length) {
    const empty = document.createElement('p'); empty.className = 'muted wealth-empty';
    empty.textContent = 'Im Profil sind keine finanziellen Ziele hinterlegt.'; list.append(empty);
  }
}

function wealthDecimal(value, {zero=true}={}) {
  const normalized = String(value ?? '').trim().replace(',', '.');
  if (!/^\d+(?:\.\d{1,2})?$/.test(normalized) || (!zero && Number(normalized) <= 0))
    throw new Error(zero ? 'Beträge müssen positiv oder null sein und höchstens zwei Nachkommastellen haben.' : 'Positionswerte müssen größer als null sein.');
  return Number(normalized).toFixed(2);
}

function wealthDraft() {
  const asOf = $('wealth-as-of').value;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(asOf)) throw new Error('Bitte einen gültigen Stichtag wählen.');
  const manualPositions = [];
  const identifiers = new Set();
  for (const item of $('wealth-positions').querySelectorAll('[data-position]')) {
    const value = name => item.querySelector(`[name="${name}"]`).value.trim();
    const id = value('id'), label = value('label'), owner = value('owner'), valuedOn = value('valued_on');
    if (!id || !label || !owner || !valuedOn) throw new Error('Bitte alle Felder jeder manuellen Position ausfüllen.');
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(id)) throw new Error('Kennungen dürfen nur Buchstaben, Ziffern, Punkt, Unterstrich und Bindestrich enthalten.');
    if (identifiers.has(id)) throw new Error('Jede manuelle Position benötigt eine eindeutige Kennung.');
    identifiers.add(id);
    manualPositions.push({id, label, kind:value('kind'), amount:wealthDecimal(value('amount'), {zero:false}), currency:'EUR', valued_on:valuedOn, owner});
  }
  const goalProgress = [];
  for (const item of $('wealth-goals').querySelectorAll('[data-goal-id]')) {
    const current = item.querySelector('[name="current_amount"]').value.trim();
    const valuedOn = item.querySelector('[name="valued_on"]').value;
    if (!current && !valuedOn) continue;
    if (!current || !valuedOn) throw new Error('Zu jedem eingetragenen Zielstand gehört ein Bewertungsdatum.');
    goalProgress.push({goal_id:item.dataset.goalId, current_amount:wealthDecimal(current), valued_on:valuedOn});
  }
  return {as_of:asOf, latest_revision:wealthLatestRevision, manual_positions:manualPositions, goal_progress:goalProgress};
}

function wealthRenderTotals(result) {
  const totals = result?.totals || {};
  $('wealth-assets').textContent = wealthCurrency(totals.assets);
  $('wealth-liabilities').textContent = wealthCurrency(totals.liabilities);
  $('wealth-net-worth').textContent = wealthCurrency(totals.net_worth);
  $('wealth-investments').textContent = wealthCurrency(totals.investments);
  const excluded = result?.excluded_depots;
  const count = Array.isArray(excluded) ? excluded.length : Number(excluded?.count ?? excluded ?? 0);
  $('wealth-depot-note').textContent = count
    ? `${count} Depotkonto${count === 1 ? '' : 'en'} nicht automatisch bewertet. Manuelle Anlagen bleiben ausdrücklich getrennt.`
    : 'Depotkonten werden nicht automatisch bewertet; es wurde kein Depotwert in die Summe übernommen.';
}

function wealthRenderHistory(history, selectedRevision) {
  wealthHistory = Array.isArray(history) ? history : wealthHistory;
  const select = $('wealth-history');
  select.replaceChildren(new Option('Aktueller Entwurf', ''));
  for (const item of [...wealthHistory].reverse()) {
    const label = `Revision ${item.revision} · ${item.as_of} · ${wealthCurrency(item.net_worth)}`;
    select.append(new Option(label, String(item.revision)));
  }
  select.value = selectedRevision ? String(selectedRevision) : '';
  $('wealth-history-load').disabled = !select.value;
}

function wealthApply(result, {historical=false}={}) {
  wealthReview = null;
  wealthRevision = result.revision ?? null;
  const newestHistory = Math.max(0, ...(result.history || wealthHistory).map(item => Number(item.revision) || 0));
  wealthLatestRevision = Number(result.latest_revision ?? newestHistory ?? wealthLatestRevision ?? 0);
  $('wealth-as-of').value = result.as_of || state?.as_of || '';
  wealthRenderPositions(result.manual_positions || []);
  wealthRenderGoals(result.goal_progress || []);
  wealthRenderTotals(result);
  wealthRenderHistory(result.history, historical ? wealthRevision : null);
  $('wealth-revision').textContent = wealthRevision === null
    ? 'Noch nicht gespeichert'
    : `${historical ? 'Historie' : 'Revision'} ${wealthRevision}`;
  $('wealth-save').disabled = true;
  wealthEditVersion += 1;
}

function wealthInvalidate() {
  wealthEditVersion += 1;
  wealthRequest += 1;
  wealthReview = null;
  $('wealth-save').disabled = true;
  wealthSetStatus('Eingaben geändert. Bitte eine neue Vorschau berechnen.');
}

async function wealthLoadLatest() {
  const request = ++wealthRequest;
  wealthSetStatus('Vermögensstand wird geladen …');
  try {
    const loaded = await api('/api/wealth-load', {});
    if (request !== wealthRequest) return false;
    const result = loaded.snapshot
      ? {...loaded.snapshot, latest_revision:loaded.history?.[0]?.revision || loaded.snapshot.revision, history:loaded.history}
      : {as_of:state?.as_of || '', revision:null, latest_revision:0, manual_positions:[], goal_progress:[], totals:{}, excluded_depots:[], history:loaded.history || []};
    wealthApply(result);
    wealthSetStatus(result.revision ? `Revision ${result.revision} geladen.` : 'Noch kein Vermögensstand gespeichert.');
    wealthInitialized = true;
    return true;
  } catch (error) {
    if (request === wealthRequest) wealthSetStatus(`Vermögensstand nicht geladen: ${error.message}`, true);
    throw error;
  }
}

async function wealthLoadRevision() {
  const revision = Number($('wealth-history').value);
  if (!Number.isInteger(revision) || revision < 1) throw new Error('Bitte eine gespeicherte Revision auswählen.');
  const request = ++wealthRequest;
  $('wealth-history-load').disabled = true;
  wealthSetStatus(`Revision ${revision} wird geladen …`);
  try {
    const loaded = await api('/api/wealth-load', {revision});
    if (request !== wealthRequest || $('wealth-history').value !== String(revision)) return false;
    if (!loaded.snapshot) throw new Error('Die gewählte Vermögensrevision ist nicht verfügbar.');
    const result = {...loaded.snapshot, latest_revision:loaded.history?.[0]?.revision || loaded.snapshot.revision, history:loaded.history};
    wealthApply(result, {historical:true});
    wealthSetStatus(`Historische Revision ${revision} geladen. Laden ändert keine Daten.`);
    return true;
  } catch (error) {
    if (request === wealthRequest) wealthSetStatus(`Historische Revision nicht geladen: ${error.message}`, true);
    throw error;
  } finally {
    if (request === wealthRequest) $('wealth-history-load').disabled = !$('wealth-history').value;
  }
}

async function wealthPreview() {
  const draft = wealthDraft(), request = ++wealthRequest, version = wealthEditVersion;
  wealthReview = null;
  $('wealth-preview').disabled = true;
  $('wealth-save').disabled = true;
  wealthSetStatus('Vorschau wird berechnet …');
  try {
    const result = await api('/api/wealth-preview', draft);
    if (request !== wealthRequest || version !== wealthEditVersion) return false;
    wealthReview = {result, draft};
    wealthRenderTotals(result);
    $('wealth-revision').textContent = `Vorschau für Revision ${result.next_revision}`;
    $('wealth-save').disabled = !result.review_token;
    wealthSetStatus(`Vorschau zum ${result.as_of || draft.as_of} geprüft. Speichern bleibt eine ausdrückliche Aktion.`);
    return true;
  } catch (error) {
    if (request === wealthRequest) wealthSetStatus(`Vorschau fehlgeschlagen: ${error.message}`, true);
    throw error;
  } finally {
    if (request === wealthRequest) $('wealth-preview').disabled = false;
  }
}

async function wealthSave() {
  if (!wealthReview?.result?.review_token) throw new Error('Bitte zuerst eine aktuelle Vorschau berechnen.');
  const {draft, result:review} = wealthReview;
  const request = ++wealthRequest, version = wealthEditVersion;
  $('wealth-preview').disabled = true;
  $('wealth-save').disabled = true;
  wealthSetStatus('Geprüfter Vermögensstand wird gespeichert …');
  try {
    const result = await api('/api/wealth-save', {...draft, review_token:review.review_token, confirmed:true});
    const history = [
      {revision:result.revision, as_of:result.as_of, created_at:result.created_at, net_worth:result.totals?.net_worth},
      ...wealthHistory.filter(item => Number(item.revision) !== Number(result.revision)),
    ].sort((first, second) => Number(second.revision) - Number(first.revision));
    if (request !== wealthRequest || version !== wealthEditVersion) {
      wealthLatestRevision = Number(result.latest_revision ?? result.revision ?? wealthLatestRevision);
      wealthRenderHistory(history, null);
      wealthSetStatus(`Revision ${result.revision} gespeichert. Deine zwischenzeitlichen Änderungen bleiben im Formular.`);
      return false;
    }
    wealthApply({...result, latest_revision:result.revision, history});
    wealthSetStatus(`Revision ${result.revision} zum ${result.as_of} gespeichert.`);
    return true;
  } catch (error) {
    if (request === wealthRequest) {
      $('wealth-revision').textContent = 'Vorschau nicht gespeichert';
      wealthSetStatus(`Speichern fehlgeschlagen oder Speicherstatus unklar: ${error.message} Bitte den aktuellen Stand neu laden.`, true);
    }
    throw error;
  } finally {
    if (request === wealthRequest) $('wealth-preview').disabled = false;
  }
}

$('wealth-position-add').addEventListener('click', () => {
  if ($('wealth-positions').querySelector('.wealth-empty')) $('wealth-positions').replaceChildren();
  $('wealth-positions').append(wealthPosition());
  wealthInvalidate();
});
$('wealth-as-of').addEventListener('input', wealthInvalidate);
$('wealth-history').addEventListener('change', () => {
  wealthRequest += 1;
  $('wealth-history-load').disabled = !$('wealth-history').value;
});
$('wealth-history-load').addEventListener('click', () => run(wealthLoadRevision));
$('wealth-preview').addEventListener('click', () => run(wealthPreview));
$('wealth-save').addEventListener('click', () => run(wealthSave));
document.addEventListener('finance-refreshed', () => {
  if (!wealthInitialized) run(wealthLoadLatest);
});
