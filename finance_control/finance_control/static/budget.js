'use strict';

let budgetLoaded = false;
let budgetDirty = false;
let budgetRevision = null;
let budgetRowCounter = 0;
let budgetLatestRevision = 0;
let budgetActiveRevision = 0;
let budgetEditVersion = 0;
let budgetActivationPending = false;
let paymentPolicyLoaded = false;
let paymentPolicyRequestId = 0;
let paymentPolicySelectedRevision = null;
let budgetOriginalPlan = {};

const budgetKindLabels = {income:'Einnahme', fixed:'Fixkosten', variable:'Variable Ausgabe'};
const paymentPolicyEndpoint = '/api/payment-policy-load';

function budgetIsActive(revision, activeRevision = budgetActiveRevision) {
  return Number(revision) === Number(activeRevision);
}
function budgetEntryIsActive(entry, activeRevision) {
  return typeof entry?.active === 'boolean' ? entry.active : budgetIsActive(entry?.revision, activeRevision);
}
function budgetResultRevisionIsActive(revision, result) {
  const entry = result.history?.find(item => Number(item.revision) === Number(revision));
  return entry ? budgetEntryIsActive(entry, result.active_revision) : budgetIsActive(revision, result.active_revision);
}

function paymentPolicyStatusLabel(status) {
  return {draft:'Entwurf', confirmed:'Bestätigt', review_needed:'Prüfung offen'}[status] || 'Status offen';
}
function paymentPolicyDate(value) { return value || 'offen'; }
function paymentPolicyTimestamp(value) {
  if (!value) return 'Zeit unbekannt';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('de-DE', {
    dateStyle:'medium', timeStyle:'short', timeZone:'Europe/Berlin',
  });
}
function paymentPolicyField(list, label, value, numeric = false) {
  const item = document.createElement('div'), term = document.createElement('dt'), description = document.createElement('dd');
  term.textContent = label; description.textContent = numeric ? eur(value) : value;
  item.append(term, description); list.append(item);
}
function paymentPolicyPhaseCard(phase, active, labels, historical) {
  const card = document.createElement('article'); card.className = `payment-policy-phase${active ? ' is-active' : ''}`;
  const head = document.createElement('div'); head.className = 'payment-policy-phase-head';
  const title = document.createElement('h4'); title.textContent = phase.name;
  const status = document.createElement('span'); status.className = `payment-policy-status${phase.status === 'confirmed' ? ' is-confirmed' : phase.status === 'review_needed' ? ' is-review-needed' : ''}`; status.textContent = paymentPolicyStatusLabel(phase.status);
  head.append(title, status); card.append(head);
  const inactivePeriod = `Bedingte Phase · Referenz ab ${paymentPolicyDate(phase.valid_from)} · Aktivierung durch Trigger`;
  const period = document.createElement('p'); period.className = 'muted payment-policy-phase-period'; period.textContent = !active ? inactivePeriod : historical ? 'In dieser Revision als aktiv markiert' : `Aktiv seit ${paymentPolicyDate(phase.valid_from)}${phase.valid_to ? ` bis ${phase.valid_to}` : ''}`; card.append(period);
  const values = document.createElement('dl'); values.className = 'payment-policy-values';
  if (Array.isArray(phase.allocations)) {
    for (const allocation of phase.allocations) {
      const participant = labels.get(allocation.person_id) || allocation.person_id;
      paymentPolicyField(values, `${participant} Quote`, `${(Number(allocation.share) * 100).toLocaleString('de-DE', {maximumFractionDigits:2})} %`);
      paymentPolicyField(values, `Einzahlung ${participant} Gemeinschaftskonto`, allocation.contribution, true);
      paymentPolicyField(values, `Frei verbleibend ${participant}`, allocation.free, true);
    }
  } else {
    paymentPolicyField(values, `${labels.a} Quote`, `${(Number(phase.person_a_share) * 100).toLocaleString('de-DE', {maximumFractionDigits:2})} %`);
    paymentPolicyField(values, `${labels.b} Quote`, `${(Number(phase.person_b_share) * 100).toLocaleString('de-DE', {maximumFractionDigits:2})} %`);
    paymentPolicyField(values, `Einzahlung ${labels.a} Gemeinschaftskonto`, phase.person_a_contribution, true);
    paymentPolicyField(values, `Einzahlung ${labels.b} Gemeinschaftskonto`, phase.person_b_contribution, true);
    paymentPolicyField(values, `Frei verbleibend ${labels.a}`, phase.person_a_free, true);
    paymentPolicyField(values, `Frei verbleibend ${labels.b}`, phase.person_b_free, true);
  }
  paymentPolicyField(values, 'Puffer Gemeinschaftskonto', phase.joint_buffer, true);
  paymentPolicyField(values, 'Sonder-/Übergangslast', phase.special_load === null || phase.special_load === undefined ? 'Keine' : eur(phase.special_load));
  paymentPolicyField(values, 'Trigger', phase.trigger || 'Keiner hinterlegt');
  paymentPolicyField(values, 'Quelle', phase.source || 'Nicht hinterlegt');
  card.append(values);
  return card;
}
function paymentPolicyPhaseGroup(parent, titleText, phases, active, labels, historical) {
  if (!phases.length) return;
  const group = document.createElement('section'); group.className = 'payment-policy-phase-group';
  const heading = document.createElement('h4'); heading.textContent = titleText; group.append(heading);
  const cards = document.createElement('div'); cards.className = 'payment-policy-phase-cards';
  for (const phase of phases) cards.append(paymentPolicyPhaseCard(phase, active, labels, historical));
  group.append(cards); parent.append(group);
}
function paymentPolicyRender(result) {
  const policy = result.policy, content = $('payment-policy-content'), principles = $('payment-policy-principle-list'), phases = $('payment-policy-phases');
  principles.replaceChildren(); phases.replaceChildren();
  const select = $('payment-policy-history');
  const selected = String(result.revision || 0);
  paymentPolicySelectedRevision = result.revision || null;
  if (select) select.value = selected;
  const selectedOption = select?.selectedOptions[0];
  $('payment-policy-history-meta').textContent = result.revision ? `Gespeichert am ${paymentPolicyTimestamp(selectedOption?.dataset.createdAt)} · Basisrevision ${result.base_revision ?? 'keine'}` : 'Noch keine Revision gespeichert.';
  const historical = Number(result.revision) < Number(result.latest_revision);
  $('payment-policy-history-note').hidden = !historical;
  $('payment-policy').classList.toggle('is-historical', historical);
  $('payment-policy-phase-hint').textContent = historical ? 'In dieser Revision als aktiv markierte Phase und weitere gespeicherte Zeiträume' : 'Aktive Phase und weitere gespeicherte Zeiträume';
  if (!policy) {
    $('payment-policy-revision').textContent = 'Nicht hinterlegt';
    $('payment-policy-status').textContent = 'Noch keine Zahlungslogik gespeichert.';
    content.hidden = true;
    return;
  }
  $('payment-policy-revision').textContent = `Revision ${result.revision}`;
  $('payment-policy-status').textContent = `${policy.title} · gespeichert, rein lesend.`;
  for (const principle of policy.principles || []) { const item = document.createElement('li'); item.textContent = principle; principles.append(item); }
  if (!principles.children.length) { const item = document.createElement('li'); item.textContent = 'Keine Grundprinzipien gespeichert.'; principles.append(item); }
  const labels = new Map((policy.participants || []).map(person => [person.id, person.label]));
  labels.a = policy.person_a_label || 'Person A'; labels.b = policy.person_b_label || 'Person B';
  const active = (policy.phases || []).filter(phase => phase.active), other = (policy.phases || []).filter(phase => !phase.active);
  paymentPolicyPhaseGroup(phases, historical ? 'Als aktiv markierte Phase' : 'Aktive Phase', active, true, labels, historical);
  paymentPolicyPhaseGroup(phases, 'Weitere Phasen', other, false, labels, historical);
  if (!phases.children.length) { const empty = document.createElement('p'); empty.className = 'muted'; empty.textContent = 'Keine Phasen gespeichert.'; phases.append(empty); }
  content.hidden = false;
}
function paymentPolicyRememberRevision(revision) {
  try { localStorage.setItem('finance-control-payment-policy-revision', String(revision)); } catch { /* Selection persistence is optional. */ }
}
function paymentPolicyStoredRevision() {
  try { const value = Number(localStorage.getItem('finance-control-payment-policy-revision')); return Number.isInteger(value) && value > 0 ? value : null; } catch { return null; }
}
function paymentPolicyPopulateHistory(history, selectedRevision) {
  const select = $('payment-policy-history');
  if (!select) return;
  select.replaceChildren();
  for (const entry of [...history].reverse()) {
    const option = document.createElement('option'); option.value = String(entry.revision);
    option.dataset.createdAt = entry.created_at || '';
    option.textContent = `Revision ${entry.revision} · ${entry.title} · ${paymentPolicyTimestamp(entry.created_at)}`;
    select.append(option);
  }
  if (history.length) select.value = String(selectedRevision);
  select.disabled = !history.length;
}
async function paymentPolicyLoad(force = false) {
  if (paymentPolicyLoaded && !force) return;
  const requestId = ++paymentPolicyRequestId;
  const select = $('payment-policy-history');
  if (select) select.disabled = true;
  try {
    const latest = await api(paymentPolicyEndpoint, {});
    if (requestId !== paymentPolicyRequestId) return;
    const history = latest.history || [];
    const storedRevision = paymentPolicyStoredRevision();
    const wanted = history.some(entry => entry.revision === storedRevision) ? storedRevision : latest.revision;
    let result = latest, rememberedLoadFailed = false;
    if (wanted && wanted !== latest.revision) {
      try { result = await api(paymentPolicyEndpoint, {revision:wanted}); }
      catch {
        if (requestId !== paymentPolicyRequestId) return;
        rememberedLoadFailed = true;
      }
    }
    if (requestId !== paymentPolicyRequestId) return;
    paymentPolicyRememberRevision(result.revision);
    paymentPolicyPopulateHistory(history, result.revision);
    paymentPolicyRender(result); paymentPolicyLoaded = true;
    if (rememberedLoadFailed) $('payment-policy-status').textContent = `${result.policy.title} · aktueller Stand. Die gemerkte ältere Revision konnte nicht geladen werden.`;
  } catch (error) {
    if (requestId !== paymentPolicyRequestId) return;
    if (select) {
      const option = document.createElement('option'); option.textContent = 'Nicht verfügbar';
      select.replaceChildren(option); select.disabled = true;
    }
    $('payment-policy-revision').textContent = 'Nicht verfügbar';
    $('payment-policy-status').textContent = `Zahlungslogik konnte nicht geladen werden. Prüfe die Verbindung und lade die Seite erneut. (${error.message})`;
    $('payment-policy-content').hidden = true;
  }
}
async function paymentPolicySelectRevision(revision) {
  const number = Number(revision);
  if (!Number.isInteger(number) || number < 1) return;
  const requestId = ++paymentPolicyRequestId;
  $('payment-policy-status').textContent = `Revision ${number} wird rein lesend geladen …`;
  $('payment-policy-history').disabled = true;
  try {
    const result = await api(paymentPolicyEndpoint, {revision:number});
    if (requestId !== paymentPolicyRequestId) return;
    paymentPolicyRememberRevision(result.revision);
    paymentPolicyRender(result); paymentPolicyLoaded = true;
  } catch (error) {
    if (requestId !== paymentPolicyRequestId) return;
    $('payment-policy-status').textContent = `Revision ${number} konnte nicht geladen werden. Die bisherige Ansicht bleibt erhalten. (${error.message})`;
    $('payment-policy-history').value = String(paymentPolicySelectedRevision || '');
  } finally {
    if (requestId === paymentPolicyRequestId) $('payment-policy-history').disabled = false;
  }
}

