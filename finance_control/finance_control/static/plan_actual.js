'use strict';

let planActualBudget = null;
let planActualCatalog = null;
let planActualInitialized = false;
let planActualBusy = false;
let planActualRequest = 0;
let planActualMappingBusy = false;
let planActualDailyView = 'TOTAL';
let planActualTrendRange = 'month';
let planActualTrendGranularity = 'monthly';
let planActualTrendCumulative = true;
let planActualAvailableFromMonth = '';
let planActualEnsurePromise = null;

async function planActualEnsureTrend() {
  const current = window.planActualLatestResult;
  if (!current || planActualTrendRange === 'month' || current.monthly_trend) return current;
  const revision = $('plan-actual-revision').value;
  const month = $('plan-actual-month').value;
  const personBreakdown = $('plan-actual-person-toggle').checked;
  const enriched = await api('/api/budget-actual', {
    revision: Number(revision), month, person_breakdown: personBreakdown, include_trend: true,
  });
  if (revision !== $('plan-actual-revision').value || month !== $('plan-actual-month').value
      || personBreakdown !== $('plan-actual-person-toggle').checked) return null;
  window.planActualLatestResult = enriched;
  if (enriched.available_from_month) $('plan-actual-month').min = enriched.available_from_month;
  planActualUpdateMonthNavigation();
  return enriched;
}

async function planActualRefreshTrend() {
  try {
    const result = await planActualEnsureTrend();
    if (result) planActualRenderDaily(result.daily, result.daily_metadata,
      result.daily_by_person, result.account_balance_change);
  } catch (error) {
    planActualSetStatus(`Verlauf konnte nicht geladen werden: ${error.message}`, true);
  }
}

const planActualStatusLabels = {
  within: 'Im Rahmen',
  exact: 'Exakt im Plan',
  unbudgeted: 'Ohne Budget',
  exceeded: 'Überschritten',
  no_actual: 'Noch keine Buchung',
  income: 'Einnahme erfasst',
};

function planActualSignedEur(value, direction = null) {
  const numeric = Number(value || 0);
  if (!numeric) return eur(0);
  const signed = direction === 'income' ? Math.abs(numeric)
    : direction === 'expense' ? -Math.abs(numeric) : numeric;
  return `${signed > 0 ? '+' : '−'}${eur(Math.abs(signed))}`;
}

function planActualExpenseValue(value) {
  const amount = Math.abs(Number(value || 0));
  return amount ? -amount : 0;
}
function planActualIncomeValue(value) { return Math.abs(Number(value || 0)); }
function planActualVariance(actual, planned) { return Number(actual || 0) - Number(planned || 0); }
function planActualPersonTotals(breakdown, ids) {
  const sum = (field, convert) => ids.reduce((total, id) => total
    + convert(breakdown.totals[field]?.[id]), 0);
  const income = {
    planned: sum('planned_income', planActualIncomeValue),
    actual: sum('actual_income', planActualIncomeValue),
  };
  income.variance = planActualVariance(income.actual, income.planned);
  const expenses = {
    planned: sum('planned_expenses', planActualExpenseValue),
    actual: sum('actual_expenses', planActualExpenseValue),
  };
  expenses.variance = planActualVariance(expenses.actual, expenses.planned);
  const balance = {
    planned: income.planned + expenses.planned,
    actual: income.actual + expenses.actual,
  };
  balance.variance = planActualVariance(balance.actual, balance.planned);
  return {income, expenses, balance};
}
function planActualPersonDetailRows(result, personId) {
  const rows = (result.rows || []).filter(row =>
    Number(row.planned_by_person?.[personId] || 0)
      || Number(row.actual_by_person?.[personId] || 0))
    .map(row => {
      const kind = row.kind === 'income' ? 'income' : 'expense';
      const convert = kind === 'income' ? planActualIncomeValue : planActualExpenseValue;
      return {row, kind, planned: convert(row.planned_by_person?.[personId]),
        actual: convert(row.actual_by_person?.[personId])};
    });
  for (const [kind, category, key, convert] of [
    ['income', 'Einnahmen', 'income', planActualIncomeValue],
    ['expense', 'Ausgaben', 'expenses', planActualExpenseValue],
  ]) {
    const unmapped = convert(result.person_breakdown?.unmapped_by_person?.[key]?.[personId]);
    if (unmapped) rows.push({kind, row: {label: `Ist-${category} ohne zugeordnete Planposition`},
      planned: result.basis?.type === 'retrospective_reference' ? unmapped : 0, actual: unmapped});
  }
  return rows.sort((left, right) => Math.abs(planActualVariance(right.actual, right.planned))
    - Math.abs(planActualVariance(left.actual, left.planned)));
}

function planActualRenderRetrospectivePositions(card, positions, personId) {
  const selected = (positions || []).filter(position => position.person_id === personId);
  if (!selected.length) {
    const heading = document.createElement('h5'); heading.textContent = 'Tatsächliche Monatspositionen';
    heading.className = 'plan-actual-retrospective-heading'; card.append(heading);
    const list = document.createElement('div'); list.className = 'plan-actual-retrospective-list';
    const empty = document.createElement('p'); empty.className = 'muted';
    empty.textContent = 'Keine tatsächlichen Monatspositionen zugeordnet.'; list.append(empty);
    card.append(list);
    return true;
  }
  const disclosure = document.createElement('details');
  disclosure.className = 'plan-actual-retrospective-disclosure';
  const summary = document.createElement('summary');
  const transactionCount = selected.reduce((total, position) =>
    total + Number(position.transaction_count || 0), 0);
  const positionLabel = selected.length === 1 ? 'tatsächliche Position' : 'tatsächliche Positionen';
  const transactionLabel = transactionCount === 1 ? 'Buchung' : 'Buchungen';
  summary.textContent = `${selected.length} ${positionLabel} · ${transactionCount} ${transactionLabel}`;
  const list = document.createElement('div'); list.className = 'plan-actual-retrospective-list';
  for (const position of selected) {
    const entry = document.createElement('article'); entry.className = 'plan-actual-retrospective-position';
    const top = document.createElement('div'); top.className = 'plan-actual-retrospective-position-head';
    const label = document.createElement('strong'); label.textContent = position.label || 'Ohne Empfänger oder Zweck';
    const amount = document.createElement('strong');
    amount.textContent = planActualSignedEur(position.signed_amount);
    top.append(label, amount); entry.append(top);
    const metadata = document.createElement('p'); metadata.className = 'muted';
    const account = position.account_id ? accountDisplayById(position.account_id) : '';
    metadata.textContent = [position.category_label, account,
      `${Number(position.transaction_count || 0)} Buchungen`].filter(Boolean).join(' · ');
    entry.append(metadata);
    const transactions = Array.isArray(position.transactions) ? position.transactions : [];
    if (transactions.length) {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = `Originalbuchungen ansehen (${transactions.length})`;
      details.append(summary, planActualDetailTable({
        total_count: transactions.length, page_size: transactions.length, page: 1,
        context: {type: 'retrospective_position'}, transactions,
      }, null));
      entry.append(details);
    }
    list.append(entry);
  }
  disclosure.append(summary, list);
  card.append(disclosure);
  return true;
}

function planActualDate(value) {
  if (!value) return '—';
  const [year, month, day] = String(value).split('-');
  return day && month && year ? `${day}.${month}.${year}` : String(value);
}

function planActualOwnerLabel(value) {
  const text = String(value || '').trim();
  if (!text || text !== text.toLocaleUpperCase('de-DE')) return text;
  return text.toLocaleLowerCase('de-DE').replace(/(^|[\s_-])([a-zäöüß])/g,
    (_match, prefix, letter) => `${prefix}${letter.toLocaleUpperCase('de-DE')}`);
}

function planActualContextLabel(label, owner) {
  const text = String(label || '');
  const ownerLabel = owner === 'JOINT' ? 'Gemeinsam' : planActualOwnerLabel(owner);
  if (!ownerLabel) return text;
  const parts = text.split(/\s*·\s*/);
  let index = 0;
  const qualifiers = ['Schätzung', 'Vorläufig vereinbartes Zielbudget'];
  while (qualifiers.includes(parts[index])) index += 1;
  // Only hide the matching owner; another person's name is diagnostic context.
  if (index >= parts.length - 1
      || parts[index].toLocaleLowerCase('de-DE') !== ownerLabel.toLocaleLowerCase('de-DE')) return text;
  parts.splice(index, 1);
  return parts.join(' · ');
}

function planActualCashItemLabel(result, item) {
  return item.label === 'Barbudget · nicht eindeutig zugeordnet'
    && Number(result.cash_activity?.receipt_spending?.by_item?.[item.item_id]) > 0
    ? 'Barzahlungen · noch nicht aufgeschlüsselt' : item.label;
}

function planActualSetStatus(text, error = false) {
  const target = $('plan-actual-status');
  target.textContent = text || '';
  target.className = error ? 'error' : '';
}

function planActualSetMappingStatus(text, error = false) {
  const target = $('plan-actual-mapping-status');
  target.textContent = text || '';
  target.className = error ? 'error' : '';
}

function planActualContext(type, itemId = null) {
  return {type, item_id: itemId};
}

function planActualDetailTable(result, direction = null) {
  const wrapper = document.createElement('div');
  wrapper.className = 'plan-actual-details';
  const note = document.createElement('p');
  note.className = 'muted';
  const pages = Math.max(1, Math.ceil((result.total_count || 0) / (result.page_size || 25)));
  note.textContent = `${result.total_count || 0} Buchungen · Seite ${result.page || 1} von ${pages}`;
  wrapper.append(note);
  const scroll = document.createElement('div'); scroll.className = 'table-wrap';
  const table = document.createElement('table');
  const head = document.createElement('thead');
  const header = document.createElement('tr');
  const itemDetails = result.context && result.context.type === 'item';
  const labels = ['Datum', 'Konto', 'Empfänger und Zweck', 'Eigene Kategorie', 'Betrag EUR'];
  if (itemDetails) labels.push('Davon angerechnet EUR');
  for (const label of labels) {
    const th = document.createElement('th'); th.textContent = label;
    if (label === 'Betrag EUR') th.className = 'numeric';
    header.append(th);
  }
  head.append(header); table.append(head);
  const body = document.createElement('tbody');
  for (const booking of result.transactions || []) {
    const row = document.createElement('tr');
    cell(row, booking.date || '—');
    cell(row, accountDisplayById(booking.account_id));
    cell(row, [booking.counterparty, booking.description].filter(Boolean).join(' · ') || 'Ohne Beschreibung');
    cell(row, booking.category_label || booking.category_id || 'Nicht bestätigt');
    cell(row, planActualSignedEur(booking.amount), true);
    if (itemDetails) cell(row, planActualSignedEur(
      booking.allocated_amount, direction || (Number(booking.amount) >= 0 ? 'income' : 'expense')), true);
    body.append(row);
  }
  if (!body.children.length) {
    const row = document.createElement('tr');
    const empty = cell(row, 'Für diesen Posten gibt es keine Buchungen.');
    empty.colSpan = itemDetails ? 6 : 5;
    body.append(row);
  }
  table.append(body); scroll.append(table); wrapper.append(scroll);
  if (pages > 1) {
    const actions = document.createElement('div'); actions.className = 'actions';
    wrapper.append(actions);
  }
  return wrapper;
}

async function planActualLoadDetails(context, target, page = 1, direction = null) {
  target.replaceChildren();
  const result = await api('/api/budget-actual-details', {
    revision: Number($('plan-actual-revision').value),
    month: $('plan-actual-month').value,
    context,
    page,
  });
  const content = planActualDetailTable(result, direction);
  if ((result.total_count || 0) > (result.page_size || 25)) {
    const actions = content.querySelector('.actions');
    if (result.page > 1) button(actions, 'Vorige Seite', () => run(() => planActualLoadDetails(context, target, result.page - 1, direction)));
    if (result.has_more) button(actions, 'Nächste Seite', () => run(() => planActualLoadDetails(context, target, result.page + 1, direction)));
  }
  target.append(content);
}

function planActualDetailsButton(label, context, target, controlsId, direction = null) {
  const control = document.createElement('button');
  control.type = 'button'; control.className = 'secondary'; control.textContent = label;
  target.id = controlsId;
  control.setAttribute('aria-expanded', 'false'); control.setAttribute('aria-controls', controlsId);
  control.addEventListener('click', () => run(async () => {
    const open = control.getAttribute('aria-expanded') === 'true';
    control.setAttribute('aria-expanded', String(!open));
    target.hidden = open;
    if (!open && !target.children.length) {
      try { await planActualLoadDetails(context, target, 1, direction); }
      catch (error) {
        control.setAttribute('aria-expanded', 'false'); target.hidden = true;
        planActualSetStatus(`Buchungen konnten nicht geladen werden: ${error.message}`, true);
      }
    }
  }));
  return control;
}

function planActualCoverageItem(parent, label, count, value, context, id) {
  const card = document.createElement('article');
  const heading = document.createElement('strong'); heading.textContent = label;
  const summary = document.createElement('span');
  summary.textContent = `${count || 0} Buchungen · ${eur(value || 0)}`;
  const details = document.createElement('div'); details.id = id; details.hidden = true;
  card.append(heading, summary);
  if (Number(count || 0) > 0) card.append(planActualDetailsButton('Buchungen ansehen', context, details, id));
  card.append(details); parent.append(card);
}

function planActualRenderCoverage(coverage, planAvailable = true) {
  const target = $('plan-actual-coverage'); target.replaceChildren();
  const hasAction = Number(coverage.unclassified?.count || 0) > 0
    || Number(coverage.unmapped?.count || 0) > 0;
  target.hidden = !hasAction;
  if (!hasAction) return;
  planActualCoverageItem(target, 'Kategorie noch offen', coverage.unclassified.count,
    coverage.unclassified.absolute, planActualContext('unclassified'), 'plan-actual-unclassified-details');
  planActualCoverageItem(target, planAvailable ? 'Bestätigt, aber keiner Planposition zugeordnet' : 'Bestätigte Ist-Buchungen ohne Zuordnung', coverage.unmapped.count,
    Number(coverage.unmapped.income || 0) + Number(coverage.unmapped.expenses || 0),
    planActualContext('unmapped'), 'plan-actual-unmapped-details');
  const excluded = document.createElement('article');
  const heading = document.createElement('strong'); heading.textContent = 'Nicht als Verbrauch gezählt';
  const summary = document.createElement('span');
  summary.textContent = `${coverage.excluded.transfer_count || 0} Umbuchungspaare (${coverage.excluded.transfer_transaction_count || 0} Buchungen) · ${coverage.excluded.card_settlement_count || 0} Kartenabrechnungen · ${coverage.excluded.cash_withdrawal_count || 0} Bargeldabhebungen · ${coverage.excluded.depot_movement_count || 0} Depotbewegungen`;
  excluded.append(heading, summary); target.append(excluded);
}

