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
  const api = async (route, data) => {
    try { return {ok: true, value: await window.api(`/api/administration/${route}`, data)}; }
    catch (error) { return {ok: false, code: typeof error?.code === 'string' ? error.code : ''}; }
  };
  const failure = code => ({
    stale_revision: 'Die Bankverbindung wurde geändert. Lade die Verwaltung neu.',
    unknown_connection: 'Die Bankverbindung ist nicht mehr verfügbar.',
    forbidden: 'Für diese Bankverbindung fehlt die Berechtigung.',
    invalid_target: 'Wähle ein gültiges Zielkonto.',
    invalid_bank: 'Diese Bankverbindung unterstützt diesen Kartenabruf nicht.',
    duplicate_card_target: 'Dieses Zielkonto ist bereits einer Karte zugeordnet.',
    invalid_card_metadata: 'Prüfe die Kartennummer und die optionalen Kartenangaben.',
    server_cards_unavailable: 'Der sichere Kartenspeicher ist derzeit nicht verfügbar.',
    bank_read_busy: 'Es läuft bereits ein Bankabruf.',
    bank_read_timeout: 'Die Einrichtung der Bankfreigabe hat das Zeitlimit erreicht.',
    auth_rejected: 'Die Bank hat die Anmeldung abgelehnt.',
    bank_failure: 'Die Bankfreigabe konnte nicht eingerichtet werden.',
    invalid_bank_auth_selection: 'Die gewählte TAN-Methode oder das TAN-Gerät ist nicht verfügbar.',
    card_operation_unsupported: 'Die Bank bietet den benötigten Kreditkartenabruf für diesen Zugang nicht an.',
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
    root.append(el('h3', 'Postbank Mastercard'),
      el('p', 'Karten hier zuordnen. Umsätze und Kontostand über „Konten aktualisieren“ abrufen.', 'pbc-note'));
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
    let activeAuth = 0;
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
    const configureCardAuth = async (cardId, auth, into) => {
      if (busy || !live() || !into.isConnected) return;
      const token = ++activeAuth;
      const current = () => live() && token === activeAuth && into.isConnected;
      into.replaceChildren();
      setBusy(true);
      setStatus('Bankfreigabe wird eingerichtet. Bestätige bei Bedarf in der Banking-App.');
      const today = new Date().toISOString().slice(0, 10);
      let state = await request('card-read', {...base(), confirmed:true, card_id:cardId,
        tan_method:auth.tan_method || null, tan_medium:auth.tan_medium || null,
        start:today, end:today});
      if (!current()) return;
      if (!state) { setBusy(false); return; }
      const deadline = Date.now() + 350000;
      while (current() && state.status === 'running' && Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        if (!current()) return;
        state = await request('card-read-state', {...base(), card_id:cardId});
        if (!current()) return;
        if (!state) { setBusy(false); return; }
      }
      if (!current()) return;
      if (state.status === 'running') {
        setBusy(false); setStatus(failure('bank_read_timeout'), true); return;
      }
      const result = state.status === 'complete' ? state.result : state;
      if (!result || result.status === 'error') {
        setBusy(false); setStatus(failure(result?.code), true); return;
      }
      if (['needs_method', 'needs_medium'].includes(result.status)) {
        setBusy(false);
        const method = result.status === 'needs_method';
        const options = (Array.isArray(result.options) ? result.options : [])
          .map(option => ({value:method ? option?.key : option?.label, label:option?.label}))
          .filter(option => typeof option.value === 'string' && option.value.length > 0
            && option.value.length <= 80 && typeof option.label === 'string'
            && option.label.length > 0 && option.label.length <= 80
            && !/[\x00-\x1f\x7f]/.test(option.value + option.label)
            && (!method || /^[0-9]{1,8}$/.test(option.value)));
        if (!options.length || options.length > 20) {
          setStatus('Die Bank hat keine auswählbare Freigabe geliefert.', true); return;
        }
        const panel = el('div', '', 'pbc-auth');
        const choice = select(method ? 'TAN-Methode auswählen' : 'TAN-Gerät auswählen', options);
        const accept = el('button', 'Auswahl übernehmen', 'pbc-secondary');
        accept.type = 'button'; accept.disabled = true;
        choice.addEventListener('change', () => { accept.disabled = !choice.value; });
        accept.addEventListener('click', () => {
          if (!choice.value || busy || !current()) return;
          void configureCardAuth(cardId, {...auth,
            ...(method ? {tan_method:choice.value} : {tan_medium:choice.value})}, into);
        });
        panel.append(field(method ? 'TAN-Methode' : 'TAN-Gerät', choice), accept);
        into.append(panel);
        setStatus('Wähle einmalig die von der Bank angebotene Freigabe.');
        return;
      }
      if (result.status !== 'ok') {
        setBusy(false); setStatus('Die Bankfreigabe konnte nicht sicher bestätigt werden.', true); return;
      }
      // This owner-only setup probe is never rendered or imported as a preview.
      const saved = await request('card-auth-save', {...base(), card_id:cardId, confirmed:true});
      if (!current()) return;
      if (!saved) { setBusy(false); return; }
      const refreshed = await reloadCards('Bankfreigabe gespeichert. Konten aktualisieren liest Umsätze und Kontostand.');
      if (!live()) return;
      setBusy(false);
      if (refreshed) changed();
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
        const remove = el('button', 'Zuordnung entfernen', 'pbc-danger');
        remove.type = 'button';
        remove.addEventListener('click', async () => {
          if (busy || !window.confirm('Diese Kartenzuordnung entfernen?')) return;
          setBusy(true);
          const answer = await request('card-delete', {...base(), confirmed: true, card_id: card.card_id});
          setBusy(false);
          if (answer && await reloadCards('Kartenzuordnung entfernt.')) changed();
        });
        const authPanel = el('div', '', 'pbc-auth-setup');
        if (card.auth_configured === false) {
          const configure = el('button', 'Bankfreigabe einrichten', 'pbc-secondary');
          configure.type = 'button';
          configure.addEventListener('click', () => { void configureCardAuth(card.card_id, {}, authPanel); });
          actions.append(configure);
        }
        actions.append(remove);
        item.append(actions, authPanel);
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
