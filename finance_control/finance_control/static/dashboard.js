'use strict';
// The dashboard receives an already scoped, read-only snapshot from app.js.
(() => {
  const key = 'finance-control-dashboard-view';
  let snapshot = null;
  let selected = null;
  let query = '';
  let area = 'dashboard';
  const expandedAccounts = new Set();
  const mobileQuery = window.matchMedia('(max-width: 700px)');
  mobileQuery.addEventListener('change', event => {
    document.querySelectorAll('#dashboard-root .fd-account-picker, #category-outflows-root .fd-account-picker')
      .forEach(picker => { picker.open = !event.matches; });
  });
  let view = (() => { try { return localStorage.getItem(key) === 'guru' ? 'guru' : 'blick'; } catch { return 'blick'; } })();
  const el = (tag, className, value) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined && value !== null) node.textContent = String(value);
    return node;
  };
  const add = (parent, tag, className, value) => { const node = el(tag, className, value); parent.append(node); return node; };
  const iconPaths = {
    account: 'M3 7h18v12H3z M3 7V5a2 2 0 0 1 2-2h14 M16 13h5 M17 13v2',
    savings: 'M4 12a8 8 0 0 1 16 0v5H4z M7 17v3 M17 17v3 M9 11h.01 M15 11h.01 M12 5V3',
    cash: 'M3 6h18v12H3z M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z M5 9h1 M18 15h1',
    card: 'M3 5h18v14H3z M3 9h18 M6 15h5',
    depot: 'M4 19V5 M4 19h17 M7 15l4-4 3 2 5-6',
    book: 'M12 5c-3-2-6-2-9-1v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-3-1-6-1-9 1z M12 5v15',
    energy: 'M13 2 5 13h6l-1 9 9-12h-6z',
    transfer: 'M4 7h15l-3-3 M19 7l-3 3 M20 17H5l3-3 M5 17l3 3',
    income: 'M12 3v14 M8 13l4 4 4-4 M4 20h16',
    category: 'M3 4h9l9 9-8 8-10-10z M8 8h.01',
  };
  const accountIcon = kind => ({SAVINGS:'savings',CASH:'cash',CREDIT_CARD:'card',DEPOT:'depot',INVESTMENT:'depot'})[String(kind || '').toUpperCase()] || 'account';
  const categoryIcon = name => {
    const value = String(name || '').toLocaleLowerCase('de-DE');
    if (/umbuch|transfer/.test(value)) return 'transfer';
    if (/bücher|buch|lese/.test(value)) return 'book';
    if (/energie|strom|gas/.test(value)) return 'energy';
    if (/gehalt|einkommen/.test(value)) return 'income';
    return 'category';
  };
  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.classList.add('fd-icon');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.8');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', iconPaths[name] || iconPaths.category);
    svg.append(path);
    return svg;
  }
  function iconLabel(parent, className, name, label) {
    const wrap = add(parent, 'span', className);
    wrap.append(icon(name), document.createTextNode(label));
    return wrap;
  }
  const money = (value, currency = 'EUR', signed = false) => {
    if (value === null || value === undefined || value === '') return 'Unbekannt';
    const raw = String(value).trim();
    if (!/^[+-]?\d+(?:\.\d{1,2})?$/.test(raw)) return 'Unbekannt';
    const negative = raw.startsWith('-');
    const digits = raw.replace(/^[+-]/, '').split('.');
    const cents = BigInt(digits[0]) * 100n + BigInt((digits[1] || '').padEnd(2, '0'));
    const integer = cents / 100n;
    const fraction = String(cents % 100n).padStart(2, '0');
    const grouped = String(integer).replace(/\B(?=(\d{3})+(?!\d))/g, '.');
    return `${negative ? '−' : signed && cents !== 0n ? '+' : ''}${grouped},${fraction} ${currency || 'EUR'}`;
  };
  const cents = value => {
    const raw = String(value ?? '').trim();
    if (!/^[+-]?\d+(?:\.\d{1,2})?$/.test(raw)) return null;
    const parts = raw.replace(/^[+-]/, '').split('.');
    const magnitude = BigInt(parts[0]) * 100n + BigInt((parts[1] || '').padEnd(2, '0'));
    return raw.startsWith('-') ? -magnitude : magnitude;
  };
  // Used only for non-negative category outflow totals.
  const moneyFromCents = (value, currency) => money(`${value / 100n}.${String(value % 100n).padStart(2, '0')}`, currency);
  const tone = value => value === null || value === undefined || value === '' ? 'unknown'
    : String(value).trim().startsWith('-') ? 'negative' : 'positive';
  const date = value => {
    const match = String(value || '').match(/^(\d{4})-(\d{2})-(\d{2})/);
    return match ? `${match[3]}.${match[2]}.${match[1]}` : String(value || '');
  };
  const accountName = account => account.display_name || account.id || 'Konto';
  const kindName = kind => ({CHECKING:'Girokonto',CURRENT:'Girokonto',SAVINGS:'Sparkonto',CASH:'Bargeld',CREDIT_CARD:'Kreditkarte',DEPOT:'Depot',INVESTMENT:'Depot'}[String(kind || '').toUpperCase()] || kind || 'Konto');
  const reasonName = reason => ({MISSING_BALANCE:'Kein Kontostand vorhanden',INCOMPLETE_HISTORY:'Datenhistorie unvollständig',NO_HISTORY:'Kein Verlauf vorhanden',NO_MARKET_VALUE:'Kein Marktwert vorhanden',UNKNOWN:'Grund unbekannt',INCOMPLETE_SOURCE_COVERAGE:'Buchungshistorie unvollständig; Kontostand nicht belegbar',OPENING_BALANCE_UNAVAILABLE:'Anfangsbestand fehlt für diesen Monat',FUTURE_MONTH:'Monat liegt nach dem Stichtag'}[String(reason || '').toUpperCase()] || reason);
  const accountKey = account => String(account.id);
  const accounts = () => Array.isArray(snapshot?.accounts) ? snapshot.accounts : [];
  const bookings = () => Array.isArray(snapshot?.bookings) ? snapshot.bookings : [];
  const current = () => accounts().find(account => accountKey(account) === selected) || null;
  const activeRoot = () => document.getElementById(area === 'category-outflows' ? 'category-outflows-root' : 'dashboard-root');
  const bookingCounts = () => {
    const counts = new Map();
    for (const item of bookings()) {
      const id = String(item.account_id);
      counts.set(id, (counts.get(id) || 0) + 1);
    }
    return counts;
  };
  const relevantAccounts = () => {
    const counts = bookingCounts();
    return [...accounts()].sort((a, b) =>
      (counts.get(accountKey(b)) || 0) - (counts.get(accountKey(a)) || 0)
      || accountName(a).localeCompare(accountName(b), 'de')
      || accountKey(a).localeCompare(accountKey(b), 'de'));
  };
  const ownerGroups = () => {
    const groups = new Map();
    for (const account of accounts()) {
      const label = account.owner_label || account.owner || 'Ohne Zuordnung';
      if (!groups.has(label)) groups.set(label, []);
      groups.get(label).push(account);
    }
    return new Map([...groups].sort((a, b) =>
      Number(a[1][0].owner === "JOINT") - Number(b[1][0].owner === "JOINT")
      || a[0].localeCompare(b[0], "de")));
  };
  function pick(id) {
    if (selected === id) return;
    selected = id;
    const root = activeRoot();
    root?.querySelectorAll('.fd-account').forEach(card => {
      const active = card.dataset.accountId === id;
      card.classList.toggle('is-selected', active);
      card.querySelector('summary')?.setAttribute('aria-current', active ? 'true' : 'false');
    });
    const main = root?.querySelector('.fd-main');
    if (main) populateMain(main);
    if (mobileQuery.matches) main?.scrollIntoView({block:'start', behavior:'smooth'});
  }
  function accountButton(account, className, count) {
    const id = accountKey(account);
    const card = el('details', `${className}${selected === id ? ' is-selected' : ''}`);
    card.dataset.accountId = id;
    card.open = expandedAccounts.has(id);
    const isSavings = String(account.kind || '').toUpperCase() === 'SAVINGS';
    const summary = add(card, 'summary', `fd-account-summary${isSavings ? ' fd-account-summary-savings' : ''}`);
    summary.setAttribute('aria-current', selected === id ? 'true' : 'false');
    iconLabel(summary, 'fd-account-name', accountIcon(account.kind), accountName(account));
    add(summary, 'small', 'fd-account-kind', `${account.owner_label || account.owner || 'Ohne Zuordnung'} · ${kindName(account.kind)} · ${count} erfasste Buchung${count === 1 ? '' : 'en'}`);
    const headerAmount = isSavings ? account.change : account.end;
    add(summary, 'strong', `fd-account-amount ${tone(headerAmount)}`,
      `${isSavings ? 'Monatsbewegung · ' : ''}${money(headerAmount, account.currency, isSavings)}`);
    const body = add(card, 'div', 'fd-account-body');
    add(body, 'p', 'fd-account-freshness', `Letzte erfasste Buchung: ${account.last_booking_date ? date(account.last_booking_date) : 'unbekannt'} · Abrufzeit nicht erfasst`);
    body.append(metrics(account));
    if (account.note || account.reason) add(body, 'p', 'fd-account-note', [account.note, reasonName(account.reason)].filter(Boolean).join(' · '));
    summary.addEventListener('click', () => pick(id));
    card.addEventListener('toggle', () => { if (card.open) expandedAccounts.add(id); else expandedAccounts.delete(id); });
    return card;
  }
  function accountList(className) {
    const wrap = el('div', className);
    if (!accounts().length) add(wrap, 'p', 'fd-empty', 'Keine Konten für diesen Stichtag.');
    const counts = bookingCounts();
    if (view === 'blick') {
      for (const account of relevantAccounts()) wrap.append(accountButton(account, 'fd-account', counts.get(accountKey(account)) || 0));
      return wrap;
    }
    for (const [owner, items] of ownerGroups()) {
      const section = add(wrap, 'section', 'fd-owner');
      add(section, 'h3', 'fd-owner-title', owner);
      for (const account of items) section.append(accountButton(account, 'fd-account', counts.get(accountKey(account)) || 0));
    }
    return wrap;
  }
  function monthLabel() {
    const month = String(snapshot?.month || snapshot?.as_of || '').slice(0, 7);
    if (!/^\d{4}-\d{2}$/.test(month)) return 'Ausgewählter Monat';
    const [year, number] = month.split('-').map(Number);
    return new Intl.DateTimeFormat('de-DE', {month: 'long', year: 'numeric'}).format(new Date(year, number - 1, 1));
  }
  function metrics(account) {
    const wrap = el('div', 'fd-metrics');
    for (const [label, value, signed] of [
      ['Zugänge', account.inflow, false], ['Abgänge', account.outflow, false], ['Monatsbewegung', account.change, true]]) {
      const card = add(wrap, 'div', 'fd-metric');
      add(card, 'span', '', label);
      add(card, 'strong', signed ? tone(value) : '', money(value, account.currency, signed));
    }
    return wrap;
  }
  function categories() {
    const section = el('section', 'fd-categories');
    add(section, 'p', 'fd-kicker', monthLabel());
    add(section, 'h2', '', 'Abgänge nach Kategorie');
    add(section, 'p', 'fd-disclosure', `Kontosicht einschließlich Umbuchungen · bis ${date(snapshot.as_of)}.`);
    const account = current();
    if (!account) { add(section, 'p', 'fd-empty', 'Wähle ein Konto aus.'); return section; }
    add(section, 'p', 'fd-category-account', `${account.owner_label || account.owner || 'Konto'} · ${accountName(account)}`);
    const groups = new Map();
    for (const item of bookings().filter(entry => String(entry.account_id) === selected)) {
      const value = cents(item.amount);
      if (value === null || value >= 0n) continue;
      const currency = item.currency || account.currency || 'EUR';
      const category = item.category || 'Ohne Kategorie';
      const key = `${currency}\u0000${category}`;
      const prior = groups.get(key) || {category,currency,value:0n};
      prior.value += -value; groups.set(key,prior);
    }
    if (!groups.size) { add(section, 'p', 'fd-empty', 'Keine Abgänge im Monat.'); return section; }
    const ordered = [...groups.values()].sort((a,b) => a.currency.localeCompare(b.currency) || (a.value > b.value ? -1 : a.value < b.value ? 1 : a.category.localeCompare(b.category)));
    const list = add(section, 'div', 'fd-category-list');
    for (const item of ordered) {
      const row = add(list, 'div', 'fd-category');
      iconLabel(row, 'fd-category-name', categoryIcon(item.category), item.category);
      add(row, 'strong', '', moneyFromCents(item.value,item.currency));
    }
    return section;
  }
  function bookingList() {
    const section = el('section', 'fd-bookings');
    const top = add(section, 'div', 'fd-bookings-head');
    const title = add(top, 'div');
    add(title, 'p', 'fd-kicker', monthLabel());
    add(title, 'h2', '', 'Buchungen');
    const label = add(top, 'label', 'fd-search-label');
    add(label, 'span', '', 'Buchungen suchen');
    const search = add(label, 'input', 'fd-search');
    search.type = 'search'; search.placeholder = 'Text, Kategorie, Betrag'; search.value = query;
    search.addEventListener('input', () => { query = search.value; updateBookings(section); });
    const rows = add(section, 'div', 'fd-booking-rows');
    rows.setAttribute('aria-live', 'polite');
    populateBookings(rows);
    return section;
  }
  function updateBookings(section) { populateBookings(section.querySelector('.fd-booking-rows')); }
  const bookingDateOrder = (a, b) => String(b.date || '').localeCompare(String(a.date || ''))
    || String(a.external_id || '').localeCompare(String(b.external_id || ''));
  function bookingRow(parent, item, account, index) {
    const row = add(parent, 'tr', 'fd-booking');
    add(row, 'td', 'fd-booking-date', date(item.date));
    add(row, 'td', 'fd-booking-counterparty', item.counterparty || item.description || 'Ohne Gegenpartei');
    add(row, 'td', 'fd-booking-description', item.description || '—');
    const category = add(row, 'td', 'fd-booking-category');
    iconLabel(category, 'fd-category-name', categoryIcon(item.category), item.category || 'Ohne Kategorie');
    add(row, 'td', `fd-booking-amount ${tone(item.amount)}`, money(item.amount, item.currency || account.currency, true));
    const action = add(row, 'td', 'fd-booking-action');
    const button = add(action, 'button', 'fd-booking-toggle', 'Details');
    button.type = 'button';
    button.setAttribute('aria-expanded', 'false');
    const detailRow = add(parent, 'tr', 'fd-booking-expanded');
    detailRow.hidden = true;
    const detailCell = add(detailRow, 'td'); detailCell.colSpan = 6;
    const body = add(detailCell, 'div', 'fd-booking-detail');
    const detailId = `fd-booking-detail-${index}`;
    body.id = detailId;
    button.setAttribute('aria-controls', detailId);
    button.addEventListener('click', () => {
      detailRow.hidden = !detailRow.hidden;
      button.setAttribute('aria-expanded', String(!detailRow.hidden));
    });
    for (const [label, value] of [['Gegenpartei', item.counterparty], ['Verwendungszweck', item.description], ['Kategorie', item.category], ['Datum', date(item.date)]]) {
      const row = add(body, 'div'); add(row, 'span', '', label); add(row, 'p', '', value || '—');
    }
    if (item.external_id != null) {
      const edit = add(body, 'button', 'fd-booking-edit', 'Kategorie und Belege bearbeiten');
      edit.type = 'button';
      edit.addEventListener('click', () => document.dispatchEvent(new CustomEvent('finance-dashboard-edit',
        {detail:{account_id:item.account_id,external_id:item.external_id}})));
    }
  }
  function populateBookings(rows) {
    rows.replaceChildren();
    const account = current();
    if (!account) { add(rows, 'p', 'fd-empty', 'Kein Konto ausgewählt.'); return; }
    const term = query.trim().toLocaleLowerCase('de-DE');
    const list = bookings().filter(item => String(item.account_id) === selected)
      .filter(item => !term || [item.counterparty, item.description, item.category, item.amount, item.date,
        money(item.amount, item.currency || account.currency), date(item.date)]
        .some(value => String(value || '').toLocaleLowerCase('de-DE').includes(term)))
      .sort(bookingDateOrder);
    add(rows, 'p', 'fd-result-count', `${list.length} Buchung${list.length === 1 ? '' : 'en'}`);
    if (!list.length) { add(rows, 'p', 'fd-empty', term ? 'Keine Buchung zum Suchtext.' : 'Keine Buchungen in diesem Monat.'); return; }
    const scroller = add(rows, 'div', 'fd-table-scroll');
    scroller.tabIndex = 0;
    scroller.setAttribute('role', 'region');
    scroller.setAttribute('aria-label', 'Buchungstabelle');
    const table = add(scroller, 'table', 'fd-booking-table');
    add(table, 'caption', 'fd-table-caption', `${account.owner_label || account.owner || 'Konto'} · ${accountName(account)}`);
    const head = add(table, 'thead');
    const header = add(head, 'tr');
    for (const name of ['Datum', 'Gegenpartei', 'Verwendungszweck', 'Kategorie', 'Betrag', 'Details']) {
      const cell = add(header, 'th', '', name); cell.scope = 'col';
    }
    const body = add(table, 'tbody');
    let priorDay = null;
    list.forEach((item, index) => {
      if (item.date !== priorDay) {
        const day = add(body, 'tr', 'fd-booking-day');
        const heading = add(day, 'th', '', date(item.date));
        heading.colSpan = 6; heading.scope = 'row';
        priorDay = item.date;
      }
      bookingRow(body, item, account, index);
    });
  }
  function populateMain(main) {
    main.replaceChildren();
    main.append(area === 'category-outflows' ? categories() : bookingList());
  }
  function paint() {
    const root = activeRoot();
    if (!root || !snapshot) return;
    if (!accounts().some(account => accountKey(account) === selected)) {
      const first = relevantAccounts()[0];
      selected = first ? accountKey(first) : null;
    }
    root.replaceChildren(); root.className = `fd-root fd-${view}`;
    const hero = add(root, 'header', 'fd-hero');
    const intro = add(hero, 'div');
    add(intro, 'p', 'fd-kicker', 'FINANCE CONTROL · KONTEN');
    add(intro, 'h1', '', area === 'category-outflows' ? 'Abgänge nach Kategorie' : 'Buchungen');
    add(intro, 'p', 'fd-hero-sub', `${monthLabel()} · Stichtag ${date(snapshot.as_of)}`);
    const controls = add(hero, 'div', 'fd-controls');
    const cutoffForm = add(controls, 'form', 'fd-cutoff-form');
    const cutoffLabel = add(cutoffForm, 'label'); add(cutoffLabel, 'span', '', 'Stand am');
    const cutoff = add(cutoffLabel, 'input'); cutoff.type = 'date'; cutoff.required = true;
    cutoff.value = String(snapshot.as_of || '').slice(0, 10);
    const cutoffSubmit = add(cutoffForm, 'button', '', 'Anzeigen'); cutoffSubmit.type = 'submit';
    cutoffForm.addEventListener('submit', event => {
      event.preventDefault();
      document.dispatchEvent(new CustomEvent('finance-dashboard-cutoff', {detail: {as_of: cutoff.value}}));
    });
    const switcher = add(controls, 'div', 'fd-switch');
    switcher.setAttribute('role', 'group'); switcher.setAttribute('aria-label', 'Dashboard-Stil');
    for (const [id, label] of [['blick', 'finanzblick-Stil'], ['guru', 'Finanzguru-Stil']]) {
      const button = add(switcher, 'button', '', label); button.type = 'button';
      button.setAttribute('aria-pressed', String(view === id)); button.dataset.view = id;
      button.addEventListener('click', () => { view = id; try { localStorage.setItem(key, id); } catch {} paint(); activeRoot()?.querySelector('[data-view="' + id + '"]')?.focus({preventScroll:true}); });
    }
    if (snapshot.error) {
      const error = add(root, 'p', 'fd-note', snapshot.error);
      error.setAttribute('role', 'alert');
      return;
    }
    const layout = add(root, 'div', 'fd-layout');
    const side = add(layout, 'aside', 'fd-sidebar');
    const picker = add(side, 'details', 'fd-account-picker');
    picker.open = !mobileQuery.matches;
    const sideHead = add(picker, 'summary', 'fd-side-head');
    add(sideHead, 'h2', '', 'Konten wählen');
    add(sideHead, 'span', '', String(accounts().length));
    picker.append(accountList('fd-account-list'));
    const main = add(layout, 'div', 'fd-main');
    populateMain(main);
    const footer = add(root, 'p', 'fd-footer');
    footer.append('Für Haushaltsplan und verfügbare Mittel: ');
    const link = add(footer, 'a', '', 'Plan & Ist'); link.href = '#plan-actual';
  }
  window.financeDashboardRender = (payload, requestedArea = 'dashboard') => {
    if (!payload || typeof payload !== 'object') return;
    snapshot = payload;
    area = requestedArea === 'category-outflows' ? requestedArea : 'dashboard';
    paint();
  };
  // app.js reloads the active area after refresh(). Hidden dashboards are
  // loaded on navigation, avoiding duplicate and invisible DOM rebuilds.
})();
