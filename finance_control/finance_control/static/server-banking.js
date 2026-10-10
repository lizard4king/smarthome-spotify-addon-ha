'use strict';

(() => {
  const renderGenerations = new WeakMap();
  const node = (tag, text = '', className = '') => {
    const element = document.createElement(tag);
    element.textContent = text;
    if (className) element.className = className;
    return element;
  };

  const validConnection = connection => connection && typeof connection.id === 'string' &&
    connection.id.length > 0 && Number.isSafeInteger(connection.revision) && connection.revision >= 0;

  const diagnosticText = (code, diagnostic) => {
    if (!['bank_failure', 'invalid_bank_result', 'authorization_required', 'auth_rejected'].includes(code) || !diagnostic ||
        typeof diagnostic !== 'object' || Array.isArray(diagnostic) ||
        !Object.hasOwn(diagnostic, 'stage') ||
        Object.keys(diagnostic).some(key => !['stage', 'bank_error_code'].includes(key))) return '';
    const stages = {accounts: 'Beim Lesen der Kontenliste.', balance: 'Beim Lesen des Kontostands.'};
    if (typeof diagnostic.stage !== 'string' || !Object.hasOwn(stages, diagnostic.stage)) return '';
    if (!Object.hasOwn(diagnostic, 'bank_error_code')) return ` ${stages[diagnostic.stage]}`;
    if (!['bank_failure', 'invalid_bank_result'].includes(code) || typeof diagnostic.bank_error_code !== 'string') return '';
    const details = {
      ONLINE_LOGIN_REQUIRED: 'Die Bank verlangt eine erneute Online-Anmeldung.',
      CREDENTIALS_REJECTED: 'Die Bank hat die Zugangsdaten abgelehnt.',
      AUTH_TEMPORARY: 'Die Bankfreigabe ist vorübergehend nicht verfügbar.',
      UNSUPPORTED: 'Die Bank unterstützt diesen Abruf nicht.',
      CONNECTION: 'Die Verbindung zur Bank ist fehlgeschlagen.',
      TIMEOUT: 'Die Bank hat nicht rechtzeitig geantwortet.',
      TLS: 'Die gesicherte Verbindung zur Bank ist fehlgeschlagen.',
      DIALOG_INIT: 'Der Bankdialog konnte nicht gestartet werden.',
      NO_RESPONSE: 'Die Bank hat keine Antwort geliefert.',
      BANK_REJECTED: 'Die Bank hat die Anfrage abgelehnt.',
      UNKNOWN: 'Die Bank meldet einen nicht näher bestimmten Fehler.',
      DATA_FORMAT: 'Das Datenformat der Bankantwort ist ungültig.',
      IDENTIFICATION_FORMAT: 'Die Bank konnte die Identifikation nicht verarbeiten.',
      PRODUCT_FORMAT: 'Die Bank konnte die Produktkennung nicht verarbeiten.',
      STATEMENT_INCOMPLETE: 'Der Kontoauszug ist unvollständig.',
      STATEMENT_FORMAT: 'Das Format des Kontoauszugs ist ungültig.',
      STATEMENT_ID_MISSING: 'Im Kontoauszug fehlt eine Buchungskennung.',
    };
    if (!Object.hasOwn(details, diagnostic.bank_error_code)) return '';
    return ` ${stages[diagnostic.stage]} ${details[diagnostic.bank_error_code]}`;
  };
  const balanceError = (code, diagnostic) => {
    const messages = {
    authorization_required: 'Die Freigabe für den Bankabruf fehlt.',
    auth_rejected: 'Die Bank hat die Anmeldung abgelehnt. Prüfe den gespeicherten Zugang.',
    bank_failure: 'Der Bankabruf ist fehlgeschlagen. Die genaue Ursache ist noch unbekannt. (bank_failure)',
    invalid_bank_result: 'Die Bankantwort konnte nicht verarbeitet werden. (invalid_bank_result)',
    bank_read_unavailable: 'Der Bankabruf ist auf dem Server derzeit nicht verfügbar. (bank_read_unavailable)',
    server_banking_unsupported: 'Der Server unterstützt diesen Bankabruf nicht.',
    unsupported_platform: 'Diese Plattform unterstützt den Bankabruf nicht.',
    too_many_accounts: 'Die Bank liefert mehr Konten als unterstützt. (too_many_accounts)',
    duplicate_account: 'Die Bank liefert ein Konto mehrfach. Die Kontostände sind nicht eindeutig zuordenbar. (duplicate_account)',
    stale_revision: 'Die Bankverbindung wurde geändert. Lade die Verwaltung neu und starte erneut.',
    unknown_connection: 'Die Bankverbindung ist nicht mehr verfügbar.',
    forbidden: 'Du darfst diese Bankverbindung nicht abrufen.',
    unauthorized: 'Die Serversitzung ist abgelaufen. Melde Dich erneut an.',
    invalid_bank: 'Diese Bank wird für den Abruf nicht unterstützt.',
    invalid_request: 'Die Abrufanfrage ist ungültig. Lade die Verwaltung neu. (invalid_request)',
    bank_read_timeout: 'Der Bankabruf hat das Zeitlimit erreicht. Es wurde kein neuer Abruf gestartet.',
    bank_read_busy: 'Für diese Bankverbindung läuft bereits ein Abruf.',
    vault_unavailable: 'Der sichere Serverspeicher ist derzeit nicht verfügbar.',
    bank_product_unavailable: 'Die FinTS-Produktkennung fehlt auf dem Server.',
    invalid_bank_auth_selection: 'Die gewählte TAN-Methode oder das TAN-Gerät ist für diese Bank nicht verfügbar.',
    };
    const base = typeof code === 'string' && Object.hasOwn(messages, code)
      ? messages[code] : 'Kontostände konnten nicht abgerufen werden. Bitte prüfe den Serverstatus.';
    const detail = diagnosticText(code, diagnostic);
    return (code === 'bank_failure' && detail ? 'Der Bankabruf ist fehlgeschlagen.' : base) + detail;
  };

  const safeText = (value, max = 128) => typeof value === 'string' && value.length <= max ? value : '';

  function setStatus(status, message, error = false) {
    status.textContent = message;
    status.classList.toggle('fsb-error', error);
  }

  function recordLabel(connection, index) {
    return typeof connection.label === 'string' && connection.label.trim()
      ? connection.label.trim() : `Bankverbindung ${index + 1}`;
  }

  async function render(host, connections = [], onChanged = () => {}) {
    if (!host || typeof host.replaceChildren !== 'function') return;
    const generation = (renderGenerations.get(host) || 0) + 1;
    renderGenerations.set(host, generation);
    host.replaceChildren();
    host.classList.add('fsb-host');
    const root = node('div', '', 'fsb-root');
    const status = node('p', 'Serverzugänge werden geladen.', 'fsb-status');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    root.append(status);
    host.append(root);
    const isCurrent = () => host.isConnected && renderGenerations.get(host) === generation && host.contains(root);

    let serverState;
    try {
      serverState = await window.api('/api/administration/bank-credentials-state', {});
    } catch (_) {
      if (!isCurrent()) return;
      setStatus(status, 'Serverzugänge konnten nicht geladen werden.', true);
      return;
    }
    if (!isCurrent()) return;

    if (!serverState || serverState.supported !== true) {
      root.append(node('p', 'Serverseitiges Speichern wird auf dieser Installation nicht unterstützt.', 'fsb-notice'));
      setStatus(status, '');
      return;
    }
    if (!Array.isArray(serverState.connections) || !Array.isArray(connections)) {
      setStatus(status, 'Serverzugänge konnten nicht geladen werden.', true);
      return;
    }

    const known = new Map();
    connections.forEach((connection, index) => {
      if (!validConnection(connection) || known.has(connection.id)) return;
      known.set(connection.id, {
        revision: connection.revision,
        label: recordLabel(connection, index),
        bankId: typeof connection.bank_id === 'string' ? connection.bank_id : '',
      });
    });

    const validState = serverState.connections.filter(validConnection);
    const counts = new Map();
    validState.forEach(connection => counts.set(connection.id, (counts.get(connection.id) || 0) + 1));
    const records = validState.filter(connection => counts.get(connection.id) === 1 &&
      known.has(connection.id) && known.get(connection.id).revision === connection.revision);

    if (!records.length) {
      root.append(node('p', 'Keine passende aktive Bankverbindung vorhanden.', 'fsb-notice'));
      setStatus(status, '');
      return;
    }

    let busy = false;
    const controls = () => root.querySelectorAll('button,input');
    const setBusy = value => {
      busy = value;
      controls().forEach(control => {
        control.disabled = value || (control.classList.contains('fsb-read-button') && control.dataset.credentialsPresent !== 'true');
      });
    };
    const refresh = () => {
      if (typeof onChanged !== 'function') return;
      try {
        const result = onChanged();
        if (result && typeof result.catch === 'function') result.catch(() => {});
      } catch (_) {
        // The server mutation has already completed; do not surface callback details.
      }
    };
    const request = async (action, payload) => {
      setBusy(true);
      try {
        const result = await window.api(`/api/administration/${action}`, payload);
        return {ok: true, result};
      } catch (_) {
        return {ok: false};
      } finally {
        setBusy(false);
      }
    };

    const showBalances = (container, accounts, accountErrors = []) => {
      container.replaceChildren();
      const list = node('div', '', 'fsb-balance-list');
      for (const account of accounts) {
        if (!account || typeof account !== 'object') continue;
        const masked = safeText(account.masked_account, 64) || 'Konto';
        const amount = typeof account.amount === 'string' ||
          (typeof account.amount === 'number' && Number.isFinite(account.amount))
          ? String(account.amount) : '—';
        const currency = safeText(account.currency, 12);
        const bookedOn = safeText(account.booked_on, 32);
        const row = node('article', '', 'fsb-balance-row');
        row.append(node('strong', `${masked} · ${amount}${currency ? ` ${currency}` : ''}`));
        row.append(node('small', bookedOn ? `Stand: ${bookedOn}` : 'Stand unbekannt'));
        list.append(row);
      }
      const readCount = list.childElementCount;
      for (const account of accountErrors) {
        const row = node('article', '', 'fsb-balance-row fsb-error');
        row.append(node('strong', `${safeText(account?.masked_account, 64) || 'Konto'} · nicht abrufbar`));
        row.append(node('small', balanceError(account?.code, account?.diagnostic)));
        list.append(row);
      }
      if (!list.childElementCount) list.append(node('p', 'Der Abruf enthielt keine Kontostände.', 'fsb-notice'));
      container.append(list);
      return readCount;
    };

    const readBalances = async (connection, credentialPresent, tanMethod, tanMedium, readStatus, results) => {
      if (busy || !credentialPresent || !isCurrent()) return;
      setBusy(true);
      setStatus(readStatus, 'Kontostände werden einmalig abgerufen.');
      results.replaceChildren();
      const payload = {
        id: connection.id,
        revision: connection.revision,
        confirmed: true,
        tan_method: tanMethod,
        tan_medium: tanMedium,
      };
      let job;
      try {
        job = await window.api('/api/administration/bank-balances-read', payload);
      } catch (error) {
        if (isCurrent()) setStatus(readStatus, balanceError(error?.code, error?.diagnostic), true);
        setBusy(false);
        return;
      }
      if (!isCurrent()) return;
      if (!job || job.status !== 'running' || typeof job.job_id !== 'string' || !job.job_id) {
        setStatus(readStatus, balanceError(job?.code, job?.diagnostic), true);
        setBusy(false);
        return;
      }

      const started = Date.now();
      while (isCurrent() && Date.now() - started < 100000) {
        await new Promise(resolve => window.setTimeout(resolve, 1000));
        if (!isCurrent()) return;
        let state;
        try {
          state = await window.api('/api/administration/bank-balances-state', {job_id: job.job_id});
        } catch (error) {
          if (isCurrent()) setStatus(readStatus, error?.code
            ? balanceError(error.code, error.diagnostic) : 'Der Abrufstatus konnte nicht geladen werden.', true);
          setBusy(false);
          return;
        }
        if (!isCurrent()) return;
        if (state?.status === 'running') continue;
        if (state?.status === 'error') {
          setStatus(readStatus, balanceError(state.code, state.diagnostic), true);
          setBusy(false);
          return;
        }
        if (state?.status === 'complete' && state.result?.status === 'ok' && Array.isArray(state.result.accounts)) {
          const errors = Array.isArray(state.result.account_errors) ? state.result.account_errors : [];
          const readCount = showBalances(results, state.result.accounts, errors);
          const message = readCount === 0 ? 'Es wurden keine Kontostände gelesen.'
            : (errors.length ? `${readCount} Kontostände gelesen, ${errors.length} nicht abrufbar.`
              : 'Kontostände wurden gelesen.');
          setStatus(readStatus, message + (readCount ? ' Sie wurden nicht in Buchungen übernommen.' : ''),
            errors.length > 0 || readCount === 0);
          setBusy(false);
          return;
        }
        setStatus(readStatus, 'Der Abrufstatus ist nicht verfügbar. Bitte prüfe den Serverstatus.', true);
        setBusy(false);
        return;
      }
      if (isCurrent()) {
        setStatus(readStatus, 'Der Bankabruf dauert länger als erwartet. Es wird nicht erneut gestartet.', true);
        setBusy(false);
      }
    };

    for (const [index, connection] of records.entries()) {
      const source = known.get(connection.id);
      const card = node('section', '', 'fsb-card');
      const heading = node('h3', source.label, 'fsb-heading');
      let credentialPresent = connection.credentials_present === true ||
        connection.server_credentials_present === true;
      const indicator = node('p', credentialPresent
        ? 'Zugangsdaten sind auf dem Server hinterlegt.'
        : (source.bankId === 'ING' ? 'ING-Anbindung per QR-Login ist noch nicht eingerichtet.' : 'Noch keine Zugangsdaten auf dem Server.'), 'fsb-presence');
      card.append(heading, indicator);

      const form = node('form', '', 'fsb-form');
      form.noValidate = true;
      const usernameLabel = node('label', 'Benutzerkennung');
      const username = node('input');
      username.type = 'password';
      username.name = 'username';
      username.autocomplete = 'off';
      username.spellcheck = false;
      username.required = true;
      username.maxLength = 256;
      username.setAttribute('maxlength', '256');
      usernameLabel.append(username);

      const pinLabel = node('label', 'Online-Banking-Passwort / PIN');
      const pin = node('input');
      pin.type = 'password';
      pin.name = 'pin';
      pin.autocomplete = 'off';
      pin.spellcheck = false;
      pin.required = true;
      pin.maxLength = 1024;
      pin.setAttribute('maxlength', '1024');
      pinLabel.append(pin, node('small', 'Keine PIN zum Entsperren der Banking-App.', 'fsb-field-help'));

      const consentLabel = node('label', '', 'fsb-check');
      const consent = node('input');
      consent.type = 'checkbox';
      consent.required = true;
      consentLabel.append(consent, node('span', 'Ich bestätige, dass dieser Zugang auf dem Server gespeichert werden soll.'));
      const save = node('button', 'Zugang auf Server speichern', 'fsb-primary');
      save.type = 'submit';
      const editCredentials = node('button', 'Zugangsdaten ändern', 'fsb-secondary fsb-edit-credentials');
      editCredentials.type = 'button';
      editCredentials.hidden = !credentialPresent;
      const formMessage = node('p', '', 'fsb-form-message');
      formMessage.setAttribute('role', 'status');
      form.append(usernameLabel, pinLabel, consentLabel, save, formMessage);
      form.hidden = credentialPresent || source.bankId === 'ING';
      card.append(form);
      if (source.bankId === 'ING') {
        form.replaceChildren();
        card.append(node('p', 'Für diesen Weg wird eine separate Bankanbindung benötigt.', 'fsb-notice'));
      }
      card.append(editCredentials);
      if (source.bankId === 'ING') editCredentials.hidden = true;
      editCredentials.addEventListener('click', () => {
        if (busy || !credentialPresent) return;
        if (form.hidden) {
          form.hidden = false;
          editCredentials.textContent = 'Änderung abbrechen';
          username.focus();
          return;
        }
        username.value = '';
        pin.value = '';
        consent.checked = false;
        form.hidden = true;
        editCredentials.textContent = 'Zugangsdaten ändern';
        setStatus(formMessage, '');
      });

      const readPanel = node('div', '', 'fsb-balance-panel');
      const readButton = node('button', 'Kontostände abrufen', 'fsb-read-button');
      readButton.type = 'button';
      readButton.disabled = !credentialPresent;
      readButton.dataset.credentialsPresent = String(credentialPresent);
      const readStatus = node('p', '', 'fsb-form-message');
      readStatus.setAttribute('role', 'status');
      readStatus.setAttribute('aria-live', 'polite');
      const results = node('div', '', 'fsb-balance-results');
      if (source.bankId !== 'ING') {
        const tanDetails = node('details', '', 'fsb-tan-details');
        tanDetails.append(node('summary', 'TAN-Methode oder Gerät angeben (optional)'));
        const tanFields = node('div', '', 'fsb-tan-fields');
        const methodLabel = node('label', 'TAN-Methode (Ziffern)');
        const methodInput = node('input');
        methodInput.type = 'text';
        methodInput.name = 'tan_method';
        methodInput.inputMode = 'numeric';
        methodInput.autocomplete = 'off';
        methodInput.spellcheck = false;
        methodInput.maxLength = 8;
        methodInput.setAttribute('maxlength', '8');
        methodLabel.append(methodInput);
        const mediumLabel = node('label', 'TAN-Gerät oder Medium');
        const mediumInput = node('input');
        mediumInput.type = 'text';
        mediumInput.name = 'tan_medium';
        mediumInput.autocomplete = 'off';
        mediumInput.spellcheck = false;
        mediumInput.maxLength = 32;
        mediumInput.setAttribute('maxlength', '32');
        mediumLabel.append(mediumInput);
        tanFields.append(methodLabel, mediumLabel);
        tanDetails.append(tanFields);
        readPanel.append(tanDetails);
        readButton.addEventListener('click', () => {
          if (busy) return;
          const method = methodInput.value;
          const medium = mediumInput.value;
          methodInput.value = '';
          mediumInput.value = '';
          if (method && !/^\d{1,8}$/.test(method)) {
            setStatus(readStatus, 'Die TAN-Methode muss aus höchstens acht Ziffern bestehen.', true);
            return;
          }
          if (medium.length > 32) {
            setStatus(readStatus, 'Das TAN-Gerät oder Medium ist zu lang.', true);
            return;
          }
          readBalances(connection, credentialPresent, method || null, medium || null, readStatus, results);
        });
      } else {
        readButton.addEventListener('click', () => readBalances(connection, credentialPresent, null, null, readStatus, results));
      }
      readPanel.append(readButton, readStatus, results);
      card.append(readPanel);
      let removing = false;
      let removalForm = null;
      const removeButton = node('button', 'Zugang entfernen', 'fsb-secondary');
      removeButton.type = 'button';
      removeButton.hidden = !credentialPresent;
      if (source.bankId === 'ING') removeButton.hidden = true;
      const removeForm = node('form', '', 'fsb-remove-form');
      removeForm.noValidate = true;
      removeForm.hidden = true;
      const removeConsentLabel = node('label', '', 'fsb-check');
      const removeConsent = node('input');
      removeConsent.type = 'checkbox';
      removeConsent.required = true;
      removeConsentLabel.append(removeConsent,
        node('span', 'Ich bestätige, die gespeicherten Zugangsdaten vom Server zu entfernen.'));
      const confirmRemove = node('button', 'Zugang endgültig entfernen', 'fsb-danger');
      confirmRemove.type = 'submit';
      const cancelRemove = node('button', 'Abbrechen', 'fsb-secondary');
      cancelRemove.type = 'button';
      const removeMessage = node('p', '', 'fsb-form-message');
      removeMessage.setAttribute('role', 'status');
      removeForm.append(removeConsentLabel, confirmRemove, cancelRemove, removeMessage);
      removalForm = removeForm;
      removeButton.addEventListener('click', () => {
        if (busy) return;
        removing = true;
        removeButton.hidden = true;
        removeForm.hidden = false;
        removeConsent.focus();
      });
      cancelRemove.addEventListener('click', () => {
        if (busy) return;
        removing = false;
        removeConsent.checked = false;
        removeForm.hidden = true;
        removeButton.hidden = false;
        setStatus(removeMessage, '');
      });
      removeForm.addEventListener('submit', async event => {
        event.preventDefault();
        if (busy || !removing) return;
        if (!removeConsent.checked) {
          setStatus(removeMessage, 'Bitte bestätige das Entfernen des Serverzugangs.', true);
          return;
        }
        const payload = {id: connection.id, revision: connection.revision, confirmed: true};
        const outcome = await request('bank-credentials-delete', payload);
        if (!outcome.ok) {
          setStatus(removeMessage, 'Zugang konnte nicht entfernt werden. Bitte prüfe den Serverstatus.', true);
          return;
        }
        if (Number.isSafeInteger(outcome.result?.revision)) connection.revision = outcome.result.revision;
        credentialPresent = false;
        readButton.disabled = true;
        readButton.dataset.credentialsPresent = 'false';
        setStatus(indicator, 'Keine Zugangsdaten auf dem Server hinterlegt.');
        form.hidden = false;
        editCredentials.hidden = true;
        username.value = '';
        pin.value = '';
        consent.checked = false;
        removeButton.hidden = true;
        removeForm.hidden = true;
        removing = false;
        setStatus(status, 'Zugang vom Server entfernt.');
        refresh();
      });
      card.append(removeButton, removeForm);

      form.addEventListener('submit', async event => {
        event.preventDefault();
        if (busy) return;
        let usernameValue = username.value;
        let pinValue = pin.value;
        username.value = '';
        pin.value = '';
        if (!usernameValue || !pinValue) {
          usernameValue = '';
          pinValue = '';
          setStatus(formMessage, 'Benutzerkennung und PIN sind erforderlich.', true);
          return;
        }
        if (usernameValue.length > 256 || pinValue.length > 1024) {
          usernameValue = '';
          pinValue = '';
          setStatus(formMessage, 'Benutzerkennung oder PIN ist zu lang.', true);
          return;
        }
        if (!consent.checked) {
          usernameValue = '';
          pinValue = '';
          setStatus(formMessage, 'Bitte bestätige das Speichern auf dem Server.', true);
          return;
        }
        const payload = {
          id: connection.id,
          revision: connection.revision,
          confirmed: true,
          username: usernameValue,
          pin: pinValue,
        };
        usernameValue = '';
        pinValue = '';
        setStatus(formMessage, 'Zugang wird gespeichert.');
        const outcome = await request('bank-credentials-save', payload);
        payload.username = '';
        payload.pin = '';
        if (!outcome.ok) {
          setStatus(formMessage, 'Zugang konnte nicht gespeichert werden. Bitte prüfe den Serverstatus.', true);
          return;
        }
        if (Number.isSafeInteger(outcome.result?.revision)) connection.revision = outcome.result.revision;
        credentialPresent = true;
        readButton.disabled = false;
        readButton.dataset.credentialsPresent = 'true';
        consent.checked = false;
        form.hidden = true;
        editCredentials.hidden = false;
        editCredentials.textContent = 'Zugangsdaten ändern';
        setStatus(indicator, 'Zugangsdaten sind auf dem Server hinterlegt.');
        if (removalForm) {
          removalForm.hidden = true;
          removalForm.querySelector('input[type="checkbox"]').checked = false;
          removeButton.hidden = false;
        }
        setStatus(status, 'Zugang auf Server gespeichert. Kontostände können per Klick einmalig gelesen werden.');
        refresh();
      });
      root.append(card);
    }
    setStatus(status, '');
  }

  window.financeServerBanking = {render};
})();