function budgetSetStatus(text) { $('budget-status').textContent = text || ''; }
function budgetInvalidatePlanActual(revision) {
  if (typeof planActualInitialized === 'undefined') return;
  planActualInitialized = false;
  const select = $('plan-actual-revision'), target = String(revision);
  if (select) {
    select.value = [...select.options].some(option => option.value === target) ? target : '';
    if (typeof planActualInvalidateComparison === 'function') planActualInvalidateComparison();
  }
}
function budgetNewId() {
  budgetRowCounter += 1;
  return `manual-${Date.now()}-${budgetRowCounter}`;
}
function budgetInvalidate() {
  budgetDirty = true;
  budgetEditVersion += 1;
  $('budget-result-details').hidden = true;
  $('budget-result-rows').replaceChildren();
  $('budget-warnings').hidden = true;
  $('budget-warnings').open = false;
  $('budget-chart').setAttribute('hidden', '');
  $('budget-chart').replaceChildren();
  $('budget-confidence').hidden = true;
  budgetClearPositionDetails();
  for (const id of ['budget-total-income','budget-total-expenses','budget-first-cashflow','budget-total-cashflow']) $(id).textContent = '—';
  $('budget-preview-status').textContent = 'Entwurf geändert. Bitte die Vorschau neu berechnen.';
}
function budgetClearPositionDetails() {
  $('budget-month-detail-rows').replaceChildren();
  $('budget-month-details').hidden = true;
  $('budget-month-details').open = false;
  $('budget-outside-horizon-list').replaceChildren();
  $('budget-outside-horizon').hidden = true;
  $('budget-detail-legacy').hidden = true;
}
function budgetHorizonMonths(plan) {
  const horizon = Number(plan && plan.horizon_months);
  return Number.isInteger(horizon) && horizon >= 1 && horizon <= 120 ? horizon : 12;
}
function budgetDescribePosition(item) {
  const cadence = {1:'monatlich',2:'alle zwei Monate',3:'vierteljährlich',6:'halbjährlich',12:'jährlich'}[Number(item.interval_months || 1)] || `alle ${item.interval_months} Monate`;
  return `${item.label} · ${budgetKindLabels[item.kind] || item.kind} · ${eur(item.amount)} je Zahlung · ${cadence} ab ${item.start_month} bis ${item.end_month || 'fortlaufend'} · ${item.confirmed ? 'bestätigt' : 'unbestätigt'} · Quelle: ${item.source}`;
}
function budgetRenderMonthDetails(period, items) {
  const details = $('budget-month-details'), body = $('budget-month-detail-rows');
  body.replaceChildren();
  $('budget-month-details-summary').textContent = `Positionen im ${period} (${items.length})`;
  for (const item of items) {
    const row = document.createElement('tr');
    cell(row, item.label); cell(row, budgetKindLabels[item.kind] || item.kind); cell(row, amount(item.amount), true);
    cell(row, item.source); cell(row, item.confirmed ? 'Bestätigt' : 'Unbestätigt'); body.append(row);
  }
  details.hidden = false;
  details.open = true;
}
function budgetControl(labelText, input, id) {
  const label = document.createElement('label');
  label.htmlFor = id;
  label.textContent = labelText;
  input.id = id;
  label.append(input);
  return label;
}
function budgetInput(type, value) {
  const input = document.createElement('input');
  input.type = type;
  input.value = value || '';
  return input;
}
function budgetUpdateItemHeading(item) {
  const label = item.querySelector('[data-budget-field="label"]').value.trim();
  const kind = item.querySelector('[data-budget-field="kind"]').value;
  item.querySelector('h3').textContent = `${budgetKindLabels[kind]} · ${label || 'ohne Bezeichnung'}`;
  item.dataset.confirmed = String(item.querySelector('[data-budget-field="confirmed"]').checked);
}
function budgetAddItem(values = {}, persisted = false) {
  const itemId = String(values.id || budgetNewId());
  const token = `budget-item-${++budgetRowCounter}`;
  const item = document.createElement('article');
  item.className = 'budget-item';
  item.dataset.itemId = itemId;
  item.dataset.persisted = String(persisted);
  const head = document.createElement('div'); head.className = 'budget-item-head';
  const heading = document.createElement('h3'); heading.id = `${token}-heading`; item.setAttribute('aria-labelledby', heading.id); head.append(heading);
  button(head, 'Position entfernen', () => { item.remove(); budgetUpdateCount(); budgetInvalidate(); });
  const grid = document.createElement('div'); grid.className = 'budget-item-grid';

  const labelInput = budgetInput('text', values.label); labelInput.required = true; labelInput.maxLength = 160; labelInput.dataset.budgetField = 'label';
  const kindSelect = document.createElement('select'); kindSelect.dataset.budgetField = 'kind';
  for (const kind of ['income','fixed','variable']) { const option = document.createElement('option'); option.value = kind; option.textContent = budgetKindLabels[kind]; kindSelect.append(option); }
  kindSelect.value = values.kind || 'variable';
  const amountInput = budgetInput('text', values.amount); amountInput.required = true; amountInput.inputMode = 'decimal'; amountInput.pattern = '\\d+(?:[.,]\\d{1,2})?'; amountInput.placeholder = '0,00'; amountInput.dataset.budgetField = 'amount';
  const intervalSelect = document.createElement('select'); intervalSelect.dataset.budgetField = 'interval_months';
  for (const [value, text] of [[1,'Monatlich'],[2,'Alle zwei Monate'],[3,'Vierteljährlich'],[6,'Halbjährlich'],[12,'Jährlich']]) { const option = document.createElement('option'); option.value = value; option.textContent = text; intervalSelect.append(option); }
  if (values.interval_months && ![1,2,3,6,12].includes(Number(values.interval_months))) { const option = document.createElement('option'); option.value = values.interval_months; option.textContent = `Alle ${values.interval_months} Monate`; intervalSelect.append(option); }
  intervalSelect.value = String(values.interval_months || 1);
  const startInput = budgetInput('month', values.start_month); startInput.required = true; startInput.dataset.budgetField = 'start_month';
  const endInput = budgetInput('month', values.end_month); endInput.dataset.budgetField = 'end_month';
  const sourceInput = budgetInput('text', values.source); sourceInput.required = true; sourceInput.maxLength = 240; sourceInput.placeholder = 'z. B. Vertrag, manuelle Annahme'; sourceInput.readOnly = persisted; sourceInput.dataset.budgetField = 'source';
  const confirmedInput = budgetInput('checkbox'); confirmedInput.checked = Boolean(values.confirmed); confirmedInput.dataset.budgetField = 'confirmed';
  const accountSelect = document.createElement('select'); accountSelect.dataset.budgetField = 'account_id';
  for (const [value, text] of [['', 'Kein Girokonto zugeordnet'], ...budgetSetupChoices('account', values.account_id)]) {
    const option = document.createElement('option'); option.value = value; option.textContent = text; accountSelect.append(option);
  }
  accountSelect.value = values.account_id || '';

  grid.append(
    budgetControl('Bezeichnung', labelInput, `${token}-label`),
    budgetControl('Art', kindSelect, `${token}-kind`),
    budgetControl('Betrag EUR je Zahlung', amountInput, `${token}-amount`),
    budgetControl('Zahlungsrhythmus', intervalSelect, `${token}-interval`),
    budgetControl('Gültig ab', startInput, `${token}-start`),
    budgetControl('Gültig bis (optional)', endInput, `${token}-end`),
    budgetControl('Girokonto (optional)', accountSelect, `${token}-account`)
  );
  const sourceLabel = budgetControl(persisted ? 'Quelle (nach dem Speichern unveränderlich)' : 'Quelle', sourceInput, `${token}-source`); sourceLabel.className = 'budget-source';
  const confirmedLabel = budgetControl('Position fachlich bestätigt', confirmedInput, `${token}-confirmed`); confirmedLabel.className = 'budget-confirmed';
  for (const [control, name] of [[labelInput,'Bezeichnung'],[kindSelect,'Art'],[amountInput,'Betrag'],[intervalSelect,'Zahlungsrhythmus'],[startInput,'Startmonat'],[endInput,'Endmonat'],[sourceInput,'Quelle'],[confirmedInput,'Bestätigung'],[accountSelect,'Girokonto']]) control.setAttribute('aria-label', `${name} der Position ${itemId}`);
  grid.append(sourceLabel, confirmedLabel);
  item.append(head, grid); $('budget-items').append(item);
  const syncMonthRange = () => { endInput.min = startInput.value; if (endInput.value && startInput.value && endInput.value < startInput.value) endInput.setCustomValidity('Der Endmonat darf nicht vor dem Startmonat liegen.'); else endInput.setCustomValidity(''); };
  item.addEventListener('input', () => { syncMonthRange(); budgetUpdateItemHeading(item); budgetInvalidate(); });
  item.addEventListener('change', () => { syncMonthRange(); budgetUpdateItemHeading(item); budgetInvalidate(); });
  syncMonthRange(); budgetUpdateItemHeading(item); budgetUpdateCount();
}
function budgetUpdateCount() {
  const count = $('budget-items').children.length;
  $('budget-item-count').textContent = String(count);
  $('budget-empty-items').hidden = count > 0;
  budgetRefreshPersonalChoices();
  budgetRefreshAllocationItems();
}
function budgetSetupSection(title, description) {
  const section = document.createElement('details'), summary = document.createElement('summary'), help = document.createElement('p');
  summary.textContent = title; help.textContent = description; help.className = 'muted'; section.append(summary, help); $('budget-setup').append(section);
  return section;
}
function budgetSetupChoices(type, value) {
  const accounts = (state?.accounts || []).filter(account => account.kind === 'CHECKING');
  const profileParticipants = typeof profilePeople === 'function' ? profilePeople().map(person => person.id) : [];
  const accountParticipants = (state?.accounts || []).flatMap(account => Object.keys(account.shares || {}));
  const choices = type === 'account' ? accounts.map(account => [account.id, accountDisplay(account)])
    : [...new Set([...profileParticipants, ...accountParticipants,
      ...(budgetOriginalPlan.household_split || []).map(entry => entry.party_id)])]
      .map(id => [id, profilePersonLabel(id)]);
  if (value && !choices.some(([id]) => id === value)) choices.push([value, `${value} (gespeicherter Wert)`]);
  return choices;
}
function budgetSetupField(parent, field, label, value, type = 'text', limit = 240) {
  let input;
  if (['account', 'party', 'direction'].includes(type)) {
    input = document.createElement('select');
    const choices = type === 'direction' ? [['inflow', 'Eingang'], ['outflow', 'Ausgang']] : [['', 'Bitte auswählen'], ...budgetSetupChoices(type, value)];
    for (const [id, text] of choices) { const option = document.createElement('option'); option.value = id; option.textContent = text; input.append(option); }
    input.value = value || '';
  } else {
    input = budgetInput(type === 'money' || type === 'signed' ? 'text' : type, value ?? '');
    if (type === 'money' || type === 'signed') { input.inputMode = 'decimal'; input.pattern = `${type === 'signed' ? '-?' : ''}\\d+(?:[.,]\\d{1,2})?`; input.placeholder = '0,00'; }
    if (type === 'text') input.maxLength = limit;
    if (type === 'number') { input.min = '1'; input.max = '100'; input.step = '1'; }
  }
  input.required = true; input.dataset.setupField = field;
  parent.append(budgetControl(label, input, `budget-setup-${++budgetRowCounter}`));
  return input;
}
function budgetSetupRow(parent, title, values = {}) {
  const row = document.createElement('article'); row.className = 'budget-setup-row';
  row.dataset.rowId = values.id || budgetNewId();
  const header = document.createElement('div'); header.className = 'budget-item-head';
  const heading = document.createElement('h4'); heading.textContent = title; header.append(heading);
  button(header, `${title} entfernen`, () => { row.remove(); budgetInvalidate(); });
  const grid = document.createElement('div'); grid.className = 'budget-setup-grid';
  row.append(header, grid); parent.append(row); return {row, grid};
}
function budgetSetupList(section, name, label, add, values, maximum) {
  const list = document.createElement('div'); list.dataset.setupList = name; section.append(list);
  for (const value of values || []) add(list, value);
  button(section, label, () => {
    if (list.children.length >= maximum) throw new Error(`Hier sind höchstens ${maximum} Einträge möglich.`);
    add(list, {}); section.open = true; budgetInvalidate();
  });
  return list;
}
function budgetAddCashflow(list, value) {
  const {grid} = budgetSetupRow(list, 'Erwartete Zahlung', value);
  budgetSetupField(grid, 'label', 'Bezeichnung', value.label, 'text', 160);
  budgetSetupField(grid, 'direction', 'Richtung', value.direction || 'outflow', 'direction');
  budgetSetupField(grid, 'amount', 'Betrag EUR', value.amount, 'money');
  budgetSetupField(grid, 'due_date', 'Erwartetes Buchungsdatum', value.due_date, 'date');
  budgetSetupField(grid, 'evidence', 'Quelle / Begründung', value.evidence);
}
function budgetAddPayday(list, value) {
  const {row, grid} = budgetSetupRow(list, 'Gehaltszeitraum', value);
  budgetSetupField(grid, 'account_id', 'Girokonto', value.account_id, 'account');
  budgetSetupField(grid, 'owner_label', 'Name für die Anzeige', value.owner_label, 'text', 120);
  budgetSetupField(grid, 'last_salary_date', 'Letzter Gehaltseingang', value.last_salary_date, 'date');
  budgetSetupField(grid, 'next_salary_date', 'Nächster Gehaltseingang', value.next_salary_date, 'date');
  budgetSetupField(grid, 'next_salary_basis', 'Grundlage des nächsten Termins', value.next_salary_basis);
  budgetSetupField(grid, 'target_balance', 'Gewünschter Kontostand vor dem Gehalt (EUR)', value.target_balance, 'signed');
  const help = document.createElement('p'); help.className = 'muted'; help.textContent = 'Erwartete Zahlungen einzeln erfassen. Betrag immer positiv; die Richtung legt Eingang oder Ausgang fest. Bereits gebuchte Zahlungen entfernen.'; row.append(help);
  budgetSetupList(row, 'cashflows', 'Erwartete Zahlung hinzufügen', budgetAddCashflow, value.cashflows, 100);
}
function budgetAddTransfer(list, value) {
  const {grid} = budgetSetupRow(list, 'Angekündigte Umbuchung', value);
  budgetSetupField(grid, 'label', 'Bezeichnung', value.label, 'text', 160);
  budgetSetupField(grid, 'from_account_id', 'Von Girokonto', value.from_account_id, 'account');
  budgetSetupField(grid, 'to_account_id', 'Nach Girokonto', value.to_account_id, 'account');
  budgetSetupField(grid, 'amount', 'Betrag EUR', value.amount, 'money');
  budgetSetupField(grid, 'value_date', 'Erwartete Wertstellung', value.value_date, 'date');
  budgetSetupField(grid, 'evidence', 'Quelle / Begründung', value.evidence);
}
function budgetPersonalChoices(container, selected) {
  container.replaceChildren();
  const choices = new Map([...$('budget-items').children].map(item => [item.dataset.itemId, item.querySelector('[data-budget-field="label"]').value || 'Ohne Bezeichnung']));
  for (const id of selected) if (!choices.has(id)) choices.set(id, `${id} (Position entfernt – Zuordnung bitte lösen)`);
  for (const [id, label] of choices) {
    const input = budgetInput('checkbox'); input.value = id; input.checked = selected.includes(id);
    const control = budgetControl(label, input, `budget-personal-${++budgetRowCounter}`); control.className = 'check'; container.append(control);
  }
  if (!choices.size) { const empty = document.createElement('p'); empty.textContent = 'Zuerst Budgetpositionen anlegen.'; container.append(empty); }
}
function budgetRefreshPersonalChoices() {
  for (const container of $('budget-setup').querySelectorAll('[data-personal-items]')) budgetPersonalChoices(container, [...container.querySelectorAll('input:checked')].map(input => input.value));
}
function budgetAddShare(list, value) {
  const {row, grid} = budgetSetupRow(list, 'Personenanteil', value);
  budgetSetupField(grid, 'party_id', 'Person', value.party_id, 'party');
  budgetSetupField(grid, 'percent', 'Anteil am gemeinsamen Überschuss (%)', value.share === undefined ? '' : Math.round(Number(value.share) * 100), 'number');
  const details = document.createElement('details'), summary = document.createElement('summary'); summary.textContent = 'Persönliche Budgetpositionen zuordnen';
  const choices = document.createElement('div'); choices.className = 'budget-personal-choices'; choices.dataset.personalItems = '';
  budgetPersonalChoices(choices, value.personal_item_ids || []); details.append(summary, choices); row.append(details);
}
function budgetRenderSetup(plan) {
  $('budget-setup').replaceChildren();
  const liquidity = budgetSetupSection('1 · Girokonten für „Heute verfügbar“', 'Nur ausgewählte Konten werden zusammengerechnet. Ohne Auswahl wird diese Kontostandsübersicht nicht berechnet.');
  const choices = document.createElement('div'); choices.id = 'budget-liquidity-accounts'; choices.className = 'budget-account-choices'; liquidity.append(choices);
  const accounts = new Map(budgetSetupChoices('account').map(choice => choice));
  for (const id of plan.liquidity_accounts || []) if (!accounts.has(id)) accounts.set(id, `${id} (gespeichertes Konto)`);
  for (const [id, label] of accounts) {
    const input = budgetInput('checkbox'); input.value = id; input.checked = (plan.liquidity_accounts || []).includes(id);
    const control = budgetControl(label, input, `budget-liquidity-${++budgetRowCounter}`); control.className = 'check'; choices.append(control);
  }
  if (!accounts.size) { const empty = document.createElement('p'); empty.textContent = 'Noch keine Girokonten angelegt.'; choices.append(empty); }
  const payday = budgetSetupSection('2 · Ausgaben bis zum nächsten Gehalt', 'Je Girokonto: letzter und nächster Gehaltseingang, Zielkontostand sowie erwartete Ein- und Ausgänge. Diese Angaben begrenzen den verfügbaren Betrag bis zum Gehalt.');
  budgetSetupList(payday, 'payday_cycles', 'Gehaltszeitraum hinzufügen', budgetAddPayday, plan.payday_cycles, 10);
  const transfers = budgetSetupSection('3 · Angekündigte Umbuchungen', 'Nur bereits veranlasste, noch nicht vollständig gebuchte Umbuchungen zwischen eigenen Girokonten. Nach vollständiger Buchung entfernen. Dies erfasst eine Planannahme und löst keine Überweisung aus.');
  budgetSetupList(transfers, 'pending_transfers', 'Umbuchung hinzufügen', budgetAddTransfer, plan.pending_transfers, 100);
  const split = budgetSetupSection('4 · Gemeinsamen Überschuss aufteilen', 'Anteile zusammen: 100 %. Persönlich zugeordnete Positionen werden separat der jeweiligen Person zugerechnet. Jede Position darf nur einer Person gehören. Ohne Einträge wird kein Personenanteil berechnet.');
  budgetSetupList(split, 'household_split', 'Personenanteil hinzufügen', budgetAddShare, plan.household_split, 10);
  budgetRenderAllocations(plan);
}
function budgetAllocationKey(value) { return JSON.stringify([value.account_id, value.external_id]); }
function budgetAllocationItemOptions(select, selected) {
  select.replaceChildren();
  const options = [['', 'Planposition auswählen'], ...[...$('budget-items').children].map(item => [item.dataset.itemId, `${budgetKindLabels[item.querySelector('[data-budget-field="kind"]').value]} · ${item.querySelector('[data-budget-field="label"]').value || 'Ohne Bezeichnung'}`])];
  if (selected && !options.some(([id]) => id === selected)) options.push([selected, `${selected} (Position entfernt)`]);
  for (const [id, text] of options) { const option = document.createElement('option'); option.value = id; option.textContent = text; select.append(option); }
  select.value = selected || '';
  const visibleLabel = select.parentElement?.querySelector('[data-allocation-item-label]');
  if (visibleLabel) visibleLabel.textContent = select.value ? select.selectedOptions[0].textContent : '';
}
function budgetRefreshAllocationItems() {
  for (const select of $('budget-setup').querySelectorAll('[data-allocation-item]')) budgetAllocationItemOptions(select, select.value);
}
function budgetAllocationSummary(group) {
  const entries = [...group.querySelector('[data-allocation-parts]').children];
  const weights = entries.map(entry => Number(entry.querySelector('[data-allocation-weight]').value.replace(',', '.')));
  const total = weights.reduce((sum, weight) => sum + weight, 0);
  group.querySelector('[data-allocation-summary]').textContent = entries.length === 1
    ? 'Die gesamte Buchung wird dieser Planposition zugeordnet.'
    : weights.every(weight => Number.isFinite(weight) && weight > 0) && total > 0
      ? `Aufteilung in der angezeigten Reihenfolge: ${weights.map(weight => `${(100 * weight / total).toLocaleString('de-DE', {maximumFractionDigits:2})} %`).join(' / ')}.`
      : 'Für jede Teilzuordnung ein positives Gewicht angeben, zum Beispiel 2 und 1 für zwei Drittel und ein Drittel.';
}
function budgetAddAllocationPart(group, value = {}) {
  const row = document.createElement('div'); row.className = 'budget-allocation-part';
  const select = document.createElement('select'); select.dataset.allocationItem = ''; select.required = true; budgetAllocationItemOptions(select, value.item_id);
  const weight = budgetInput('text', value.weight); weight.dataset.allocationWeight = ''; weight.inputMode = 'decimal'; weight.pattern = '\\d+(?:[.,]\\d{1,2})?'; weight.placeholder = 'z. B. 1 oder 2';
  const itemLabel = budgetControl('Planposition', select, `budget-allocation-item-${++budgetRowCounter}`), visibleLabel = document.createElement('small'); visibleLabel.dataset.allocationItemLabel = ''; itemLabel.append(visibleLabel);
  visibleLabel.textContent = select.value ? select.selectedOptions[0].textContent : '';
  select.addEventListener('change', () => { visibleLabel.textContent = select.value ? select.selectedOptions[0].textContent : ''; });
  row.append(itemLabel, budgetControl('Gewicht (für mehrere Positionen erforderlich)', weight, `budget-allocation-weight-${++budgetRowCounter}`));
  button(row, 'Teilzuordnung entfernen', () => { row.remove(); budgetAllocationSummary(group); budgetInvalidate(); });
  group.querySelector('[data-allocation-parts]').append(row);
  row.addEventListener('input', () => budgetAllocationSummary(group)); budgetAllocationSummary(group);
}
function budgetAllocationDescription(transaction) {
  return `${transaction.date} · ${transaction.counterparty || 'Ohne Gegenpartei'} · ${eur(transaction.amount)} · ${transaction.account_label || transaction.account_id}${transaction.description ? ` · ${transaction.description}` : ''}`;
}
function budgetAddAllocationGroup(list, key, entries, transaction = null) {
  const identity = budgetAllocationKey(key);
  const existing = [...list.children].find(row => row.dataset.allocationKey === identity);
  if (existing) { existing.scrollIntoView({block:'center'}); throw new Error('Diese Buchung ist bereits zugeordnet. Bitte die vorhandene Zuordnung bearbeiten.'); }
  const {row, grid} = budgetSetupRow(list, 'Buchungszuordnung'); grid.remove(); row.dataset.allocationKey = identity;
  if (transaction?.allocation_type) row.dataset.transactionType = transaction.allocation_type;
  const description = document.createElement('p'); description.className = 'budget-allocation-description'; description.textContent = transaction ? budgetAllocationDescription(transaction) : `${key.account_id} · Buchung ${key.external_id}`;
  row.append(description);
  const status = document.createElement('p'); status.className = 'muted'; status.setAttribute('role', 'status'); row.append(status);
  button(row, 'Buchungsdetails prüfen', async () => {
    status.textContent = 'Buchung wird geladen …';
    try {
      const result = await api('/api/classification-transaction-get', key);
      if (!row.isConnected) return;
      const tx = result.transaction; description.textContent = budgetAllocationDescription(tx);
      status.textContent = tx.confirmed && tx.category && !tx.is_transfer ? 'Kategorie bestätigt. Die Zuordnung wird beim Speichern erneut geprüft.' : 'Diese Buchung ist nicht mehr als Einnahme oder Ausgabe bestätigt. Zuordnung entfernen oder zuerst ihre Kategorie bestätigen.';
    } catch (error) { if (row.isConnected) status.textContent = `Buchungsdetails konnten nicht geladen werden: ${error.message}`; }
  });
  const parts = document.createElement('div'); parts.dataset.allocationParts = '';
  const summary = document.createElement('p'); summary.dataset.allocationSummary = ''; summary.className = 'muted'; summary.setAttribute('aria-live', 'polite'); row.append(parts, summary);
  for (const entry of entries.length ? entries : [{}]) budgetAddAllocationPart(row, entry);
  button(row, 'Weitere Planposition zuordnen', () => { budgetAddAllocationPart(row); budgetInvalidate(); });
  return row;
}
function budgetRenderAllocations(plan) {
  const section = budgetSetupSection('5 · Einzelne Buchungen Planpositionen zuordnen', 'Nur bestätigte Einnahmen und Ausgaben. Eine Einzelzuordnung hat Vorrang vor der Kategoriezuordnung. Für mehrere Positionen positive Gewichte angeben: 2 und 1 teilt die Buchung im Verhältnis 2:1. Die Summe muss nicht 100 sein. Entfernen stellt die normale Kategoriezuordnung wieder her.');
  const groups = document.createElement('div'); groups.id = 'budget-actual-allocations';
  const grouped = new Map();
  for (const entry of plan.actual_allocations || []) { const key = budgetAllocationKey(entry); if (!grouped.has(key)) grouped.set(key, []); grouped.get(key).push(entry); }
  for (const entries of grouped.values()) budgetAddAllocationGroup(groups, {account_id:entries[0].account_id, external_id:entries[0].external_id}, entries);
  const search = document.createElement('div'); search.className = 'budget-setup-grid'; search.dataset.budgetSearch = '';
  const query = budgetInput('search'), from = budgetInput('date', plan.start_month ? `${plan.start_month}-01` : ''), to = budgetInput('date');
  query.placeholder = 'Gegenpartei, Verwendungszweck oder Buchungs-ID';
  search.append(budgetControl('Bestätigte Buchung suchen', query, 'budget-allocation-query'), budgetControl('Buchungsdatum ab (optional)', from, 'budget-allocation-from'), budgetControl('Buchungsdatum bis (optional)', to, 'budget-allocation-to'));
  const results = document.createElement('div'); results.className = 'budget-allocation-results';
  const status = document.createElement('p'); status.className = 'muted'; status.setAttribute('role', 'status');
  let requestVersion = 0, currentPage = 0;
  const searchBookings = async page => {
    if (from.value && to.value && from.value > to.value) throw new Error('Das Von-Datum darf nicht nach dem Bis-Datum liegen.');
    const version = ++requestVersion; status.textContent = 'Bestätigte Buchungen werden gesucht …';
    const request = {reviewed:true, query:query.value.trim(), page, order:'date'};
    if (from.value) request.date_from = from.value; if (to.value) request.date_to = to.value;
    try {
      const result = await api('/api/classification-list', request);
      if (!section.isConnected || version !== requestVersion) return;
      currentPage = result.page; results.replaceChildren();
      const catalog = new Map((result.categories || []).map(category => [category.id, category.transaction_type]));
      const transactions = result.rows.filter(tx => tx.confirmed && tx.category && !tx.is_transfer);
      status.textContent = `Seite ${result.page + 1} von ${Math.max(1, result.pages)} · ${transactions.length} zuordenbare Buchungen auf dieser Seite.`;
      for (const tx of transactions) {
        const card = document.createElement('article'), description = document.createElement('p'); description.textContent = budgetAllocationDescription(tx); card.append(description);
        button(card, 'Dieser Buchung Planpositionen zuordnen', () => { const group = budgetAddAllocationGroup(groups, {account_id:tx.account_id, external_id:tx.external_id}, [], {...tx, allocation_type:catalog.get(tx.category)}); budgetInvalidate(); group.scrollIntoView({block:'center'}); }); results.append(card);
      }
      previous.disabled = result.page === 0; next.disabled = result.page + 1 >= result.pages;
    } catch (error) { if (section.isConnected && version === requestVersion) status.textContent = `Suche fehlgeschlagen: ${error.message}`; }
  };
  section.append(search);
  const actions = document.createElement('div'); actions.className = 'actions';
  button(actions, 'Bestätigte Buchungen suchen', () => searchBookings(0));
  const previous = button(actions, 'Vorige Ergebnisse', () => searchBookings(Math.max(0, currentPage - 1))), next = button(actions, 'Weitere Ergebnisse', () => searchBookings(currentPage + 1));
  previous.disabled = true; next.disabled = true;
  search.addEventListener('input', () => { requestVersion += 1; results.replaceChildren(); previous.disabled = true; next.disabled = true; status.textContent = 'Suchfilter geändert. Bitte die Suche erneut ausführen.'; });
  search.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); run(() => searchBookings(0)); } });
  section.append(actions, status, results, groups);
}
function budgetReadAllocations(items) {
  const entries = [], itemById = new Map(items.map(item => [item.id, item]));
  for (const group of $('budget-actual-allocations').children) {
    const [account_id, external_id] = JSON.parse(group.dataset.allocationKey), seen = new Set();
    const parts = [...group.querySelector('[data-allocation-parts]').children];
    if (!parts.length) throw new Error('Bitte eine Planposition wählen oder die leere Buchungszuordnung entfernen.');
    for (const part of parts) {
      const item_id = part.querySelector('[data-allocation-item]').value, weight = part.querySelector('[data-allocation-weight]').value.trim().replace(',', '.');
      if (!itemById.has(item_id)) throw new Error('Bitte eine vorhandene Planposition für jede Buchungszuordnung wählen.');
      if (seen.has(item_id)) throw new Error('Eine Buchung darf jeder Planposition nur einmal zugeordnet sein.'); seen.add(item_id);
      const expected = itemById.get(item_id).kind === 'income' ? 'income' : 'expense';
      if (group.dataset.transactionType && group.dataset.transactionType !== expected) throw new Error('Einnahmen können nur Einnahmepositionen zugeordnet werden; Ausgaben nur Ausgabepositionen.');
      if ((parts.length > 1 && !weight) || (weight && (!/^\d+(?:\.\d{1,2})?$/.test(weight) || Number(weight) <= 0 || Number(weight) > 999999999999.99))) throw new Error('Bei einer Aufteilung braucht jede Position ein positives Gewicht mit höchstens zwei Nachkommastellen.');
      entries.push({account_id, external_id, item_id, ...(weight ? {weight} : {})});
    }
  }
  if (entries.length > 20000) throw new Error('Höchstens 20.000 Einzelzuordnungen sind möglich.');
  return entries.length || Object.hasOwn(budgetOriginalPlan, 'actual_allocations') ? {actual_allocations:entries} : {};
}
function budgetReadSetup(items) {
  const result = {}, root = $('budget-setup');
  const selected = [...$('budget-liquidity-accounts').querySelectorAll('input:checked')].map(input => input.value);
  const checking = new Set((state?.accounts || []).filter(account => account.kind === 'CHECKING').map(account => account.id));
  const account = id => { if (!checking.has(id)) throw new Error('Bitte ein vorhandenes Girokonto auswählen.'); return id; };
  const rows = name => [...root.querySelector(`[data-setup-list="${name}"]`).children];
  const read = row => name => { const input = row.querySelector(`[data-setup-field="${name}"]`), value = input.value.trim(); if (!value) throw new Error(`Bitte „${input.parentElement.firstChild.textContent}“ ausfüllen.`); return value; };
  const money = text => { const value = text.replace(',', '.'); if (!/^-?\d+(?:\.\d{1,2})?$/.test(value) || Math.abs(Number(value)) > 999999999999.99) throw new Error('Bitte einen centgenauen Betrag im zulässigen Bereich eingeben.'); return value; };
  if (selected.length > 10) throw new Error('Bitte höchstens zehn Girokonten für „Heute verfügbar“ auswählen.');
  if (selected.length) result.liquidity_accounts = selected.map(account);
  const seenAccounts = new Set();
  result.payday_cycles = rows('payday_cycles').map(row => {
    const field = read(row), accountId = account(field('account_id'));
    if (seenAccounts.has(accountId)) throw new Error('Pro Girokonto ist nur ein Gehaltszeitraum möglich.'); seenAccounts.add(accountId);
    if (field('next_salary_date') <= field('last_salary_date')) throw new Error('Der nächste Gehaltseingang muss nach dem letzten liegen.');
    const cashflows = [...row.querySelector('[data-setup-list="cashflows"]').children].map(flow => { const f = read(flow); return {label:f('label'), direction:f('direction'), amount:money(f('amount')), due_date:f('due_date'), evidence:f('evidence')}; });
    return {account_id:accountId, owner_label:field('owner_label'), last_salary_date:field('last_salary_date'), next_salary_date:field('next_salary_date'), next_salary_basis:field('next_salary_basis'), target_balance:money(field('target_balance')), cashflows};
  });
  result.pending_transfers = rows('pending_transfers').map(row => {
    const field = read(row), from = account(field('from_account_id')), to = account(field('to_account_id'));
    if (from === to) throw new Error('Eine Umbuchung benötigt zwei verschiedene Girokonten.');
    return {id:row.dataset.rowId, label:field('label'), from_account_id:from, to_account_id:to, amount:money(field('amount')), value_date:field('value_date'), evidence:field('evidence')};
  });
  const seenParties = new Set(), seenItems = new Set(), itemIds = new Set(items.map(item => item.id));
  const split = rows('household_split').map(row => {
    const field = read(row), party = field('party_id'), percent = Number(field('percent'));
    if (seenParties.has(party)) throw new Error('Jede Person darf nur einmal in der Aufteilung vorkommen.'); seenParties.add(party);
    if (!Number.isInteger(percent) || percent < 1 || percent > 100) throw new Error('Bitte ganze Prozentanteile zwischen 1 und 100 eingeben.');
    const personal = [...row.querySelectorAll('[data-personal-items] input:checked')].map(input => input.value);
    for (const id of personal) { if (!itemIds.has(id)) throw new Error('Eine persönliche Position wurde entfernt. Bitte ihre Zuordnung lösen.'); if (seenItems.has(id)) throw new Error('Eine Budgetposition ist mehreren Personen zugeordnet. Bitte nur eine Person auswählen.'); seenItems.add(id); }
    return {party_id:party, share:(percent / 100).toFixed(2), personal_item_ids:personal};
  });
  if (split.length) {
    if (split.reduce((sum, entry) => sum + Math.round(Number(entry.share) * 100), 0) !== 100) throw new Error('Die Personenanteile müssen zusammen genau 100 % ergeben.');
    result.household_split = split;
  }
  for (const field of ['payday_cycles', 'pending_transfers']) if (!result[field].length && !Object.hasOwn(budgetOriginalPlan, field)) delete result[field];
  return result;
}
function budgetReadPlan() {
  const form = $('budget-form');
  const invalid = form.querySelector(':invalid');
  if (invalid) { for (let parent = invalid.parentElement; parent; parent = parent.parentElement) if (parent.tagName === 'DETAILS') parent.open = true; }
  if (!form.reportValidity()) throw new Error('Bitte die markierten Pflichtfelder, Beträge und Datumsangaben prüfen.');
  const items = [];
  for (const item of $('budget-items').children) {
    const read = name => item.querySelector(`[data-budget-field="${name}"]`);
    const accountId = read('account_id').value;
    if (accountId && !(state?.accounts || []).some(account => account.id === accountId && account.kind === 'CHECKING')) throw new Error('Bitte für die Planposition ein vorhandenes Girokonto auswählen oder die Kontozuordnung entfernen.');
    const amountText = read('amount').value.trim().replace(',', '.');
    if (!/^\d+(?:\.\d{1,2})?$/.test(amountText)) throw new Error('Beträge müssen nichtnegative, centgenaue Dezimalzahlen sein.');
    items.push({
      ...(accountId ? {account_id:accountId} : {}),
      id:item.dataset.itemId,
      label:read('label').value.trim(),
      kind:read('kind').value,
      amount:amountText,
      interval_months:Number(read('interval_months').value),
      start_month:read('start_month').value,
      end_month:read('end_month').value || null,
      source:read('source').value.trim(),
      confirmed:read('confirmed').checked
    });
  }
  const plan = {title:$('budget-title').value.trim(), start_month:$('budget-start-month').value, horizon_months:Number($('budget-horizon-months').value), notes:$('budget-notes').value.trim(), items};
  if (Object.hasOwn(budgetOriginalPlan, 'actual_mappings')) plan.actual_mappings = structuredClone(budgetOriginalPlan.actual_mappings);
  const itemById = new Map(items.map(item => [item.id, item]));
  const fallbackItem = itemById.get(budgetOriginalPlan.unmapped_expense_item_id);
  if (fallbackItem && fallbackItem.kind !== 'income') plan.unmapped_expense_item_id = fallbackItem.id;
  const cashItem = itemById.get(budgetOriginalPlan.cash_receipt_item_id);
  if (cashItem?.kind === 'variable') plan.cash_receipt_item_id = cashItem.id;
  try { return {...plan, ...budgetReadSetup(items), ...budgetReadAllocations(items)}; }
  catch (error) { budgetSetStatus(error.message); throw error; }
}
function budgetRenderHistory(history, activeRevision) {
  const body = $('budget-history-rows'); body.replaceChildren();
  for (const entry of history || []) {
    const row = document.createElement('tr'); cell(row, String(entry.revision));
    cell(row, `${entry.title}${budgetEntryIsActive(entry, activeRevision) ? ' · aktiv' : ''}`);
    cell(row, paymentPolicyTimestamp(entry.created_at));
    const actions = cell(row, '');
    button(actions, 'Laden / ableiten', async () => {
      if (budgetDirty && !window.confirm('Ungespeicherte Änderungen verwerfen?')) return;
      const version = budgetEditVersion;
      const result = await api('/api/budget-load', {revision:entry.revision});
      if (version !== budgetEditVersion) throw new Error('Entwurf inzwischen geändert; bitte erneut laden.');
      budgetShowSnapshot(result); cockpitNavigate('planning');
    });
    if (budgetEntryIsActive(entry, activeRevision)) {
      const status = document.createElement('strong'); status.textContent = 'Aktive Planung'; actions.append(status);
    } else {
      const activateButton = button(actions, budgetActivationPending ? 'Aktivierung läuft …' : 'Als aktive Planung übernehmen', async () => {
      if (budgetActivationPending) return;
      if (budgetDirty && !window.confirm('Ungespeicherte Änderungen verwerfen und diese Revision aktivieren?')) return;
      const version = budgetEditVersion;
      budgetActivationPending = true;
      for (const control of $('budget-history-rows').querySelectorAll('button')) {
        if (control.textContent.includes('aktive Planung übernehmen')) { control.disabled = true; control.textContent = 'Aktivierung läuft …'; }
      }
      try {
        const result = await api('/api/budget-activate', {revision:entry.revision, active_revision:activeRevision});
        if (version !== budgetEditVersion) {
          budgetActiveRevision = result.active_revision ?? entry.revision;
          if (Array.isArray(result.history)) budgetRenderHistory(result.history, budgetActiveRevision);
          $('budget-revision').textContent = budgetRevision === null
            ? 'Noch nicht gespeichert'
            : `Revision ${budgetRevision}${budgetResultRevisionIsActive(budgetRevision, result) ? ' · aktive Planung' : ' · nicht aktiv'}`;
          budgetSetStatus(`Revision ${entry.revision} aktiviert. Deine zwischenzeitlichen Änderungen bleiben im Formular.`);
          budgetInvalidatePlanActual(budgetActiveRevision);
          return;
        }
        budgetShowSnapshot(result);
        budgetInvalidatePlanActual(result.active_revision ?? entry.revision);
        cockpitNavigate('planning');
      } finally {
        budgetActivationPending = false;
        for (const control of $('budget-history-rows').querySelectorAll('button')) {
          if (control.textContent.includes('Aktivierung läuft')) {
            control.disabled = false; control.textContent = 'Als aktive Planung übernehmen';
          }
        }
        if (activateButton.isConnected) { activateButton.disabled = false; activateButton.textContent = 'Als aktive Planung übernehmen'; }
      }
      });
    }
    for (const [label, key] of [['CSV','download_url'],['JSON','snapshot_url']]) button(actions, label, async () => {
      const result = await api('/api/budget-export', {revision:entry.revision}); download(result[key]);
    });
    body.append(row);
  }
  for (const id of ['budget-first','budget-second']) {
    const select = $(id), previous = select.value; select.replaceChildren();
    for (const entry of history || []) {
      const option = document.createElement('option'); option.value = entry.revision;
      option.dataset.horizonMonths = String(budgetHorizonMonths(entry));
      option.textContent = `R${entry.revision} · ${entry.title}${budgetEntryIsActive(entry, activeRevision) ? ' · aktiv' : ''} · ab ${entry.start_month} · ${budgetHorizonMonths(entry)} Monate`; select.append(option);
    }
    if ([...select.options].some(o => o.value === previous)) select.value = previous;
  }
  if ((history || []).length > 1 && $('budget-first').value === $('budget-second').value) $('budget-second').selectedIndex = history.length - 1;
  $('budget-compare').disabled = !(history || []).length;
  $('budget-comparison').hidden = true;
  $('budget-empty-history').hidden = Boolean((history || []).length);
}
function budgetRenderPlan(result) {
  const plan = result.plan;
  budgetOriginalPlan = structuredClone(plan || {});
  budgetEditVersion += 1;
  budgetLatestRevision = result.latest_revision ?? result.revision;
  budgetActiveRevision = result.active_revision ?? budgetLatestRevision;
  budgetRevision = result.revision === undefined ? null : result.revision;
  $('budget-revision').textContent = plan && budgetRevision !== null
    ? `Revision ${budgetRevision}${budgetIsActive(budgetRevision) ? ' · aktive Planung' : ' · nicht aktiv'}`
    : 'Noch nicht gespeichert';
  $('budget-title').value = plan ? plan.title : '';
  $('budget-start-month').value = plan ? plan.start_month : '';
  $('budget-horizon-months').value = String(budgetHorizonMonths(plan));
  $('budget-notes').value = plan ? plan.notes : '';
  $('budget-items').replaceChildren();
  for (const item of (plan ? plan.items : [])) budgetAddItem(item, true);
  budgetRenderSetup(plan || {});
  budgetUpdateCount(); budgetRenderHistory(result.history, budgetActiveRevision);
  budgetDirty = false; budgetLoaded = true;
  return plan;
}
function budgetRenderCalculation(result) {
  const rows = result.rows || [];
  budgetClearPositionDetails();
  $('budget-total-income').textContent = eur(result.totals.income);
  $('budget-total-expenses').textContent = eur(result.totals.expenses);
  $('budget-first-cashflow').textContent = rows.length ? eur(rows[0].cashflow) : '—';
  $('budget-total-cashflow').textContent = eur(result.totals.cashflow);
  const range = rows.length ? ` Zeitraum ${rows[0].period} bis ${rows.at(-1).period}.` : '';
  $('budget-preview-status').textContent = `Berechnete Veränderung des Budgetentwurfs.${range} Kein Anfangsbestand und keine automatische Übernahme aus älteren Planungen.`;
  const warnings = result.warnings || [], warningBox = $('budget-warnings'), warningList = $('budget-warning-list'); warningBox.open = false; warningList.replaceChildren();
  if (warnings.length) { $('budget-warning-summary').textContent = `${warnings.length} ${warnings.length === 1 ? 'Position noch unbestätigt' : 'Positionen noch unbestätigt'} — Annahmen werden mitgerechnet`; for (const warning of warnings) { const item = document.createElement('li'); item.textContent = warning; warningList.append(item); } warningBox.hidden = false; } else warningBox.hidden = true;
  const hasBreakdown = budgetHasBreakdown(result);
  $('budget-confidence').hidden = !hasBreakdown;
  if (hasBreakdown) {
    $('budget-estimate-count').textContent = warnings.length === 1 ? '1 Schätzwert' : `${warnings.length} Schätzwerte`;
    $('budget-confirmed-change').textContent = eur(result.totals.confirmed.cashflow);
    $('budget-estimated-income').textContent = eur(result.totals.estimated.income);
    $('budget-estimated-expenses').textContent = eur(result.totals.estimated.expenses);
  }
  $('budget-chart').toggleAttribute('hidden', rows.length === 0);
  const series = [{name:'Mit allen Positionen', values:rows.map(row => row.cumulative)}];
  if (hasBreakdown) series.push({name:'Nur bestätigte Positionen', values:rows.map(row => row.confirmed.cumulative)});
  chart('budget-chart', series, rows.map(row => row.period));
  const body = $('budget-result-rows'); body.replaceChildren();
  const hasPositionDetails = rows.every(entry => Array.isArray(entry.items)) && Array.isArray(result.outside_horizon);
  if (hasPositionDetails) {
    const outside = result.outside_horizon;
    if (outside.length) {
      $('budget-outside-horizon-heading').textContent = `${outside.length} Position${outside.length === 1 ? '' : 'en'} außerhalb des Vorschauzeitraums`;
      for (const item of outside) { const li = document.createElement('li'); li.textContent = budgetDescribePosition(item); $('budget-outside-horizon-list').append(li); }
      $('budget-outside-horizon').hidden = false;
    }
  } else $('budget-detail-legacy').hidden = false;
  for (const entry of rows) {
    const row = document.createElement('tr'); cell(row, entry.period);
    for (const key of ['income','fixed','variable','cashflow','cumulative']) cell(row, amount(entry[key]), true);
    const actions = cell(row, '');
    if (hasPositionDetails) button(actions, `Positionen ${entry.period}`, () => budgetRenderMonthDetails(entry.period, entry.items));
    else actions.textContent = 'Nicht verfügbar';
    body.append(row);
  }
  $('budget-result-details').hidden = rows.length === 0;
}
function budgetHasBreakdown(result) {
  const rows = result?.rows || [];
  return Boolean(result?.totals?.confirmed && result?.totals?.estimated
    && rows.every(row => row.confirmed && row.estimated));
}
function budgetCoreSignature(result) {
  const rowKeys = ['period','income','fixed','variable','cashflow','cumulative'];
  return JSON.stringify({
    rows:(result?.rows || []).map(row => Object.fromEntries(rowKeys.map(key => [key,row[key]]))),
    totals:Object.fromEntries(['income','expenses','cashflow'].map(key => [key,result?.totals?.[key]]))
  });
}
function budgetMergeBreakdown(stored, current) {
  return {
    ...stored,
    rows:stored.rows.map((row, index) => ({
      ...row,
      confirmed:current.rows[index].confirmed,
      estimated:current.rows[index].estimated,
      items:Array.isArray(row.items) ? row.items : current.rows[index].items
    })),
    totals:{...stored.totals, confirmed:current.totals.confirmed, estimated:current.totals.estimated},
    outside_horizon:Array.isArray(stored.outside_horizon) ? stored.outside_horizon : current.outside_horizon
  };
}
async function budgetPlanRequest(route, payload) {
  try { return await api(route, payload); }
  catch (error) {
    const explanations = {
      'actual allocation must reference a classified transaction':'Eine zugeordnete Buchung ist nicht mehr als Einnahme oder Ausgabe bestätigt. Bitte ihre Kategorie prüfen oder die Einzelzuordnung entfernen.',
      'actual allocation transaction and item have incompatible types':'Eine Buchungszuordnung verbindet Einnahmen und Ausgaben. Bitte eine Planposition derselben Art wählen.',
      'actual allocation references an unknown item':'Eine zugeordnete Planposition wurde entfernt. Bitte die Einzelzuordnung ändern oder entfernen.',
      'actual mapping references an unknown item':'Eine entfernte Planposition hat noch eine Kategoriezuordnung. Bitte zuerst unter Plan/Ist diese Zuordnung lösen.'
    };
    const message = explanations[error.message] || error.message; budgetSetStatus(message); throw new Error(message);
  }
}
async function budgetCalculate(plan = null) {
  const current = plan || budgetReadPlan();
  const version = budgetEditVersion;
  const result = await budgetPlanRequest('/api/budget-calculate', {plan:current});
  if (version !== budgetEditVersion) return;
  budgetRenderCalculation(result);
  budgetSetStatus('Vorschau berechnet; der Entwurf wurde dadurch nicht gespeichert.');
}
async function budgetLoad(force = false) {
  if (!state || !state.csrf) return;
  await paymentPolicyLoad(force);
  if (budgetLoaded && !force) return;
  if (budgetDirty && !force) return;
  const version = budgetEditVersion;
  const result = await api('/api/budget-load', {});
  if (version !== budgetEditVersion) return;
  const plan = budgetRenderPlan(result);
  if (plan) {
    let calculation = result.calculation, derivedBreakdown = false;
    if (!budgetHasBreakdown(calculation)) {
      const renderedVersion = budgetEditVersion;
      try {
        const current = await api('/api/budget-calculate', {plan});
        if (renderedVersion !== budgetEditVersion) return;
        if (budgetCoreSignature(current) === budgetCoreSignature(calculation)) {
          calculation = budgetMergeBreakdown(calculation, current);
          derivedBreakdown = true;
        }
      } catch {
        if (renderedVersion !== budgetEditVersion) return;
      }
    }
    budgetRenderCalculation(calculation); budgetSnapshotStatus(result, derivedBreakdown);
  }
  else {
    budgetInvalidate(); budgetDirty = false;
    $('budget-preview-status').textContent = 'Noch keine Vorschau berechnet. Der leere Entwurf enthält keine angenommenen Beträge.';
    budgetSetStatus('Kein gespeicherter Budgetentwurf vorhanden.');
  }
}

