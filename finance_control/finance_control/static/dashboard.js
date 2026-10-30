'use strict';
// The dashboard receives an already scoped, read-only snapshot from app.js.
(() => {
  const key = 'finance-control-dashboard-view';
  let snapshot = null;
  let selected = null;
  const selectedAccounts = new Set();
  let selectionInitialized = false;
  const accountPickerState = {mobile:null,desktop:null};
  let forecastOverviewOpen = false;
  let timelineObserver = null;
  let timelinePositioned = '';
  let pendingTimelineAnchor = null;
  let extensionArmed = false;
  let lastTimelineScroll = 0;
  let query = '';
  const filters = {category:'', direction:'', status:''};
  const forecast = {enabled:true, until:'', asOf:'', key:'', data:null,
    error:'', loading:false, request:0};
  let drilldown = null;
  const paymentReviewMounts = new WeakMap();
  const expandedBookings = new Set();
  const scrollPositions = new Map();
  let visibleArea = null;
  const disclosureStates = new Map();
  window.addEventListener('scroll', () => {
    if (visibleArea) scrollPositions.set(visibleArea, window.scrollY);
  }, {passive:true});
  document.addEventListener('cockpit-area-changed', event => {
    visibleArea = ['dashboard', 'category-outflows'].includes(event.detail.id) ? event.detail.id : null;
  });
  let area = 'dashboard';
  const expandedAccounts = new Set();
  const mobileQuery = window.matchMedia('(max-width: 700px)');
  mobileQuery.addEventListener('change', event => {
    const picker = activeRoot()?.querySelector('.fd-account-picker');
    if (picker) {
      accountPickerState[picker.dataset.viewport] = picker.open;
      picker.dataset.viewport = event.matches ? 'mobile' : 'desktop';
      picker.open = accountPickerState[picker.dataset.viewport] ?? !event.matches;
    }
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
    if (has('haustier', 'tierbedarf', 'tierarzt', 'hund', 'katze', 'hunde', 'katzen')) return 'paw-print';
    if (has('wohnen', 'miete', 'wohnung', 'haus')) return 'house';
    if (has('gastronomie', 'restaurant', 'essen', 'café', 'cafe')) return 'utensils';
    if (contains('energie') || has('strom', 'gas', 'heizung')) return 'energy';
    if (has('öpnv', 'oepnv', 'öffis', 'oeffis', 'bus', 'tram')) return 'bus-front';
    if (has('bahn', 'zug', 'eisenbahn')) return 'train-front';
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
  lucideNodes['paw-print'] = [["circle",{"cx":"11","cy":"4","r":"2"}],["circle",{"cx":"18","cy":"8","r":"2"}],["circle",{"cx":"20","cy":"16","r":"2"}],["path",{"d":"M9 10a5 5 0 0 1 5 5v3.5a3.5 3.5 0 0 1-6.84 1.045Q6.52 17.48 4.46 16.84A3.5 3.5 0 0 1 5.5 10Z"}]];
  lucideNodes['bus-front'] = [["path",{"d":"M4 6 2 7"}],["path",{"d":"M10 6h4"}],["path",{"d":"m22 7-2-1"}],["rect",{"width":"16","height":"16","x":"4","y":"3","rx":"2"}],["path",{"d":"M4 11h16"}],["path",{"d":"M8 15h.01"}],["path",{"d":"M16 15h.01"}],["path",{"d":"M6 19v2"}],["path",{"d":"M18 21v-2"}]];
  lucideNodes['train-front'] = [["path",{"d":"M8 3.1V7a4 4 0 0 0 8 0V3.1"}],["path",{"d":"m9 15-1-1"}],["path",{"d":"m15 15 1-1"}],["path",{"d":"M9 19c-2.8 0-5-2.2-5-5v-4a8 8 0 0 1 16 0v4c0 2.8-2.2 5-5 5Z"}],["path",{"d":"m8 19-2 3"}],["path",{"d":"m16 19 2 3"}]];
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
  const componentRows = (item, fallbackCurrency) => {
    if (item?.cash_split?.status !== 'explicit' || !Array.isArray(item.components) || item.components.length < 1) return [];
    if (item.components.length === 1 && item.components[0]?.component_type !== 'cash_transfer') return [];
    const original = cents(item.amount);
    const currency = item.currency || fallbackCurrency || 'EUR';
    if (original === null || original >= 0n) return [];
    const entries = item.components.map(component => ({
      component,
      value: cents(component?.amount),
      currency: component?.currency || currency,
    }));
    if (entries.some(entry => entry.value === null || entry.value >= 0n || entry.currency !== currency)) return [];
    if (entries.reduce((sum, entry) => sum + entry.value, 0n) !== original) return [];
    return entries;
  };
  const componentName = component => ({purchase:'Einkauf',cash_transfer:'Bargeldauszahlung'}[component?.component_type]
    || component?.category || 'Teilbetrag');
  const componentSummaryAmount = (value, currency) => {
    const formatted = moneyFromCents(value < 0n ? -value : value, currency || 'EUR');
    return currency === 'EUR' || !currency ? formatted.replace(/ EUR$/, ' €') : formatted;
  };
  const tone = value => value === null || value === undefined || value === '' ? 'unknown'
    : String(value).trim().startsWith('-') ? 'negative' : 'positive';
  const date = value => {
    const match = String(value || '').match(/^(\d{4})-(\d{2})-(\d{2})/);
    return match ? `${match[3]}.${match[2]}.${match[1]}` : String(value || '');
  };
  const shiftDate = (value, days) => {
    const parsed = /^\d{4}-\d{2}-\d{2}$/.test(String(value || '')) ? new Date(value + 'T12:00:00Z') : null;
    if (!parsed || !Number.isFinite(parsed.getTime())) return '';
    parsed.setUTCDate(parsed.getUTCDate() + days);
    return parsed.toISOString().slice(0,10);
  };
  const selectedIDs = () => [...selectedAccounts].sort();
  const includesAccount = id => area === 'dashboard' ? selectedAccounts.has(String(id)) : String(id) === selected;
  const forecastKey = () => JSON.stringify([selectedIDs(), snapshot?.as_of, forecast.until]);
  function ensureForecast() {
    if (area !== 'dashboard' || !forecast.enabled || !selectedAccounts.size) return;
    const key = forecastKey();
    if (forecast.key === key && (forecast.loading || forecast.data || forecast.error)) return;
    forecast.key = key; forecast.data = null; forecast.error = ''; forecast.loading = false;
    const request = ++forecast.request;
    if (typeof api !== 'function') {
      forecast.error = 'Kontoprognose hier nicht verfügbar.';
      refreshForecastDisplay(); return;
    }
    forecast.loading = true; refreshForecastDisplay();
    const requestData = {as_of:snapshot.as_of, until:forecast.until, account_id:selectedAccounts.size === 1 ? selectedIDs()[0] : ''};
    Promise.resolve().then(() => api('/api/recurring-view', requestData)).then(data => {
      if (request !== forecast.request || key !== forecastKey() || area !== 'dashboard') return;
      forecast.data = data; forecast.error = ''; forecast.loading = false; refreshForecastDisplay();
    }).catch(error => {
      if (request !== forecast.request || key !== forecastKey() || area !== 'dashboard') return;
      forecast.error = `Kontoprognose nicht geladen: ${error?.message || 'Unbekannter Fehler'}`;
      forecast.loading = false; refreshForecastDisplay();
    });
  }
  function refreshForecastDisplay() {
    const root = document.getElementById('dashboard-root');
    if (area !== 'dashboard' || !root) return;
    const section = root.querySelector('.fd-bookings');
    if (!section) return;
    const status = section.querySelector('.fd-forecast-status');
    if (status) status.textContent = !forecast.enabled ? 'Prognose ausgeblendet.'
      : forecast.error || (forecast.loading ? 'Prognose wird geladen …' :
        forecast.data && forecast.key === forecastKey() ? `Prognose bis ${date(forecast.until)}. ${(filters.category || filters.status || filters.direction === 'transfer') ? 'Erwartungen bei diesem Filter ausgeblendet; für sie liegt keine Kategorie oder Umbuchung vor.' : 'Erwartungen folgen Suche und Richtungsfilter.'}` : 'Prognose wird vorbereitet …');
    const balance = section.querySelector('.fd-forecast-balance');
    if (balance) renderForecastBalance(balance);
    const rows = section.querySelector('.fd-booking-rows');
    if (rows) populateBookings(rows);
  }
  function invalidateForecast() {
    ++forecast.request; forecast.key = ''; forecast.data = null; forecast.error = ''; forecast.loading = false;
    timelineObserver?.disconnect(); extensionArmed = false;
  }
  function extendTimeline() {
    if (area !== 'dashboard' || !forecast.enabled || forecast.loading || !selectedAccounts.size) return;
    const limit = shiftDate(snapshot.as_of,366);
    const next = shiftDate(forecast.until,30);
    if (!next || forecast.until >= limit) return;
    forecast.until = next > limit ? limit : next;
    extensionArmed = false; invalidateForecast();
    const until = activeRoot()?.querySelector('.fd-forecast-until'); if (until) until.value = forecast.until;
    ensureForecast();
  }
  function jumpTimeline(target) {
    if (!target) return;
    const scroller = target.closest('.fd-table-scroll');
    if (scroller) scroller.scrollTo({top:Math.max(0,scroller.scrollTop + target.getBoundingClientRect().top - scroller.getBoundingClientRect().top),behavior:'instant'});
    target.tabIndex = -1; target.focus({preventScroll:true});
  }
  document.addEventListener('finance-booking-contract-changed', invalidateForecast);
  document.addEventListener('finance-payments-changed', invalidateForecast);
  let lastProjectionRead = 0;
  function refreshProjectionOnReturn() {
    if (area !== 'dashboard' || document.visibilityState === 'hidden'
        || location.hash !== '#dashboard' || document.querySelector('dialog[open]')
        || Date.now() - lastProjectionRead < 30000) return;
    lastProjectionRead = Date.now();
    invalidateForecast(); ensureForecast();
  }
  window.addEventListener('focus', refreshProjectionOnReturn);
  document.addEventListener('visibilitychange', refreshProjectionOnReturn);
  const currentBalanceText = account => {
    const balance = account?.current_balance;
    if (!balance || typeof balance !== 'object' || balance.amount === null || balance.amount === undefined) {
      return {amount:'—', stand:''};
    }
    return {amount:money(balance.amount, balance.currency || account.currency),
      stand:balance.source === 'confirmed_reference' ? `Berechnet aus Referenz · ${date(balance.reference_booked_on) || 'Datum unbekannt'}` : (date(balance.booked_on) ? `Stand ${date(balance.booked_on)}` : '')};
  };
  const accountName = account => account.display_name || account.id || 'Konto';
  const kindName = kind => ({CHECKING:'Girokonto',CURRENT:'Girokonto',SAVINGS:'Sparkonto',CASH:'Bargeld',CREDIT_CARD:'Kreditkarte',DEPOT:'Depot',INVESTMENT:'Depot'}[String(kind || '').toUpperCase()] || kind || 'Konto');
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
    if (selected === id && (area !== 'dashboard' || selectedAccounts.size === 1 && selectedAccounts.has(id))) return;
    selected = id;
    if (area === 'dashboard') {selectedAccounts.clear(); selectedAccounts.add(id); invalidateForecast();}
    drilldown = null;
    const root = activeRoot();
    root?.querySelectorAll('.fd-account').forEach(card => {
      const active = card.dataset.accountId === id;
      card.classList.toggle('is-selected', active);
      card.querySelector('summary')?.setAttribute('aria-current', active ? 'true' : 'false');
    });
    const main = root?.querySelector('.fd-main');
    if (area === 'dashboard') paint();
    else {
      if (main) populateMain(main);
      if (mobileQuery.matches) main?.scrollIntoView({block:'start', behavior:'smooth'});
    }
  }
  function accountButton(account, className, count) {
    const id = accountKey(account);
    const card = el('details', `${className}${includesAccount(id) ? ' is-selected' : ''}`);
    card.dataset.accountId = id;
    card.open = expandedAccounts.has(id);
    const isSavings = String(account.kind || '').toUpperCase() === 'SAVINGS';
    const summary = add(card, 'summary', `fd-account-summary${isSavings ? ' fd-account-summary-savings' : ''}`);
    summary.setAttribute('aria-current', includesAccount(id) ? 'true' : 'false');
    const name = add(summary, 'span', 'fd-account-name');
    name.append(window.financeAccountIcon(account));
    name.append(document.createTextNode(accountName(account)));
    const accountKindSummary = `${account.owner_label || account.owner || 'Ohne Zuordnung'} · ${kindName(account.kind)} · ${count} erfasste Buchung${count === 1 ? '' : 'en'}`;
    const latestIsTransactionDate = account.last_booking_date_basis === 'transaction_date';
    const latestDate = account.last_booking_date ? date(account.last_booking_date) : 'unbekannt';
    const latestTransactionText = `Letzter Umsatz: ${latestDate} · PayPal-Transaktionsdatum`;
    const latestBookingSummary = latestIsTransactionDate ? latestTransactionText : `Letzte Buchung: ${latestDate}`;
    const balance = currentBalanceText(account);
    const balanceBox = add(summary, 'span', 'fd-account-balance');
    const amount = add(balanceBox, 'strong', `fd-account-amount ${tone(account.current_balance?.amount)}`, balance.amount);
    if (!account.current_balance || account.current_balance.amount === null || account.current_balance.amount === undefined)
      amount.setAttribute('aria-label', `Kontostand fehlt für ${accountName(account)}`);
    if (balance.stand) add(balanceBox, 'small', 'fd-account-balance-date', balance.stand);
    const body = add(card, 'div', 'fd-account-body');
    body.append(metrics(account));
    const evidence = add(body, 'details', 'fd-account-evidence');
    add(evidence, 'summary', '', 'Daten & Abgleich');
    add(evidence, 'p', 'fd-account-kind', accountKindSummary);
    add(evidence, 'p', 'fd-account-freshness fd-account-last-booking', latestBookingSummary);
    if (account.note) add(evidence, 'p', 'fd-account-note', account.note);
    if (window.financeReconciliation) window.financeReconciliation(evidence, account.reconciliation);
    if (account.current_balance?.source === 'confirmed_reference') {
      const ref = account.current_balance;
      add(evidence, 'p', '', `Referenz: ${date(ref.reference_booked_on) || 'Datum unbekannt'} · ${money(ref.reference_amount, ref.currency || account.currency)} · ${ref.source_reference || 'Quelle nicht angegeben'}`);
      add(evidence, 'p', '', 'Die Vollständigkeit der Buchungen nach der Referenz ist nicht geprüft.');
    }
    const referenceDetails = add(evidence, 'details', 'fd-account-reference');
    add(referenceDetails, 'summary', '', 'Referenzsaldo hinterlegen');
    const form = add(referenceDetails, 'form');
    const field = (labelText, type) => {
      const label = add(form, 'label', '', labelText + ' ');
      const input = add(label, 'input'); input.type = type; input.required = true;
      return input;
    };
    const referenceAmount = field(`Betrag (${account.currency})`, 'text');
    referenceAmount.inputMode = 'decimal'; referenceAmount.placeholder = 'z. B. -123,45';
    const referenceDate = field('Datum', 'date');
    const source = field('Quelle', 'text'); source.maxLength = 500;
    const basisLabel = add(form, 'label', '', 'Grundlage ');
    const basis = add(basisLabel, 'select');
    for (const [value, text] of [['statement_day_end', 'Auszug zum Tagesende'], ['owner_confirmed_day', 'Von mir bestätigter Stand']]) {
      const option = add(basis, 'option', '', text); option.value = value;
    }
    const confirmationLabel = add(form, 'label');
    const confirmation = add(confirmationLabel, 'input'); confirmation.type = 'checkbox'; confirmation.required = true;
    confirmationLabel.append(document.createTextNode(' Ich bestätige diesen Referenzsaldo.'));
    const submit = add(form, 'button', '', 'Referenzsaldo speichern'); submit.type = 'submit';
    const status = add(form, 'p'); status.setAttribute('role', 'status');
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (!confirmation.checked) { status.textContent = 'Bitte bestätige den Referenzsaldo.'; return; }
      const amount = referenceAmount.value.trim().replace(',', '.');
      if (!/^[+-]?\d+(?:\.\d{1,2})?$/.test(amount)) {
        status.textContent = 'Bitte einen Betrag mit höchstens zwei Nachkommastellen eingeben.'; return;
      }
      submit.disabled = true; status.textContent = 'Wird gespeichert …';
      try {
        await window.api('/api/balance-reference', {account_id: account.id, amount, currency: account.currency,
          booked_on: referenceDate.value, source_reference: source.value.trim(), basis: basis.value, confirmed: confirmation.checked});
        status.textContent = 'Gespeichert. Ansicht wird aktualisiert …';
        try { await window.financeRefreshAfterImport(); }
        catch (_) { status.textContent = 'Gespeichert. Die Ansicht konnte nicht aktualisiert werden; bitte lade die Seite neu.'; }
      } catch (_) {
        status.textContent = 'Speichern fehlgeschlagen. Bitte Angaben und Verbindung prüfen.'; submit.disabled = false;
      }
    });
    if (!account.current_balance || account.current_balance.amount === null || account.current_balance.amount === undefined) {
      const nextAction = add(body, 'button', 'fd-account-balance-action', 'Bankabruf öffnen');
      nextAction.type = 'button';
      nextAction.addEventListener('click', () => window.financeBankRefresh?.openDetails?.());
    }
    if (area === 'category-outflows') summary.addEventListener('click', () => pick(id));
    card.addEventListener('toggle', () => { if (card.open) expandedAccounts.add(id); else expandedAccounts.delete(id); });
    if (area !== 'dashboard') return card;
    const choice = el('div', 'fd-account-choice');
    const label = add(choice, 'label', 'fd-account-select');
    const checkbox = add(label, 'input'); checkbox.type = 'checkbox'; checkbox.checked = selectedAccounts.has(id);
    checkbox.dataset.accountId = id; checkbox.setAttribute('aria-label', `${accountName(account)} auswählen`);
    add(label, 'span', '', 'Auswählen');
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) selectedAccounts.add(id); else selectedAccounts.delete(id);
      invalidateForecast(); paint();
      activeRoot()?.querySelector(`.fd-account-select input[data-account-id="${CSS.escape(id)}"]`)?.focus({preventScroll:true});
    });
    choice.append(card); return choice;
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
      [`${monthLabel()} · Zugänge`, account.inflow, false], [`${monthLabel()} · Abgänge`, account.outflow, false], [`${monthLabel()} · Monatsbewegung`, account.change, true]]) {
      const card = add(wrap, 'div', 'fd-metric');
      add(card, 'span', '', label);
      add(card, 'strong', signed ? tone(value) : '', money(value, account.currency, signed));
    }
    return wrap;
  }
  const categoryLabel = item => {
    const label = item.category_status === 'open' ? 'Kategorie offen' : (item.category || 'Kategorie offen');
    return item.category_status === 'proposal' ? `${label} · Vorschlag` : label;
  };
  const categoryStatusLabel = status => ({confirmed:'Bestätigt',proposal:'Vorschlag · noch nicht bestätigt',imported:'Importiert',open:'Offen'})[status];
  const bookingKey = item => JSON.stringify([item.account_id,item.external_id]);
  const transfer = item => item.is_transfer === true || item.direction === 'transfer'
    || item.category_id === 'TRANSFER' || item.category === 'Umbuchung' || item.category === 'TRANSFER';
  const direction = item => transfer(item) ? 'transfer' : (cents(item.amount) > 0n ? 'income' : 'expense');
  function filteredBookings() {
    const account = current();
    const term = query.trim().toLocaleLowerCase('de-DE');
    return bookings().filter(item => includesAccount(item.account_id))
      .filter(item => !filters.direction || direction(item) === filters.direction)
      .filter(item => !filters.status || (item.category_status || 'open') === filters.status)
      .filter(item => !filters.category || categoryLabel(item) === filters.category)
      .filter(item => !term || [item.counterparty,item.description,item.category,item.amount,item.date,
        money(item.amount,item.currency || account?.currency),date(item.date)]
        .some(value => String(value || '').toLocaleLowerCase('de-DE').includes(term)))
      .sort(bookingDateOrder);
  }
  // Native disclosures retain their state when a fresh snapshot replaces the DOM.
  function disclosure(parent, className, label, key) {
    const node = add(parent, 'details', className);
    node.dataset.disclosure = `${area}:${key}`;
    node.open = disclosureStates.get(node.dataset.disclosure) || false;
    add(node, 'summary', '', label);
    node.addEventListener('toggle', () => {
      if (node.isConnected) disclosureStates.set(node.dataset.disclosure, node.open);
    });
    return node;
  }
  function filterControls(section, update) {
    const controls = add(section, 'div', 'fd-filters');
    const label = add(controls, 'label', 'fd-search-label');
    add(label, 'span', '', 'Buchungen suchen');
    const search = add(label, 'input', 'fd-search');
    search.type = 'search'; search.placeholder = 'Empfänger, Zweck, Betrag'; search.value = query;
    search.addEventListener('input', () => {query = search.value; drilldown = null; update();});
    const filter = disclosure(controls, 'fd-filter-panel', 'Filter', 'filters');
    const summary = filter.querySelector('summary');
    const filterLabel = () => {
      const count = Object.values(filters).filter(Boolean).length;
      summary.textContent = count ? `Filter (${count})` : 'Filter';
    };
    filterLabel();
    const advanced = add(filter, 'div', 'fd-filter-advanced');
    const categories = [...new Set([
      ...bookings().filter(item => includesAccount(item.account_id)).map(categoryLabel),
      ...(filters.category ? [filters.category] : []),
    ])].sort((a,b) => a.localeCompare(b,'de'));
    for (const [key,labelText,choices] of [
      ['category','Kategorie',categories.map(value => [value,value])],
      ['direction','Richtung',[['income','Zugänge'],['expense','Abgänge'],['transfer','Umbuchungen']]],
      ['status','Kategoriestatus',[['confirmed','Bestätigt'],['imported','Importiert'],['proposal','Vorschlag'],['open','Offen']]],
    ]) {
      const field = add(advanced, 'label'); add(field,'span','',labelText);
      const select = add(field,'select',`fd-filter-${key}`);
      add(select,'option','','Alle').value = '';
      for (const [value,text] of choices) add(select,'option','',text).value = value;
      select.value = filters[key];
      select.addEventListener('change', () => {filters[key] = select.value; filterLabel(); drilldown = null; update();});
    }
    const reset = add(advanced,'button','fd-filter-reset','Filter zurücksetzen'); reset.type = 'button';
    reset.addEventListener('click', () => {
      query = ''; search.value = ''; Object.keys(filters).forEach(key => {filters[key] = '';});
      advanced.querySelectorAll('select').forEach(select => {select.value = '';});
      drilldown = null; filterLabel(); update();
    });
  }
  // Only a checksum-valid full leading IBAN is hidden in the compact display.
  // Imported source text and the complete detail remain untouched.
  function recipientDisplay(value) {
    const original = String(value || '');
    const lengths = {AT:20,BE:16,CH:21,DE:22,ES:24,FR:27,GB:22,IT:27,LU:20,NL:18,PL:28,PT:25};
    const start = /^\s*([A-Z]{2})\d{2}/iu.exec(original);
    const length = start && lengths[start[1].toUpperCase()];
    if (!length) return original;
    let compact = start[0].trim().toUpperCase(), position = start[0].length;
    while (compact.length < length && position < original.length) {
      const character = original[position++];
      if (/\s/u.test(character)) continue;
      if (!/[A-Z0-9]/iu.test(character)) return original;
      compact += character.toUpperCase();
    }
    const numeric = ['AT','BE','DE','ES','PL','PT'].includes(start[1].toUpperCase());
    if (compact.length !== length || (numeric && !/^\d+$/u.test(compact.slice(4)))
      || (numeric ? /\p{N}/u : /[\p{L}\p{N}]/u).test(original[position] || '')) return original;
    let remainder = 0;
    for (const character of compact.slice(4) + compact.slice(0,4)) {
      const digits = /[A-Z]/u.test(character) ? String(character.charCodeAt(0)-55) : character;
      for (const digit of digits) remainder = (remainder*10 + Number(digit)) % 97;
    }
    return remainder === 1 ? (original.slice(position).replace(/^[\s,;:/–—-]+/u,'').trim() || original) : original;
  }
  function categoryEntries(item, account) {
    const components = componentRows(item,account.currency);
    const amounts = components.length ? components.map(({component,value}) => ({
      ...component, category_status:component.category_status || item.category_status,
      value, currency:component.currency || item.currency || account.currency || 'EUR',
    })) : [{...item,value:cents(item.amount),currency:item.currency || account.currency || 'EUR'}];
    return amounts.filter(entry => entry.value !== null && entry.value < 0n).map(entry => {
      const path = Array.isArray(entry.category_path) && entry.category_path.length
        ? entry.category_path.map(node => ({id:node.id,label:node.label}))
        : [{id:entry.category_id || null,label:categoryLabel(entry)}];
      // Keep proposals distinguishable from confirmed labels, including in a path.
      path[path.length-1] = {...path[path.length-1],label:categoryLabel(entry)};
      return {...entry,path,booking:item};
    });
  }
  const pathKey = path => JSON.stringify(path.map(node => [node.id,node.label]));
  function categories() {
    const section = el('section', 'fd-categories');
    add(section, 'p', 'fd-kicker', monthLabel());
    add(section, 'h2', '', 'Abgänge nach Kategorie');
    add(section, 'p', 'fd-disclosure', `Kontosicht einschließlich Umbuchungen · bis ${date(snapshot.as_of)}.`);
    const account = current();
    if (!account) {add(section,'p','fd-empty','Wähle ein Konto aus.'); return section;}
    add(section,'p','fd-category-account',`${account.owner_label || account.owner || 'Konto'} · ${accountName(account)}`);
    let result;
    filterControls(section, () => renderCategories(result,account));
    result = add(section,'div','fd-category-results');
    result.setAttribute('aria-live','polite');
    renderCategories(result,account);
    return section;
  }
  function renderCategories(result,account) {
    result.replaceChildren();
    const entries = filteredBookings().flatMap(item => categoryEntries(item,account));
    if (!entries.length) {add(result,'p','fd-empty','Keine Abgänge für diesen Filter.'); return;}
    const groups = new Map();
    for (const entry of entries) {
      // Build the actual authoritative root-to-leaf tree, separately per currency.
      for (let depth=1; depth<=entry.path.length; depth++) {
        const path = entry.path.slice(0,depth);
        const key = JSON.stringify([entry.currency,pathKey(path)]);
        const prior = groups.get(key) || {path,currency:entry.currency,value:0n};
        prior.value += -entry.value; groups.set(key,prior);
      }
    }
    const list = add(result,'div','fd-category-list');
    const appendLevel = (parentPath, currency, container) => {
      const children = [...groups.values()].filter(group => group.currency===currency
        && group.path.length===parentPath.length+1
        && pathKey(group.path.slice(0,-1))===pathKey(parentPath))
        .sort((a,b) => a.value>b.value ? -1 : a.value<b.value ? 1 : a.path.at(-1).label.localeCompare(b.path.at(-1).label,'de'));
      for (const group of children) {
        const branch = add(container,'div','fd-category-branch');
        const row = add(branch,'button','fd-category'); row.type='button';
        row.dataset.depth=String(parentPath.length);
        iconLabel(row,'fd-category-name',categoryIcon(group.path.at(-1).label),group.path.at(-1).label);
        add(row,'strong','',moneyFromCents(group.value,group.currency));
        row.setAttribute('aria-pressed',String(drilldown?.key===JSON.stringify([currency,pathKey(group.path)])));
        row.addEventListener('click', () => {
          drilldown={key:JSON.stringify([currency,pathKey(group.path)]),path:group.path,currency};
          renderCategories(result,account);
          result.querySelector('.fd-category-drilldown')?.scrollIntoView({block:'nearest'});
        });
        const subtree = add(branch,'div','fd-category-children');
        appendLevel(group.path,currency,subtree);
      }
    };
    for (const currency of [...new Set(entries.map(entry => entry.currency))].sort()) appendLevel([],currency,list);
    if (drilldown) {
      const matching = entries.filter(entry => entry.currency===drilldown.currency
        && pathKey(entry.path.slice(0,drilldown.path.length))===pathKey(drilldown.path));
      const detail = add(result,'section','fd-category-drilldown');
      add(detail,'h3','',drilldown.path.map(node=>node.label).join(' › '));
      add(detail,'p','fd-disclosure','Originalbuchungen zum gewählten Kategorieanteil; Aufteilungen stehen in den Details.');
      const unique = [...new Map(matching.map(entry => [bookingKey(entry.booking),entry.booking])).values()];
      const rows = add(detail,'div','fd-booking-rows');
      populateBookings(rows,unique);
    }
  }
  function bookingList() {
    const section = el('section', 'fd-bookings');
    const balances = add(section, 'div', 'fd-selected-balances');
    for (const account of accounts().filter(account => selectedAccounts.has(accountKey(account)))) {
      const balance = currentBalanceText(account);
      const currentBalance = add(balances, 'div', 'fd-selected-balance');
      add(currentBalance, 'span', '', `${accountName(account)} · Kontostand`);
      add(currentBalance, 'strong', `fd-current-balance ${tone(account.current_balance?.amount)}`, balance.amount);
      add(currentBalance, 'small', '', balance.stand);
    }
    const top = add(section, 'div', 'fd-bookings-head');
    const title = add(top, 'div');
    add(title, 'p', 'fd-kicker', monthLabel());
    add(title, 'h2', '', 'Buchungen');
    filterControls(section, () => updateBookings(section));
    if (forecast.asOf !== snapshot.as_of) {
      forecast.asOf = snapshot.as_of;
      forecast.until = shiftDate(snapshot.as_of, 30);
      forecast.key = ''; forecast.data = null; forecast.error = '';
      ++forecast.request;
    }
    const prognosis = disclosure(section, 'fd-prognosis-panel', 'Prognose', 'prognosis');
    const navigation = add(prognosis, 'nav', 'fd-timeline-navigation'); navigation.setAttribute('aria-label', 'Buchungszeitraum');
    for (const [label,selector] of [['Vergangenheit','.fd-booking'],['Heute','.fd-today'],['Zukunft','.fd-forecast-row']]) {
      const button = add(navigation, 'button', '', label); button.type = 'button';
      button.addEventListener('click', () => jumpTimeline(section.querySelector(selector)));
    }
    const controls = add(prognosis, 'div', 'fd-forecast-controls');
    const toggleLabel = add(controls, 'label');
    const toggle = add(toggleLabel, 'input', 'fd-forecast-toggle');
    toggle.type = 'checkbox'; toggle.checked = forecast.enabled;
    toggleLabel.append(document.createTextNode('Erwartete Zahlungen in der Buchungsliste'));
    toggle.addEventListener('change', () => {
      forecast.enabled = toggle.checked;
      if (!forecast.enabled) { ++forecast.request; forecast.loading = false; forecast.key = ''; }
      refreshForecastDisplay();
      if (forecast.enabled) ensureForecast();
    });
    const untilLabel = add(controls, 'label'); add(untilLabel, 'span', '', 'Prognose bis');
    const until = add(untilLabel, 'input', 'fd-forecast-until');
    until.type = 'date'; until.min = snapshot.as_of; until.max = shiftDate(snapshot.as_of, 366);
    until.value = forecast.until;
    until.addEventListener('change', () => {
      if (!until.validity.valid || until.value < snapshot.as_of || until.value > until.max) return;
      forecast.until = until.value; forecast.key = ''; forecast.data = null; forecast.error = '';
      ++forecast.request; refreshForecastDisplay(); ensureForecast();
    });
    const reload = add(controls, 'button', 'fd-forecast-reload', 'Prognose aktualisieren');
    reload.type = 'button';
    reload.addEventListener('click', () => {
      ++forecast.request; forecast.key = ''; forecast.data = null; forecast.error = ''; forecast.loading = false;
      refreshForecastDisplay(); ensureForecast();
    });
    const status = add(prognosis, 'p', 'fd-forecast-status');
    status.setAttribute('role', 'status');
    const overview = add(prognosis, 'details', 'fd-forecast-overview');
    overview.open = forecastOverviewOpen;
    add(overview, 'summary', '', 'Saldoübersicht und Datengrundlage');
    overview.addEventListener('toggle', () => {if (overview.isConnected) forecastOverviewOpen = overview.open;});
    const balance = add(overview, 'div', 'fd-forecast-balance');
    const rows = add(section, 'div', 'fd-booking-rows');
    rows.setAttribute('aria-live', 'polite');
    status.textContent = !forecast.enabled ? 'Prognose ausgeblendet.' : forecast.error ||
      (forecast.loading ? 'Prognose wird geladen …' : forecast.data && forecast.key === forecastKey() ?
        `Prognose bis ${date(forecast.until)}. ${(filters.category || filters.status || filters.direction === 'transfer') ? 'Erwartungen bei diesem Filter ausgeblendet; für sie liegt keine Kategorie oder Umbuchung vor.' : 'Erwartungen folgen Suche und Richtungsfilter.'}` : 'Prognose wird vorbereitet …');
    renderForecastBalance(balance);
    populateBookings(rows);
    queueMicrotask(ensureForecast);
    return section;
  }
  function updateBookings(section) { refreshForecastDisplay(); }
  const stringOrder = (a,b) => String(a) < String(b) ? -1 : String(a) > String(b) ? 1 : 0;
  const timelineOrder = (a,b) => {
    const dayAndAccount = stringOrder(a.day,b.day) || stringOrder(a.item.account_id,b.item.account_id);
    if (dayAndAccount) return dayAndAccount;
    if (a.kind !== b.kind) return a.kind === 'booking' ? -1 : 1;
    const left = a.item.account_movement_balance?.sequence_index;
    const right = b.item.account_movement_balance?.sequence_index;
    if (a.kind === 'booking' && Number.isInteger(left) && Number.isInteger(right)) return left - right;
    return stringOrder(a.item.external_id || a.item.id,b.item.external_id || b.item.id);
  };
  const bookingDateOrder = (a, b) => String(a.date || '').localeCompare(String(b.date || ''))
    || String(a.external_id || '').localeCompare(String(b.external_id || ''));
  const forecastWarnings = {
      paypal_transaction_date_not_booking_date:'PayPal-Buchungsdatum fehlt.',
    same_day_projection_order_assumed:'Die Reihenfolge virtueller Zahlungen am selben Tag ist anhand der Regel-ID angenommen.',
    stale_recorded_balance:'Der belegte Kontostand ist älter als sieben Tage.',
    recorded_balance_missing:'Kein belegter Kontostand vorhanden.',
    recorded_balance_ambiguous:'Widersprüchliche Kontostände; Projektion unbekannt.',
    balance_rollforward_ledger_completeness_unverified:'Fortschreibung aus importierten Buchungen; Vollständigkeit nicht bestätigt.',
    unresolved_payments_excluded:'Ungeklärte Zahlungen sind nicht eingerechnet.',
    paypal_credit_source_stale:'PayPal-Planstand geändert; Kontozuordnung erneut bestätigen. Raten sind nicht eingerechnet.',
    paypal_credit_account_ineligible:'Zahlungskonto passt nicht zur Laufzeit des PayPal-Plans; Raten sind nicht eingerechnet.',
    competing_payment_schedule:'Möglicherweise doppelte Zahlungsreihe; ungeklärte Raten sind nicht eingerechnet.',
    paypal_credit_booking_assignment_conflict:'Eine passende Buchung ist ausdrücklich anders eingeordnet; PayPal-Ratenzuordnung offen.',

    projection_conditional_on_imported_ledger:'Die Prognose setzt vollständige importierte Buchungen voraus.',
    transfers_not_inferred:'Umbuchungen werden nicht automatisch ergänzt.',
    budgets_not_payment_events:'Budgets sind Grenzen und keine zusätzlichen Zahlungen.'
  };
  function renderForecastBalance(target) {
    target.replaceChildren();
    if (!forecast.enabled || !forecast.data || forecast.key !== forecastKey()) return;
    for (const account of forecast.data.accounts || []) {
      if (!selectedAccounts.has(String(account.account_id))) continue;
      const supplied = accounts().find(item => accountKey(item) === String(account.account_id));
      if (!supplied || supplied.currency !== account.currency) continue;
      const card = add(target, 'div', 'fd-account-projection');
      add(card, 'strong', '', `${accountName(supplied)} · Kontoprognose am ${date(forecast.until)}: ${money(account.projected_balance, account.currency)}`);
      add(card, 'p', '', 'Teilprognose · alle erwarteten Zahlungen; unabhängig von Buchungsfiltern.');
      const evidence = add(card, 'details', 'fd-forecast-evidence');
      add(evidence, 'summary', '', 'Datengrundlage & Grenzen');
      add(evidence, 'p', '', account.recorded_balance
        ? `Saldoanker: ${money(account.recorded_balance.amount, account.currency)} am ${date(account.recorded_balance.booked_on)}`
        : 'Saldoanker fehlt; kein belegter Kontostand.');
      for (const warning of [...(forecast.data.warnings || []), ...(account.warnings || [])])
        add(evidence, 'p', 'fd-forecast-warning', forecastWarnings[warning] || 'Die Datenbasis muss geprüft werden.');
    }
  }
  function forecastEvents() {
    if (area !== 'dashboard' || !forecast.enabled || !forecast.data || forecast.key !== forecastKey() || !selectedAccounts.size) return [];
    if (filters.category || filters.status || filters.direction === 'transfer') return [];
    const term = query.trim().toLocaleLowerCase('de-DE');
    return (forecast.data.events || []).filter(item => item.status === 'expected'
      && selectedAccounts.has(String(item.account_id)) && item.currency === accounts().find(account => accountKey(account) === String(item.account_id))?.currency
      && item.expected_date > snapshot.as_of && item.expected_date <= forecast.until)
      .filter(item => !filters.direction || (String(item.amount).startsWith('-') ? 'expense' : 'income') === filters.direction)
      .filter(item => !term || [item.label,item.amount,item.expected_date,date(item.expected_date),
        money(item.amount,item.currency)].some(value => String(value || '').toLocaleLowerCase('de-DE').includes(term)))
      .sort((a,b) => String(a.expected_date).localeCompare(String(b.expected_date)) || stringOrder(a.id,b.id));
  }
  function forecastRow(parent, event) {
    const row = add(parent, 'tr', 'fd-forecast-row');
    add(row, 'td', 'fd-forecast-date', date(event.expected_date));
    row.dataset.timelineKey = `forecast:${event.account_id}:${event.id}:${event.expected_date}`;
    const name = add(row, 'td', 'fd-forecast-name', event.label || 'Erwartete Zahlung');
    const ownAccount = accounts().find(item => accountKey(item) === String(event.account_id));
    add(name, 'small', 'fd-row-account', `${accountName(ownAccount || {})} · ${event.currency}`);
    add(row, 'td', 'fd-forecast-status-label', 'Virtuell · Erwartet');
    add(row, 'td', 'fd-forecast-origin', event.basis === 'auto_detected'
      ? 'Automatisch erkannt · geschätzt' : event.basis === 'confirmed_paypal_credit' ? 'Bestätigter PayPal-Ratenplan' : event.document_id ? 'Bestätigter Vertrag' : 'Bestätigte Regel');
    add(row, 'td', 'fd-forecast-amount', money(event.amount,event.currency,true));
    const account = (forecast.data.accounts || []).find(item =>
      String(item.account_id) === String(event.account_id) && item.currency === event.currency);
    const point = account?.points?.find(item => item.date === event.expected_date);
    const suppliedBalance = Object.hasOwn(event, 'projected_balance') ? event.projected_balance : point?.amount;
    const knownPoint = cents(suppliedBalance) !== null;
    const running = add(row, 'td', 'fd-forecast-running-balance', knownPoint ? money(suppliedBalance,event.currency) : '—');
    running.title = Object.hasOwn(event, 'projected_balance') ? 'Prognose nach dieser virtuellen Zahlung · Reihenfolge am selben Tag angenommen; unabhängig von Filtern' : 'Prognose als Tagespunkt · unabhängig von Filtern';
    if (knownPoint) add(running, 'small', '', 'Prognose');
    const action = add(row, 'td', 'fd-forecast-action');
    const details = add(action, 'details', 'fd-forecast-details');
    add(details, 'summary', '', 'Details');
    const body = add(details, 'div', 'fd-forecast-detail-body');
    add(body, 'p', '', `Erwarteter Termin: ${date(event.expected_date)}${event.date_estimated ? ' · geschätzt' : ''}`);
    add(body, 'p', '', `Betrag: ${money(event.amount,event.currency,true)}${event.amount_estimated ? ' · geschätzt' : ''}`);
    add(body, 'p', '', 'Prognose je Zahlung; die Reihenfolge am selben Tag wird anhand der Regel-ID angenommen. Unabhängig von Buchungsfiltern.');
    if (event.document_id && window.financeBookingContracts?.documentButton) window.financeBookingContracts.documentButton(body,event.document_id,'Vertragsdokument öffnen');
    if (Object.hasOwn(event, 'projected_balance')) add(body, 'p', '', `Prognose nach dieser Zahlung: ${money(event.projected_balance,event.currency)}`);
    else if (point) add(body, 'p', '', `Gelieferter Kontoprognosepunkt am ${date(point.date)}: ${money(point.amount,event.currency)}`);
    const candidate = (forecast.data.candidates || []).find(item => item.id === event.rule_id);
    const budgetRef = candidate?.source_refs?.find(ref => ref.type === 'budget_item');
    add(body, 'p', '', budgetRef ? 'Aus Budgetposition abgeleitet.' : 'Plan-Zuordnung nicht belegt.');
    for (const warning of event.warnings || []) add(body, 'p', 'fd-forecast-warning',
      forecastWarnings[warning] || 'Diese erwartete Zahlung muss geprüft werden.');
    const link = add(body, 'a', '', 'Verträge & Zahlungen öffnen'); link.href = '#recurring';
  }
  function historyPanel(parent, identity) {
    const details = add(parent, 'details', 'fd-booking-history');
    add(details, 'summary', '', 'Buchungsverlauf');
    const content = add(details, 'div', 'fd-history-content');
    lazyHistory(details, content, '/api/booking-history', identity, (data, target) => {
      const statuses = {active:'Aktiv',superseded:'Ersetzt',voided:'Entfernt'};
      for (const version of data.versions || []) {
        const card = add(target, 'article', 'fd-history-version');
        add(card, 'strong', '', `Version ${version.revision} · ${statuses[version.status] || version.status}`);
        add(card, 'p', '', `${date(version.date)} · ${money(version.amount,version.currency,true)}`);
        add(card, 'p', '', `Quellkategorie: ${version.source_category || '—'}`);
        add(card, 'p', '', `Import ${version.import_id} · ${version.imported_at || '—'}`);
        if (version.voided_at) add(card, 'p', '', `Archiviert: ${version.voided_at} · ${version.actor || '—'}`);
        if (version.archived_data_status === 'invalid') add(card,'p','','Archivierte Zusatzdaten sind beschädigt.');
        const context = version.context || {};
        if (context.counterparty || context.description) add(card, 'p', '', [context.counterparty,context.description].filter(Boolean).join(' · '));
        const dependencyLabels = {
          transaction_context:'Gegenpartei und Verwendungszweck',classification_overrides:'Kategorie',
          classification_document_links:'Belege',booking_contract_links:'Verträge',
          transfer_correction_members:'Bestätigte Umbuchungen',transfer_single_legs:'Weitere Umbuchungen',
          bonsy_cash_allocations:'Bargeldbelege',rejected_document_links:'Abgelehnte Belegzuordnungen',
          classification_model_reviews:'Kategorievorschläge',classification_model_rejections:'Abgelehnte Kategorien',
          payment_mail_events:'Zahlungsbestätigungen'
        };
        const dependencies = Object.entries(version.dependencies || {}).map(([name,items]) =>
          `${dependencyLabels[name] || 'Weitere Zuordnungen'}: ${items.length}`);
        if (dependencies.length) add(card, 'p', '', `Archivierte Zuordnungen: ${dependencies.join(' · ')}`);
      }
      for (const event of data.events || []) {
        const trace = add(target, 'details', 'fd-history-event');
        add(trace, 'summary', '', `Journal ${event.id} · ${{inserted:'Angelegt',updated:'Geändert',removed:'Entfernt'}[event.action] || event.action} · ${event.occurred_at}`);
        if (event.archived_data_status === 'invalid') add(trace,'p','','Archivierte Zusatzdaten sind beschädigt.');
        for (const [label,value] of [['Vorher',event.previous?.transaction],['Danach',event.current]]) {
          if (value && Object.keys(value).length) add(trace, 'p', '', `${label}: ${date(value.date)} · ${value.amount || '—'} ${value.currency || ''} · Quellkategorie: ${value.category || '—'}`);
        }
        const context = event.previous?.context || {};
        if (context.counterparty || context.description) add(trace,'p','',[context.counterparty,context.description].filter(Boolean).join(' · '));
      }
      if (!(data.versions || []).length && !(data.events || []).length) add(target, 'p', '', 'Kein Buchungsverlauf vorhanden.');
    });
  }
  function lazyHistory(details, content, route, identity, render) {
    let request = 0; let loaded = false;
    const basis = snapshot;
    const load = async (offset = 0) => {
      const token = ++request;
      const loading = add(content, 'p', 'fd-history-loading', 'Verlauf wird geladen …');
      try {
        if (typeof api !== 'function') throw new Error('unavailable');
        const data = await api(route, {...identity,limit:50,offset});
        if (token !== request || !details.isConnected || basis !== snapshot || !details.open) return;
        loading.remove(); loaded = true;
        render(data,content);
        if (data.has_more) {
          const more = add(content, 'button', 'fd-history-more', 'Weitere Einträge laden'); more.type = 'button';
          more.addEventListener('click', () => {more.remove(); load(offset + 50);});
        }
      } catch {
        if (token !== request || !details.isConnected || basis !== snapshot || !details.open) return;
        loading.textContent = 'Buchungsverlauf nicht verfügbar. Zum erneuten Laden schließen und öffnen.';
      }
    };
    details.addEventListener('toggle', () => {
      if (!details.open) {++request; loaded = false; content.replaceChildren(); return;}
      if (!loaded) {content.replaceChildren(); load();}
    });
  }
  function removedHistory(parent) {
    const outer = disclosure(parent,'fd-removed-history','Entfernte Buchungen · separater Verlauf','removed');
    add(outer,'p','','Archivierte Buchungen sind nicht in Buchungssummen und Kontoständen enthalten.');
    for (const account of accounts().filter(item => includesAccount(accountKey(item)))) {
      const details = disclosure(outer,'fd-account-history',accountName(account),`removed:${accountKey(account)}`);
      const content = add(details,'div','fd-history-content');
      lazyHistory(details,content,'/api/booking-history-inactive',{account_id:accountKey(account)},(data,target) => {
        for (const item of data.bookings || []) {
          const card = add(target,'article','fd-history-version');
          add(card,'p','',`${date(item.date)} · ${money(item.amount,item.currency,true)} · Entfernt`);
          historyPanel(card,{account_id:accountKey(account),external_id:item.external_id});
        }
        if (!(data.bookings || []).length) add(target,'p','','Keine entfernten Buchungen vorhanden.');
      });
    }
  }
  function bookingRow(parent, item, account, index) {
    const row = add(parent, 'tr', 'fd-booking');
    row.dataset.timelineKey = `booking:${item.account_id}:${item.external_id}`;
    const dateCell = add(row, 'td', 'fd-booking-date', date(item.date));
    const transactionDate = item.date_basis === 'transaction_date';
    if (transactionDate) {
      dateCell.classList.add('fd-booking-transaction-date');
      add(dateCell, 'small', 'fd-booking-date-basis', 'PayPal-Transaktionsdatum');
    }
    const counterparty = add(row, 'td', 'fd-booking-counterparty');
    if (window.financeCounterparties) window.financeCounterparties.decorate(add(counterparty, 'span'), {...item,counterparty:recipientDisplay(item.counterparty)},
      {fallbackIcon: () => icon(categoryIcon(item.category))});
    else counterparty.textContent = recipientDisplay(item.counterparty) || item.description || 'Ohne Gegenpartei';
    add(counterparty, 'small', 'fd-row-account', `${accountName(account)} · ${item.currency || account.currency}`);
    window.financeBookingContracts?.decorate(counterparty,item);
    const description = add(row, 'td', 'fd-booking-description');
    add(description, 'span', '', item.description || '—');
    const components = componentRows(item,account.currency);
    if (components.length) {
      add(description, 'span', 'fd-booking-cash-split', `Davon ${components.map(({component,value}) =>
        `${componentName(component)} ${componentSummaryAmount(value,component.currency || item.currency || account.currency || 'EUR')}`
      ).join(' · ')}`);
    } else if (item.cash_split?.status === 'review_required') {
      add(description, 'span', 'fd-booking-cash-review', 'Bargeldanteil prüfen');
    }
    const category = add(row, 'td', 'fd-booking-category');
    category.dataset.categoryStatus = item.category_status || '';
    iconLabel(category, 'fd-category-name', categoryIcon(item.category), categoryLabel(item));
    add(row, 'td', `fd-booking-amount ${tone(item.amount)}`, money(item.amount, item.currency || account.currency, true));
    const balance = item.account_movement_balance || item.account_day_balance;
    const movement = Boolean(item.account_movement_balance);
    const knownBalance = !transactionDate && balance && cents(balance.amount) !== null && balance.date === item.date
      && balance.currency === (item.currency || account.currency);
    const running = add(row, 'td', 'fd-booking-balance', knownBalance ? money(balance.amount,balance.currency) : '—');
    running.title = knownBalance ? movement ? 'Berechnet je Buchung · Reihenfolge innerhalb eines Tages angenommen' : 'Berechneter Tagesstand · aus belegtem Stand und importierten Buchungen'
      : transactionDate ? 'PayPal-Transaktionsdatum: kein belegter Tagesstand' : 'Kein belegter Tagesstand';
    if (knownBalance) add(running, 'small', '', movement ? 'Berechnet' : 'Tagesstand');
    const action = add(row, 'td', 'fd-booking-action');
    const button = add(action, 'button', 'fd-booking-toggle', 'Details');
    button.type = 'button';
    button.setAttribute('aria-expanded', 'false');
    const detailRow = add(parent, 'tr', 'fd-booking-expanded');
    detailRow.hidden = !expandedBookings.has(bookingKey(item));
    button.setAttribute('aria-expanded',String(!detailRow.hidden));
    const detailCell = add(detailRow, 'td'); detailCell.colSpan = 7;
    const body = add(detailCell, 'div', 'fd-booking-detail');
    const detailId = `${area}-fd-booking-detail-${index}`;
    body.id = detailId;
    button.setAttribute('aria-controls', detailId);
    button.addEventListener('click', () => {
      detailRow.hidden = !detailRow.hidden;
      if (detailRow.hidden) expandedBookings.delete(bookingKey(item)); else expandedBookings.add(bookingKey(item));
      button.setAttribute('aria-expanded', String(!detailRow.hidden));
    });
    const detailFields = [['Konto',accountName(account)],['Gegenpartei', item.counterparty], ['Verwendungszweck', item.description], ['Kategorie', categoryLabel(item)]];
    const status = categoryStatusLabel(item.category_status);
    if (status) detailFields.push(['Kategoriestatus', status]);
    if (item.source_category && item.source_category !== item.category) detailFields.push(['Quellangabe', item.source_category]);
    detailFields.push([transactionDate ? 'PayPal-Transaktionsdatum' : 'Datum', date(item.date)]);
    if (knownBalance) detailFields.push([movement ? 'Berechnet je Buchung' : 'Berechneter Tagesstand', `${money(balance.amount,balance.currency)} · Basis ${date(balance.anchor_booked_on)}`]);
    for (const [label, value] of detailFields) {
      const row = add(body, 'div'); add(row, 'span', '', label); add(row, 'p', '', value || '—');
    }
    if (!transactionDate) add(body, 'p', 'fd-booking-balance-note', movement ? 'Berechnet je Buchung. Die Reihenfolge innerhalb eines Tages ist anhand der external_id angenommen, nicht von der Bank bestätigt. Die Berechnung setzt vollständige importierte Buchungen voraus und bleibt unabhängig von Buchungsfiltern.' : 'Tagesstand; keine belegte Reihenfolge innerhalb eines Tages. Die Berechnung setzt vollständige importierte Buchungen voraus.');
    window.financeBookingContracts?.mount(body,item);
    const transferActionMode = !item.is_transfer ? 'confirm'
      : item.single_leg_transfer ? 'revoke' : item.counterpart_missing ? 'confirm' : null;
    if (item.external_id != null && transferActionMode) {
      const transferAction = add(body, 'div', 'fd-booking-transfer-action');
      if (item.counterpart_missing && transferActionMode === 'confirm') add(transferAction, 'p', 'fd-booking-balance-note', 'Die Quelle nennt Umbuchung, eine Gegenbuchung fehlt. Die Buchung ist neutral gerechnet.');
      const transferButton = add(transferAction, 'button', `fd-booking-transfer-${transferActionMode}`,
        transferActionMode === 'revoke' ? 'Umbuchung aufheben'
          : item.is_transfer ? 'Als einseitige Umbuchung bestätigen' : 'Umbuchung bestätigen');
      transferButton.type = 'button';
      transferButton.addEventListener('click', () => {
        if (typeof window.BookingTransfers?.open !== 'function') {
          let error = transferAction.querySelector('[role="alert"]');
          if (!error) {
            error = add(transferAction, 'p', 'fd-booking-transfer-error', 'Die Umbuchungsbestätigung ist derzeit nicht verfügbar.');
            error.setAttribute('role', 'alert');
            error.setAttribute('aria-live', 'polite');
          }
          return;
        }
        const host = document.createElement('div');
        host.className = 'fd-booking-transfer-host';
        transferAction.append(host);
        window.BookingTransfers.open(host, item.account_id, item.external_id,
          transferActionMode === 'revoke' ? 'revoke' : undefined);
      });
    }
    if (!knownBalance) add(body, 'p', 'fd-booking-balance-note', transactionDate ? 'PayPal-Transaktionsdatum: kein Nachweis eines Tagesendstands.' : 'Der Tagesstand ist ohne eindeutige Saldenbasis oder bei bekannten Datenlücken nicht verfügbar.');
    const originalAmount = add(body, 'div', 'fd-booking-original-amount');
    add(originalAmount, 'span', '', 'Originalbetrag');
    add(originalAmount, 'p', '', money(item.amount,item.currency || account.currency,true));
    if (components.length) {
      const breakdown = add(body, 'div', 'fd-booking-component-breakdown');
      add(breakdown, 'span', '', 'Aufteilung des Buchungsbetrags');
      const list = add(breakdown, 'div', 'fd-booking-components');
      for (const {component,value} of components) {
        const line = add(list, 'p', 'fd-booking-component');
        add(line, 'span', '', `${componentName(component)} · ${categoryLabel({...component,category_status:component.category_status || item.category_status})}`);
        add(line, 'strong', '', money(component.amount,component.currency || item.currency || account.currency,true));
      }
      add(breakdown, 'p', 'fd-booking-cash-note', 'Die Bargeldauszahlung verschiebt Vermögen vom Girokonto in das Barvermögen; sie ist keine zusätzliche Ausgabe.');
    } else if (item.cash_split?.status === 'review_required') {
      add(body, 'p', 'fd-booking-cash-review-detail', 'Bargeldanteil prüfen. Die vorhandene Buchung bleibt unverändert, bis eine Aufteilung vorliegt.');
    }
    if (item.external_id != null) {
      historyPanel(body,{account_id:item.account_id,external_id:item.external_id});
      const edit = add(body, 'button', 'fd-booking-edit', 'Kategorie und Belege bearbeiten');
      edit.type = 'button';
      edit.addEventListener('click', () => document.dispatchEvent(new CustomEvent('finance-dashboard-edit',
        {detail:{account_id:item.account_id,external_id:item.external_id}})));
    }
  }
  function populateBookings(rows, supplied = null) {
    const savedScroll = rows.querySelector('.fd-table-scroll')?.scrollTop || pendingTimelineAnchor?.scroll || 0;
    const oldScroller = rows.querySelector('.fd-table-scroll');
    const oldAnchor = [...rows.querySelectorAll('[data-timeline-key]')].find(node => node.getBoundingClientRect().bottom > (oldScroller?.getBoundingClientRect().top || 0));
    const anchorKey = oldAnchor?.dataset.timelineKey || pendingTimelineAnchor?.key;
    const anchorTop = oldAnchor ? oldAnchor.getBoundingClientRect().top - oldScroller.getBoundingClientRect().top : pendingTimelineAnchor?.top;
    pendingTimelineAnchor = null;
    timelineObserver?.disconnect();
    rows.replaceChildren();
    const account = current();
    if (area === 'dashboard' ? !selectedAccounts.size : !account) {add(rows,'p','fd-empty','Kein Konto ausgewählt.'); return;}
    const term = query.trim();
    const list = supplied || filteredBookings();
    const expected = supplied ? [] : forecastEvents();
    add(rows, 'p', 'fd-result-count', `${list.length} Buchung${list.length === 1 ? '' : 'en'} · ${expected.length} Erwartung${expected.length === 1 ? '' : 'en'}`);
    if (!list.length && !expected.length) add(rows, 'p', 'fd-empty', term ? 'Keine Buchung oder Erwartung zum Suchtext.' : 'Keine Buchungen oder Erwartungen für diese Auswahl.');
    const scroller = add(rows, 'div', 'fd-table-scroll');
    scroller.tabIndex = 0;
    scroller.setAttribute('role', 'region');
    scroller.setAttribute('aria-label', 'Buchungstabelle');
    const table = add(scroller, 'table', 'fd-booking-table');
    add(table, 'caption', 'fd-table-caption', area === 'dashboard' ? `${selectedAccounts.size} Konto${selectedAccounts.size === 1 ? '' : 'en'} · Kontostände getrennt je Konto und Währung` : `${account.owner_label || account.owner || 'Konto'} · ${accountName(account)}`);
    const head = add(table, 'thead');
    const header = add(head, 'tr');
    for (const name of ['Datum', 'Gegenpartei', 'Zweck', 'Kategorie', 'Betrag', 'Kontostand', 'Details']) {
      const cell = add(header, 'th', '', name); cell.scope = 'col';
      if (name === 'Zweck') cell.title = 'Verwendungszweck';
      if (name === 'Kontostand') cell.title = 'Berechneter Kontostand je Buchung; ältere Daten ausdrücklich als Tagesstand, virtuelle Zahlungen als Prognose gekennzeichnet';
    }
    const body = add(table, 'tbody');
    let priorDay = null;
    const merged = [...list.map((item,index) => ({kind:'booking', item, index, day:item.date})),
      ...expected.map(item => ({kind:'forecast', item, day:item.expected_date}))]
      .sort(timelineOrder);
    let today = null;
    const addToday = () => {
      if (supplied || today) return;
      today = add(body, 'tr', 'fd-today'); today.tabIndex = -1; today.dataset.timelineKey = `today:${snapshot.as_of}`;
      const cell = add(today, 'th', '', `Heute · ${date(snapshot.as_of)}`); cell.colSpan = 7; cell.scope = 'row';
    };
    merged.forEach(({kind,item,index,day:dayValue}) => {
      if (dayValue > snapshot.as_of) addToday();
      if (dayValue !== priorDay) {
        const day = add(body, 'tr', 'fd-booking-day');
        const heading = add(day, 'th', '', date(dayValue));
        heading.colSpan = 7; heading.scope = 'row';
        priorDay = dayValue;
      }
      if (kind === 'booking') bookingRow(body, item, accounts().find(account => accountKey(account) === String(item.account_id)) || account, index);
      else forecastRow(body, item);
    });
    addToday();
    if (!supplied) {
      const prognosis = rows.closest('.fd-bookings')?.querySelector('.fd-prognosis-panel') || rows;
      prognosis.querySelector('.fd-timeline-more')?.remove();
      const more = add(prognosis, 'button', 'fd-timeline-more', 'Weitere 30 Tage'); more.type = 'button';
      more.disabled = forecast.loading || !forecast.enabled || forecast.until >= shiftDate(snapshot.as_of,366);
      if (forecast.until >= shiftDate(snapshot.as_of,366)) more.textContent = 'Prognosegrenze erreicht · 366 Tage';
      more.addEventListener('click', () => extendTimeline());
      let userScroll = false; let touchY = null;
      scroller.addEventListener('pointerdown', event => {if (event.target === scroller) userScroll = true;});
      scroller.addEventListener('wheel', event => {if (event.deltaY > 0) userScroll = true;}, {passive:true});
      scroller.addEventListener('touchstart', event => {touchY = event.touches[0]?.clientY ?? null;}, {passive:true});
      scroller.addEventListener('touchmove', event => {if (touchY !== null && event.touches[0]?.clientY < touchY - 8) userScroll = true;}, {passive:true});
      scroller.addEventListener('keydown', event => {if (['ArrowDown','PageDown','End',' '].includes(event.key)) userScroll = true;});
      scroller.addEventListener('scroll', () => {if (userScroll && scroller.scrollTop > lastTimelineScroll + 8) extensionArmed = true; lastTimelineScroll = scroller.scrollTop;}, {passive:true});
      const positionKey = JSON.stringify([snapshot.as_of,selectedIDs()]);
      requestAnimationFrame(() => {
        if (!rows.isConnected || !scroller.isConnected || area !== 'dashboard') return;
        const anchor = [...rows.querySelectorAll('[data-timeline-key]')].find(node => node.dataset.timelineKey === anchorKey);
        if (timelinePositioned !== positionKey && today) {
          scroller.scrollTop = Math.max(0,scroller.scrollTop + today.getBoundingClientRect().top - scroller.getBoundingClientRect().top);
          if (!forecast.enabled || forecast.data || forecast.error) timelinePositioned = positionKey;
        } else if (anchor && anchorTop !== undefined) scroller.scrollTop = scroller.scrollTop + anchor.getBoundingClientRect().top - scroller.getBoundingClientRect().top - anchorTop;
        else scroller.scrollTop = savedScroll;
        lastTimelineScroll = scroller.scrollTop; extensionArmed = false;
      });
      if ('IntersectionObserver' in window) {
        const key = forecastKey(); const request = forecast.request;
        timelineObserver = new IntersectionObserver(entries => {
          if (entries.some(entry => entry.isIntersecting) && extensionArmed && rows.isConnected
              && area === 'dashboard' && visibleArea === 'dashboard' && key === forecastKey() && request === forecast.request
              && !filters.category && !filters.status && filters.direction !== 'transfer'
              && scroller.scrollHeight > scroller.clientHeight + 24) extendTimeline();
        }, {root:scroller});
        const sentinel = add(body, 'tr', 'fd-timeline-sentinel'); const cell = add(sentinel,'td'); cell.colSpan = 7;
        timelineObserver.observe(sentinel);
      }
    }
  }
  function populateMain(main, preservedReview = null) {
    main.replaceChildren();
    main.append(area === 'category-outflows' ? categories() : bookingList());
    if (area === 'dashboard' && window.financePaymentReviewMount) {
      const scope = {as_of: snapshot.as_of,
        until: forecast.until || shiftDate(snapshot.as_of, 90), account_id: ''};
      const scopeKey = JSON.stringify(scope);
      // Account selection and presentation redraws do not change this all-account
      // scope. Keep the same mount, including its in-flight read and open evidence.
      if (preservedReview?.dataset.paymentReviewScope === scopeKey) main.append(preservedReview);
      else {
        if (preservedReview) paymentReviewMounts.get(preservedReview)?.destroy?.();
        const review = add(main, 'section', 'fd-payment-review');
        review.dataset.paymentReviewScope = scopeKey;
        paymentReviewMounts.set(review, window.financePaymentReviewMount(review, scope));
      }
    }
  }
  function paint() {
    const root = activeRoot();
    if (!root || !snapshot) return;
    if (!accounts().some(account => accountKey(account) === selected)) {
      const first = relevantAccounts()[0];
      selected = first ? accountKey(first) : null;
    }
    const previouslySelected = selectedAccounts.size;
    const available = new Set(accounts().map(accountKey));
    for (const id of selectedAccounts) if (!available.has(id)) selectedAccounts.delete(id);
    if (previouslySelected && !selectedAccounts.size) selectionInitialized = false;
    if (!selectionInitialized && selected) {selectedAccounts.add(selected); selectionInitialized = true;}
    root.querySelectorAll('details[data-disclosure]').forEach(node => disclosureStates.set(node.dataset.disclosure, node.open));
    const oldPicker = root.querySelector('.fd-account-picker');
    if (oldPicker) accountPickerState[oldPicker.dataset.viewport] = oldPicker.open;
    const oldOverview = root.querySelector('.fd-forecast-overview');
    if (oldOverview) forecastOverviewOpen = oldOverview.open;
    const oldScroller = root.querySelector('.fd-table-scroll');
    const oldAnchor = oldScroller && [...oldScroller.querySelectorAll('[data-timeline-key]')].find(node => node.getBoundingClientRect().bottom > oldScroller.getBoundingClientRect().top);
    pendingTimelineAnchor = oldScroller ? {scroll:oldScroller.scrollTop,key:oldAnchor?.dataset.timelineKey,top:oldAnchor ? oldAnchor.getBoundingClientRect().top - oldScroller.getBoundingClientRect().top : undefined} : null;
    const bankTools = area === 'dashboard' ? document.querySelector('#dashboard .fc-bank-tools') : null;
    const oldReview = area === 'dashboard' ? root.querySelector('.fd-payment-review') : null;
    document.body.dataset.financeView = view;
    root.replaceChildren(); root.className = `fd-root fd-${view}`;
    const hero = add(root, 'header', 'fd-hero');
    const intro = add(hero, 'div');
    add(intro, 'p', 'fd-kicker', 'FINANCE CONTROL · KONTEN');
    add(intro, 'h1', '', area === 'category-outflows' ? 'Abgänge nach Kategorie' : 'Buchungen');
    add(intro, 'p', 'fd-hero-sub', monthLabel());
    const options = disclosure(root, 'fd-more-options', 'Weitere Optionen', 'options');
    options.id = `${area}-options`;
    if (bankTools) options.append(bankTools);
    const basis = disclosure(options, 'fd-data-basis', 'Datengrundlage', 'basis');
    add(basis, 'p', '', 'Buchungen: importierte Buchungen · Kontostände: letzter belegter Stand');
    const controls = add(options, 'div', 'fd-controls');
    const cutoffForm = add(options, 'form', 'fd-cutoff-form');
    const cutoffLabel = add(cutoffForm, 'label'); add(cutoffLabel, 'span', '', 'Stand am');
    const cutoff = add(cutoffLabel, 'input'); cutoff.type = 'date'; cutoff.required = true;
    cutoff.value = String(snapshot.as_of || '').slice(0, 10);
    const cutoffSubmit = add(cutoffForm, 'button', '', 'Anzeigen'); cutoffSubmit.type = 'submit';
    cutoffForm.addEventListener('submit', event => {
      event.preventDefault();
      document.dispatchEvent(new CustomEvent('finance-dashboard-cutoff', {detail: {as_of: cutoff.value}}));
    });
    controls.prepend(window.financeViewSwitcher());
    if (snapshot.error) {
      const error = add(root, 'p', 'fd-note', snapshot.error);
      error.setAttribute('role', 'alert');
      return;
    }
    const layout = add(root, 'div', 'fd-layout');
    const side = add(layout, 'aside', 'fd-sidebar');
    const picker = add(side, 'details', 'fd-account-picker');
    picker.dataset.viewport = mobileQuery.matches ? 'mobile' : 'desktop';
    picker.open = accountPickerState[picker.dataset.viewport] ?? !mobileQuery.matches;
    picker.addEventListener('toggle', () => {if (picker.isConnected) accountPickerState[picker.dataset.viewport] = picker.open;});
    const sideHead = add(picker, 'summary', 'fd-side-head');
    add(sideHead, 'h2', '', 'Konten wählen');
    add(sideHead, 'span', 'fd-picker-count', area === 'dashboard' ? `${selectedAccounts.size} ausgewählt` : String(accounts().length));
    if (area === 'dashboard') {
      const choices = add(picker,'div','fd-account-selection-controls');
      const all = add(choices,'button','','Alle Konten wählen'); all.type = 'button';
      all.addEventListener('click', () => {for (const account of accounts()) selectedAccounts.add(accountKey(account)); invalidateForecast(); paint();});
      add(choices,'span','fd-selected-count',`${selectedAccounts.size} ausgewählt`);
    }
    picker.append(accountList('fd-account-list'));
    const main = add(layout, 'div', 'fd-main');
    populateMain(main, oldReview);
    for (const account of accounts().filter(item => includesAccount(accountKey(item)))) {
      const balance = account.current_balance;
      add(basis, 'p', '', `${accountName(account)} · Datenquelle: ${balance?.source || 'nicht angegeben'} · Stand: ${date(balance?.booked_on) || 'unbekannt'} · Abruf: ${balance?.retrieved_at || 'nicht angegeben'}`);
    }
    if (area === 'dashboard') removedHistory(options);
    root.append(options);
    const footer = add(root, 'p', 'fd-footer');
    footer.append('Für Haushaltsplan und verfügbare Mittel: ');
    const link = add(footer, 'a', '', 'Plan & Ist'); link.href = '#plan-actual';
  }
  // One persisted presentation mode for overview and account workspace.
  window.financeViewMode = () => view;
  window.financeViewSwitcher = () => {
    const switcher = el('div', 'fd-switch fc-view-switch');
    switcher.setAttribute('role', 'group'); switcher.setAttribute('aria-label', 'Ansicht');
    for (const [id, label] of [['guru', 'Finanzguru'], ['blick', 'Finanzblick']]) {
      const button = add(switcher, 'button', '', label); button.type = 'button';
      button.dataset.view = id; button.setAttribute('aria-pressed', String(view === id));
      button.addEventListener('click', () => {
        view = id; try { localStorage.setItem(key, id); } catch {}
        document.body.dataset.financeView = view;
        if (['dashboard', 'category-outflows'].includes(visibleArea)) paint();
        document.dispatchEvent(new CustomEvent('finance-view-changed'));
        const active = document.querySelector('main>section:not([hidden])');
        active?.querySelector('[data-view="' + id + '"]')?.focus({preventScroll:true});
      });
    }
    return switcher;
  };
  window.financeAccountIcon = account => {
    const brand = bankBrand(account);
    if (!brand) return icon(accountIcon(account.kind));
    const logo = el('img', 'fd-bank-logo');
    logo.src = {postbank:'/bank-postbank.svg',sparkasse:'/bank-sparkasse.png',ing:'/bank-ing.svg',paypal:'/bank-paypal.png'}[brand];
    logo.alt = ''; logo.setAttribute('aria-hidden','true'); return logo;
  };
  window.financeDashboardRender = (payload, requestedArea = 'dashboard') => {
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)
        || !Array.isArray(payload.accounts) || !Array.isArray(payload.bookings))
      throw new Error('Die Daten für die Buchungsansicht fehlen oder sind unvollständig.');
    const rootId = requestedArea === 'category-outflows' ? 'category-outflows-root' : 'dashboard-root';
    if (!document.getElementById(rootId))
      throw new Error('Der Anzeigebereich für die Buchungen fehlt.');
    if (requestedArea === 'category-outflows' && area === 'dashboard' && forecast.loading) {
      ++forecast.request; forecast.loading = false; forecast.key = '';
    }
    if (snapshot !== payload) {
      ++forecast.request; forecast.key = ''; forecast.data = null; forecast.error = ''; forecast.loading = false;
    }
    snapshot = payload;
    area = requestedArea === 'category-outflows' ? requestedArea : 'dashboard';
    const scroll = scrollPositions.get(area) || 0;
    paint();
    visibleArea = area;
    const renderedArea = area;
    requestAnimationFrame(() => {if (visibleArea === renderedArea) window.scrollTo(0,scroll);});
  };
  window.financeDashboardSelectAccount = id => {
    selected = String(id);
    selectedAccounts.clear(); selectedAccounts.add(selected); selectionInitialized = true; invalidateForecast();
    drilldown = null;
    if (typeof window.cockpitNavigate === 'function') window.cockpitNavigate('dashboard');
    else if (typeof cockpitNavigate === 'function') cockpitNavigate('dashboard');
    if (snapshot) {area = 'dashboard'; paint();}
  };
  document.addEventListener('finance-booking-transfer-changed', () => {
    if (typeof window.financeRefreshAfterImport === 'function') void window.financeRefreshAfterImport();
  });
  // app.js reloads the active area after refresh(). Hidden dashboards are
  // loaded on navigation, avoiding duplicate and invisible DOM rebuilds.
})();
