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
    if (parsed === null || typeof currency !== 'string' || !/^[A-Z]{3}$/.test(currency) || currency === 'XXX') return '—';
    try { return new Intl.NumberFormat('de-DE', {style: 'currency', currency}).format(parsed); }
    catch (_) { return '—'; }
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
      status(parent, 'Abruf nicht verfügbar.'); return;
    }
    const accounts = result.value.accounts;
    if (!accounts.length) { status(parent, 'Keine Wallet-Stände geliefert.'); return; }
    const labels = {CLBD: 'Gebuchter Abschlussstand', ITBD: 'Gebucht · untertägig',
      CLAV: 'Verfügbarer Abschlussstand', ITAV: 'Verfügbar · untertägig',
      XPCD: 'Anbietersaldo · XPCD'};
    const renderAccount = account => {
      const item = node('div', undefined, 'fc-home-paypal-item');
      item.append(node('strong', text(account?.name, 'Eigenes PayPal')));
      const balances = (Array.isArray(account?.balances) ? account.balances : [])
        .filter(balance => balance && Object.hasOwn(labels, balance.balance_type));
      if (!balances.length) status(item, '— · kein Stand geliefert.');
      const renderBalance = balance => {
        const row = node('div', undefined, 'fc-home-paypal-balance');
        row.append(node('span', labels[balance.balance_type]),
          node('strong', money(balance.amount, balance.currency), 'fc-home-paypal-amount'));
        const bankDate = date(balance.reference_date);
        const retrievedAt = balance.retrieved_at || result.value.retrieved_at;
        const retrievalDate = typeof retrievedAt === 'string' && Number.isFinite(Date.parse(retrievedAt))
          ? date(retrievedAt.slice(0, 10)) : null;
        row.append(node('small', bankDate ? 'Stand ' + bankDate
          : retrievalDate ? 'Abruf ' + retrievalDate : 'Zeitpunkt unbekannt', 'fc-home-paypal-meta muted'));
        const evidence = node('details', undefined, 'fc-home-paypal-evidence');
        evidence.append(node('summary', 'Hinweis'));
        evidence.append(node('p', `${bankDate ? 'Bankstichtag ' + bankDate : 'Kein Bankstichtag geliefert'}; Abruf ${retrieved(retrievedAt)}.`, 'fc-home-paypal-meta muted'));
        if (balance.balance_type === 'XPCD') evidence.append(node('p',
          'Momentansaldo des Anbieters; kein gebuchter Abschlussstand und keine Finance-Control-Prognose.',
          'fc-home-paypal-meta muted'));
        row.append(evidence);
        return row;
      };
      for (const balance of balances.slice(0, 4)) item.append(renderBalance(balance));
      if (balances.length > 4) {
        const moreBalances = node('details', undefined, 'fc-home-paypal-more');
        moreBalances.append(node('summary', `${balances.length - 4} weitere Salden`));
        for (const balance of balances.slice(4)) moreBalances.append(renderBalance(balance));
        item.append(moreBalances);
      }
      return item;
    };
    for (const account of accounts.slice(0, 3)) parent.append(renderAccount(account));
    if (accounts.length > 3) {
      const more = node('details', undefined, 'fc-home-paypal-more');
      more.append(node('summary', `${accounts.length - 3} weitere Wallets`));
      for (const account of accounts.slice(3)) more.append(renderAccount(account));
      parent.append(more);
    }
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
      const extra = node('details', undefined, 'fc-home-paypal-evidence');
      extra.append(node('summary', 'Details'));
      if (plan.outstanding_principal != null) extra.append(node('p', `Offenes Kapital: ${amount(plan.outstanding_principal) !== null && amount(plan.outstanding_principal) >= 0 ? money(plan.outstanding_principal, plan.currency) : '—'}`, 'fc-caption'));
      const derived = entry.derived || {}, due = date(derived.next_due_date);
      if (due || derived.next_payment_amount != null) extra.append(node('p', `Nächste Planrate: ${due || 'Termin unbekannt'} · ${amount(derived.next_payment_amount) !== null && amount(derived.next_payment_amount) >= 0 ? money(derived.next_payment_amount, plan.currency) : '—'}`, 'fc-caption'));
      if (extra.childElementCount > 1) item.append(extra);
      parent.append(item);
    }
    if (open.length > 3) {
      const more = node('details', undefined, 'fc-home-paypal-more');
      more.append(node('summary', `${open.length - 3} weitere Raten`));
      for (const entry of open.slice(3)) more.append(node('p', `${text(entry.plan.label, 'PayPal-Rate')} · ${money(entry.plan.remaining_total, entry.plan.currency)}`, 'fc-caption'));
      parent.append(more);
    }
    if (supported.length !== result.value.plans.length) {
      const note = node('details', undefined, 'fc-home-paypal-evidence');
      note.append(node('summary', 'Weitere Einträge'), node('p', 'Diese Einträge haben keine bestätigte PayPal-Portal-Quelle.', 'fc-caption'));
      parent.append(note);
    }
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