$('budget-form').addEventListener('input', event => { if (event.target.closest('[data-budget-search]')) return; if (event.target.dataset.budgetField === 'label') { budgetRefreshPersonalChoices(); budgetRefreshAllocationItems(); } if (!event.target.closest('.budget-item')) budgetInvalidate(); });
$('budget-form').addEventListener('change', event => { if (event.target.closest('[data-budget-search]')) return; if (event.target.dataset.budgetField === 'kind') budgetRefreshAllocationItems(); if (!event.target.closest('.budget-item')) budgetInvalidate(); });
$('payment-policy-history').addEventListener('change', event => { void paymentPolicySelectRevision(event.target.value); });
$('budget-form').addEventListener('submit', event => { event.preventDefault(); run(budgetCalculate); });
for (const [id, kind] of [['budget-add-income','income'],['budget-add-fixed','fixed'],['budget-add-variable','variable']]) {
  $(id).addEventListener('click', () => { budgetAddItem({kind, start_month:$('budget-start-month').value}); $('budget-items-details').open = true; budgetInvalidate(); });
}
$('budget-save').addEventListener('click', () => run(async () => {
  const plan = budgetReadPlan();
  const version = budgetEditVersion;
  const result = await budgetPlanRequest('/api/budget-save', {revision:budgetLatestRevision, base_revision:budgetRevision || null, plan});
  budgetLatestRevision = result.latest_revision;
  if (version !== budgetEditVersion) { budgetSetStatus(`Revision ${result.revision} gespeichert. Deine zwischenzeitlichen Änderungen bleiben im Formular.`); return; }
  budgetShowSnapshot(result);
}));
$('budget-reload').addEventListener('click', () => run(async () => {
  if (budgetDirty && !window.confirm('Ungespeicherte Änderungen verwerfen und den gespeicherten Stand neu laden?')) return;
  budgetDirty = false;
  await budgetLoad(true);
}));