function planActualRenderLiquidity(liquidity) {
  const target = $('plan-actual-liquidity'); target.replaceChildren();
  target.hidden = !liquidity || !liquidity.available;
  if (target.hidden) return;
  const heading = document.createElement('h3');
  heading.textContent = `Verfügbar auf ${liquidity.accounts.length} Girokonten`;
  const note = document.createElement('p'); note.className = 'muted';
  note.textContent = `Kontostand zum ${liquidity.as_of} plus vorgemerkte interne Umbuchungen und sichere Eingänge minus noch fest erwartete Ausgaben. Lockere Budgets werden separat gezeigt.`;
  target.append(heading, note);
  const summary = document.createElement('div'); summary.className = 'plan-actual-liquidity-summary';
  for (const [label, key] of [
    ['Aktuell auf Girokonten', 'current_balance'], ['Sicher noch eingehend', 'expected_income'],
    ['Fest noch ausgehend', 'committed_expenses'], ['Kontostand danach', 'available_after_commitments'],
  ]) {
    const article = document.createElement('article');
    const span = document.createElement('span'); span.textContent = label;
    const direction = key === 'expected_income' ? 'income'
      : key === 'committed_expenses' ? 'expense' : null;
    const strong = document.createElement('strong');
    strong.textContent = planActualSignedEur(liquidity.totals[key], direction);
    if ((key === 'current_balance' || key === 'available_after_commitments')
        && Number(liquidity.totals[key]) < 0) strong.className = 'negative';
    article.append(span, strong); summary.append(article);
  }
  target.append(summary);
  const flexible = document.createElement('p'); flexible.className = 'muted';
  flexible.textContent = `Der Kontostand danach kann negativ sein und ist keine Aussage darüber, was noch ausgegeben werden darf. Unbestätigte Schätzungen: ${planActualSignedEur(liquidity.totals.estimated_income, 'income')} Einnahmen und ${planActualSignedEur(liquidity.totals.estimated_expenses, 'expense')} Ausgaben. Noch nicht ausgeschöpfte lockere Budgets: ${eur(liquidity.totals.flexible_budget_remaining)}.`;
  target.append(flexible);
  if ((liquidity.unassigned || []).length) {
    const unassigned = document.createElement('p'); unassigned.className = 'notice';
    unassigned.textContent = `Nicht in dieser Kontenprognose enthalten: ${liquidity.unassigned.map(item => `${item.label} ${eur(item.amount)}`).join(', ')}. Diese Beträge bleiben im Monatsrahmen sichtbar.`;
    target.append(unassigned);
  }
  const scroll = document.createElement('div'); scroll.className = 'table-wrap';
  const table = document.createElement('table');
  table.innerHTML = '<thead><tr><th>Girokonto</th><th class="numeric">Gebucht</th><th class="numeric">Vorgemerkt</th><th class="numeric">Nach Vormerkung</th><th class="numeric">Sichere Eingänge</th><th class="numeric">Feste Ausgaben</th><th class="numeric">Kontostand danach</th></tr></thead>';
  const body = document.createElement('tbody');
  for (const account of liquidity.accounts || []) {
    const row = document.createElement('tr');
    cell(row, account.label);
    const current = cell(row, planActualSignedEur(account.current_balance), true);
    if (Number(account.current_balance) < 0) current.classList.add('negative');
    cell(row, planActualSignedEur(account.pending_transfer), true);
    cell(row, planActualSignedEur(account.balance_after_pending), true);
    cell(row, planActualSignedEur(account.expected_income, 'income'), true);
    cell(row, planActualSignedEur(account.committed_expenses, 'expense'), true);
    const available = cell(row, planActualSignedEur(account.available_after_commitments), true);
    if (Number(account.available_after_commitments) < 0) available.classList.add('negative');
    body.append(row);
    const detailRow = document.createElement('tr'); detailRow.className = 'plan-actual-detail-row';
    const detailCell = document.createElement('td'); detailCell.colSpan = 7;
    const details = document.createElement('details'); details.className = 'plan-actual-account-details';
    const detailsSummary = document.createElement('summary');
    detailsSummary.textContent = `Fest erwartete Buchungen für ${account.account_id} anzeigen`;
    const list = document.createElement('ul');
    const entries = [
      ...(account.income_items || []).map(item => ({...item, direction: 'Eingang'})),
      ...(account.commitments || []).map(item => ({...item, direction: 'Ausgabe'})),
      ...(account.estimates || []).map(item => ({...item,
        direction: item.kind === 'income' ? 'Einnahmenschätzung, nicht eingerechnet' : 'Ausgabenschätzung, nicht abgezogen'})),
    ];
    for (const item of entries) {
      const line = document.createElement('li');
      const direction = item.direction.includes('Eingang') || item.direction.includes('Einnahme')
        ? 'income' : 'expense';
      line.textContent = `${item.label}: ${planActualSignedEur(item.amount, direction)} ${item.direction}`;
      list.append(line);
    }
    if (!list.children.length) {
      const line = document.createElement('li');
      line.textContent = 'Keine weiteren festen Buchungen im Plan.'; list.append(line);
    }
    details.append(detailsSummary, list); detailCell.append(details);
    detailRow.append(detailCell); body.append(detailRow);
  }
  table.append(body); scroll.append(table); target.append(scroll);
}

function planActualRenderPayday(payday) {
  const target = $('plan-actual-payday'); target.replaceChildren();
  target.hidden = !payday || !payday.available;
  if (target.hidden) return;
  const heading = document.createElement('h3');
  heading.textContent = 'Frei ausgebbar bis zum nächsten Gehalt';
  const note = document.createElement('p'); note.className = 'muted';
  note.textContent = 'Kontostand heute nach den erfassten Fälligkeiten bis zum jeweiligen Gehaltstermin. Die Untergrenze ist der geplante Kontostand unmittelbar vor dem Gehalt; nicht erfasste Buchungen und ein abweichender Gehaltseingang sind darin nicht enthalten.';
  const summaryCards = document.createElement('div'); summaryCards.className = 'plan-actual-payday-totals';
  const addSummary = (label, value) => {
    const card = document.createElement('div'); card.className = 'plan-actual-payday-total';
    const span = document.createElement('span'); span.textContent = label;
    const strong = document.createElement('strong'); strong.textContent = value;
    card.append(span, strong); summaryCards.append(card);
  };
  addSummary('Liquiditätsspielraum insgesamt', planActualAvailable(payday.total_free_spendable));
  addSummary('Davon Budgets im Monatsrahmen', eur(payday.flexible_budget_remaining));
  addSummary('Davon nicht verplant', planActualAvailable(payday.unallocated_after_budgets));
  target.append(heading, note, summaryCards);
  const scroll = document.createElement('div'); scroll.className = 'table-wrap';
  const table = document.createElement('table');
  table.innerHTML = '<thead><tr><th>Person / Konto</th><th>Gehaltstermin</th><th class="numeric">Heute</th><th class="numeric">Noch eingehend</th><th class="numeric">Noch fällig</th><th class="numeric">Untergrenze vor Gehalt</th><th class="numeric">Kurzfristig frei</th></tr></thead>';
  const body = document.createElement('tbody');
  for (const cycle of payday.cycles || []) {
    const row = document.createElement('tr');
    cell(row, `${cycle.owner_label} · ${cycle.account_id}`);
    cell(row, planActualDate(cycle.next_salary_date));
    cell(row, planActualSignedEur(cycle.current_balance), true);
    cell(row, planActualSignedEur(cycle.upcoming_inflows, 'income'), true);
    cell(row, planActualSignedEur(cycle.upcoming_outflows, 'expense'), true);
    cell(row, planActualSignedEur(cycle.projected_balance), true);
    const free = cell(row, planActualAvailable(cycle.free_spendable), true);
    free.className += ' plan-actual-payday-value';
    body.append(row);
    const detailRow = document.createElement('tr'); detailRow.className = 'plan-actual-detail-row';
    const detailCell = document.createElement('td'); detailCell.colSpan = 7;
    const details = document.createElement('details'); details.className = 'plan-actual-account-details';
    const summary = document.createElement('summary');
    summary.textContent = `${cycle.cashflows.length} Fälligkeiten, Untergrenze und Gehaltsannahme anzeigen`;
    const list = document.createElement('ul');
    const targetLine = document.createElement('li');
    targetLine.textContent = `Untergrenze vor dem Gehalt am ${planActualDate(cycle.next_salary_date)}: ${eur(cycle.target_balance)}. Gehaltsannahme: ${cycle.next_salary_basis}. Berechnet nur mit den unten aufgeführten Fälligkeiten.`;
    list.append(targetLine);
    for (const flow of cycle.cashflows) {
      const line = document.createElement('li');
      line.textContent = `${flow.due_date} · ${flow.label}: ${planActualSignedEur(
        flow.amount, flow.direction === 'inflow' ? 'income' : 'expense')} · ${flow.evidence}`;
      list.append(line);
    }
    details.append(summary, list); detailCell.append(details); detailRow.append(detailCell); body.append(detailRow);
  }
  table.append(body); scroll.append(table); target.append(scroll);
}

function planActualMoney(value) { return eur(Number(value || 0)); }
function planActualPlanMoney(value, direction = null) {
  return value == null ? '—' : planActualSignedEur(value, direction);
}
function planActualAvailable(value) { return eur(Math.max(0, Number(value || 0))); }
function planActualTreePlanIst(value, kind) {
  return kind === 'expense' ? planActualSignedEur(value, 'expense') : planActualMoney(value);
}

function planActualHasNewFields(result) {
  return Array.isArray(result.tree) && Array.isArray(result.daily);
}

function planActualSetView(mode, persist = true) {
  const cockpit = mode === 'cockpit' && $('plan-actual-cockpit').dataset.available === 'true';
  $('plan-actual-cockpit').hidden = !cockpit;
  $('plan-actual-classic').hidden = cockpit;
  $('plan-actual-cockpit-mode').setAttribute('aria-pressed', String(cockpit));
  $('plan-actual-classic-mode').setAttribute('aria-pressed', String(!cockpit));
  $('plan-actual-cockpit-mode').classList.toggle('secondary', !cockpit);
  $('plan-actual-classic-mode').classList.toggle('secondary', cockpit);
  if (persist) sessionStorage.setItem('finance-control-plan-actual-view', cockpit ? 'cockpit' : 'classic');
  if (cockpit) planActualScheduleMonthChartResize();
}

function planActualRenderDue(payday) {
  const list = $('plan-actual-due-list'); list.replaceChildren();
  const flows = (payday?.cycles || []).flatMap(cycle => (cycle.cashflows || []).map(flow => ({
    ...flow, account: cycle.account_id, owner: cycle.owner_label,
  }))).sort((a, b) => String(a.due_date).localeCompare(String(b.due_date))).slice(0, 5);
  for (const flow of flows) {
    const item = document.createElement('li');
    const signed = planActualSignedEur(flow.amount, flow.direction === 'inflow' ? 'income' : 'expense');
    item.textContent = `${flow.due_date} · ${flow.label} · ${flow.owner || flow.account}: ${signed}`;
    item.className = flow.direction === 'inflow' ? 'plan-actual-due-inflow' : 'plan-actual-due-outflow';
    list.append(item);
  }
  if (!flows.length) {
    const empty = document.createElement('li'); empty.textContent = 'Keine kommenden Fälligkeiten verfügbar.'; list.append(empty);
  }
}

function planActualMonthChartLayout(svg, min, max) {
  // The single-month SVG fits its panel; range charts keep their wider scrollable canvas.
  const viewport = document.documentElement?.clientWidth || 900;
  const available = svg.parentNode?.clientWidth || svg.clientWidth || Math.max(1, viewport - 100);
  const width = Math.min(900, Math.max(1, Math.floor(available)));
  const compact = width < 440
    ? new Intl.NumberFormat('de-DE', {style: 'currency', currency: 'EUR',
      notation: 'compact', maximumFractionDigits: 1}) : null;
  let formatTick = value => compact ? compact.format(value) : eur(value);
  let tickFontSize = width < 240 ? 10 : 12;
  const canvas = document.createElement?.('canvas');
  const context = canvas?.getContext?.('2d');
  const measuredWidth = () => {
    const labels = [min, (min + max) / 2, max].map(formatTick);
    if (context) context.font = `${tickFontSize}px Segoe UI, Arial, sans-serif`;
    return Math.ceil(Math.max(...labels.map(label => context
      ? context.measureText(label).width : label.length * tickFontSize * .65)));
  };
  const right = 16, minPlot = Math.max(32, Math.floor(width * .25));
  const maxLabelWidth = width - right - minPlot - 10;
  let labelWidth = measuredWidth();
  if (width < 440 && labelWidth > maxLabelWidth) {
    formatTick = value => `${Number(value).toExponential(1)} €`;
    tickFontSize = 8;
    labelWidth = measuredWidth();
  }
  const pad = {left: Math.max(width < 240 ? 58 : 72, labelWidth + 10),
    right, top: 24, bottom: 44};
  return {width, pad, formatTick, tickFontSize, labelWidth};
}

function planActualMonthDateTicks(days, plotWidth) {
  const last = days.length - 1;
  const middle = Math.floor(last / 2);
  const indices = plotWidth < 140 ? [0, last] : [0, middle, last];
  return [...new Set(indices)].map(index => ({index,
    label: String(days[index].date || '').slice(plotWidth < 80 ? 8 : 5)}));
}

function planActualBalanceEndLabel(asOf) {
  const match = /^(\d{4})-(\d{2})-(\d{2})(?:$|T)/.exec(String(asOf || ''));
  if (!match) return 'Stand zum Auswertungsschnitt';
  const [, year, month, day] = match.map(Number);
  const lastDay = new Date(Date.UTC(year, month, 0)).getUTCDate();
  if (month < 1 || month > 12 || day < 1 || day > lastDay)
    return 'Stand zum Auswertungsschnitt';
  return day === lastDay ? 'Monatsende'
    : `Stand am ${String(day).padStart(2, '0')}.${String(month).padStart(2, '0')}.`;
}

