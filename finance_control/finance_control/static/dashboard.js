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
    account: 'wallet', savings: 'piggy-bank', cash: 'banknote', card: 'credit-card',
    depot: 'chart-no-axes-combined', book: 'book-open', energy: 'zap',
    transfer: 'arrow-left-right', income: 'arrow-down-to-line', category: 'tag',
  };
  const accountIcon = kind => ({SAVINGS:'savings',CASH:'cash',CREDIT_CARD:'card',DEPOT:'depot',INVESTMENT:'depot'})[String(kind || '').toUpperCase()] || 'account';
  // Match bank words only in public-facing metadata. Account IDs are private identifiers.
  const bankBrand = account => {
    if (String(account.kind || '').toUpperCase() === 'CASH') return null;
    for (const value of [account.institution, account.display_name]) {
      if (typeof value !== 'string') continue;
      const label = value.toLocaleLowerCase('de-DE');
      if (/(^|[^\p{L}])postbank(?=$|[^\p{L}])/u.test(label)) return 'postbank';
      if (/(^|[^\p{L}])(?:naspa|sparkasse)(?=$|[^\p{L}])/u.test(label)) return 'sparkasse';
      if (/(^|[^\p{L}])ing(?=$|[^\p{L}])/u.test(label)) return 'ing';
      if (/(^|[^\p{L}])paypal(?=$|[^\p{L}])/u.test(label)) return 'paypal';
    }
    return null;
  };
  const categoryIcon = name => {
    const value = String(name || '').toLocaleLowerCase('de-DE');
    const words = value.split(/[^\p{L}\p{N}]+/u).filter(Boolean);
    const has = (...prefixes) => words.some(word => prefixes.some(prefix => word.startsWith(prefix)));
    const contains = part => words.some(word => word.includes(part));
    if (contains('umbuch') || has('transfer')) return 'transfer';
    if (has('bücher', 'buch', 'lese')) return 'book';
    if (has('lebensmittel', 'supermarkt', 'nahrung')) return 'shopping-basket';
    if (has('einkauf', 'shopping', 'kleidung')) return 'shopping-bag';
    if (has('wohnen', 'miete', 'wohnung', 'haus')) return 'house';
    if (has('gastronomie', 'restaurant', 'essen', 'café', 'cafe')) return 'utensils';
    if (contains('energie') || has('strom', 'gas', 'heizung')) return 'energy';
    if (has('mobilität', 'mobilitaet', 'auto', 'verkehr', 'transport')) return 'car-front';
    if (has('gesundheit', 'arzt', 'apotheke')) return 'heart-pulse';
    if (has('kinder', 'kind', 'baby')) return 'baby';
    if (has('versicherung')) return 'shield-check';
    if (has('telefon', 'internet', 'mobilfunk')) return 'wifi';
    if (has('abo', 'abonnement')) return 'repeat-2';
    if (has('urlaub', 'reise')) return 'plane';
    if (has('freizeit', 'kultur', 'kino')) return 'ticket';
    if (contains('steuer') || has('abgabe')) return 'receipt-text';
    if (contains('einkommen') || has('gehalt', 'lohn')) return 'income';
    if (has('bargeld', 'barabhebung')) return 'cash';
    return 'category';
  };
  // Fixed Lucide SVG nodes: https://github.com/lucide-icons/lucide/tree/main/icons
  const lucideNodes = {
    "wallet": [["path",{"d":"M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1"}],["path",{"d":"M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"}]],
    "piggy-bank": [["path",{"d":"M11 17h3v2a1 1 0 0 0 1 1h2a1 1 0 0 0 1-1v-3a3.16 3.16 0 0 0 2-2h1a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1h-1a5 5 0 0 0-2-4V3a4 4 0 0 0-3.2 1.6l-.3.4H11a6 6 0 0 0-6 6v1a5 5 0 0 0 2 4v3a1 1 0 0 0 1 1h2a1 1 0 0 0 1-1z"}],["path",{"d":"M16 10h.01"}],["path",{"d":"M2 8v1a2 2 0 0 0 2 2h1"}]],
    "banknote": [["rect",{"width":"20","height":"12","x":"2","y":"6","rx":"2"}],["circle",{"cx":"12","cy":"12","r":"2"}],["path",{"d":"M6 12h.01M18 12h.01"}]],
    "credit-card": [["rect",{"width":"20","height":"14","x":"2","y":"5","rx":"2"}],["line",{"x1":"2","x2":"22","y1":"10","y2":"10"}],["path",{"d":"M6 14h2"}]],
    "chart-no-axes-combined": [["path",{"d":"M12 16v5"}],["path",{"d":"M16 14.639V21"}],["path",{"d":"M20 10.656V21"}],["path",{"d":"m22 3-8.646 8.646a.5.5 0 0 1-.708 0L9.354 8.354a.5.5 0 0 0-.707 0L2 15"}],["path",{"d":"M4 18.463V21"}],["path",{"d":"M8 14.656V21"}]],
    "book-open": [["path",{"d":"M12 5v16"}],["path",{"d":"M20.001 19A2 2 0 0022 17V5a2 2 0 00-1.999-2L16 3.002A5 5 0 0012 5a5 5 0 00-4-2H4a2 2 0 00-2 2v12a2 2 0 001.999 2H8a5 5 0 014 2 5 5 0 014-2z"}]],
    "zap": [["path",{"d":"M15.914 4a1.5 1.5 0 00-2.474-1.561l-9 9A1.5 1.5 0 005.5 14h4.002a.5.5 0 01.471.666L8.086 20a1.5 1.5 0 002.475 1.56l9-9A1.5 1.5 0 0018.5 10h-3.997a.5.5 0 01-.472-.667z"}]],
    "arrow-left-right": [["path",{"d":"M8 3 4 7l4 4"}],["path",{"d":"M4 7h16"}],["path",{"d":"m16 21 4-4-4-4"}],["path",{"d":"M20 17H4"}]],
    "arrow-down-to-line": [["path",{"d":"M12 17V3"}],["path",{"d":"m6 11 6 6 6-6"}],["path",{"d":"M19 21H5"}]],
    "tag": [["path",{"d":"M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 2 0 0 0 .586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 0-3.42z"}],["circle",{"cx":"7.5","cy":"7.5","r":".5","fill":"currentColor"}]],
    "shopping-basket": [["path",{"d":"m15 11-1 9"}],["path",{"d":"m19 11-4-7"}],["path",{"d":"M2 11h20"}],["path",{"d":"m3.5 11 1.6 7.4a2 2 0 0 0 2 1.6h9.8a2 2 0 0 0 2-1.6l1.7-7.4"}],["path",{"d":"M4.5 15.5h15"}],["path",{"d":"m5 11 4-7"}],["path",{"d":"m9 11 1 9"}]],
    "shopping-bag": [["path",{"d":"M16 10a4 4 0 0 1-8 0"}],["path",{"d":"M3.103 6.034h17.794"}],["path",{"d":"M3.4 5.467a2 2 0 0 0-.4 1.2V20a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6.667a2 2 0 0 0-.4-1.2l-2-2.667A2 2 0 0 0 17 2H7a2 2 0 0 0-1.6.8z"}]],
    "house": [["path",{"d":"M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"}],["path",{"d":"M3 10a2 2 0 0 1 .709-1.528l7-6a2 2 0 0 1 2.582 0l7 6A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"}]],
    "car-front": [["path",{"d":"m21 8-2 2-1.5-3.7A2 2 0 0 0 15.646 5H8.4a2 2 0 0 0-1.903 1.257L5 10 3 8"}],["path",{"d":"M7 14h.01"}],["path",{"d":"M17 14h.01"}],["rect",{"width":"18","height":"8","x":"3","y":"10","rx":"2"}],["path",{"d":"M5 18v2"}],["path",{"d":"M19 18v2"}]],
    "utensils": [["path",{"d":"M3 2v7c0 1.1.9 2 2 2h4a2 2 0 0 0 2-2V2"}],["path",{"d":"M7 2v20"}],["path",{"d":"M21 15V2a5 5 0 0 0-5 5v6c0 1.1.9 2 2 2h3Zm0 0v7"}]],
    "heart-pulse": [["path",{"d":"M2 9.5a5.5 5.5 0 0 1 9.591-3.676.56.56 0 0 0 .818 0A5.49 5.49 0 0 1 22 9.5c0 2.29-1.5 4-3 5.5l-5.492 5.313a2 2 0 0 1-3 .019L5 15c-1.5-1.5-3-3.2-3-5.5"}],["path",{"d":"M3.22 13H9.5l.5-1 2 4.5 2-7 1.5 3.5h5.27"}]],
    "baby": [["path",{"d":"M10 16c.5.3 1.2.5 2 .5s1.5-.2 2-.5"}],["path",{"d":"M15 12h.01"}],["path",{"d":"M19.38 6.813A9 9 0 0 1 20.8 10.2a2 2 0 0 1 0 3.6 9 9 0 0 1-17.6 0 2 2 0 0 1 0-3.6A9 9 0 0 1 12 3c2 0 3.5 1.1 3.5 2.5s-.9 2.5-2 2.5c-.8 0-1.5-.4-1.5-1"}],["path",{"d":"M9 12h.01"}]],
    "shield-check": [["path",{"d":"M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"}],["path",{"d":"m9 12 2 2 4-4"}]],
    "wifi": [["path",{"d":"M12 20h.01"}],["path",{"d":"M2 8.82a15 15 0 0 1 20 0"}],["path",{"d":"M5 12.859a10 10 0 0 1 14 0"}],["path",{"d":"M8.5 16.429a5 5 0 0 1 7 0"}]],
    "repeat-2": [["path",{"d":"m2 9 3-3 3 3"}],["path",{"d":"M13 18H7a2 2 0 0 1-2-2V6"}],["path",{"d":"m22 15-3 3-3-3"}],["path",{"d":"M11 6h6a2 2 0 0 1 2 2v10"}]],
    "ticket": [["path",{"d":"M2 9a3 3 0 0 1 0 6v2a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-2a3 3 0 0 1 0-6V7a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2Z"}],["path",{"d":"M13 5v2"}],["path",{"d":"M13 17v2"}],["path",{"d":"M13 11v2"}]],
    "plane": [["path",{"d":"M17.8 19.2 16 11l3.5-3.5C21 6 21.5 4 21 3c-1-.5-3 0-4.5 1.5L13 8 4.8 6.2c-.5-.1-.9.1-1.1.5l-.3.5c-.2.5-.1 1 .3 1.3L9 12l-2 3H4l-1 1 3 2 2 3 1-1v-3l3-2 3.5 5.3c.3.4.8.5 1.3.3l.5-.2c.4-.3.6-.7.5-1.2z"}]],
    "receipt-text": [["path",{"d":"M13 16H8"}],["path",{"d":"M14 8H8"}],["path",{"d":"M16 12H8"}],["path",{"d":"M4 3a1 1 0 0 1 1-1 1.3 1.3 0 0 1 .7.2l.933.6a1.3 1.3 0 0 0 1.4 0l.934-.6a1.3 1.3 0 0 1 1.4 0l.933.6a1.3 1.3 0 0 0 1.4 0l.933-.6a1.3 1.3 0 0 1 1.4 0l.934.6a1.3 1.3 0 0 0 1.4 0l.933-.6A1.3 1.3 0 0 1 19 2a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1 1.3 1.3 0 0 1-.7-.2l-.933-.6a1.3 1.3 0 0 0-1.4 0l-.934.6a1.3 1.3 0 0 1-1.4 0l-.933-.6a1.3 1.3 0 0 0-1.4 0l-.933.6a1.3 1.3 0 0 1-1.4 0l-.934-.6a1.3 1.3 0 0 0-1.4 0l-.933.6a1.3 1.3 0 0 1-.7.2 1 1 0 0 1-1-1z"}]],
  };
  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.classList.add('fd-icon');
    svg.dataset.icon = iconPaths[name] || name;
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    for (const [tag, attrs] of lucideNodes[iconPaths[name] || name] || lucideNodes.tag) {
      const child = document.createElementNS('http://www.w3.org/2000/svg', tag);
      for (const [key, value] of Object.entries(attrs)) child.setAttribute(key, value);
      svg.append(child);
    }
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
    const name = add(summary, 'span', 'fd-account-name');
    const brand = bankBrand(account);
    if (brand) {
      const logo = add(name, 'img', 'fd-bank-logo');
      logo.src = {postbank:'/bank-postbank.svg',sparkasse:'/bank-sparkasse.png',
        ing:'/bank-ing.svg',paypal:'/bank-paypal.png'}[brand];
      logo.alt = ''; logo.setAttribute('aria-hidden', 'true');
    } else name.append(icon(accountIcon(account.kind)));
    name.append(document.createTextNode(accountName(account)));
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
    const counterparty = add(row, 'td', 'fd-booking-counterparty');
    if (window.financeCounterparties) window.financeCounterparties.decorate(add(counterparty, 'span'), item);
    else counterparty.textContent = item.counterparty || item.description || 'Ohne Gegenpartei';
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
