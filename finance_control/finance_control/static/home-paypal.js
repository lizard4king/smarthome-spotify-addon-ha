'use strict';
// Owner-scoped display only. Never aggregate wallet balances or credit plans.
(() => {
  const requests = new WeakMap();
  const node = (tag, text, className = '') => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const text = (value, fallback) => typeof value === 'string' && value.trim()
    ? value.slice(0, 160) : fallback;
  const amount = value => typeof value === 'string' && /^-?(?:0|[1-9]\d*)(?:\.\d{1,2})?$/.test(value)
    && Number.isFinite(Number(value)) ? Number(value) : null;
  const money = (value, currency) => {
    const parsed = amount(value);
    if (parsed === null || typeof currency !== 'string' || !/^[A-Z]{3}$/.test(currency) || currency === 'XXX') return 'Stand unbekannt';
    try { return new Intl.NumberFormat('de-DE', {style: 'currency', currency}).format(parsed); }
    catch (_) { return 'Stand unbekannt'; }
  };
  const date = value => {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    const parsed = new Date(`${value}T12:00:00Z`);
    return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value
      ? parsed.toLocaleDateString('de-DE', {day: '2-digit', month: '2-digit', year: 'numeric', timeZone: 'UTC'}) : null;
  };
  const retrieved = value => {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T/.test(value) || !Number.isFinite(Date.parse(value))) return 'Zeitpunkt unbekannt';
    return new Date(value).toLocaleString('de-DE');
  };
  const status = (parent, message) => parent.append(node('p', message, 'fc-home-paypal-status muted'));
  function wallet(parent, result) {
    parent.append(node('h4', 'PayPal-Wallet'));
    if (result.status !== 'fulfilled' || !Array.isArray(result.value?.accounts)) {
      status(parent, 'Wallet-Stände nicht verfügbar.'); return;
    }
    const accounts = result.value.accounts;
    if (!accounts.length) { status(parent, 'Keine PayPal-Wallet-Stände gespeichert.'); return; }
    const labels = {CLBD: 'Gebuchter Abschlussstand', ITBD: 'Gebucht · untertägig',
      CLAV: 'Verfügbarer Abschlussstand', ITAV: 'Verfügbar · untertägig',
      XPCD: 'Anbietersaldo · XPCD'};
    for (const account of accounts.slice(0, 3)) {
      const item = node('div', undefined, 'fc-home-paypal-item');
      item.append(node('strong', text(account?.name, 'Eigenes PayPal')));
      const balances = (Array.isArray(account?.balances) ? account.balances : [])
        .filter(balance => balance && Object.hasOwn(labels, balance.balance_type));
      if (!balances.length) status(item, 'Stand unbekannt · kein unterstützter Wallet-Stand geliefert.');
      for (const balance of balances.slice(0, 4)) {
        const row = node('div', undefined, 'fc-home-paypal-balance');
        row.append(node('span', labels[balance.balance_type]),
          node('strong', money(balance.amount, balance.currency), 'fc-home-paypal-amount'));
        if (balance.balance_type === 'XPCD') row.append(node('small',
          'Momentansaldo des Anbieters; kein gebuchter Abschlussstand und keine Finance-Control-Prognose.',
          'fc-home-paypal-meta muted'));
        const bankDate = date(balance.reference_date);
        row.append(node('small', `${bankDate ? 'Bankstichtag ' + bankDate : 'Bankstichtag nicht geliefert'}; abgerufen ${retrieved(balance.retrieved_at || result.value.retrieved_at)}`, 'fc-home-paypal-meta muted'));
        item.append(row);
      }
      if (balances.length > 4) status(item, `Weitere ${balances.length - 4} Wallet-Salden unter PayPal-Details.`);
      parent.append(item);
    }
    if (accounts.length > 3) status(parent, `Weitere ${accounts.length - 3} PayPal-Konten unter PayPal-Details.`);
  }
  function credits(parent, result) {
    parent.append(node('h4', 'Offene Raten inkl. künftiger Zinsen'));
    if (result.status !== 'fulfilled' || !Array.isArray(result.value?.plans)) {
      status(parent, 'Ratenstände nicht verfügbar.'); return;
    }
    const supported = result.value.plans.filter(entry => entry?.source === 'manual_paypal_portal' && entry.plan && typeof entry.plan === 'object');
    const open = supported.filter(entry => amount(entry.plan.remaining_total) !== 0);
    if (!open.length) status(parent, 'Keine offenen, manuell geprüften Ratenstände erfasst.');
    for (const entry of open.slice(0, 3)) {
      const plan = entry.plan, item = node('div', undefined, 'fc-home-paypal-item');
      item.append(node('strong', text(plan.label, 'PayPal-Ratenzahlung')),
        node('strong', amount(plan.remaining_total) !== null && amount(plan.remaining_total) >= 0
          ? money(plan.remaining_total, plan.currency) : 'Stand unbekannt', 'fc-home-paypal-amount'),
        node('small', `Manuell geprüft · Stand ${date(plan.as_of) || 'nicht ausgewiesen'} · Quelle: PayPal-Portal`, 'fc-home-paypal-meta muted'));
      if (plan.outstanding_principal != null) item.append(node('p', `Offenes Kapital ohne künftige Zinsen: ${amount(plan.outstanding_principal) !== null && amount(plan.outstanding_principal) >= 0 ? money(plan.outstanding_principal, plan.currency) : 'Stand unbekannt'}`, 'fc-caption'));
      const derived = entry.derived || {}, due = date(derived.next_due_date);
      if (due || derived.next_payment_amount != null) item.append(node('p', `Nächste Planrate: ${due || 'Termin unbekannt'} · ${amount(derived.next_payment_amount) !== null && amount(derived.next_payment_amount) >= 0 ? money(derived.next_payment_amount, plan.currency) : 'Betrag unbekannt'}`, 'fc-caption'));
      parent.append(item);
    }
    if (open.length > 3) status(parent, `Weitere ${open.length - 3} Ratenstände unter PayPal-Details.`);
    if (supported.length !== result.value.plans.length) status(parent, 'Weitere Einträge haben keine bestätigte PayPal-Portal-Quelle.');
    parent.append(node('p', 'Gespeicherte, datierte Angaben; kein aktueller Bankstand. Wallet und Raten werden getrennt ausgewiesen.', 'fc-caption'));
  }
  window.financePayPalOverviewLoad = async host => {
    if (!host?.isConnected || document.getElementById('home-paypal') !== host) return;
    const request = Symbol(); requests.set(host, request);
    const head = node('div', undefined, 'section-head fc-home-paypal-head');
    const link = node('a', 'PayPal-Details', 'fc-text-link'); link.href = '#dashboard';
    head.append(node('h3', 'Eigene PayPal-Stände'), link);
    const walletHost = node('div', undefined, 'fc-home-paypal-wallet');
    const creditHost = node('div', undefined, 'fc-home-paypal-credits');
    walletHost.setAttribute('role', 'status'); creditHost.setAttribute('role', 'status');
    status(walletHost, 'Wallet-Stände werden geladen …'); status(creditHost, 'Ratenstände werden geladen …');
    host.replaceChildren(head, walletHost, creditHost);
    const results = await Promise.allSettled([
      Promise.resolve().then(() => window.api('/api/administration/paypal-balances', {})),
      Promise.resolve().then(() => window.api('/api/administration/paypal-credit-list', {})),
    ]);
    if (!host.isConnected || document.getElementById('home-paypal') !== host || requests.get(host) !== request) return;
    walletHost.replaceChildren(); creditHost.replaceChildren();
    wallet(walletHost, results[0]); credits(creditHost, results[1]);
  };
})();