function planActualRenderDaily(daily, metadata = null, views = null, accountBalance = null) {
  const svg = $('plan-actual-daily-chart'); svg.replaceChildren();
  svg.setAttribute('viewBox', '0 0 900 300');
  const heading = svg.closest('section')?.querySelector('h3');
  if (heading) heading.textContent = planActualTrendCumulative
    ? 'Kumulierter Verlauf im Monat' : 'Tageswerte im Monat';
  const note = $('plan-actual-daily-note');
  planActualRenderTrendComparisonTable(svg, null, 'month', planActualDailyView);
  let controls = $('plan-actual-daily-controls');
  if (!controls) {
    controls = document.createElement('div'); controls.id = 'plan-actual-daily-controls';
    controls.className = 'plan-actual-daily-controls';
    const rangeLabel = document.createElement('label'); rangeLabel.textContent = 'Zeitraum';
    const rangeSelect = document.createElement('select'); rangeSelect.id = 'plan-actual-trend-range';
    rangeSelect.setAttribute('aria-label', 'Zeitraum des Kurvenverlaufs');
    for (const [value, label] of [['month', 'Aktueller Monat'], ['3', '3 Monate'],
      ['6', '6 Monate'], ['year', 'Kalenderjahr']]) {
      const option = document.createElement('option'); option.value = value;
      option.textContent = label; rangeSelect.append(option);
    }
    rangeSelect.addEventListener('change', () => {
      planActualTrendRange = rangeSelect.value;
      void planActualRefreshTrend();
    });
    rangeLabel.append(rangeSelect);
    const granularityLabel = document.createElement('label'); granularityLabel.textContent = 'Granularität';
    const granularitySelect = document.createElement('select');
    granularitySelect.id = 'plan-actual-trend-granularity';
    granularitySelect.setAttribute('aria-label', 'Granularität des Kurvenverlaufs');
    for (const [value, label] of [['daily', 'Täglich'], ['monthly', 'Monatlich']]) {
      const option = document.createElement('option'); option.value = value;
      option.textContent = label; granularitySelect.append(option);
    }
    granularitySelect.addEventListener('change', () => {
      planActualTrendGranularity = granularitySelect.value;
      void planActualRefreshTrend();
    });
    granularityLabel.append(granularitySelect);
    const displayLabel = document.createElement('label'); displayLabel.textContent = 'Darstellung';
    const displaySelect = document.createElement('select'); displaySelect.id = 'plan-actual-trend-display';
    displaySelect.setAttribute('aria-label', 'Darstellung des Kurvenverlaufs');
    for (const [value, label] of [['cumulative', 'Kumuliert'], ['values', 'Einzelwerte']]) {
      const option = document.createElement('option'); option.value = value;
      option.textContent = label; displaySelect.append(option);
    }
    displaySelect.addEventListener('change', () => {
      planActualTrendCumulative = displaySelect.value === 'cumulative';
      void planActualRefreshTrend();
    });
    displayLabel.append(displaySelect);
    const label = document.createElement('label'); label.textContent = 'Ansicht';
    const select = document.createElement('select'); select.id = 'plan-actual-daily-view';
    select.setAttribute('aria-label', 'Personenansicht des Verlaufs');
    select.addEventListener('change', () => {
      planActualDailyView = select.value;
      void planActualRefreshTrend();
    });
    label.append(select); controls.append(rangeLabel, granularityLabel, displayLabel, label); svg.parentNode.insertBefore(controls, note);
  }
  const rangeSelect = $('plan-actual-trend-range');
  if (rangeSelect) rangeSelect.value = planActualTrendRange;
  const granularitySelect = $('plan-actual-trend-granularity');
  if (granularitySelect) {
    granularitySelect.disabled = planActualTrendRange === 'month';
    granularitySelect.value = planActualTrendRange === 'month' ? 'daily' : planActualTrendGranularity;
  }
  const displaySelect = $('plan-actual-trend-display');
  if (displaySelect) displaySelect.value = planActualTrendCumulative ? 'cumulative' : 'values';
  const selected = $('plan-actual-daily-view');
  const result = window.planActualLatestResult;
  const reportingScope = result?.reporting_scope;
  const individualIds = reportingScope?.mode === 'individual'
    ? (reportingScope.included_person_ids || []) : null;
  const viewOptions = [{id: 'TOTAL', label: reportingScope ? 'Betrachtete Konten gesamt' : 'Gesamt'},
    ...(result?.daily_people || []).filter(person => person.id !== 'TOTAL'
      && (!individualIds || individualIds.includes(person.id)))];
  if (selected) {
    selected.replaceChildren();
    for (const person of viewOptions) {
      if (person.id !== 'TOTAL' && !views?.[person.id]) continue;
      const option = document.createElement('option'); option.value = person.id;
      option.textContent = person.label; selected.append(option);
    }
    if (![...selected.options].some(option => option.value === planActualDailyView))
      planActualDailyView = 'TOTAL';
    selected.value = planActualDailyView;
  }
  let balanceSummary = $('plan-actual-daily-balance');
  if (!balanceSummary) {
    balanceSummary = document.createElement('div'); balanceSummary.id = 'plan-actual-daily-balance';
    balanceSummary.className = 'plan-actual-daily-balance';
    svg.parentNode.insertBefore(balanceSummary, note);
  }
  balanceSummary.replaceChildren();
  balanceSummary.hidden = planActualTrendRange !== 'month' || !planActualTrendCumulative;
  const balanceForView = accountBalance?.by_person?.[planActualDailyView];
  if (!accountBalance?.available || !balanceForView) {
    const unavailable = document.createElement('p'); unavailable.className = 'muted';
    unavailable.textContent = 'Kontostände nicht verfügbar: Für den Monatsanfang fehlt ein belastbarer Anfangsbestand der ausgewählten Girokonten.';
    balanceSummary.append(unavailable);
  } else {
    for (const [label, key] of [['Monatsanfang', 'start'],
      [planActualBalanceEndLabel(accountBalance.as_of), 'end'],
      ['Veränderung', 'change']]) {
      const value = document.createElement('div'); value.className = 'plan-actual-daily-balance-value';
      const caption = document.createElement('span'); caption.textContent = label;
      const amount = document.createElement('strong'); amount.textContent = eur(balanceForView[key]);
      value.append(caption, amount); balanceSummary.append(value);
    }
    const basis = document.createElement('p'); basis.className = 'muted';
    basis.textContent = `Kontostandsänderung aus Rohbuchungen der Girokonten einschließlich Umbuchungen, bis ${accountBalance.as_of}.`;
    balanceSummary.append(basis);
  }
  const activeDaily = planActualDailyView === 'TOTAL'
    ? daily : (views?.[planActualDailyView] || daily);
  daily = planActualMonthDailyValues(activeDaily, planActualTrendCumulative);
  let legend = $('plan-actual-daily-legend');
  if (!legend) {
    legend = document.createElement('div'); legend.id = 'plan-actual-daily-legend';
    legend.className = 'plan-actual-daily-legend'; legend.setAttribute('aria-label', 'Legende');
    svg.parentNode.insertBefore(legend, svg);
  }
  legend.replaceChildren();
  if (!svg.parentNode.classList?.contains('plan-actual-chart-scroll')) {
    const wrapper = document.createElement('div'); wrapper.className = 'plan-actual-chart-scroll';
    svg.parentNode.insertBefore(wrapper, svg); wrapper.append(svg);
  }
  if (planActualTrendRange !== 'month') {
    svg.classList.add('is-monthly');
    if (planActualTrendGranularity === 'daily') {
      svg.classList.add('is-daily-range');
      planActualRenderDailyTrend(svg, note, legend, result?.monthly_trend,
        planActualTrendRange, planActualDailyView, planActualTrendCumulative);
    } else {
      svg.classList.remove('is-daily-range');
      planActualRenderMonthlyTrend(svg, note, legend, result?.monthly_trend,
        planActualTrendRange, planActualDailyView, 'monthly', planActualTrendCumulative);
    }
    planActualRenderTrendComparisonTable(svg, result?.monthly_trend,
      planActualTrendRange, planActualDailyView);
    planActualAppendReportingHistoryNote(note, result);
    return;
  }
  svg.classList.remove('is-monthly');
  svg.classList.remove('is-daily-range');
  if (!daily.length) {
    svg.setAttribute('aria-label', 'Keine Tageswerte verfügbar');
    note.textContent = 'Für diesen Monat sind noch keine Tageswerte verfügbar.';
    planActualAppendReportingHistoryNote(note, result);
    return;
  }
  svg.setAttribute('aria-label', planActualTrendCumulative
    ? `${result?.plan_available === false ? 'Kumulierte Ist-Werte' : 'Kumulierte Plan- und Ist-Werte'} im Monatsverlauf`
    : `${result?.plan_available === false ? 'Tägliche Ist-Einzelwerte' : 'Tägliche Plan- und Ist-Einzelwerte'} im Monatsverlauf`);
  if (heading) heading.textContent = planActualTrendCumulative
    ? 'Kumulierter Verlauf im Monat' : 'Tageswerte im Monat';
  const reconstructed = result?.basis?.plan_source === 'reconstructed_from_monthly_actuals';
  note.textContent = `${metadata?.as_of ? `Auswertung bis ${metadata.as_of}` : 'Auswertungsschnitt nicht angegeben'}. ${result?.plan_available === false
    ? 'Kein gespeicherter Monatsplan; gezeigt werden nur Ist-Buchungen.' : reconstructed
    ? 'Der historische Plan entspricht den Ist-Buchungen dieses Monats, einschließlich der Buchungstage. Die Nullabweichung folgt aus dieser Rekonstruktion und ist kein unabhängiger Plan-Ist-Nachweis.'
    : planActualTrendCumulative
    ? 'Eindeutig datierte Planbeträge sind als Stufen enthalten; der übrige Monatsplan wird zeitanteilig verteilt.'
    : 'Angezeigt werden die Buchungen und Plananteile des jeweiligen Tages.'}`;
  planActualAppendReportingHistoryNote(note, result);
  const height = 300;
  const series = [
    {key: 'cumulative_expenses', label: 'Ist-Ausgaben (Betrag)', color: '#b34435'},
    {key: 'planned_cumulative_expenses', label: 'Plan-Ausgaben (Betrag)', color: '#b34435', planned: true},
    {key: 'cumulative_income', label: 'Ist-Einnahmen', color: '#28734b'},
    {key: 'planned_cumulative_income', label: 'Plan-Einnahmen', color: '#28734b', planned: true},
  ].filter(item => !item.planned || result?.plan_available !== false);
  if (planActualTrendCumulative && accountBalance?.available && balanceForView?.points?.length) {
    series.push({key: 'change', label: 'Kontostandsänderung Girokonto',
      color: '#34495e', balance: true});
  }
  const valueFor = (day, item, index) => item.balance
    ? Number(balanceForView.points[index]?.change || 0)
    : item.key.includes('expenses') ? Math.abs(Number(day[item.key] || 0))
      : Number(day[item.key] || 0);
  for (const item of series) {
    const entry = document.createElement('span'); entry.className = 'plan-actual-daily-legend-item';
    const sample = document.createElement('span'); sample.className = 'plan-actual-daily-legend-line';
    sample.style.borderTopColor = item.color;
    if (item.planned) sample.classList.add('is-plan');
    const label = document.createElement('span'); label.textContent = item.label;
    entry.append(sample, label); legend.append(entry);
  }
  const values = daily.flatMap((day, index) => series.map(item => valueFor(day, item, index)));
  const min = Math.min(0, ...values), max = Math.max(0, ...values), span = max - min || 1;
  const {width, pad, formatTick, tickFontSize} = planActualMonthChartLayout(svg, min, max);
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const x = index => planActualDailyX(index, daily.length, pad.left, width - pad.right);
  const y = value => height - pad.bottom - (Number(value || 0) - min) * (height - pad.top - pad.bottom) / span;
  const ns = 'http://www.w3.org/2000/svg';
  const axis = document.createElementNS(ns, 'path');
  axis.setAttribute('d', `M ${pad.left} ${pad.top} V ${height-pad.bottom} M ${pad.left} ${y(0)} H ${width-pad.right}`);
  axis.setAttribute('fill', 'none'); axis.setAttribute('stroke', '#9eb1bb'); svg.append(axis);
  for (const fraction of [0, .5, 1]) {
    const value = max - span * fraction, gridY = y(value);
    const grid = document.createElementNS(ns, 'line'); grid.setAttribute('x1', String(pad.left));
    grid.setAttribute('x2', String(width-pad.right)); grid.setAttribute('y1', String(gridY));
    grid.setAttribute('y2', String(gridY)); grid.setAttribute('stroke', '#dbe5e9'); svg.append(grid);
    const label = document.createElementNS(ns, 'text'); label.setAttribute('x', String(pad.left-8));
    label.setAttribute('y', String(gridY+4)); label.setAttribute('text-anchor', 'end');
    label.style.fontSize = `${tickFontSize}px`;
    label.textContent = formatTick(value); svg.append(label);
  }
  for (const item of series) {
    const path = document.createElementNS(ns, 'path');
    const d = item.balance
      ? [`M ${x(0)} ${y(0)}`, ...daily.map((day, point) =>
        `L ${x(point)} ${y(valueFor(day, item, point))}`)].join(' ')
      : daily.map((day, point) => `${point ? 'L' : 'M'} ${x(point)} ${y(valueFor(day, item, point))}`).join(' ');
    path.setAttribute('d', d); path.setAttribute('fill', 'none'); path.setAttribute('stroke', item.color);
    path.setAttribute('stroke-width', item.balance ? '4' : '3');
    if (item.planned) path.setAttribute('stroke-dasharray', '8 6');
    path.setAttribute('aria-label', item.label); svg.append(path);
  }
  planActualMonthDateTicks(daily, width - pad.left - pad.right).forEach(({index, label: text}) => {
    const label = document.createElementNS(ns, 'text'); label.setAttribute('x', String(x(index))); label.setAttribute('y', String(height - 12)); label.setAttribute('text-anchor', index === 0 ? 'start' : index === daily.length - 1 ? 'end' : 'middle'); label.textContent = text; svg.append(label);
  });
}

function planActualReportingHistoryNote(result) {
  const scope = result?.reporting_scope;
  const history = result?.monthly_trend?.reporting_history;
  const cutoff = scope?.joint_from_month || history?.joint_from_month;
  const priorId = scope?.prior_person_id || history?.prior_person_id;
  if (!cutoff || !priorId) return '';
  const person = [...(result?.person_breakdown?.people || []), ...(result?.daily_people || [])]
    .find(entry => entry.id === priorId);
  const label = person?.label || priorId;
  const [year, month] = cutoff.split('-').map(Number);
  const prior = new Date(Date.UTC(year, month - 2, 1)).toISOString().slice(0, 7);
  return `Kontenumfang: Bis einschließlich ${prior} nur ${label}; gemeinsamer Kontenumfang ab ${cutoff}.`;
}

function planActualVisiblePeople(people, reportingScope) {
  const entries = people || [];
  if (reportingScope?.mode !== 'individual') return entries;
  const included = reportingScope.included_person_ids || [];
  return entries.filter(person => included.includes(person.id));
}

function planActualReportingTotalLabel(reportingScope, people) {
  if (reportingScope?.mode !== 'individual') return 'Summe: Andreas + Erlene + Gemeinsam';
  const ids = reportingScope.included_person_ids || [];
  const person = (people || []).find(entry => entry.id === reportingScope.prior_person_id)
    || (people || []).find(entry => ids.includes(entry.id));
  return `Summe: ${person?.label || reportingScope.prior_person_id || ids[0] || 'betrachtete Person'}`;
}

