'use strict';
// Presentation only. Amounts and balance coverage are supplied by the backend.
(() => {
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const hasAmount = value => value !== null && value !== undefined && value !== '' && /^[+-]?\d+(?:\.\d{1,2})?$/.test(String(value));
  const moneyText = (value, currency = 'EUR') => hasAmount(value) ? new Intl.NumberFormat('de-DE',
    {style: 'currency', currency: currency || 'EUR'}).format(Number(value)) : 'Betrag fehlt';
  const dateText = value => value ? new Date(value + 'T12:00:00').toLocaleDateString('de-DE') : 'unbekannt';
  const navigate = id => cockpitNavigate(id);
  const link = (text, target) => { const a = el('a', 'fc-text-link', text); a.href = '#' + target; return a; };
  const section = (title, target, action) => {
    const box = el('article', 'fc-home-panel'), head = el('div', 'section-head');
    head.append(el('h3', '', title));
    if (target) head.append(link(action || 'Alle ansehen', target));
    box.append(head); return box;
  };
  function retryNotice(root, error, retry) {
    root.querySelector('.fc-retry-notice')?.remove();
    const box = el('div', 'fc-retry-notice'); box.setAttribute('role', 'alert');
    box.append(el('p', '', `Nicht aktualisiert: ${error.message}`));
    const button = el('button', 'secondary', 'Erneut versuchen'); button.type = 'button';
    button.addEventListener('click', retry); box.append(button); root.prepend(box);
  }
  function accountButton(account) {
    const button = el('button', 'fc-home-account'); button.type = 'button';
    const icon = el('span', 'fc-home-icon');
    icon.append(window.financeAccountIcon(account));
    icon.setAttribute('aria-hidden', 'true');
    const name = el('span', 'fc-home-account-name');
    name.append(el('strong', '', account.display_name), el('small', 'muted',
      [account.owner_label, account.institution].filter(Boolean).join(' · ')));
    const balance = hasAmount(account.current_balance?.amount) ? account.current_balance : null;
    const value = el('span', 'fc-home-account-value');
    const amount = el('strong', balance && Number(balance.amount) < 0 ? 'fc-negative' : '',
      balance ? moneyText(balance.amount, balance.currency) : '—');
    if (!balance) amount.setAttribute('aria-label', `Kontostand fehlt für ${account.display_name}`);
    value.append(amount);
    if (balance) value.append(el('small', 'muted', dateText(balance.booked_on)));
    button.append(icon, name, value);
    button.addEventListener('click', () => window.financeDashboardSelectAccount(account.id));
    return button;
  }
  function renderOverview(data) {
    const root = document.getElementById('home-root'), fragment = document.createDocumentFragment();
    root.className = `fc-home fc-home-${window.financeViewMode()}`;
    const heading = el('div', 'fc-page-heading'); heading.append(el('h2', '', 'Übersicht'), window.financeViewSwitcher());
    const dateForm = el('form', 'fc-home-period'), label = el('label', '', 'Buchungen bis');
    const input = el('input'); input.type = 'date'; input.value = state.as_of; input.required = true;
    input.setAttribute('aria-label', 'Buchungen bis'); label.append(input);
    const submit = el('button', 'secondary', 'Anzeigen'); dateForm.append(label, submit);
    dateForm.addEventListener('submit', event => { event.preventDefault(); document.getElementById('cutoff').value = input.value; void run(refresh); });
    fragment.append(heading);
    const accounts = section('Kontostände', 'dashboard', 'Buchungen'); accounts.classList.add('fc-home-accounts');
    const list = el('div', 'fc-home-account-list');
    const ordered = [...data.accounts].sort((a, b) => {
      const usage = id => data.bookings.filter(item => item.account_id === id).length;
      return usage(b.id) - usage(a.id) || a.display_name.localeCompare(b.display_name, 'de');
    });
    for (const account of ordered) {
      const row = el('div', 'fc-home-account-row');
      row.append(accountButton(account));
      if (!hasAmount(account.current_balance?.amount) || account.reconciliation) {
        const details = el('details', 'fc-home-account-evidence');
        details.append(el('summary', '', 'Details'));
        if (!hasAmount(account.current_balance?.amount)) {
          const action = el('button', 'fc-home-account-action', 'Bankabruf öffnen');
          action.type = 'button';
          action.addEventListener('click', () => {
            if (typeof window.financeBankRefresh?.openDetails === 'function')
              window.financeBankRefresh.openDetails();
            else {
              let status = details.querySelector('[role="alert"]');
              if (!status) {
                status = el('p', 'fc-home-account-action-status', 'Bankabruf ist derzeit nicht verfügbar.');
                status.setAttribute('role', 'alert');
                status.setAttribute('aria-live', 'polite');
                details.append(status);
              }
            }
          });
          details.append(action);
        }
        if (account.reconciliation && window.financeReconciliation)
          window.financeReconciliation(details, account.reconciliation);
        row.append(details);
      }
      list.append(row);
    }
    if (!ordered.length) list.append(el('p', 'muted', 'Noch keine Konten vorhanden.'), link('Konten einrichten', 'accounts'));
    accounts.append(list); fragment.append(accounts);
    const paypal = el('article', 'fc-home-panel'); paypal.id = 'home-paypal';
    paypal.append(el('h3', '', 'Eigene PayPal-Stände'), el('p', 'muted', 'Guthaben und offene Raten werden geladen …'));
    fragment.append(paypal);
    fragment.append(dateForm);
    const hero = el('details', 'fc-balance-hero'); hero.append(el('summary', '', 'Gesamtstände & Datengrundlage'));
    for (const item of data.balance_summary || []) {
      const line = el('div', 'fc-balance-line');
      line.append(el('strong', '', item.known_count ? moneyText(item.amount, item.currency) : 'Nicht verfügbar'),
        el('span', '', `${item.known_count} von ${item.account_count} Konten · ${item.currency}`));
      hero.append(line);
      if (item.known_count) hero.append(el('small', '', `Belegte Stände: ${dateText(item.oldest_booked_on)} bis ${dateText(item.newest_booked_on)}`));
    }
    if (!(data.balance_summary || []).length) hero.append(el('strong', '', 'Noch keine Konten'));
    hero.append(el('p', '', 'Fehlende Stände sind nicht Null. Keine Depotbewertung oder Budgetprognose.'));
    fragment.append(hero);
    const columns = el('div', 'fc-home-grid'), movements = section('Kontobewegungen', 'dashboard', 'Buchungen');
    movements.append(el('p', 'muted', `${dateText(data.month + '-01')} – ${dateText(data.as_of)}`));
    const flow = el('div', 'fc-flow-grid');
    for (const item of data.period_totals || []) for (const [key, title] of [['inflow', 'Zugänge'], ['outflow', 'Abgänge']]) {
      const metric = el('div'); metric.append(el('span', 'muted', title), el('strong', key === 'inflow' ? 'fc-positive' : '', moneyText(item[key], item.currency))); flow.append(metric);
    }
    const periodIncomplete = Boolean(data.period_incomplete_account_ids?.length);
    const movementDetails = el('details', 'fc-caption');
    movementDetails.append(el('summary', '', periodIncomplete ? 'Datengrundlage (unvollständig)' : 'Datengrundlage'),
      el('p', '', 'Aus Kontosicht, einschließlich Umbuchungen. Für Haushaltsausgaben verwende die Analyse.'));
    movements.append(flow, movementDetails);
    if (periodIncomplete) movementDetails.append(el('p', 'notice', `Unvollständiger Zeitraum: Für ${data.period_incomplete_account_ids.length} Konten bestehen bekannte Datenlücken. Die Summen enthalten nur vorhandene Buchungen.`));
    columns.append(movements);
    const budget = section('Budgets', 'plan-actual', 'Öffnen'); budget.id = 'home-budget';
    budget.append(el('p', 'muted fc-budget-status', 'Gespeicherter Plan wird geprüft …')); columns.append(budget); fragment.append(columns);
    const recent = section('Letzte Buchungen', 'dashboard'), bookings = el('div', 'fc-home-bookings');
    for (const item of data.bookings.slice(0, 5)) {
      const button = el('button', 'fc-home-booking'); button.type = 'button';
      const text = el('span'); text.append(el('strong', '', item.counterparty || item.description || 'Buchung'),
        el('small', 'muted', `${dateText(item.date)} · ${item.category || 'Kategorie offen'}`));
      button.append(text, el('strong', Number(item.amount) < 0 ? '' : 'fc-positive', moneyText(item.amount, item.currency)));
      button.addEventListener('click', () => {
        window.financeDashboardSelectAccount(item.account_id);
        document.dispatchEvent(new CustomEvent('finance-dashboard-edit', {detail: {account_id: item.account_id, external_id: item.external_id}}));
      }); bookings.append(button);
    }
    if (!data.bookings.length) bookings.append(el('p', 'muted', 'Keine Buchungen im gewählten Monat. Wähle einen anderen Stichtag.'));
    recent.append(bookings); fragment.append(recent);
    const expected = section('Was steht als Nächstes an?', 'recurring', 'Verträge & Zahlungen'); expected.id = 'home-expected'; expected.classList.add('rc-home-teaser'); expected.append(el('p', 'muted', 'Erwartete Zahlungen und Kontoprognose ab heute werden geladen …')); fragment.append(expected);
    root.replaceChildren(fragment); root.dataset.loaded = 'true';
    return budget;
  }
  async function loadBudget(root, data) {
    try {
      const saved = await api('/api/budget-load', {});
      if (document.getElementById('home-budget') !== root) return;
      if (!saved.revision) { root.querySelector('.fc-retry-notice')?.remove(); root.querySelector('.fc-budget-status').textContent = 'Noch kein Planstand gespeichert.'; root.append(link('Budget erstellen', 'planning')); return; }
      const result = await api('/api/budget-actual', {revision: saved.revision, month: data.month});
      if (document.getElementById('home-budget') !== root) return;
      root.querySelector('.fc-retry-notice')?.remove();
      root.querySelector('.fc-caption')?.remove();
      root.querySelector('.fc-budget-status').textContent = result.basis?.type === 'retrospective_reference' ? 'Historischer Vergleich · kein gespeichertes Monatsbudget.' : !result.plan_available ? 'Für diesen Monat ist kein Budget verfügbar.' : `Restliches Ausgabenbudget im Monat: ${moneyText(result.totals.remaining_expense_budget)}`;
      if (result.plan_available && result.basis?.type !== 'retrospective_reference') root.append(el('p', 'fc-caption', 'Budgetrest aus Plan & Ist, kein Kontostand. Vollständiger Kalendermonat.'));
    } catch (error) { if (document.getElementById('home-budget') === root) retryNotice(root, error, () => void loadBudget(root, data)); }
  }
  async function loadExpected(root) {
    const now = new Date(), end = new Date(now); end.setDate(end.getDate() + 30);
    const localDate = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
    const asOf = localDate(now), until = localDate(end);
    try {
      const data = await api('/api/recurring-view', {as_of: asOf, until, account_id: ''});
      if (document.getElementById('home-expected') !== root) return;
      root.querySelectorAll('p,.rc-event,.rc-metrics').forEach(n => n.remove()); root.querySelector('.fc-retry-notice')?.remove();
      root.append(el('p', 'fc-caption', `Ab heute (${dateText(asOf)}) bis ${dateText(until)} · unabhängig vom Buchungsstichtag oben.`));
      for (const event of (data.events || []).filter(item => item.status === 'expected').slice(0, 3)) {
        const row = el('div', 'rc-event'); row.append(el('span', '', `${dateText(event.expected_date)} · ${event.label}${event.estimated ? ' · geschätzt' : ''}`), el('strong', '', moneyText(event.amount, event.currency))); root.append(row);
      }
      if (!(data.events || []).some(item => item.status === 'expected')) root.append(el('p', 'muted', 'Keine erwarteten Zahlungen in den nächsten 30 Tagen.'));
      const forecasts = el('div', 'rc-metrics');
      for (const account of (data.accounts || []).slice(0, 2)) {
        const item = el('div'); item.append(el('span', 'muted', account.label + ' · Prognose'), el('strong', '', account.projected_balance == null ? 'Stand unbekannt' : moneyText(account.projected_balance, account.currency))); forecasts.append(item);
      }
      root.append(forecasts, el('p', 'fc-caption', 'Teilprognose: nur erfasste regelmäßige Zahlungen. Ungeplante variable Ausgaben und fehlende Verträge sind nicht enthalten.'));
      root.querySelector('.fc-payment-review')?.remove();
      if (typeof window.financePaymentReviewMount === 'function') {
        const review = el('section'); root.append(review);
        window.financePaymentReviewMount(review, {as_of: asOf, until, account_id: ''});
      }
    } catch (error) { if (document.getElementById('home-expected') === root) retryNotice(root, error, () => void loadExpected(root)); }
  }
  document.addEventListener('finance-view-changed', () => {
    const root = document.getElementById('home-root');
    if (root) {
      root.className = `fc-home fc-home-${window.financeViewMode()}`;
      root.querySelectorAll('[data-view]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.view === window.financeViewMode())));
    }
  });
  window.financeHomeLoad = async () => {
    const root = document.getElementById('home-root'); root.setAttribute('aria-busy', 'true');
    try {
      if (state.dashboard?.error) throw new Error(state.dashboard.error);
      const budget = renderOverview(state.dashboard);
      await Promise.all([loadBudget(budget, state.dashboard), loadExpected(document.getElementById('home-expected')),
        typeof window.financePayPalOverviewLoad === 'function'
          ? window.financePayPalOverviewLoad(document.getElementById('home-paypal')) : Promise.resolve()]);
    } catch (error) { retryNotice(root, error, () => void run(refresh)); }
    finally { root.setAttribute('aria-busy', 'false'); }
  };
})();