function budgetShowSnapshot(result) {
  budgetRenderPlan(result); budgetRenderCalculation(result.calculation);
  budgetSnapshotStatus(result);
}
function budgetSnapshotStatus(result, derivedBreakdown = false) {
  const activation = budgetIsActive(result.revision, result.active_revision)
    ? 'Aktive Planung.' : `Nicht aktiv; aktiv ist R${result.active_revision}.`;
  budgetSetStatus(`Revision ${result.revision} geladen; Grundlage ${result.base_revision ? 'R' + result.base_revision : 'nicht hinterlegt'}. ${activation} ${result.calculation_status === 'saved' ? 'Gespeichertes Ergebnis.' : 'Altbestand: Ergebnis mit aktuellem Rechenmodell neu berechnet.'}${derivedBreakdown ? ' Anteile und Positionsdetails wurden lesend ergänzt; die gespeicherten Monats- und Gesamtwerte stimmen exakt überein.' : ''}`);
}
for (const id of ['budget-first','budget-second']) $(id).addEventListener('change', () => { $('budget-comparison').hidden = true; budgetClearPositionDetails(); });
$('budget-compare').addEventListener('click', () => run(async () => {
  $('budget-comparison').hidden = true;
  budgetClearPositionDetails();
  const first = Number($('budget-first').value), second = Number($('budget-second').value);
  const selected = id => $(id).selectedOptions[0];
  const firstHorizon = budgetHorizonMonths({horizon_months:selected('budget-first').dataset.horizonMonths});
  const secondHorizon = budgetHorizonMonths({horizon_months:selected('budget-second').dataset.horizonMonths});
  if (firstHorizon !== secondHorizon) throw new Error(`Die Revisionen haben unterschiedliche Vorschauzeiträume (${firstHorizon} und ${secondHorizon} Monate) und können nicht verglichen werden.`);
  const result = await api('/api/budget-compare', {first, second});
  if (first !== Number($('budget-first').value) || second !== Number($('budget-second').value)) return;
  chart('budget-comparison-chart', [{name:`R${first}`,values:result.rows.map(r=>r.first)}, {name:`R${second}`,values:result.rows.map(r=>r.second)}], result.rows.map(r=>r.period));
  const horizon = result.rows.length;
  $('budget-comparison-summary').textContent = `Differenz R${second} minus R${first} nach ${horizon} Monaten: ${eur(result.rows.at(-1).difference)}. Veränderung ohne Anfangsbestand. ${[result.first,result.second].some(r=>r.calculation_status !== 'saved') ? 'Altbestand enthalten: dessen Ergebnis wurde neu berechnet.' : 'Beide Ergebnisse gespeichert.'} Unbestätigte Positionen: R${first}: ${result.first.calculation.warnings.length}, R${second}: ${result.second.calculation.warnings.length}.`;
  const body = $('budget-comparison-rows'); body.replaceChildren();
  for (const r of result.rows) { const row = document.createElement('tr'); cell(row,r.period); for (const k of ['first','second','difference','cashflow_difference']) cell(row,amount(r[k]),true); body.append(row); }
  const describe = i => i ? budgetDescribePosition(i) : 'Nicht enthalten';
  const changes = $('budget-comparison-changes'); changes.replaceChildren();
  for (const c of result.changes) { const li = document.createElement('li'); li.textContent = `${c.id}: ${describe(c.before)} → ${describe(c.after)}`; changes.append(li); }
  if (!result.changes.length) { const li = document.createElement('li'); li.textContent = 'Keine geänderten Positionen.'; changes.append(li); }
  $('budget-comparison-notes').textContent = result.notes_changed ? `Notizen R${first}: ${result.first.plan.notes} | R${second}: ${result.second.plan.notes}` : 'Notizen unverändert.';
  $('budget-comparison').hidden = false;
}));