function planActualAppendReportingHistoryNote(note, result) {
  const historyNote = planActualReportingHistoryNote(result);
  if (historyNote && !note.textContent.includes(historyNote))
    note.textContent = `${note.textContent} ${historyNote}`.trim();
}

function planActualDailyX(index, count, left, right) {
  if (count <= 1) return (left + right) / 2;
  return left + index * (right - left) / (count - 1);
}

function planActualMonthDailyValues(points, cumulative) {
  if (cumulative) return points;
  const previous = {cumulative_expenses: 0, planned_cumulative_expenses: 0,
    cumulative_income: 0, planned_cumulative_income: 0};
  return points.map(point => {
    const values = {...point};
    for (const key of Object.keys(previous)) {
      if (point[key] == null) { values[key] = null; continue; }
      const current = Number(point[key] || 0);
      values[key] = (key.includes('expenses') ? Math.abs(current - previous[key])
        : current - previous[key]);
      previous[key] = current;
    }
    return values;
  });
}

function planActualRenderDailyTrend(svg, note, legend, trend, range, view, cumulative = true) {
  const selectedMonth = trend?.selected_month || '';
  const sourcePoints = trend?.daily_points_by_person?.[view] || [];
  let startMonth = selectedMonth;
  if (range === 'year') startMonth = `${selectedMonth.slice(0, 4)}-01`;
  else {
    const offset = range === '3' ? -2 : -5;
    const [year, month] = selectedMonth.split('-').map(Number);
    const start = new Date(Date.UTC(year, month - 1 + offset, 1));
    startMonth = start.toISOString().slice(0, 7);
  }
  const months = planActualDailyCumulativeRange(
    sourcePoints, `${startMonth}-01`, view,
    trend?.daily_balance_available_by_range?.[range] ?? trend?.daily_balance_available,
    Object.fromEntries((trend?.months || []).map(row => [row.month, row.plan_source])), cumulative);
  planActualRenderMonthlyTrend(svg, note, legend,
    {...trend, months}, 'daily', view, 'daily', cumulative);
}

function planActualDailyCumulativeRange(sourcePoints, startDate, view, balanceAvailable,
                                        planSources = {}, cumulative = true) {
  const points = sourcePoints.filter(point => point.date >= startDate);
  const running = {actual_income: 0, actual_expenses: 0, planned_income: 0,
    planned_expenses: 0, balance: 0};
  return points.map(point => {
    const values = {};
    for (const key of ['actual_income', 'actual_expenses', 'planned_income', 'planned_expenses']) {
      const raw = point[`${key}_delta`];
      const delta = raw == null && key.startsWith('planned_') ? null : Number(raw || 0);
      // Once a planned day is unavailable, its cumulative series is unknown for the range.
      running[key] = delta == null || running[key] == null ? null : running[key] + delta;
      values[key] = cumulative ? running[key] : delta;
    }
    if (balanceAvailable && point.balance_change_delta !== null)
      running.balance += Number(point.balance_change_delta || 0);
    return {
      month: point.date,
      plan: {[view]: {income: values.planned_income, expenses: values.planned_expenses}},
      actual: {[view]: {income: values.actual_income, expenses: values.actual_expenses}},
      balance_available: Boolean(cumulative && balanceAvailable
        && point.balance_change_delta !== null),
      balance_change: {[view]: running.balance},
      plan_source: planSources[point.date.slice(0, 7)],
    };
  });
}

function planActualRenderMonthlyTrend(svg, note, legend, trend, range, view,
                                      granularity = 'monthly', cumulative = true) {
  svg.classList.add('is-monthly');
  svg.classList.remove('is-daily-range');
  const heading = svg.closest('section')?.querySelector('h3');
  if (heading) heading.textContent = granularity === 'daily'
    ? (cumulative ? 'Tagesgenauer kumulierter Verlauf im Zeitraum' : 'Tageswerte im Zeitraum')
    : (cumulative ? 'Kumulierter Monatsverlauf im Zeitraum' : 'Monatsvergleich im Zeitraum');
  const selectedMonth = trend?.selected_month;
  const allMonths = trend?.months || [];
  let months = allMonths;
  if (range === '3' || range === '6') months = allMonths.slice(-Number(range));
  if (range === 'year') months = allMonths.filter(row => row.month.startsWith(`${selectedMonth?.slice(0, 4)}-`));
  months = planActualMonthlyRangeValues(months, view, cumulative);
  legend.replaceChildren();
  const hasBalance = cumulative && months.some(row => row.balance_available
    && row.balance_change?.[view] !== undefined);
  const hasPlan = months.some(row => row.plan?.[view]?.income != null
    || row.plan?.[view]?.expenses != null);
  const series = [
    {key: 'expenses', source: 'actual', label: 'Ist-Ausgaben (Betrag)', color: '#b34435'},
    ...(hasPlan ? [{key: 'expenses', source: 'plan', label: 'Plan-Ausgaben (Betrag)', color: '#b34435', planned: true}] : []),
    {key: 'income', source: 'actual', label: 'Ist-Einnahmen', color: '#28734b'},
    ...(hasPlan ? [{key: 'income', source: 'plan', label: 'Plan-Einnahmen', color: '#28734b', planned: true}] : []),
    ...(hasBalance ? [{key: 'balance', source: 'balance', label: 'Kontostandsänderung (rechte Achse)', color: '#34495e'}] : []),
  ];
  for (const item of series) {
    const entry = document.createElement('span'); entry.className = 'plan-actual-daily-legend-item';
    const sample = document.createElement('span'); sample.className = 'plan-actual-daily-legend-line';
    sample.style.borderTopColor = item.color;
    if (item.planned) sample.classList.add('is-plan');
    const label = document.createElement('span'); label.textContent = item.label;
    entry.append(sample, label); legend.append(entry);
  }
  if (!months.length) {
    svg.setAttribute('aria-label', 'Keine Monatswerte verfügbar');
    note.textContent = 'Für den gewählten Zeitraum sind keine Monatswerte verfügbar.';
    return;
  }
  svg.setAttribute('aria-label', `${granularity === 'daily' ? 'Tagesgenaue' : 'Monatliche'} ${cumulative ? 'kumulierte' : 'einzelne'} ${hasPlan ? 'Plan- und Ist-Werte' : 'Ist-Werte'} im Zeitraum${hasBalance ? ' mit Kontostandsänderung' : ''}`);
  const savedProjection = months.some(row => row.plan_source === 'saved_revision_projection');
  const fixedActuals = months.some(row => row.plan_source === 'reconstructed_from_monthly_actuals');
  const planNote = [
    fixedActuals
      ? 'Für Monate mit Ist-Rückblick wird der Gesamtplan aus den Ist-Buchungen dieses Monats rekonstruiert. Die Nullabweichung dort ist kein unabhängiger Plan-Ist-Nachweis.'
      : '',
    savedProjection
      ? `Weitere Monate innerhalb Revision ${trend.revision} verwenden die dort gespeicherten Budgetpositionen.`
      : '',
    !fixedActuals && !savedProjection && hasPlan
      ? `Planwerte aus Revision ${trend.revision}.` : '',
    months.some(row => row.plan_source === 'no_saved_budget')
      ? 'Für Monate ohne gespeicherten Plan fehlen Plan- und Abweichungswerte.' : '',
    cumulative && months.some(row => row.plan_source === 'no_saved_budget')
      ? 'Kumulierte Planlinie endet, sobald ein Monatsplan fehlt; spätere vorhandene Monatspläne sind als Einzelwerte sichtbar.' : '',
  ].filter(Boolean).join(' ');
  const valueLabel = cumulative ? 'ab Zeitraumstart laufend kumuliert' : 'als Einzelwerte je Tag bzw. Monat';
  const endRow = months.at(-1);
  const endSummary = cumulative
    ? ` Endwerte: Ist-Ausgaben ${eur(Math.abs(Number(endRow.actual?.[view]?.expenses || 0)))}, Ist-Einnahmen ${eur(endRow.actual?.[view]?.income || 0)}${hasBalance ? `, Kontostandsänderung ${eur(endRow.balance_change?.[view] || 0)}` : ''}.`
    : '';
  note.textContent = `${months[0].month} bis ${endRow.month}: Werte ${valueLabel}. ${granularity === 'daily' ? 'Istwerte aus Buchungstagen.' : 'Istwerte aus Buchungen.'} ${planNote} ${cumulative ? (hasBalance ? 'Kontostandsänderung aus Rohbuchungen der Girokonten einschließlich Umbuchungen.' : 'Kontostandsänderung für diesen Zeitraum nicht verfügbar.') : ''}${endSummary}`;
  const width = granularity === 'daily' ? Math.max(1200, months.length * 4) : 900;
  if (granularity === 'daily') svg.classList.add('is-daily-range');
  const height = 300, pad = {left: 76, right: 90, top: 24, bottom: 52};
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const numericValue = (row, item) => {
    if (item.source === 'balance') {
      if (!row.balance_available || row.balance_change?.[view] === undefined) return null;
      return Number(row.balance_change[view]);
    }
    const raw = row[item.source]?.[view]?.[item.key];
    if (raw == null) return null;
    const amount = Number(raw);
    return Math.abs(amount);
  };
  const comparisonValues = months.flatMap(row => series.filter(item => item.source !== 'balance')
    .map(item => numericValue(row, item)).filter(value => value !== null));
  const balanceValues = (cumulative ? months : []).map(row => numericValue(row, {source: 'balance'}))
    .filter(value => value !== null);
  const leftMin = Math.min(0, ...comparisonValues), leftMax = Math.max(0, ...comparisonValues);
  const leftSpan = leftMax - leftMin || 1;
  const rightMin = Math.min(0, ...balanceValues), rightMax = Math.max(0, ...balanceValues);
  const rightSpan = rightMax - rightMin || 1;
  const x = index => pad.left + index * (width - pad.left - pad.right) / Math.max(1, months.length - 1);
  const yLeft = value => height - pad.bottom - (Number(value || 0) - leftMin)
    * (height - pad.top - pad.bottom) / leftSpan;
  const yRight = value => height - pad.bottom - (Number(value || 0) - rightMin)
    * (height - pad.top - pad.bottom) / rightSpan;
  const yFor = (value, item) => item.source === 'balance' ? yRight(value) : yLeft(value);
  const ns = 'http://www.w3.org/2000/svg';
  const axis = document.createElementNS(ns, 'path');
  axis.setAttribute('d', `M ${pad.left} ${pad.top} V ${height-pad.bottom} M ${pad.left} ${yLeft(0)} H ${width-pad.right}${hasBalance ? ` M ${width-pad.right} ${pad.top} V ${height-pad.bottom}` : ''}`);
  axis.setAttribute('fill', 'none'); axis.setAttribute('stroke', '#9eb1bb'); svg.append(axis);
  const leftAxisTitle = document.createElementNS(ns, 'text');
  leftAxisTitle.setAttribute('x', String(pad.left)); leftAxisTitle.setAttribute('y', '14');
  leftAxisTitle.setAttribute('class', 'plan-actual-axis-title');
  leftAxisTitle.textContent = cumulative ? 'Linke Achse: kumulierte Beträge' : 'Linke Achse: Einzelbeträge';
  svg.append(leftAxisTitle);
  if (hasBalance) {
    const rightAxisTitle = document.createElementNS(ns, 'text');
    rightAxisTitle.setAttribute('x', String(width-pad.right)); rightAxisTitle.setAttribute('y', '14');
    rightAxisTitle.setAttribute('text-anchor', 'end');
    rightAxisTitle.setAttribute('class', 'plan-actual-axis-title');
    rightAxisTitle.textContent = 'Rechte Achse: Kontostandsänderung';
    svg.append(rightAxisTitle);
  }
  if (hasBalance) {
    const balanceZero = document.createElementNS(ns, 'line');
    balanceZero.setAttribute('x1', String(pad.left)); balanceZero.setAttribute('x2', String(width-pad.right));
    balanceZero.setAttribute('y1', String(yRight(0))); balanceZero.setAttribute('y2', String(yRight(0)));
    balanceZero.setAttribute('stroke', '#8495a0'); balanceZero.setAttribute('stroke-dasharray', '3 4');
    svg.append(balanceZero);
  }
  for (const fraction of [0, .5, 1]) {
    const amount = leftMax - leftSpan * fraction, gridY = yLeft(amount);
    const grid = document.createElementNS(ns, 'line'); grid.setAttribute('x1', String(pad.left));
    grid.setAttribute('x2', String(width-pad.right)); grid.setAttribute('y1', String(gridY));
    grid.setAttribute('y2', String(gridY)); grid.setAttribute('stroke', '#dbe5e9'); svg.append(grid);
    const label = document.createElementNS(ns, 'text'); label.setAttribute('x', String(pad.left-8));
    label.setAttribute('y', String(gridY+4)); label.setAttribute('text-anchor', 'end');
    label.textContent = eur(amount); svg.append(label);
    if (hasBalance) {
      const balanceAmount = rightMax - rightSpan * fraction, rightY = yRight(balanceAmount);
      const rightLabel = document.createElementNS(ns, 'text');
      rightLabel.setAttribute('x', String(width-pad.right+8));
      rightLabel.setAttribute('y', String(rightY+4)); rightLabel.setAttribute('text-anchor', 'start');
      rightLabel.setAttribute('class', 'plan-actual-right-axis-label');
      rightLabel.textContent = eur(balanceAmount); svg.append(rightLabel);
    }
  }
  for (const item of series) {
    const points = months.map((row, index) => ({index, value: numericValue(row, item)}))
      .filter(point => point.value !== null);
    if (!points.length) continue;
    const path = document.createElementNS(ns, 'path');
    path.setAttribute('d', points.map((point, index) =>
      `${index && point.index === points[index - 1].index + 1 ? 'L' : 'M'} ${x(point.index)} ${yFor(point.value, item)}`).join(' '));
    path.setAttribute('fill', 'none'); path.setAttribute('stroke', item.color);
    path.setAttribute('stroke-width', item.source === 'balance' ? '4' : '3');
    if (item.planned) path.setAttribute('stroke-dasharray', '8 6');
    path.setAttribute('aria-label', item.label); svg.append(path);
    for (const point of granularity === 'daily' ? [] : points) {
      const marker = document.createElementNS(ns, 'circle'); marker.setAttribute('cx', String(x(point.index)));
      marker.setAttribute('cy', String(yFor(point.value, item))); marker.setAttribute('r', '3.5');
      marker.setAttribute('fill', 'white'); marker.setAttribute('stroke', item.color);
      marker.setAttribute('stroke-width', '2'); svg.append(marker);
    }
  }
  months.forEach((row, index) => {
    if (granularity === 'daily' && index > 0
        && row.month.slice(0, 7) === months[index - 1].month.slice(0, 7)) return;
    const label = document.createElementNS(ns, 'text'); label.setAttribute('x', String(x(index)));
    label.setAttribute('y', String(height - 12)); label.setAttribute('text-anchor', 'middle');
    label.setAttribute('class', 'plan-actual-month-label');
    label.textContent = granularity === 'daily' ? row.month.slice(0, 7) : row.month;
    svg.append(label);
  });
}

