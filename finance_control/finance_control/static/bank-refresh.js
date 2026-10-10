'use strict';

(() => {
  const hostId = 'dashboard-bank-refresh-host';
  const stateText = value => ({
    running: 'Abruf läuft',
    updated: 'Aktualisiert',
    cooldown: 'Vor kurzem abgefragt',
    setup_required: 'Einrichtung erforderlich',
    error: 'Abruf fehlgeschlagen',
  }[value] || 'Status nicht verfügbar');
  const diagnosticText = (code, diagnostic) => {
    if (!['bank_failure', 'invalid_bank_result'].includes(code)
        || !diagnostic || typeof diagnostic !== 'object' || Array.isArray(diagnostic)
        || !Object.hasOwn(diagnostic, 'stage')
        || ![1, 2].includes(Object.keys(diagnostic).length)
        || Object.keys(diagnostic).some(key => !['stage', 'bank_error_code'].includes(key))) return '';
    const stages = {
      accounts: 'Beim Lesen der Kontenliste.',
      control_month: 'Beim Lesen des Kontrollmonats.',
      period: 'Beim Lesen des gewählten Zeitraums.',
      balance: 'Beim Lesen des Kontostands.',
    };
    if (!Object.hasOwn(stages, diagnostic.stage)) return '';
    const stage = stages[diagnostic.stage];
    if (!Object.hasOwn(diagnostic, 'bank_error_code')) return ` ${stage}`;
    if (code !== 'bank_failure') return '';
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
    return ` ${stage} ${details[diagnostic.bank_error_code]}`;
  };
  const errorText = (code, diagnostic) => ({
    authorization_required: 'Die Bankfreigabe ist erforderlich. Öffne die Banking-App und versuche es danach erneut.',
    auth_rejected: 'Die ING/Bank hat die Anmeldung abgelehnt. Prüfe den gespeicherten Zugang.',
    bank_read_busy: 'Für diese Bankverbindung läuft bereits ein Abruf.',
    bank_read_timeout: 'Der Abruf hat das Zeitlimit erreicht.',
    vault_unavailable: 'Der sichere Serverspeicher ist derzeit nicht verfügbar.',
    bank_product_unavailable: 'Die Bankverbindung ist auf dem Server noch nicht vollständig eingerichtet.',
    PREVIOUS_MONTH_CHANGED: 'Der vorherige Kontrollmonat wurde geändert. Prüfe die Buchungen erneut.',
    CONTROL_MONTH_OPENING_DATE_MISMATCH: 'Das in Finance Control erfasste Eröffnungsdatum liegt nicht vor dem Kontrollmonat. Prüfe das Eröffnungsdatum und den gewählten Zeitraum. Es wurde nichts übernommen.',
    CONTROL_MONTH_BALANCE_MISMATCH: 'Die Anfangs- oder Endsalden des Kontrollmonats stimmen nicht mit dem vorhandenen Bestand überein. Prüfe den Anfangsbestand und die vorhandenen Buchungen. Es wurde nichts übernommen.',
    CONTROL_MONTH_ROWS_MISMATCH: 'Die Buchungen des Kontrollmonats stimmen nicht vollständig mit dem vorhandenen Bestand überein. Prüfe den Vormonat. Es wurde nichts übernommen.',
    SOURCE_BINDING_CONFLICT: 'Das Bankkonto oder das Zielkonto in Finance Control ist bereits anders zugeordnet. Prüfe die Kontenauswahl und die bestehende Zuordnung. Es wurde nichts übernommen.',
    legacy_text_confirmation_required: 'Vorhandene Buchungstexte müssen geprüft werden. Öffne „Verbindung bearbeiten“ für die Gegenüberstellung.',
    LEGACY_TEXT_REVIEW_LIMIT: 'Mehr als 100 Textabweichungen benötigen eine gesonderte Prüfung.',
  }[code] || 'Der Bankabruf konnte nicht abgeschlossen werden. Prüfe die Einrichtung und den Serverstatus.')
    + diagnosticText(code, diagnostic);
  const nameFor = bank => bank === 'ING' ? 'ING'
    : bank === 'POSTBANK' ? 'Postbank'
      : bank === 'NASPA' ? 'Nassauische Sparkasse' : 'Bank';
  const validRecord = item => item && typeof item.id === 'string' && item.id
    && Number.isSafeInteger(item.revision) && item.revision >= 0
    && ['ING', 'POSTBANK', 'NASPA'].includes(item.bank_id);
  const formatDate = value => {
    if (typeof value !== 'string' || !Number.isFinite(Date.parse(value))) {
      return 'Noch kein erfolgreicher Abruf';
    }
    const formatted = new Intl.DateTimeFormat('de-DE', {
      dateStyle: 'medium', timeStyle: 'short', timeZone: 'Europe/Berlin',
    }).format(new Date(value));
    return `Letzter erfolgreicher Abruf: ${formatted}`;
  };

  let activated = false;
  let automaticStarted = false;
  let running = false;
  let lifecycle = 0;

  function message(parent, text, error = false) {
    const paragraph = document.createElement('p');
    paragraph.className = `fbr-message${error ? ' fbr-error' : ''}`;
    paragraph.setAttribute('role', 'status');
    paragraph.textContent = text;
    parent.append(paragraph);
  }

  async function mount() {
    const host = document.getElementById(hostId);
    if (!host || !host.isConnected || host.dataset.rendered === 'true') return;
    host.dataset.rendered = 'true';
    const generation = ++lifecycle;
    host.replaceChildren();
    host.classList.add('fbr-host');

    const panel = document.createElement('section');
    panel.className = 'fbr-panel';
    const heading = document.createElement('h2');
    heading.textContent = 'Bankabruf';
    const description = document.createElement('p');
    description.className = 'fbr-muted';
    description.textContent = 'Buchungen und Kontostände aus Deinen eingerichteten Bankverbindungen aktualisieren.';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'fbr-button';
    button.textContent = 'Umsätze aktualisieren';
    const status = document.createElement('div');
    status.className = 'fbr-status';
    status.setAttribute('aria-live', 'polite');
    const setup = document.createElement('div');
    setup.className = 'fbr-setup-list';
    const header = document.createElement('div');
    header.className = 'fbr-header';
    header.append(heading, button);
    panel.append(header, description, status, setup);
    host.append(panel);
    const views = new Map();

    const live = () => generation === lifecycle && host.isConnected && host.contains(panel);
    const setBusy = value => {
      running = value;
      button.disabled = value;
      button.textContent = value ? 'Banken werden aktualisiert …' : 'Umsätze aktualisieren';
    };
    const request = async (path, body) => {
      try {
        return {ok: true, data: await window.api(path, body)};
      } catch (error) {
        return {ok: false, code: typeof error?.code === 'string' ? error.code : '',
          diagnostic: error?.diagnostic};
      }
    };
    const renderConnections = rows => {
      status.replaceChildren();
      const grouped = new Map();
      for (const row of rows) {
        if (!row || typeof row.id !== 'string') continue;
        if (!grouped.has(row.id)) grouped.set(row.id, []);
        grouped.get(row.id).push(row);
      }
      for (const [id, accounts] of grouped) {
        const view = views.get(id);
        if (!view) continue;
        const priority = ['running', 'error', 'setup_required', 'cooldown', 'updated'];
        const rank = value => priority.includes(value) ? priority.indexOf(value) : priority.length;
        const row = accounts.reduce((first, next) =>
          rank(next.status) < rank(first.status) ? next : first);
        view.state.className = `fbr-state fbr-${row.status}`;
        view.state.textContent = stateText(row.status);
        const dates = accounts.map(account => account.last_success_at)
          .filter(value => typeof value === 'string' && Number.isFinite(Date.parse(value))).sort();
        view.date.textContent = formatDate(dates.at(-1));
        view.summary.textContent = row.status === 'setup_required'
          ? 'Bankabruf einrichten' : 'Verbindung bearbeiten';
        view.feedback.replaceChildren();
        for (const account of accounts) {
          const feedback = document.createElement('div');
          feedback.className = 'fbr-account-status';
          if (account.account_id) {
            const name = document.createElement('strong');
            name.textContent = typeof account.account_name === 'string' && account.account_name
              ? account.account_name : account.account_id;
            feedback.append(name);
            const outcome = document.createElement('span');
            outcome.className = 'fbr-muted';
            outcome.textContent = `${stateText(account.status)} · ${formatDate(account.last_success_at)}`;
            feedback.append(outcome);
          }
          if (['error', 'cooldown', 'setup_required'].includes(account.status)
              && (account.status === 'error' || typeof account.code === 'string')) {
            message(feedback, errorText(account.code, account.diagnostic), true);
          }
          if (Number.isSafeInteger(account.inserted) && account.inserted >= 0
              && (account.status === 'updated' || ['error', 'setup_required', 'cooldown'].includes(account.status)
                  && account.inserted > 0)) {
            const count = document.createElement('span');
            count.className = 'fbr-muted';
            count.textContent = account.status === 'updated'
              ? `${account.inserted} neue Buchungen`
              : `${account.inserted} Buchungen bereits übernommen; weiterer Abruf fehlgeschlagen.`;
            feedback.append(count);
          }
          view.feedback.append(feedback);
        }
      }
    };

    const loadSetup = async () => {
      const [administration, credentials] = await Promise.all([
        request('/api/administration'),
        request('/api/administration/bank-credentials-state', {}),
      ]);
      if (!live()) return false;
      setup.replaceChildren();
      if (!administration.ok || !credentials.ok) {
        message(setup, 'Bankeinrichtungen konnten nicht geladen werden. Es wurde kein Abruf gestartet.', true);
        button.disabled = true;
        return false;
      }
      if (administration.data?.enabled !== true) {
        message(setup, 'Die Bankverwaltung ist für diesen Zugang nicht freigeschaltet.');
        button.disabled = true;
        return false;
      }
      if (credentials.data?.supported !== true) {
        message(setup, 'Dieser Server unterstützt den sicheren Bankzugang nicht.');
        button.disabled = true;
        return false;
      }

      const connections = Array.isArray(administration.data.connections)
        ? administration.data.connections.filter(validRecord) : [];
      const credentialRows = Array.isArray(credentials.data.connections) ? credentials.data.connections : [];
      const credentialsById = new Map(credentialRows
        .filter(row => row && typeof row.id === 'string')
        .map(row => [row.id, row]));
      const refreshEligible = connections.filter(row => ['POSTBANK', 'ING', 'NASPA'].includes(row.bank_id)
        && credentialsById.get(row.id)?.server_credentials_present === true);
      if (!connections.length) {
        message(setup, 'Es sind keine eigenen Bankverbindungen vorhanden. Registriere eine Verbindung und hinterlege den Zugang in der Verwaltung.');
        button.disabled = true;
        return false;
      }

      const missingCredentials = connections.filter(row => row.bank_id !== 'ING'
        && credentialsById.get(row.id)?.server_credentials_present !== true);
      if (missingCredentials.length) {
        const banks = missingCredentials.map(row => nameFor(row.bank_id)).join(', ');
        message(setup, `${banks}: Zugang in der Verwaltung auf dem Server hinterlegen.`);
      }
      const list = document.createElement('ul');
      list.className = 'fbr-list';
      setup.append(list);
      const labels = connections.map(row => typeof row.label === 'string' && row.label.trim()
        ? row.label.trim() : nameFor(row.bank_id));
      for (const [index, row] of connections.entries()) {
        const item = document.createElement('li');
        item.className = 'fbr-row';
        item.dataset.connectionId = row.id;
        const top = document.createElement('div');
        top.className = 'fbr-row-top';
        const label = document.createElement('strong');
        label.textContent = labels[index];
        const state = document.createElement('span');
        state.className = 'fbr-state fbr-setup_required';
        state.textContent = stateText('setup_required');
        top.append(label, state);
        item.append(top);
        if (labels.filter(value => value === labels[index]).length > 1) {
          const duplicate = document.createElement('small');
          duplicate.className = 'fbr-muted';
          duplicate.textContent = `Registrierung ${labels.slice(0, index + 1).filter(value => value === labels[index]).length} · gleicher Name, separate Verbindung`;
          item.append(duplicate);
        }
        const date = document.createElement('span');
        date.className = 'fbr-muted';
        date.textContent = formatDate(null);
        const feedback = document.createElement('div');
        feedback.className = 'fbr-feedback';
        item.append(date, feedback);
        const details = document.createElement('details');
        details.className = 'fbr-import-setup';
        const summary = document.createElement('summary');
        summary.textContent = 'Bankabruf einrichten';
        const content = document.createElement('div');
        content.className = 'fbr-import-content';
        const source = document.createElement('div');
        source.className = 'fbr-import-source';
        content.append(source);
        details.append(summary, content);
        item.append(details);
        list.append(item);
        views.set(row.id, {state, date, feedback, summary});
        // Merely displaying a connection must not create a second import UI or
        // fetch its setup data. Load it once, when its own disclosure is opened.
        let bound = false;
        details.addEventListener('toggle', () => {
          if (!details.open || bound || !live()) return;
          bound = true;
          if (credentialsById.get(row.id)?.server_credentials_present === true
              && window.postbankTransactions) {
            const record = {id: row.id, revision: row.revision, bankId: row.bank_id};
            let cards = null;
            if (row.bank_id === 'POSTBANK' && window.postbankCards) {
              cards = document.createElement('div');
              content.append(cards);
              window.postbankCards.bind(cards, record,
                () => document.dispatchEvent(new CustomEvent('finance-bank-refreshed')));
            }
            window.postbankTransactions.bind(source, record,
              () => document.dispatchEvent(new CustomEvent('finance-bank-refreshed')),
              accounts => {
                if (cards && window.postbankCards?.setSourceAccounts)
                  window.postbankCards.setSourceAccounts(cards, record, accounts);
              });
          } else if (row.bank_id === 'ING') {
            message(content, 'ING-Anbindung per QR-Login ist noch nicht eingerichtet. Für diesen Weg wird eine separate Bankanbindung benötigt.');
          } else {
            message(content, 'Hinterlege den Zugang auf dem Server in der Verwaltung, um Buchungen manuell einzurichten.');
          }
        });
      }
      button.disabled = refreshEligible.length === 0;
      if (!refreshEligible.length) {
        message(setup, 'Es ist kein eigener Bankzugang für den automatischen Abruf eingerichtet.');
      }
      return refreshEligible.length > 0;
    };

    async function poll() {
      const started = Date.now();
      while (live() && Date.now() - started < 350000) {
        await new Promise(resolve => window.setTimeout(resolve, 1000));
        if (!live()) return;
        const response = await request('/api/administration/bank-refresh-state', {});
        if (!live()) return;
        if (!response.ok) {
          message(status, errorText(response.code, response.diagnostic), true);
          setBusy(false);
          return;
        }
        if (response.data?.status === 'complete' && Array.isArray(response.data.connections)) {
          renderConnections(response.data.connections);
          setBusy(false);
          document.dispatchEvent(new CustomEvent('finance-bank-refreshed'));
          return;
        }
        if (response.data?.status !== 'running') {
          message(status, 'Der Aktualisierungsstatus ist nicht verfügbar.', true);
          setBusy(false);
          return;
        }
        status.replaceChildren();
        message(status, 'Bankabruf läuft.');
      }
      if (live()) {
        message(status, 'Der Abruf läuft länger als erwartet. Prüfe den Status später erneut.', true);
        setBusy(false);
      }
    }

    async function start() {
      if (running || !live()) return;
      setBusy(true);
      status.replaceChildren();
      message(status, 'Bankabruf wird gestartet.');
      const response = await request('/api/administration/bank-refresh-start', {});
      if (!live()) return;
      if (!response.ok) {
        message(status, errorText(response.code, response.diagnostic), true);
        setBusy(false);
        return;
      }
      if (response.data?.status === 'complete' && Array.isArray(response.data.connections)) {
        renderConnections(response.data.connections);
        setBusy(false);
        document.dispatchEvent(new CustomEvent('finance-bank-refreshed'));
        return;
      }
      if (response.data?.status !== 'running') {
        message(status, 'Der Aktualisierungsstatus ist nicht verfügbar.', true);
        setBusy(false);
        return;
      }
      await poll();
    }

    button.addEventListener('click', start);
    const allowed = await loadSetup();
    if (!live() || !allowed) return;
    const state = await request('/api/administration/bank-refresh-state', {});
    if (!live()) return;
    if (state.ok && state.data?.status === 'complete' && Array.isArray(state.data.connections)) {
      renderConnections(state.data.connections);
    } else if (state.ok && state.data?.status === 'running') {
      setBusy(true);
      automaticStarted = true;
      void poll();
      return;
    }
    if (!automaticStarted) {
      automaticStarted = true;
      await start();
    }
  }

  window.financeBankRefresh = {
    activate() {
      if (activated) return;
      activated = true;
      mount().catch(() => {
        const host = document.getElementById(hostId);
        if (!host) return;
        host.replaceChildren();
        message(host, 'Bankabruf konnte nicht angezeigt werden.', true);
      });
    },
    deactivate() {
      activated = false;
      automaticStarted = false;
      running = false;
      lifecycle += 1;
      const host = document.getElementById(hostId);
      if (!host) return;
      host.dataset.rendered = 'false';
      host.replaceChildren();
    },
  };
})();
