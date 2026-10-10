'use strict';

(() => {
  const generations = new WeakMap();
  const sourceBindings = new WeakMap();
  const el = (tag, value = '', className = '') => {
    const item = document.createElement(tag);
    item.textContent = value == null ? '' : String(value);
    if (className) item.className = className;
    return item;
  };
  const text = (value, limit = 2000) => typeof value === 'string' ? value.slice(0, limit) : '';
  const isoDate = value => /^\d{4}-\d{2}-\d{2}$/.test(value || '') ? value : '';
  const api = async (route, data) => {
    try { return {ok: true, value: await window.api(`/api/administration/${route}`, data)}; }
    catch (error) { return {ok: false, code: typeof error?.code === 'string' ? error.code : ''}; }
  };
  const failure = code => ({
    stale_revision: 'Die Bankverbindung wurde geändert. Lade die Verwaltung neu.',
    unknown_connection: 'Die Bankverbindung ist nicht mehr verfügbar.',
    forbidden: 'Für diese Bankverbindung fehlt die Berechtigung.',
    invalid_target: 'Wähle ein gültiges Zielkonto.',
    invalid_bank_auth_selection: 'Die gewählte TAN-Methode oder das TAN-Gerät ist nicht verfügbar.',
    bank_read_busy: 'Es läuft bereits ein Bankabruf.',
    bank_read_timeout: 'Der Kartenabruf hat das Zeitlimit erreicht.',
    auth_rejected: 'Die Bank hat den Zugang abgelehnt.',
    bank_failure: 'Der Kartenabruf ist fehlgeschlagen.',
    invalid_bank_result: 'Die Bankantwort enthält unvollständige oder ungültige Kartendaten.',
    invalid_bank: 'Diese Bankverbindung unterstützt diesen Kartenabruf nicht.',
    card_operation_unsupported: 'Die Bank bietet den benötigten Kreditkartenabruf für diesen Zugang nicht an.',
    duplicate_card_target: 'Dieses Zielkonto ist bereits einer Karte zugeordnet.',
    invalid_card_metadata: 'Prüfe die Kartennummer und die optionalen Kartenangaben.',
    server_cards_unavailable: 'Der sichere Kartenspeicher ist derzeit nicht verfügbar.',
  })[code] || 'Die Anfrage konnte nicht abgeschlossen werden. Prüfe den Serverstatus.';
  const field = (label, input) => {
    const wrapper = el('label', '', 'pbc-field');
    wrapper.append(el('span', label), input);
    return wrapper;
  };
  const select = (placeholder, options) => {
    const input = el('select');
    const empty = el('option', placeholder);
    empty.value = '';
    input.append(empty);
    options.forEach(option => {
      const item = el('option', option.label);
      item.value = option.value;
      input.append(item);
    });
    input.value = '';
    return input;
  };
  const fmt = (amount, currency) => [text(String(amount ?? ''), 40), text(currency, 10)].filter(Boolean).join(' ');
  const normalizedPan = value => {
    if (typeof value !== 'string' || !/^[0-9 -]+$/.test(value)) return '';
    const digits = value.replace(/[ -]/g, '');
    return /^[0-9]{16}$/.test(digits) ? digits : '';
  };
  const sourceOptions = accounts => Array.isArray(accounts) ? accounts.map(account => ({
    value: typeof account?.fingerprint === 'string' && /^[a-f0-9]{64}$/i.test(account.fingerprint)
      ? account.fingerprint : '',
    label: text(account?.masked_account, 120),
  })).filter(account => account.value && account.label) : [];

  function bind(host, record, onChanged = () => {}) {
    if (!host || typeof host.replaceChildren !== 'function') return;
    const generation = (generations.get(host) || 0) + 1;
    generations.set(host, generation);
    sourceBindings.delete(host);
    host.replaceChildren();
    if (record?.bankId !== 'POSTBANK') return;
    const root = el('section', '', 'pbc-root');
    root.append(el('h3', 'Postbank Mastercard'), el('p', 'Karten zuordnen und Umsätze lesen.', 'pbc-note'));
    const status = el('p', 'Karten werden geladen.', 'pbc-status');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    const body = el('div', '', 'pbc-body');
    root.append(status, body);
    host.append(root);
    const live = () => host.isConnected && generations.get(host) === generation && host.contains(root);
    const setStatus = (message, error = false) => {
      if (!live()) return;
      status.textContent = message;
      status.classList.toggle('pbc-error', error);
    };
    if (!record?.id || !Number.isSafeInteger(record.revision) || record.revision < 0) {
      setStatus('Die Bankverbindung ist nicht verfügbar.', true);
      return;
    }
    const base = () => ({id: record.id, revision: record.revision});
    let availableSources = sourceOptions(record.sourceAccounts);
    let sourceSelect = null;
    const updateSources = accounts => {
      availableSources = sourceOptions(accounts);
      if (sourceSelect) {
        const replacement = select('Keine Kontoverbindung wählen', availableSources);
        sourceSelect.replaceChildren(...replacement.childNodes);
        sourceSelect.value = '';
      }
    };
    sourceBindings.set(host, {id: record.id, revision: record.revision, generation, updateSources});
    let busy = false;
    let activeRead = 0;
    let disabledBeforeBusy = new Map();
    const setBusy = value => {
      if (value === busy) return;
      busy = value;
      if (value) {
        disabledBeforeBusy = new Map([...root.querySelectorAll('button,select,input')]
          .map(input => [input, input.disabled]));
        disabledBeforeBusy.forEach((_, input) => { input.disabled = true; });
      } else {
        disabledBeforeBusy.forEach((disabled, input) => {
          if (input.isConnected) input.disabled = disabled;
        });
        disabledBeforeBusy.clear();
      }
    };
    const changed = () => { if (live()) onChanged(); };
    const request = async (route, payload) => {
      const answer = await api(route, payload);
      if (!live()) return null;
      if (!answer.ok) setStatus(failure(answer.code), true);
      return answer.ok ? answer.value : null;
    };
    const note = (name, value, target) => {
      if (value === null || value === undefined || value === '') return;
      const row = el('p', '', 'pbc-detail');
      row.append(el('strong', `${name}: `), el('span', Array.isArray(value) ? value.map(item => text(item)).join(' · ') : text(String(value))));
      target.append(row);
    };
    const renderResult = value => {
      const box = el('section', '', 'pbc-result');
      box.append(el('h4', 'Leseansicht – noch keine Übernahme in Buchungen'));
      const balance = value?.balance;
      const billingDate = (label, balanceValue, transactionValue) => {
        const fromBalance = isoDate(balanceValue);
        const fromTransactions = isoDate(transactionValue);
        if (fromBalance && fromTransactions && fromBalance !== fromTransactions) {
          note(`${label} (Saldoabruf)`, fromBalance, box);
          note(`${label} (Umsatzabruf)`, fromTransactions, box);
        } else note(label, fromBalance || fromTransactions, box);
      };
      if (balance && typeof balance === 'object') {
        note('Aktueller Kartenstand', fmt(balance.amount, balance.currency), box);
        note('Stand vom', isoDate(balance.as_of), box);
        note('Verfügbar', balance.available_amount == null ? '' : fmt(balance.available_amount, balance.available_currency), box);
        note('Offene Autorisierungen', balance.open_authorizations, box);
        note('Kreditrahmen', balance.credit_limit, box);
      }
      billingDate('Letzte Abrechnung', balance?.last_billing_date, value?.last_billing_date);
      billingDate('Nächste Abrechnung', balance?.expected_billing_date, value?.expected_billing_date);
      const rows = Array.isArray(value?.transactions) ? value.transactions : [];
      box.append(el('p', `${rows.length} Kartenumsätze gelesen.`, 'pbc-note'));
      const list = el('div', '', 'pbc-rows');
      rows.forEach(row => {
        if (!row || typeof row !== 'object') return;
        const item = el('article', '', 'pbc-row');
        const top = el('div', '', 'pbc-row-top');
        top.append(el('strong', text(row.merchant_name) || 'Kartenumsatz'), el('strong', fmt(row.amount, row.currency)));
        item.append(top);
        note('Belegdatum', isoDate(row.receipt_date), item);
        note('Buchungstag', isoDate(row.booking_date), item);
        note('Abrechnungsdatum', isoDate(row.billing_date), item);
        note('Wertstellung', isoDate(row.value_date), item);
        if (row.billed === true) note('Status', 'Abgerechnet', item);
        if (row.billed === false) note('Status', 'Noch nicht abgerechnet', item);
        if (Array.isArray(row.descriptions)) row.descriptions.forEach((pair, index) => {
          if (Array.isArray(pair)) pair.forEach((part, partIndex) => note(`Beschreibung ${index + 1}${partIndex ? ' Zusatz' : ''}`, part, item));
        });
        note('Land', row.country_code, item);
        note('Originalbetrag', row.original_amount == null ? '' : fmt(row.original_amount, row.original_currency), item);
        note('Wechselkurs', row.original_exchange_rate, item);
        note('Abrechnung', row.billing_label, item);
        note('Gebührencode', row.fee_code, item);
        note('Geldautomaten-Gebühr', row.atm_fee_reference, item);
        note('Auslandseinsatz-Gebühr', row.foreign_use_fee_reference, item);
        note('Terminal', row.terminal_id, item);
        note('Buchungsreferenz', row.booking_reference, item);
        list.append(item);
      });
      box.append(list);
      return box;
    };
    const renderAuth = (result, retry, into) => {
      const method = result.status === 'needs_method';
      const options = Array.isArray(result.options) ? result.options : [];
      const choices = options.map(option => ({value: text(option?.key ?? option?.label, 100), label: text(option?.label ?? option?.key, 160)}))
        .filter(option => option.value && option.label);
      if (!choices.length) { setStatus('Die Bank hat keine auswählbare Freigabe geliefert.', true); return; }
      const panel = el('div', '', 'pbc-auth');
      const choice = select(method ? 'TAN-Methode auswählen' : 'TAN-Gerät auswählen', choices);
      const button = el('button', 'Auswahl übernehmen', 'pbc-secondary');
      button.type = 'button';
      button.disabled = true;
      choice.addEventListener('change', () => { button.disabled = !choice.value; });
      button.addEventListener('click', () => {
        if (!choice.value || busy) return;
        retry(method ? {tan_method: choice.value} : {tan_medium: choice.value});
      });
      panel.append(field(method ? 'TAN-Methode' : 'TAN-Gerät', choice), button);
      into.replaceChildren(panel);
      setStatus('Die Bank verlangt eine Freigabe. Triff eine ausdrückliche Auswahl.');
    };
    const runRead = async (cardId, period, auth, into) => {
      if (busy || !live()) return;
      const token = ++activeRead;
      into.replaceChildren();
      setBusy(true);
      setStatus('Kartenumsätze werden gelesen. Bestätige bei Bedarf in der Banking-App.');
      const payload = {...base(), confirmed: true, card_id: cardId,
        tan_method: auth.tan_method || null, tan_medium: auth.tan_medium || null,
        start: period.start, end: period.end};
      const start = await request('card-read', payload);
      if (!live() || token !== activeRead) return;
      if (!start) { setBusy(false); return; }
      const deadline = Date.now() + 350000;
      let state = start;
      while (live() && token === activeRead && state?.status === 'running' && Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        if (!live() || token !== activeRead) return;
        state = await request('card-read-state', {...base(), card_id: cardId});
        if (!state) { setBusy(false); return; }
      }
      if (!live() || token !== activeRead) return;
      setBusy(false);
      if (state?.status === 'running') { setStatus('Der Kartenabruf hat das Zeitlimit erreicht.', true); return; }
      if (state?.status === 'error') { setStatus(failure(state.code), true); return; }
      const result = state?.result ?? state;
      if (result?.status === 'error') { setStatus(failure(result.code), true); return; }
      if (result?.status === 'needs_method' || result?.status === 'needs_medium') {
        renderAuth(result, selection => runRead(cardId, period, {...auth, ...selection}, into), into);
      } else if (result?.status === 'ok') {
        into.replaceChildren(renderResult(result));
        setStatus('Kartenumsätze wurden gelesen. Es wurden keine Buchungen übernommen.');
      } else setStatus('Die Bankantwort konnte nicht sicher angezeigt werden.', true);
    };
    const render = data => {
      body.replaceChildren();
      const targets = Array.isArray(data.targets) ? data.targets : [];
      const cards = Array.isArray(data.cards) ? data.cards.filter(card => card?.connection_id === record.id) : [];
      const form = el('section', '', 'pbc-panel');
      form.append(el('h4', 'Mastercard zuordnen'));
      const target = select('Zielkonto ausdrücklich wählen', targets.map(item => ({
        value: text(item?.id, 120), label: [text(item?.name, 120), text(item?.owner, 120), text(item?.currency, 10)].filter(Boolean).join(' · '),
      })).filter(item => item.value && item.label));
      const pan = el('input');
      pan.type = 'password';
      pan.inputMode = 'numeric';
      pan.autocomplete = 'off';
      pan.spellcheck = false;
      pan.maxLength = 32;
      const cardAccount = el('input');
      cardAccount.type = 'text';
      cardAccount.maxLength = 30;
      cardAccount.autocomplete = 'off';
      sourceSelect = select('Keine Kontoverbindung wählen', availableSources);
      const confirm = el('input');
      confirm.type = 'checkbox';
      const confirmation = field('Ich bestätige die Zuordnung dieser Karte zum gewählten Zielkonto.', confirm);
      confirmation.classList.add('pbc-confirm');
      const save = el('button', 'Karte speichern', 'pbc-primary');
      save.type = 'button';
      const updateSave = () => { save.disabled = busy || !target.value || !normalizedPan(pan.value) || !confirm.checked; };
      [target, pan, confirm].forEach(input => input.addEventListener('input', updateSave));
      save.addEventListener('click', async () => {
        if (busy || !target.value || !normalizedPan(pan.value) || !confirm.checked) return;
        const number = normalizedPan(pan.value);
        pan.value = '';
        updateSave();
        setBusy(true);
        const answer = await request('card-save', {...base(), confirmed: true, account_id: target.value,
          card_number: number, card_account_number: cardAccount.value.trim() || null,
          account_fingerprint: sourceSelect.value || null});
        setBusy(false);
        updateSave();
        if (answer && await reloadCards('Kartenzuordnung gespeichert.')) changed();
      });
      form.append(field('Zielkonto', target), field('Kartennummer (16 Ziffern)', pan),
        field('Kartenkonto-ID (optional, max. 30 Zeichen)', cardAccount),
        field('Bankkontoverknüpfung (optional)', sourceSelect),
        el('p', 'Falls Bank eine Kontoverbindung verlangt, zuerst Bankkonten oben abrufen und hier ausdrücklich wählen.', 'pbc-note'),
        confirmation, save);
      body.append(form);
      updateSave();
      const list = el('section', '', 'pbc-panel');
      list.append(el('h4', 'Gespeicherte Karten'));
      if (!cards.length) list.append(el('p', 'Noch keine Karte zugeordnet.', 'pbc-note'));
      cards.forEach(card => {
        if (!card || typeof card.card_id !== 'string') return;
        const item = el('article', '', 'pbc-card');
        item.append(el('strong', text(card.masked_number, 80) || 'Mastercard'));
        const match = targets.find(targetItem => targetItem.id === card.account_id);
        note('Zielkonto', match ? text(match.name, 120) : 'Nicht mehr in der Auswahlliste', item);
        const actions = el('div', '', 'pbc-actions');
        const read = el('button', 'Mastercard-Umsätze lesen', 'pbc-secondary');
        read.type = 'button';
        const remove = el('button', 'Zuordnung entfernen', 'pbc-danger');
        remove.type = 'button';
        const periodPanel = el('div', '', 'pbc-period');
        const resultPanel = el('div', '', 'pbc-read-output');
        read.addEventListener('click', () => {
          if (busy) return;
          periodPanel.replaceChildren();
          resultPanel.replaceChildren();
          const start = el('input'); start.type = 'date';
          const end = el('input'); end.type = 'date';
          const go = el('button', 'Zeitraum lesen', 'pbc-primary'); go.type = 'button'; go.disabled = true;
          const update = () => { go.disabled = !isoDate(start.value) || !isoDate(end.value) || start.value > end.value; };
          start.addEventListener('input', update); end.addEventListener('input', update);
          go.addEventListener('click', () => { if (!go.disabled) runRead(card.card_id, {start: start.value, end: end.value}, {}, resultPanel); });
          periodPanel.append(field('Von', start), field('Bis', end), go);
          setStatus('Wähle den Lesezeitraum der Karte.');
        });
        remove.addEventListener('click', async () => {
          if (busy || !window.confirm('Diese Kartenzuordnung entfernen?')) return;
          setBusy(true);
          const answer = await request('card-delete', {...base(), confirmed: true, card_id: card.card_id});
          setBusy(false);
          if (answer && await reloadCards('Kartenzuordnung entfernt.')) changed();
        });
        actions.append(read, remove);
        item.append(actions, periodPanel, resultPanel);
        list.append(item);
      });
      body.append(list);
    };
    const reloadCards = async (message = 'Kartenzuordnungen geladen.') => {
      const data = await request('card-state', base());
      if (!live() || !data) return;
      if (data.supported !== true) { setStatus('Kartenabruf für diese Verbindung nicht verfügbar.'); return; }
      render(data);
      setStatus(message);
      return true;
    };
    reloadCards();
  }
  function setSourceAccounts(host, record, accounts) {
    const binding = sourceBindings.get(host);
    if (!binding || binding.generation !== generations.get(host)
        || binding.id !== record?.id || binding.revision !== record?.revision
        || record?.bankId !== 'POSTBANK' || !host?.isConnected) return false;
    binding.updateSources(accounts);
    return true;
  }
  window.postbankCards = {bind, setSourceAccounts};
})();