function planActualMonthlyRangeValues(months, view, cumulative) {
  if (!cumulative) return months;
  const totals = {};
  return months.map(row => {
    const output = {...row, plan: {...row.plan}, actual: {...row.actual},
      balance_change: {...row.balance_change}};
    for (const series of ['plan', 'actual']) {
      const values = {...(row[series]?.[view] || {})};
      for (const key of ['income', 'expenses']) {
        const totalKey = `${series}:${key}`;
        // A cumulative plan from the period start is unknown after its first missing month.
        // Keep it null; a zero or restart would imply a complete continuous plan.
        if (series === 'plan' && (values[key] == null || totals[totalKey] === null)) {
          totals[totalKey] = null;
          values[key] = null;
          continue;
        }
        totals[totalKey] = (totals[totalKey] || 0) + Number(values[key] || 0);
        values[key] = totals[totalKey];
      }
      output[series][view] = values;
    }
    const balanceKey = `balance:${view}`;
    totals[balanceKey] = (totals[balanceKey] || 0) + Number(row.balance_change?.[view] || 0);
    output.balance_change[view] = totals[balanceKey];
    return output;
  });
}

function planActualTrendComparisonRows(months, range, selectedMonth, view) {
  let selected = months || [];
  if (range === '3' || range === '6') selected = selected.slice(-Number(range));
  if (range === 'year') selected = selected.filter(row =>
    row.month.startsWith(`${selectedMonth?.slice(0, 4)}-`));
  return selected.map(row => {
    const fixed = row.actual_fixed?.[view];
    const other = row.actual_other_expenses?.[view];
    const totalIncome = row.actual?.[view]?.income ?? null;
    const fixedIncome = fixed?.income ?? null;
    return {
      month: row.month,
      planSource: row.plan_source ?? null,
      fixedIncome,
      otherIncome: fixedIncome === null || totalIncome === null
        ? null : Number(totalIncome) - Number(fixedIncome),
      totalIncome,
      fixedExpenses: fixed?.expenses ?? null,
      otherExpenses: (other && typeof other === 'object'
        ? other.expenses ?? other.amount : other) ?? null,
      totalExpenses: row.actual?.[view]?.expenses ?? null,
    };
  });
}

function planActualRenderTrendComparisonTable(svg, trend, range, view) {
  const section = svg.closest('section');
  if (!section) return;
  let container = $('plan-actual-trend-comparison');
  if (!container) {
    container = document.createElement('div'); container.id = 'plan-actual-trend-comparison';
    container.className = 'plan-actual-trend-comparison'; section.append(container);
  }
  container.replaceChildren();
  container.hidden = range === 'month';
  if (container.hidden) return;
  const rows = planActualTrendComparisonRows(trend?.months, range, trend?.selected_month, view);
  if (!rows.length) return;
  const historicalActuals = rows.some(row => row.planSource === 'reconstructed_from_monthly_actuals');
  const onlyHistoricalActuals = historicalActuals && rows.every(
    row => row.planSource === 'reconstructed_from_monthly_actuals');
  const heading = document.createElement('h4');
  heading.textContent = onlyHistoricalActuals
    ? 'Historische Zusammensetzung (Gesamtplan = Ist gesamt)' : 'Monatsvergleich';
  const scroll = document.createElement('div'); scroll.className = 'table-wrap';
  const table = document.createElement('table'); table.className = 'plan-actual-trend-comparison-table';
  const headers = ['Monat', 'Feste Einnahmen', 'Sonstige Einnahmen', 'Einnahmen gesamt',
    'Feste Ausgaben', 'Sonstige Ausgaben', 'Ausgaben gesamt'];
  const thead = document.createElement('thead'); const headerRow = document.createElement('tr');
  for (const [index, label] of headers.entries()) {
    const th = document.createElement('th'); th.textContent = label;
    if (index) th.className = 'numeric';
    headerRow.append(th);
  }
  thead.append(headerRow); table.append(thead);
  const tbody = document.createElement('tbody');
  for (const item of rows) {
    const tr = document.createElement('tr');
    const month = document.createElement('th'); month.scope = 'row'; month.textContent = item.month;
    tr.append(month);
    for (const key of ['fixedIncome', 'otherIncome', 'totalIncome', 'fixedExpenses',
      'otherExpenses', 'totalExpenses']) {
      const td = document.createElement('td'); td.className = 'numeric';
      td.textContent = item[key] === null ? '—' : eur(Math.abs(Number(item[key])));
      tr.append(td);
    }
    tbody.append(tr);
  }
  table.append(tbody); scroll.append(table); container.append(heading, scroll);
}

function planActualNodeDelta(node) {
  return Boolean(node.attention) || (node.children || []).some(planActualNodeDelta);
}

function planActualIncomeStatus(node) {
  if (node.planned == null) return {label: 'Kein gespeicherter Monatsplan', className: 'no_actual'};
  const planned = Number(node.planned || 0), actual = Number(node.actual || 0);
  if (actual > planned + .005) return {label: 'Über Plan eingegangen', className: 'income-over'};
  if (actual >= planned - .005) return {label: 'Exakt im Plan', className: 'income-exact'};
  if (actual > 0) return {label: 'Teilweise eingegangen', className: 'income-partial'};
  return {label: 'Noch keine Einnahme', className: 'no_actual'};
}

function planActualExpenseStatus(node) {
  if (node.planned == null) return {label: 'Kein gespeicherter Monatsplan', className: 'no_actual'};
  const planned = Number(node.planned || 0), actual = Number(node.actual || 0);
  const remaining = Number(node.remaining || 0);
  if (!planned && actual) return {label: 'Ohne Budget', className: 'unbudgeted'};
  if (remaining < 0) return {label: 'Überschritten', className: 'exceeded'};
  if (planned > 0 && remaining === 0) return {label: 'Exakt im Plan', className: 'exact'};
  return {label: actual ? 'Im Rahmen' : 'Noch keine Buchung', className: 'within'};
}

function planActualVarianceClass(value) {
  const variance = Number(value || 0);
  return variance < -0.005 ? 'negative' : variance > 0.005 ? 'positive' : 'zero';
}

function planActualRenderTree(tree) {
  const target = $('plan-actual-tree'); target.replaceChildren();
  const deviationsOnly = $('plan-actual-differences-only').checked;
  let detailIndex = 0;
  const renderNode = (node, parent, level = 0, inheritedKind = '') => {
    const children = Array.isArray(node.children) ? node.children : [];
    const changed = planActualNodeDelta(node) || Boolean(node.attention);
    if (deviationsOnly && !changed) return;
    const row = document.createElement('div');
    const declaredKind = String(node.kind || node.level || '').toLowerCase();
    const kind = declaredKind || inheritedKind;
    const branchKind = ['income', 'expense'].includes(declaredKind) ? declaredKind : inheritedKind;
    row.className = `plan-actual-tree-row level-${level}${changed ? ' has-deviation' : ''}`;
    row.style.setProperty('--tree-depth', String(level));
    if (children.length) {
      const toggle = document.createElement('button'); toggle.type = 'button'; toggle.className = 'plan-actual-tree-toggle';
      const openByDefault = level < 2;
      toggle.setAttribute('aria-expanded', String(openByDefault));
      toggle.textContent = openByDefault ? '−' : '+';
      const childId = `plan-actual-tree-${++detailIndex}`;
      toggle.setAttribute('aria-controls', childId);
      toggle.setAttribute('aria-label', `${openByDefault ? 'Einklappen' : 'Aufklappen'}: ${node.label || 'Gruppe'}`);
      const nested = document.createElement('div'); nested.id = childId; nested.hidden = !openByDefault; nested.className = 'plan-actual-tree-children';
      toggle.addEventListener('click', () => {
        const open = toggle.getAttribute('aria-expanded') !== 'true';
        toggle.setAttribute('aria-expanded', String(open)); toggle.textContent = open ? '−' : '+'; nested.hidden = !open;
        toggle.setAttribute('aria-label', `${open ? 'Einklappen' : 'Aufklappen'}: ${node.label || 'Gruppe'}`);
      });
      row.append(toggle);
      const label = document.createElement('strong'); label.textContent = node.label || 'Ohne Bezeichnung'; row.append(label);
      const groupSummary = document.createElement('span'); groupSummary.className = 'plan-actual-tree-group-values';
      groupSummary.textContent = `Plan ${node.planned == null ? '—' : planActualTreePlanIst(node.planned, kind)} · Ist ${planActualTreePlanIst(node.actual, kind)} · Rest ${node.remaining == null ? '—' : planActualMoney(node.remaining)}`; row.append(groupSummary);
      parent.append(row); parent.append(nested); children.forEach(child => renderNode(child, nested, level + 1, branchKind));
      return;
    }
    const label = document.createElement('strong'); label.textContent = node.label || 'Ohne Bezeichnung'; row.append(label);
    const plan = document.createElement('span'); plan.textContent = `Plan ${node.planned == null ? '—' : planActualTreePlanIst(node.planned, branchKind)}`; row.append(plan);
    const actual = document.createElement('span'); actual.textContent = `Ist ${planActualTreePlanIst(node.actual, branchKind)}`; row.append(actual);
    const rest = document.createElement('span'); rest.textContent = `Rest ${node.remaining == null ? '—' : planActualMoney(node.remaining)}`; row.append(rest);
    const status = document.createElement('span');
    if (branchKind === 'unclassified' || kind === 'unclassified') {
      status.textContent = 'Noch nicht eingeordnet'; status.className = 'plan-actual-state near_limit';
    } else if (branchKind === 'income' || kind === 'income_item') {
      const classification = planActualIncomeStatus(node); status.textContent = classification.label; status.className = `plan-actual-state ${classification.className}`;
    } else {
      const classification = planActualExpenseStatus(node);
      if (node.attention && classification.className === 'within') {
        status.textContent = 'Schätzung oder Prüfung nötig'; status.className = 'plan-actual-state near_limit';
      } else {
        status.textContent = classification.label; status.className = `plan-actual-state ${classification.className}`;
      }
    }
    row.append(status);
    const itemId = node.item_id || (node.level === 'item' && String(node.key || '').startsWith('item:')
      ? String(node.key).slice(5) : null);
    if (itemId && branchKind !== 'unclassified' && !itemId.startsWith('unmapped:')
        && !itemId.startsWith('open-classification:')
        && Number(node.transaction_count || 0) > 0) {
      const details = document.createElement('div'); details.className = 'plan-actual-tree-detail'; details.hidden = true;
      row.append(planActualDetailsButton(`${node.transaction_count} Buchungen ansehen`, planActualContext('item', itemId), details, `plan-actual-tree-detail-${detailIndex++}`, branchKind === 'income' ? 'income' : 'expense'));
      parent.append(row, details); return;
    }
    parent.append(row);
  };
  tree.forEach(node => renderNode(node, target));
  if (!target.children.length) { const empty = document.createElement('p'); empty.className = 'muted'; empty.textContent = deviationsOnly ? 'Keine Abweichungen im gewählten Monat.' : 'Keine Plan-Ist-Gruppen verfügbar.'; target.append(empty); }
}

function planActualRenderCockpit(result) {
  const available = planActualHasNewFields(result);
  $('plan-actual-cockpit').dataset.available = String(available);
  $('plan-actual-cockpit').hidden = true;
  if (!available) {
    $('plan-actual-view-note').textContent = 'Cockpitdaten fehlen; die klassische Ansicht wird verwendet.';
    planActualSetView('classic', false); return;
  }
  $('plan-actual-view-note').textContent = result.plan_available === false
    ? 'Ist-Buchungen im Cockpit; klassische Ansicht mit vollständiger Buchungszuordnung.'
    : 'Cockpit: verfügbare Mittel und Zeiträume. Klassisch: vollständige Plan-Ist-Tabelle.';
  const historical = planActualIsHistorical(result);
  const payday = historical ? null : result.payday;
  const actualOnly = historical || result.plan_available === false;
  $('plan-actual-cockpit').dataset.historical = String(actualOnly);
  const freeCard = $('plan-actual-free-title').closest('.plan-actual-free-card');
  // A planned historical month shows actual cashflow in the bridge, once only.
  freeCard.hidden = historical && result.plan_available !== false;
  freeCard.querySelector('.eyebrow').textContent = actualOnly ? `IST · ${result.month}` : 'BIS ZUM NÄCHSTEN GEHALT';
  $('plan-actual-free-title').textContent = actualOnly ? 'Ist-Cashflow (Monatsüberschuss)' : 'Kurzfristig frei ausgebbar';
  $('plan-actual-free-value').textContent = payday?.available
    ? planActualAvailable(payday.total_free_spendable)
    : actualOnly ? planActualSignedEur(result.totals?.actual_cashflow ?? 0) : 'Nicht verfügbar';
  const salaryDates = [...new Set((payday?.cycles || []).map(cycle => cycle.next_salary_date).filter(Boolean))];
  $('plan-actual-free-period').textContent = payday?.available
    ? `Nach erfassten Fälligkeiten bis zum Gehalt am ${salaryDates.map(planActualDate).join(' und ')}. Zeitraum: heute bis zu diesem Termin.`
    : actualOnly ? 'Ist-Einnahmen minus Ist-Ausgaben; kein Kontostand. Personenzuordnung nach Inhaberschaft der Buchungskonten.'
      : 'Für diesen Zeitraum liegen keine bestätigten Gehaltsdaten vor.';
  planActualRenderDataQuality(result.data_quality);
  const split = $('plan-actual-free-split'); split.replaceChildren();
  split.hidden = !payday?.available;
  for (const cycle of payday?.available ? payday.cycles || [] : []) {
    const span = document.createElement('span');
    span.textContent = `${cycle.owner_label}: ${planActualAvailable(cycle.free_spendable)} bis ${planActualDate(cycle.next_salary_date)}`;
    split.append(span);
  }
  for (const [label, value] of [['Offene Budgets im Monatsrahmen · nicht zusätzlich', payday?.flexible_budget_remaining], ['Nicht verplant · nach Budgets bis Gehalt', payday?.unallocated_after_budgets]]) {
    if (payday?.available) {
      const span = document.createElement('span'); span.textContent = `${label}: ${eur(value || 0)}`; split.append(span);
    }
  }
  if (payday?.available) {
    const cap = document.createElement('span');
    cap.className = 'plan-actual-free-cap';
    cap.textContent = `${planActualAvailable(payday.total_free_spendable)} sind die Obergrenze bis zum nächsten Gehalt; offene Monatsbudgets kommen nicht oben drauf.`;
    split.append(cap);
  }
  window.planActualLatestResult = result;
  planActualRenderSurplusBridge(result.surplus_bridge, historical && result.plan_available !== false, result);
  planActualRenderDue(payday);
  $('plan-actual-due-list').closest('.plan-actual-due').hidden = historical || result.plan_available === false;
  $('plan-actual-tab-accounts').textContent = historical ? 'Konten-Rückblick' : 'Konten bis Gehalt';
  $('plan-actual-panel-accounts').querySelector('h3').textContent = historical
    ? 'Konten-Rückblick' : 'Konten bis Gehalt';
  planActualRenderDaily(result.daily, result.daily_metadata, result.daily_by_person,
    result.account_balance_change);
  $('plan-actual-differences-only').checked = result.plan_available === false
    ? false : $('plan-actual-differences-only').checked;
  $('plan-actual-differences-only').disabled = result.plan_available === false;
  planActualRenderTree(result.tree);
  planActualRenderCockpitAccounts(historical || result.plan_available === false
    ? {...result, payday: null, liquidity: null, historical} : result);
  const preferred = sessionStorage.getItem('finance-control-plan-actual-view') || 'cockpit';
  planActualSetView(preferred);
}

function planActualIsHistorical(result, currentMonth = planActualCurrentMonth()) {
  return result.basis?.type === 'retrospective_reference'
    || Boolean(result.month && result.month < currentMonth);
}

function planActualRenderDataQuality(quality) {
  const note = $('plan-actual-data-quality');
  note.replaceChildren();
  const warnings = Array.isArray(quality?.warnings) ? quality.warnings : [];
  const excluded = Array.isArray(quality?.excluded_accounts) ? quality.excluded_accounts : [];
  const status = quality?.status;
  note.hidden = !['incomplete', 'partial'].includes(status)
    && !warnings.length && !excluded.length;
  if (note.hidden) return;
  const heading = document.createElement('strong');
  const openCount = warnings.filter(warning => warning.code === 'classification_open')
    .reduce((sum, warning) => sum + Number(warning.count || 0), 0);
  heading.textContent = `${status === 'incomplete' ? 'Datenlage unvollständig' : 'Datenlage teilweise'}${openCount ? ` · ${openCount} Buchungen offen` : ''}`;
  note.append(heading);
  const details = document.createElement('details'), summary = document.createElement('summary');
  summary.textContent = 'Datenhinweise'; details.append(summary);
  for (const warning of warnings) {
    const line = document.createElement('span');
    line.textContent = [warning.message, warning.account_id && `Konto ${warning.account_id}`,
      warning.available_from && `verfügbar ab ${warning.available_from}`].filter(Boolean).join(' · ');
    details.append(document.createElement('br'), line);
  }
  if (excluded.length) {
    const line = document.createElement('span');
    line.textContent = `Ausgeschlossene Konten: ${excluded.join(', ')}.`;
    details.append(document.createElement('br'), line);
  }
  note.append(details);
}

function planActualHistoricalCashflow(result) {
  const totals = result?.totals || {};
  const people = (result?.daily_people || []).filter(person => person.id !== 'TOTAL');
  return {
    actualCashflow: totals.actual_cashflow ?? '0.00',
    plannedIncome: totals.planned_income ?? '0.00',
    plannedExpenses: totals.planned_expenses ?? '0.00',
    actualIncome: totals.actual_income ?? '0.00',
    actualExpenses: totals.actual_expenses ?? '0.00',
    people: people.flatMap(person => {
      const points = result?.daily_by_person?.[person.id] || [];
      const last = points.at(-1);
      if (!last) return [];
      return [{label: person.label || person.id,
        cashflow: (Number(last.cumulative_income || 0)
          - Number(last.cumulative_expenses || 0)).toFixed(2)}];
    }),
  };
}

function planActualRenderSurplusBridge(bridge, historical = false, result = null) {
  const section = $('plan-actual-surplus-bridge');
  if (historical) {
    const summary = planActualHistoricalCashflow(result);
    section.hidden = false;
    $('plan-actual-surplus-eyebrow').textContent = `RÜCKBLICK · ${result.month}`;
    $('plan-actual-surplus-joint-note').hidden = true;
    $('plan-actual-surplus-title').textContent = 'Ist- und Planwerte des Monats';
    $('plan-actual-surplus-note').textContent = 'Ist-Cashflow bedeutet Monatsüberschuss aus Ist-Einnahmen minus Ist-Ausgaben; kein Kontostand. Personensummen folgen der Inhaberschaft der Buchungskonten.';
    $('plan-actual-surplus-remaining').textContent = planActualSignedEur(summary.actualCashflow);
    const steps = $('plan-actual-surplus-steps'); steps.replaceChildren();
    const values = [
      ['Ist-Einnahmen gesamt', summary.actualIncome],
      ['Ist-Ausgaben gesamt', summary.actualExpenses, 'expense'],
      ['Plan-Einnahmen', summary.plannedIncome],
      ['Plan-Ausgaben', summary.plannedExpenses, 'expense'],
      ...summary.people.map(person => [`${person.label} · Ist-Cashflow`, person.cashflow, 'signed']),
    ];
    for (const [label, value, direction] of values) {
      const article = document.createElement('article');
      const caption = document.createElement('span'); caption.textContent = label;
      const amount = document.createElement('strong');
      amount.textContent = direction === 'expense' ? planActualSignedEur(value, 'expense')
        : direction === 'signed' ? planActualSignedEur(value) : eur(value);
      article.append(caption, amount); steps.append(article);
    }
    $('plan-actual-surplus-pending').textContent = '';
    return;
  }
  if (!bridge?.confirmed) { section.hidden = true; return; }
  section.hidden = false;
  $('plan-actual-surplus-eyebrow').textContent = 'MONATSRAHMEN';
  $('plan-actual-surplus-joint-note').hidden = false;
  $('plan-actual-surplus-title').textContent = 'Geplanter Monatssaldo';
  $('plan-actual-surplus-note').textContent = 'Unverplanter Rest einschließlich verfügbarer Budgets. Dieser Rahmen ist nicht vollständig sofort auf den Konten verfügbar.';
  const spendable = bridge.household_split?.spendable;
  $('plan-actual-surplus-remaining').textContent = eur(
    spendable?.confirmed?.total ?? bridge.confirmed.still_unallocated);
  const steps = $('plan-actual-surplus-steps'); steps.replaceChildren();
  const values = [
    ['Geplanter Monatssaldo', bridge.original_planned_surplus, null],
    ['Zusätzliche Einnahmen', bridge.confirmed.additional_income, 'income'],
    ['Ungeplante Ausgaben und feste Mehrkosten', bridge.confirmed.unplanned_expenses, 'expense'],
    ['Verfügbare Budgets · im Monatsrahmen enthalten', bridge.confirmed.budget_balance?.remaining, null],
  ];
  for (const [label, value, direction] of values) {
    const article = document.createElement('article');
    const caption = document.createElement('span'); caption.textContent = label;
    const amount = document.createElement('strong');
    amount.textContent = direction ? planActualSignedEur(value, direction) : eur(value || 0);
    article.append(caption, amount); steps.append(article);
  }
  const pending = bridge.unconfirmed || {};
  const parts = [];
  const budget = bridge.confirmed.budget_balance;
  if (budget) parts.push(`Budgetbilanz: ${eur(budget.actual_total)} von ${eur(budget.planned_total)} verbraucht`);
  parts.push(`Davon nicht verplant: ${eur(bridge.confirmed.still_unallocated)}`);
  const allocations = spendable?.confirmed?.allocations
    || bridge.household_split?.confirmed?.allocations;
  if (allocations) {
    parts.push(...Object.entries(allocations).map(([owner, value]) =>
      `${planActualOwnerLabel(owner)}: ${eur(value)}`));
  }
  if (Number(pending.count || 0)) {
    const bookingLabel = Number(pending.count) === 1 ? 'vorgemerkte oder ungeklärte Buchung' : 'vorgemerkte oder ungeklärte Buchungen';
    parts.push(`${pending.count} ${bookingLabel}: ${planActualSignedEur(pending.net || 0)}`);
    const pendingTotal = spendable?.including_unconfirmed?.total
      ?? pending.still_unallocated_if_confirmed;
    parts.push(`Falls bestätigt: ${eur(pendingTotal || 0)} insgesamt noch ausgebbar`);
  }
  $('plan-actual-surplus-pending').textContent = parts.join(' · ');
}

function planActualRenderCockpitAccounts(result) {
  const target = $('plan-actual-cockpit-accounts'); target.replaceChildren();
  const cycles = result.payday?.cycles || [];
  const liquidityById = new Map((result.liquidity?.accounts || []).map(account => [account.account_id, account]));
  if (!cycles.length && !liquidityById.size) {
    target.textContent = result.historical
      ? 'Keine heutige Kontenprognose für den historischen Monat.' : 'Kontenprognose nicht verfügbar.';
    return;
  }
  const grid = document.createElement('div'); grid.className = 'plan-actual-account-grid';
  for (const cycle of cycles) {
    const liquidity = liquidityById.get(cycle.account_id);
    const article = document.createElement('article');
    const heading = document.createElement('h4'); heading.textContent = `${cycle.owner_label} · ${cycle.account_id}`;
    const list = document.createElement('dl');
    const values = [
      ['Heute', cycle.current_balance, null],
      ['Vorgemerkt', liquidity?.pending_transfer || 0, null],
      ['Noch eingehend', cycle.upcoming_inflows, 'income'],
      ['Noch fällig', cycle.upcoming_outflows, 'expense'],
      [`Frei bis ${planActualDate(cycle.next_salary_date)}`, Math.max(0, Number(cycle.free_spendable || 0)), null],
    ];
    for (const [label, value, direction] of values) {
      const term = document.createElement('dt'); term.textContent = label;
      const description = document.createElement('dd'); description.textContent = planActualSignedEur(value, direction);
      if (Number(value) < 0 && !String(label).startsWith('Frei bis')) description.className = 'negative';
      list.append(term, description);
    }
    article.append(heading, list); grid.append(article);
  }
  for (const [accountId, liquidity] of liquidityById) {
    if (cycles.some(cycle => cycle.account_id === accountId)) continue;
    const article = document.createElement('article');
    const heading = document.createElement('h4'); heading.textContent = liquidity.label || accountId;
    const text = document.createElement('p');
    text.textContent = `Heute ${planActualSignedEur(liquidity.current_balance)} · nach Vormerkung ${planActualSignedEur(liquidity.balance_after_pending)} · nach festen Ausgaben ${planActualSignedEur(liquidity.available_after_commitments)}`;
    article.append(heading, text); grid.append(article);
  }
  target.append(grid);
}

function planActualSetupCockpitTabs() {
  const tabs = [...$('plan-actual-cockpit').querySelectorAll('[role="tab"]')];
  const activate = tab => {
    for (const current of tabs) {
      const selected = current === tab; current.setAttribute('aria-selected', String(selected)); current.tabIndex = selected ? 0 : -1;
      $(current.getAttribute('aria-controls')).hidden = !selected;
    }
    if (tab.id === 'plan-actual-tab-trend') planActualScheduleMonthChartResize();
    tab.focus();
  };
  tabs.forEach(tab => tab.addEventListener('click', () => {
    activate(tab);
  }));
  tabs.forEach(tab => tab.addEventListener('keydown', event => {
    const index = tabs.indexOf(tab);
    const next = event.key === 'ArrowRight' ? (index + 1) % tabs.length
      : event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length
        : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : -1;
    if (next >= 0) { event.preventDefault(); activate(tabs[next]); }
  }));
  $('plan-actual-differences-only').addEventListener('change', () => {
    const tree = window.planActualLatestResult?.tree; if (tree) planActualRenderTree(tree);
  });
  $('plan-actual-cockpit-mode').addEventListener('click', () => planActualSetView('cockpit'));
  $('plan-actual-classic-mode').addEventListener('click', () => planActualSetView('classic'));
}

function planActualAppendMetricGroups(container, totals, planAvailable = true) {
  const groups = document.createElement('div'); groups.className = 'plan-actual-person-metric-groups';
  const metrics = [
    ['Einnahmen', totals.income], ['Ausgaben', totals.expenses], ['Saldo', totals.balance],
  ];
  for (const [label, values] of metrics) {
    const group = document.createElement('section'); group.className = 'plan-actual-person-metric-group';
    const heading = document.createElement('h5'); heading.textContent = label; group.append(heading);
    const grid = document.createElement('div'); grid.className = 'plan-actual-person-metric-values';
    for (const [title, value, variance] of [
      ['Plan', values.planned, false], ['Ist', values.actual, false],
      ['Abweichung', values.variance, true],
    ]) {
      const item = document.createElement('div'), name = document.createElement('span'),
        amount = document.createElement('strong');
      name.textContent = title;
      amount.textContent = (title !== 'Ist' && !planAvailable) ? '—'
        : variance ? planActualSignedEur(value) : eur(value);
      if (variance && planAvailable) amount.className = `plan-actual-person-variance ${planActualVarianceClass(value)}`;
      item.append(name, amount); grid.append(item);
    }
    group.append(grid); groups.append(group);
  }
  container.append(groups);
}

function planActualBasisLabel(result) {
  const year = result.month.slice(0, 4);
  if (result.plan_available === false) return `${year} · kein gespeicherter Monatsplan`;
  if (result.basis?.type === 'retrospective_reference')
    return `${year} Rückblick (Plan=Ist)`;
  if (result.basis?.type === 'monthly_budget_projection')
    return `${year} Budgetrevision ${result.revision}`;
  return `${year} Budgetrevision ${result.revision}`;
}

function planActualTotalMetrics(totals) {
  const income = {planned: planActualIncomeValue(totals.planned_income),
    actual: planActualIncomeValue(totals.actual_income)};
  const expenses = {planned: planActualExpenseValue(totals.planned_expenses),
    actual: planActualExpenseValue(totals.actual_expenses)};
  const balance = {planned: income.planned + expenses.planned,
    actual: income.actual + expenses.actual};
  for (const metric of [income, expenses, balance])
    metric.variance = planActualVariance(metric.actual, metric.planned);
  return {income, expenses, balance};
}

function planActualRenderTotalSummary(result) {
  const box = $('plan-actual-total-metrics'); box.replaceChildren();
  $('plan-actual-total-title').textContent = 'TOTAL · Monat gesamt';
  const scopeNote = planActualReportingHistoryNote(result);
  const automaticCount = Number(result.automatic_owner_mapping_count || 0);
  const ownerNote = automaticCount > 0
    ? ` ${automaticCount} Gehaltsbuchung${automaticCount === 1 ? ' wurde' : 'en wurden'} automatisch über eindeutige Kontoinhaberschaft einer Planposition zugeordnet.` : '';
  $('plan-actual-total-basis').textContent = `Abweichung = Ist − Plan.${scopeNote ? ` ${scopeNote}` : ''}${ownerNote}`;
  planActualAppendMetricGroups(box, planActualTotalMetrics(result.totals), result.plan_available !== false);
}

function planActualExpenseReconciliation(result) {
  const cents = value => Math.round(Number(value || 0) * 100);
  const positions = (result.rows || []).filter(row => row.kind !== 'income')
    .reduce((sum, row) => sum + cents(row.actual), 0);
  const unmapped = cents(result.unmapped?.expenses);
  const total = cents(result.totals.actual_expenses);
  const remainder = total - positions - unmapped;
  return [
    ['Planpositionen', -positions / 100],
    ['Weitere Ausgaben ohne Planzuordnung', -unmapped / 100],
    ...(remainder ? [['Weitere erfasste Ausgaben', -remainder / 100]] : []),
    ['Ausgaben gesamt', -total / 100],
  ];
}

function planActualRenderExpenseReconciliation(result) {
  const box = $('plan-actual-expense-reconciliation-values'); box.replaceChildren();
  const list = document.createElement('dl'); list.className = 'plan-actual-expense-reconciliation-values';
  for (const [label, amount] of planActualExpenseReconciliation(result)) {
    const title = document.createElement('dt'), value = document.createElement('dd');
    title.textContent = label; value.textContent = planActualSignedEur(amount);
    list.append(title, value);
  }
  box.append(list);
}

function planActualRenderCash(result) {
  const section = $('plan-actual-cash'), box = $('plan-actual-cash-content');
  box.replaceChildren();
  const cash = result.cash_activity;
  section.hidden = !cash || (!Number(cash.withdrawals?.total) && !Number(cash.receipt_spending?.total));
  if (section.hidden) return;
  const withdrawals = cash.withdrawals || {}, receipts = cash.receipt_spending || {};
  const totals = document.createElement('div'); totals.className = 'cards plan-actual-cash-totals';
  for (const [label, amount] of [['Geldabhebungen', withdrawals.total],
    ['Mit Belegen bar bezahlt', receipts.total]]) {
    const card = document.createElement('article'), title = document.createElement('span'),
      value = document.createElement('strong');
    title.textContent = label; value.textContent = eur(Math.abs(Number(amount || 0)));
    card.append(title, value); totals.append(card);
  }
  box.append(totals);
  const people = [...(result.person_breakdown?.people || []), ...(result.daily_people || [])];
  const personLabel = id => people.find(person => person.id === id)?.label
    || (id === 'JOINT' ? 'Gemeinsam' : planActualOwnerLabel(id));
  const ids = [...new Set([...Object.keys(withdrawals.by_person || {}),
    ...Object.keys(receipts.by_person || {})])].filter(id =>
    Number(withdrawals.by_person?.[id]) || Number(receipts.by_person?.[id]));
  if (ids.length) {
    const wrap = document.createElement('div'); wrap.className = 'table-wrap';
    const table = document.createElement('table'); table.className = 'plan-actual-cash-table';
    const head = document.createElement('thead'), header = document.createElement('tr');
    for (const label of ['Person', 'Geldabhebungen', 'Mit Belegen bar bezahlt']) {
      const cell = document.createElement('th'); cell.scope = 'col'; cell.textContent = label; header.append(cell);
    }
    head.append(header); table.append(head);
    const body = document.createElement('tbody');
    for (const id of ids) {
      const row = document.createElement('tr'), name = document.createElement('th');
      name.scope = 'row'; name.textContent = personLabel(id); row.append(name);
      for (const amount of [withdrawals.by_person?.[id], receipts.by_person?.[id]]) {
        const cell = document.createElement('td'); cell.textContent = eur(Math.abs(Number(amount || 0))); row.append(cell);
      }
      body.append(row);
    }
    table.append(body); wrap.append(table); box.append(wrap);
  }
  const items = Object.entries(receipts.by_item || {}).filter(([, amount]) => Number(amount) > 0);
  if (items.length) {
    const details = document.createElement('details'), summary = document.createElement('summary'),
      list = document.createElement('ul'); list.className = 'plan-actual-cash-items';
    summary.textContent = 'Barbelege nach Planposition';
    for (const [id, amount] of items) {
      const item = (result.rows || []).find(row => row.item_id === id);
      const owners = Object.entries(item?.actual_by_person || {}).filter(([, value]) => Number(value) > 0);
      const owner = owners.length === 1 ? personLabel(owners[0][0]) : '';
      const label = item ? planActualContextLabel(planActualCashItemLabel(result, item), owner) : 'Planposition nicht mehr vorhanden';
      const row = document.createElement('li'), title = document.createElement('span'), value = document.createElement('strong');
      title.textContent = `${owner ? `${owner} · ` : ''}${label}`;
      value.textContent = eur(Math.abs(Number(amount))); row.append(title, value); list.append(row);
    }
    details.append(summary, list); box.append(details);
  }
}

function planActualRenderPeople(result) {
  const section = $('plan-actual-person-breakdown');
  const box = $('plan-actual-person-cards');
  const breakdown = result.person_breakdown;
  box.replaceChildren();
  section.hidden = !breakdown;
  if (!breakdown) return;
  $('plan-actual-person-basis').textContent =
    `${result.plan_available === false ? 'Kein gespeicherter Monatsplan. ' : `Plan: ${breakdown.planned_basis}. `}Ist: ${breakdown.actual_basis}. `+
    'Persönliche Konten zählen vollständig zur Person, Gemeinschaftskonten zu Gemeinsam; keine Haushaltsquote.';
  const bucketOrder = ['ANDREAS', 'ERLENE', 'JOINT'];
  const people = [...planActualVisiblePeople(breakdown.people, result.reporting_scope)]
    .sort((left, right) => {
    const leftIndex = bucketOrder.indexOf(left.id), rightIndex = bucketOrder.indexOf(right.id);
    return (leftIndex < 0 ? bucketOrder.length : leftIndex)
      - (rightIndex < 0 ? bucketOrder.length : rightIndex);
  });
  for (const person of people) {
    const card = document.createElement('article'); card.className = 'plan-actual-person-card';
    const heading = document.createElement('h4'); heading.textContent = person.label;
    card.append(heading);
    planActualAppendMetricGroups(card, planActualPersonTotals(breakdown, [person.id]), result.plan_available !== false);
    const retrospective = result.basis?.type === 'retrospective_reference'
      && Array.isArray(result.retrospective_positions);
    if (retrospective)
      planActualRenderRetrospectivePositions(card, result.retrospective_positions, person.id);
    const rows = retrospective ? [] : planActualPersonDetailRows(result, person.id);
    if (rows.length) {
      const details = document.createElement('details'), summary = document.createElement('summary'),
        tableWrap = document.createElement('div'), table = document.createElement('table'), head = document.createElement('thead'),
        header = document.createElement('tr'), body = document.createElement('tbody');
      summary.textContent = `${rows.length} Positionen · ${result.plan_available === false ? 'nach Istbetrag' : 'nach Abweichung'} sortiert`;
      tableWrap.className = 'plan-actual-person-table-wrap';
      table.className = 'plan-actual-person-table';
      for (const text of ['Art', 'Planposition', 'Plan', 'Ist', 'Abweichung']) {
        const cell = document.createElement('th'); cell.textContent = text; header.append(cell);
      }
      head.append(header);
      for (const {row, kind, planned: rowPlanned, actual: rowActual} of rows) {
        const entry = document.createElement('tr'), label = document.createElement('th');
        const type = document.createElement('td'); type.textContent = kind === 'income' ? 'Einnahme' : 'Ausgabe'; entry.append(type);
        label.scope = 'row'; label.textContent = planActualContextLabel(planActualCashItemLabel(result, row), person.label); entry.append(label);
        for (const [index, text] of [result.plan_available === false ? '—' : eur(rowPlanned), eur(rowActual),
          result.plan_available === false ? '—' : planActualSignedEur(planActualVariance(rowActual, rowPlanned))].entries()) {
          const amount = document.createElement('td'); amount.textContent = text;
          if (index === 2 && result.plan_available !== false) amount.className = `plan-actual-person-variance ${planActualVarianceClass(planActualVariance(rowActual, rowPlanned))}`;
          entry.append(amount);
        }
        body.append(entry);
      }
      table.append(head, body); tableWrap.append(table);
      details.append(summary, tableWrap); card.append(details);
    }
    box.append(card);
  }
}

function planActualRender(result) {
  $('plan-actual-result').hidden = false;
  const planAvailable = result.plan_available !== false;
  $('plan-actual-no-plan').hidden = planAvailable;
  const historical = planActualIsHistorical(result);
  $('plan-actual-revision-label').textContent = `${result.month} · ${planActualBasisLabel(result)}`;
  planActualRenderTotalSummary(result);
  planActualRenderExpenseReconciliation(result);
  planActualRenderCash(result);
  if (result.available_from_month)
    $('plan-actual-month').min = result.available_from_month;
  planActualUpdateMonthNavigation();
  $('plan-actual-planned-expenses').textContent = planActualPlanMoney(
    result.totals.planned_expenses, 'expense');
  $('plan-actual-expenses').textContent = planActualSignedEur(
    result.totals.actual_expenses, 'expense');
  $('plan-actual-fixed-remaining').textContent = planActualPlanMoney(
    result.totals.remaining_fixed_expenses, 'expense');
  $('plan-actual-variable-remaining').textContent = result.totals.remaining_variable_budget == null
    ? '—' : eur(result.totals.remaining_variable_budget);
  $('plan-actual-estimated-remaining').textContent = planActualPlanMoney(
    result.totals.remaining_estimated_expenses, 'expense');
  $('plan-actual-open-count').textContent = String(result.unclassified.count || 0);
  planActualRenderPayday(historical || !planAvailable ? null : result.payday);
  planActualRenderLiquidity(historical || !planAvailable ? null : result.liquidity);
  planActualRenderPeople(result);
  planActualRenderCockpit(result);
  planActualRenderCoverage({unmapped: result.unmapped, unclassified: result.unclassified,
    excluded: result.excluded || {}}, planAvailable);
  const body = $('plan-actual-rows'); body.replaceChildren();
  const appendUnmappedRow = (label, value, suffix, contextType) => {
    if (!Number(value || 0)) return;
    const direction = contextType === 'unmapped_income' ? 'income' : 'expense';
    const row = document.createElement('tr'); row.className = `plan-actual-unmapped-row ${direction}`;
    cell(row, label); cell(row, '—', true); cell(row, planActualSignedEur(value, direction), true);
    cell(row, '—', true);
    const stateCell = cell(row, planAvailable ? 'Zuordnung zu Planposition offen' : 'Kein gespeicherter Monatsplan');
    stateCell.className = `plan-actual-state ${planAvailable && direction === 'expense' ? 'unbudgeted' : 'near_limit'}`;
    const action = cell(row, '');
    const detailId = `plan-actual-unmapped-${suffix}`;
    const detailRow = document.createElement('tr'); detailRow.hidden = true;
    detailRow.className = 'plan-actual-detail-row';
    const detailCell = document.createElement('td'); detailCell.colSpan = 6;
    const detailTarget = document.createElement('div'); detailTarget.id = detailId;
    detailCell.append(detailTarget); detailRow.append(detailCell);
    action.append(planActualDetailsButton(
      'Buchungen ansehen', planActualContext(contextType), detailTarget, detailId));
    action.querySelector('button').addEventListener('click', () => {
      detailRow.hidden = action.querySelector('button').getAttribute('aria-expanded') !== 'true';
    });
    body.append(row, detailRow);
  };
  const appendSection = (label, kindClass) => {
    const row = document.createElement('tr'); row.className = `plan-actual-section-row ${kindClass}`;
    const heading = document.createElement('th'); heading.colSpan = 6; heading.scope = 'colgroup';
    heading.textContent = label; row.append(heading); body.append(row);
  };
  const appendOwner = label => {
    const row = document.createElement('tr'); row.className = 'plan-actual-owner-row';
    const heading = document.createElement('th'); heading.colSpan = 6; heading.scope = 'rowgroup';
    heading.textContent = label; row.append(heading); body.append(row);
  };
  const appendSubgroup = label => {
    const row = document.createElement('tr'); row.className = 'plan-actual-subgroup-row';
    const heading = document.createElement('th'); heading.colSpan = 6; heading.scope = 'rowgroup';
    heading.textContent = label; row.append(heading); body.append(row);
  };
  let rowIndex = 0;
  const renderItems = (items, owner) => {
  for (const item of items) {
    const index = rowIndex++;
    const row = document.createElement('tr');
    const classification = !planAvailable
      ? {label: 'Kein gespeicherter Monatsplan', className: 'no_actual'}
      : item.kind === 'income' && Number(item.actual)
      ? {label: planActualStatusLabels.income, className: 'income'}
      : planActualExpenseStatus(item);
    if (classification.className === 'unbudgeted') row.className = 'plan-actual-unbudgeted-item';
    const direction = item.kind === 'income' ? 'income' : 'expense';
    cell(row, `${planActualContextLabel(planActualCashItemLabel(result, item), owner)} · ${item.kind === 'income' ? 'Einnahme' : 'Ausgabe'}`);
    cell(row, planActualPlanMoney(item.planned, direction), true);
    cell(row, planActualSignedEur(item.actual, direction), true);
    const remaining = cell(row, classification.className === 'unbudgeted' ? '—'
      : planActualPlanMoney(item.remaining), true);
    if (item.remaining != null && Number(item.remaining) < 0) remaining.className += ' negative';
    const stateCell = cell(row, classification.label);
    stateCell.className = `plan-actual-state ${classification.className}`;
    const action = cell(row, '');
    const detailId = `plan-actual-item-${index}-${String(item.item_id).replace(/[^A-Za-z0-9_-]/g, '-')}`;
    const detailRow = document.createElement('tr'); detailRow.hidden = true; detailRow.className = 'plan-actual-detail-row';
    const detailCell = document.createElement('td'); detailCell.colSpan = 6;
    const detailTarget = document.createElement('div'); detailTarget.id = detailId;
    detailCell.append(detailTarget); detailRow.append(detailCell);
    action.append(planActualDetailsButton(
      `${item.transaction_count || 0} Buchungen`, planActualContext('item', item.item_id), detailTarget, detailId,
      item.kind === 'income' ? 'income' : 'expense'));
    action.querySelector('button').addEventListener('click', () => {
      detailRow.hidden = action.querySelector('button').getAttribute('aria-expanded') !== 'true';
    });
    body.append(row, detailRow);
  }
  };
  const renderKind = (label, kindClass, items, unmappedValue, unmappedSuffix, contextType,
      unmappedLabel) => {
    appendSection(label, kindClass);
    for (const owner of [...new Set([...items.map(item => item.owner_group), 'Ungeklärt'])]) {
      const ownerItems = items.filter(item => item.owner_group === owner);
      const hasUnmapped = owner === 'Ungeklärt' && Number(unmappedValue || 0);
      if (!ownerItems.length && !hasUnmapped) continue;
      appendOwner(planActualOwnerLabel(owner));
      renderItems(ownerItems.filter(item => !planAvailable || Number(item.planned) || !Number(item.actual)), owner);
      const unbudgeted = planAvailable
        ? ownerItems.filter(item => !Number(item.planned) && Number(item.actual)) : [];
      if (unbudgeted.length) {
        appendSubgroup('Sonstiges · ohne Budget');
        renderItems(unbudgeted, owner);
      }
      if (hasUnmapped) appendUnmappedRow(unmappedLabel, unmappedValue, unmappedSuffix, contextType);
    }
  };
  const allRows = result.rows || [];
  const ambiguousSalary = (result.unmapped_warnings || []).some(
    warning => warning.code === 'ambiguous_salary_owner_mapping');
  renderKind('Einnahmen', 'income', allRows.filter(item => item.kind === 'income'),
    result.unmapped.income, 'income', 'unmapped_income',
    ambiguousSalary
      ? 'Gehaltsbuchung: mehrere passende Einnahmeplanpositionen. Bitte unter Plan/Ist die Kategorie explizit zuordnen.'
      : 'Bestätigte Ist-Einnahmen · noch nicht einzeln verteilt');
  renderKind('Ausgaben', 'expense', allRows.filter(item => item.kind !== 'income'),
    result.unmapped.expenses, 'expenses', 'unmapped_expense',
    'Bestätigte Ist-Ausgaben · noch nicht einzeln verteilt');
  if (!body.children.length) {
    const row = document.createElement('tr'); const empty = cell(row, planAvailable
      ? 'Im gewählten Monat ist keine Planposition aktiv.' : 'Keine Ist-Buchungen im gewählten Monat.');
    empty.colSpan = 6; body.append(row);
  }
  planActualSetStatus(`${result.month} geladen.`);
}

function planActualMappingsByItem(plan) {
  const grouped = new Map((plan.items || []).map(item => [item.id, []]));
  for (const mapping of plan.actual_mappings || []) {
    if (grouped.has(mapping.item_id)) grouped.get(mapping.item_id).push(mapping.category_id);
  }
  return grouped;
}

function planActualRenderMappings(plan) {
  const target = $('plan-actual-mapping-items'); target.replaceChildren();
  const grouped = planActualMappingsByItem(plan);
  for (const item of plan.items || []) {
    const block = document.createElement('article'); block.className = 'plan-actual-mapping-item';
    const heading = document.createElement('h3'); heading.textContent = item.label;
    const hint = document.createElement('p'); hint.className = 'muted';
    hint.textContent = item.kind === 'income' ? 'Einnahmekategorien' : 'Ausgabenkategorien';
    const choices = document.createElement('div'); choices.className = 'plan-actual-category-choices';
    choices.setAttribute('role', 'group'); choices.setAttribute('aria-label', `Kategorien für ${item.label}`);
    const type = item.kind === 'income' ? 'income' : 'expense';
    const allowed = (planActualCatalog?.categories || []).filter(category => category.transaction_type === type);
    const parents = new Map((planActualCatalog?.parents || []).map(parent => [parent.id, parent.label]));
    for (const category of allowed) {
      const label = document.createElement('label'); label.className = 'check';
      const input = document.createElement('input'); input.type = 'checkbox';
      input.dataset.itemId = item.id; input.dataset.categoryId = category.id;
      input.checked = (grouped.get(item.id) || []).includes(category.id);
      const text = document.createElement('span');
      text.textContent = parents.has(category.parent_id)
        ? `${parents.get(category.parent_id)} › ${category.label}` : category.label;
      label.append(input, text); choices.append(label);
    }
    block.append(heading, hint, choices); target.append(block);
  }
  if (!target.children.length) {
    const empty = document.createElement('p'); empty.className = 'muted'; empty.textContent = 'Diese Revision enthält keine Budgetpositionen.'; target.append(empty);
  }
}

async function planActualLoadRevision(revision) {
  const requestedMonth = $('plan-actual-month').value;
  const loaded = await api('/api/budget-load', {revision: Number(revision)});
  if ($('plan-actual-revision').value !== String(revision)) return;
  planActualBudget = loaded;
  const periods = (planActualBudget.calculation?.rows || []).map(row => row.period);
  const month = $('plan-actual-month');
  if (periods.length) {
    const [year, monthNumber] = periods[0].split('-').map(Number);
    month.min = planActualAvailableFromMonth || `${year}-01`;
    const currentMonth = planActualCurrentMonth();
    month.max = periods.at(-1) < currentMonth ? periods.at(-1) : currentMonth;
    if (month.max < month.min) month.max = month.min;
    if (!requestedMonth || requestedMonth < month.min || requestedMonth > month.max)
      month.value = month.max;
  }
  planActualRenderMappings(planActualBudget.plan);
  planActualUpdateMonthNavigation();
}

async function planActualMetadata(force = false) {
  if (planActualInitialized && !force) return;
  const [budget, catalog, actualMetadata] = await Promise.all([
    api('/api/budget-load', {}), api('/api/classification-catalog', {}),
    api('/api/budget-actual-metadata', {}),
  ]);
  planActualAvailableFromMonth = actualMetadata.available_from_month || '';
  if (planActualAvailableFromMonth) $('plan-actual-month').min = planActualAvailableFromMonth;
  planActualCatalog = catalog;
  const select = $('plan-actual-revision'), previous = force ? select.value : ''; select.replaceChildren();
  for (const revision of budget.history || []) {
    select.append(new Option(`Revision ${revision.revision} · ${revision.title}`, String(revision.revision)));
  }
  select.value = previous && [...select.options].some(option => option.value === previous)
    ? previous : String(budget.revision || '');
  $('plan-actual-load').disabled = !select.value;
  $('plan-actual-mapping-save').disabled = planActualMappingBusy || !select.value;
  if (!select.value) {
    $('plan-actual-mapping-items').replaceChildren();
    planActualSetStatus('Speichere zuerst unter Planung eine Budgetrevision.');
    $('plan-actual-revision-label').textContent = 'Noch ohne Budgetrevision';
    planActualInitialized = true; return;
  }
  await planActualLoadRevision(Number(select.value));
  planActualInitialized = true;
}

function planActualInvalidateComparison() {
  planActualRequest += 1; planActualBusy = false;
  $('plan-actual-load').disabled = !$('plan-actual-revision').value || !$('plan-actual-month').value;
  $('plan-actual-load').setAttribute('aria-busy', 'false');
  $('plan-actual-result').hidden = true; delete $('plan-actual-result').dataset.loaded;
  $('plan-actual-no-plan').hidden = true;
  $('plan-actual-revision-label').textContent = `${$('plan-actual-month').value || 'Kein Monat'} · noch nicht geladen`;
  planActualRenderDataQuality(null);
  planActualSetStatus(`Auswahl: ${$('plan-actual-month').value || 'kein Monat'} · Revision ${$('plan-actual-revision').value || 'keine'}. Vergleich laden.`);
}
function planActualShiftMonth(offset) {
  const input = $('plan-actual-month');
  if (!/^\d{4}-\d{2}$/.test(input.value)) return;
  const [year, month] = input.value.split('-').map(Number);
  const shifted = new Date(Date.UTC(year, month - 1 + offset, 1));
  const value = `${shifted.getUTCFullYear()}-${String(shifted.getUTCMonth() + 1).padStart(2, '0')}`;
  if ((input.min && value < input.min) || (input.max && value > input.max)) return;
  input.value = value;
  input.dispatchEvent(new Event('change', {bubbles: true}));
}
function planActualUpdateMonthNavigation() {
  const input = $('plan-actual-month');
  $('plan-actual-prev-month').disabled = !input.value || Boolean(input.min && input.value <= input.min);
  $('plan-actual-next-month').disabled = !input.value || Boolean(input.max && input.value >= input.max);
}
async function planActualCompare() {
  if (!$('plan-actual-revision').value || !$('plan-actual-month').value) {
    throw new Error('Bitte Budgetrevision und Kalendermonat auswählen.');
  }
  const request = ++planActualRequest, revision = $('plan-actual-revision').value,
    month = $('plan-actual-month').value, personBreakdown = $('plan-actual-person-toggle').checked;
  const current = () => request === planActualRequest && revision === $('plan-actual-revision').value
    && month === $('plan-actual-month').value
    && personBreakdown === $('plan-actual-person-toggle').checked;
  planActualBusy = true; $('plan-actual-load').disabled = true;
  $('plan-actual-load').setAttribute('aria-busy', 'true');
  $('plan-actual-result').hidden = true;
  delete $('plan-actual-result').dataset.loaded;
  planActualSetStatus(`Vergleich für ${month} · Revision ${revision} wird geladen …`);
  try {
    const result = await api('/api/budget-actual', {
      revision: Number(revision), month, person_breakdown: personBreakdown,
      include_trend: planActualTrendRange !== 'month',
    });
    if (!current()) return false;
    planActualRender(result);
    $('plan-actual-result').hidden = false;
    $('plan-actual-result').dataset.loaded = 'true';
    planActualScheduleMonthChartResize();
    return true;
  } catch (error) {
    if (!current()) return false;
    planActualSetStatus(`Vergleich nicht aktuell: ${error.message}`, true);
    throw error;
  } finally {
    if (request === planActualRequest) {
      planActualBusy = false; $('plan-actual-load').disabled = false;
      $('plan-actual-load').setAttribute('aria-busy', 'false');
    }
  }
}

async function planActualSaveMappings() {
  if (planActualMappingBusy) return;
  if (!planActualBudget?.plan) throw new Error('Bitte zuerst eine Budgetrevision laden.');
  if (Number($('plan-actual-revision').value) !== planActualBudget.revision) throw new Error('Die ausgewählte Budgetrevision wird noch geladen. Bitte warte, bevor Du die Zuordnung speicherst.');
  const mappings = [], used = new Set();
  for (const input of $('plan-actual-mapping-items').querySelectorAll('input[data-category-id]:checked')) {
    const label = input.closest('label').textContent;
    if (used.has(input.dataset.categoryId)) throw new Error(`Die Kategorie „${label}“ ist mehr als einer Planposition zugeordnet.`);
    used.add(input.dataset.categoryId);
    mappings.push({category_id: input.dataset.categoryId, item_id: input.dataset.itemId});
  }
  const plan = JSON.parse(JSON.stringify(planActualBudget.plan)); plan.actual_mappings = mappings;
  const baseRevision = planActualBudget.revision;
  planActualMappingBusy = true; $('plan-actual-mapping-save').disabled = true;
  let saved = null;
  try {
    const latest = await api('/api/budget-load', {});
    saved = await api('/api/budget-save', {
      revision: latest.latest_revision, base_revision: baseRevision, plan,
    });
    planActualSetMappingStatus(`Zuordnung als Revision ${saved.revision} gespeichert. Die Revisionsauswahl wird aktualisiert …`);
    // Saving creates a revision; it must never replace the user's current selection.
    planActualInitialized = false; await planActualMetadata(true);
    planActualSetMappingStatus(`Zuordnung als Revision ${saved.revision} gespeichert. Deine Auswahl bleibt erhalten. Wähle die neue Revision aus, um sie zu vergleichen.`);
  } catch (error) {
    const feedback = saved
      ? `Zuordnung als Revision ${saved.revision} gespeichert. Die Revisionsauswahl konnte nicht vollständig aktualisiert werden. Bitte die Seite neu laden.`
      : `Speichern der Zuordnung aus Revision ${baseRevision} nicht bestätigt. Bitte die gespeicherten Revisionen prüfen.`;
    planActualSetMappingStatus(`${feedback} ${error.message}`, true);
    throw new Error(`${feedback} ${error.message}`, {cause:error});
  } finally {
    planActualMappingBusy = false;
    $('plan-actual-mapping-save').disabled = !$('plan-actual-revision').value;
  }
}

function planActualCurrentMonth(now = new Date()) {
  return `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, '0')}`;
}

async function planActualEnsureLoaded(force = false) {
  if (location.hash !== '#plan-actual') return;
  if (planActualEnsurePromise) {
    await planActualEnsurePromise;
    if (force === true) return planActualEnsureLoaded(true);
    return;
  }
  if (planActualBusy) return;
  if (typeof state === 'undefined' || !state?.csrf) {
    window.setTimeout(planActualEnsureLoaded, 100); return;
  }
  planActualEnsurePromise = (async () => { try {
    if (force === true) planActualInvalidateComparison();
    if (!$('plan-actual-month').value) $('plan-actual-month').value = (state.as_of || '').slice(0, 7);
    await planActualMetadata(force === true);
    if ($('plan-actual-revision').value && !$('plan-actual-result').dataset.loaded) {
      await planActualCompare();
    } else if ($('plan-actual-result').dataset.loaded) {
      planActualScheduleMonthChartResize();
    }
  } catch (error) { planActualSetStatus(error.message, true); }
  })();
  try { await planActualEnsurePromise; } finally { planActualEnsurePromise = null; }
}

let planActualMonthResizeTimer = null;
function planActualScheduleMonthChartResize() {
  window.clearTimeout(planActualMonthResizeTimer);
  if (planActualTrendRange !== 'month' || !$('plan-actual-result').dataset.loaded
      || $('plan-actual-result').hidden || $('plan-actual-cockpit').hidden
      || $('plan-actual-panel-trend').hidden) return;
  // Reuse the loaded comparison; resizing the SVG must never request or recalculate data.
  planActualMonthResizeTimer = window.setTimeout(() => {
    planActualMonthResizeTimer = null;
    const result = window.planActualLatestResult;
    if (!result || planActualTrendRange !== 'month'
        || !$('plan-actual-result').dataset.loaded || $('plan-actual-result').hidden
        || $('plan-actual-cockpit').hidden || $('plan-actual-panel-trend').hidden
        || !$('plan-actual-daily-chart').parentNode?.clientWidth) return;
    planActualRenderDaily(result.daily, result.daily_metadata,
      result.daily_by_person, result.account_balance_change);
  }, 120);
}

$('plan-actual-load').addEventListener('click', () => run(planActualCompare));
planActualSetupCockpitTabs();
$('plan-actual-revision').addEventListener('change', () => run(async () => {
  planActualInvalidateComparison();
  await planActualLoadRevision(Number($('plan-actual-revision').value));
}));
$('plan-actual-month').addEventListener('change', () => {
  planActualUpdateMonthNavigation();
  planActualInvalidateComparison();
});
$('plan-actual-prev-month').addEventListener('click', () => planActualShiftMonth(-1));
$('plan-actual-next-month').addEventListener('click', () => planActualShiftMonth(1));
$('plan-actual-person-toggle').addEventListener('change', planActualInvalidateComparison);
$('plan-actual-mapping-save').addEventListener('click', () => run(planActualSaveMappings));
window.addEventListener('hashchange', planActualEnsureLoaded);
window.addEventListener('load', planActualEnsureLoaded);
window.addEventListener('resize', planActualScheduleMonthChartResize);
window.addEventListener('orientationchange', planActualScheduleMonthChartResize);
