'use strict';

(() => {
  const generations = new WeakMap();
  const node = (tag, text = '', className = '') => {
    const element = document.createElement(tag);
    if (text !== null && text !== undefined) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  };
  const isRecord = value => value && typeof value.id === 'string' && value.id.length > 0
    && Number.isSafeInteger(value.revision) && value.revision >= 0;
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
  const errorText = (code, bankId, diagnostic) => {
    if (bankId === 'NASPA' && ['authorization_required', 'invalid_bank_auth_selection'].includes(code)) {
      return 'Der NASPA-Abruf wurde nicht freigegeben. Prüfe die S-pushTAN-App. Numerische oder grafische TAN-Verfahren werden hier noch nicht unterstützt.';
    }
    const base = ({
    auth_rejected: 'Die Bank hat die Anmeldung abgelehnt. Prüfe den hinterlegten Zugang. (auth_rejected)',
    bank_failure: 'Der Bankabruf ist fehlgeschlagen. Die genaue Ursache ist noch unbekannt. (bank_failure)',
    invalid_bank_result: 'Die Bankantwort konnte nicht verarbeitet werden. (invalid_bank_result)',
    bank_read_unavailable: 'Der Bankabruf ist auf dem Server derzeit nicht verfügbar. (bank_read_unavailable)',
    bank_import_unavailable: 'Die Übernahme der Bankbuchungen ist derzeit nicht verfügbar. (bank_import_unavailable)',
    authorization_required: 'Die Bank verlangt eine Freigabe. Prüfe Deine Banking-App und versuche es danach erneut.',
    bank_read_timeout: 'Der Bankabruf hat das Zeitlimit erreicht. Es wurde kein neuer Abruf gestartet.',
    bank_read_busy: 'Für diese Bankverbindung läuft bereits ein Abruf.',
    vault_unavailable: 'Der sichere Serverspeicher ist derzeit nicht verfügbar.',
    bank_product_unavailable: 'Die FinTS-Produktkennung fehlt auf dem Server.',
    invalid_bank_auth_selection: 'Die gewählte TAN-Methode oder das TAN-Gerät ist nicht verfügbar.',
    server_banking_unsupported: 'Der Server unterstützt diesen Bankabruf nicht.',
    stale_revision: 'Die Bankverbindung wurde geändert. Lade die Verwaltung neu und starte erneut.',
    unknown_connection: 'Die Bankverbindung ist nicht mehr verfügbar.',
    forbidden: 'Diese Bankverbindung gehört zu einem anderen Zugang.',
    LEGACY_PREFIX_AMBIGUOUS: 'Vorhandene Buchungen sind nicht eindeutig zuordenbar. Es wurde nichts übernommen.',
    LEGACY_TEXT_MISMATCH: 'Buchungstexte im vorhandenen Bestand stimmen nicht eindeutig mit der Banklieferung überein. Es wurde nichts übernommen.',
    LEGACY_TEXT_REVIEW_LIMIT: 'Es gibt mehr als 100 Textabweichungen. Die Übernahme benötigt eine gesonderte Prüfung.',
    legacy_text_confirmation_required: 'Prüfe und bestätige die gegenübergestellten Alt- und Banktexte vor der Übernahme.',
    LEGACY_MONTH_OVERLAP: 'Vorhandene Buchungen passen nicht zur Banklieferung. Es wurde nichts übernommen.',
    LEGACY_PREFIX_MISMATCH: 'Vorhandene Buchungen passen nicht zur Banklieferung. Es wurde nichts übernommen.',
    LEDGER_PREFIX_CHANGED: 'Vorhandene Buchungen passen nicht zur Banklieferung. Es wurde nichts übernommen.',
    PREFIX_CHANGED: 'Vorhandene Buchungen passen nicht zur Banklieferung. Es wurde nichts übernommen.',
    OPENING_BALANCE_MISMATCH: 'Kontrollsalden passen nicht zum vorhandenen Bestand. Es wurde nichts übernommen.',
    ACCOUNT_OPENING_MISMATCH: 'Kontrollsalden passen nicht zum vorhandenen Bestand. Es wurde nichts übernommen.',
    CONTROL_MONTH_OPENING_DATE_MISMATCH: 'Das in Finance Control erfasste Eröffnungsdatum liegt nicht vor dem Kontrollmonat. Prüfe das Eröffnungsdatum und den gewählten Zeitraum. Es wurde nichts übernommen.',
    CONTROL_MONTH_BALANCE_MISMATCH: 'Die Anfangs- oder Endsalden des Kontrollmonats stimmen nicht mit dem vorhandenen Bestand überein. Prüfe den Anfangsbestand und die vorhandenen Buchungen. Es wurde nichts übernommen.',
    CONTROL_MONTH_ROWS_MISMATCH: 'Die Buchungen des Kontrollmonats stimmen nicht vollständig mit dem vorhandenen Bestand überein. Prüfe den Vormonat. Es wurde nichts übernommen.',
    SOURCE_BINDING_CONFLICT: 'Das Bankkonto oder das Zielkonto in Finance Control ist bereits anders zugeordnet. Prüfe die Kontenauswahl und die bestehende Zuordnung. Es wurde nichts übernommen.',
    LEDGER_CONTROL_CHANGED: 'Kontrollsalden passen nicht zum vorhandenen Bestand. Es wurde nichts übernommen.',
    SOURCE_BINDING_REQUIRED: 'Der vorherige Monat ist noch nicht vollständig mit der Bank abgeglichen.',
    PREVIOUS_MONTH_UNVERIFIED: 'Der vorherige Monat ist noch nicht vollständig mit der Bank abgeglichen.',
    PREVIOUS_MONTH_CHANGED: 'Der vorherige Kontrollmonat wurde geändert. Prüfe die Buchungen erneut.',
    stale_preview: 'Der Datenbestand wurde geändert. Bitte eine neue Vorschau abrufen.',
    invalid_target: 'Bitte ein unterstütztes Giro- oder Sparkonto als Ziel wählen.',
    invalid_period: 'Bitte einen Monat der letzten 90 Tage und einen Stichtag im selben Monat bis heute wählen.',
    }[code] || 'Der Bankabruf konnte nicht abgeschlossen werden. Bitte prüfe den Serverstatus.');
    const detail = diagnosticText(code, diagnostic);
    return (code === 'bank_failure' && detail ? 'Der Bankabruf ist fehlgeschlagen.' : base) + detail;
  };
  const safeString = (value, max = 1000) => typeof value === 'string' && value.length <= max ? value : '';
  const dateValue = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : '—';
  const todayISO = () => {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
  };
  const monthStartISO = () => `${todayISO().slice(0, 7)}-01`;

  function bind(container, record, refresh = () => {}, onSourceAccounts = () => {}) {
    if (!container || typeof container.replaceChildren !== 'function') return;
    const generation = (generations.get(container) || 0) + 1;
    generations.set(container, generation);
    container.replaceChildren();
    container.classList.add('pbt-host');
    const root = node('section', '', 'pbt-root');
    const bankName = record?.bankId === 'ING' ? 'ING'
      : record?.bankId === 'NASPA' ? 'Nassauische Sparkasse' : 'Postbank';
    const heading = node('h3', `${bankName}-Buchungen`, 'pbt-title');
    const intro = node('p', record?.bankId === 'NASPA'
      ? 'Lies gebuchte Giro-Umsätze für einen Zeitraum ein und prüfe sie vor der Übernahme.'
      : 'Lies gebuchte Giro- oder Spar-Umsätze für einen Zeitraum ein und prüfe sie vor der Übernahme. Weitere Konten behalten eine eigene Zuordnung unter diesem Bankzugang.', 'pbt-intro');
    const status = node('p', 'Zielkonten werden geladen.', 'pbt-status');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    const content = node('div', '', 'pbt-content');
    root.append(heading, intro, status, content);
    container.append(root);
    const current = () => container.isConnected && generations.get(container) === generation && container.contains(root);
    const setStatus = (message, error = false) => {
      status.textContent = message;
      status.classList.toggle('pbt-error', error);
    };
    const invalid = () => !isRecord(record) || !['POSTBANK','ING','NASPA'].includes(record.bankId);
    if (invalid()) {
      setStatus(record?.bankId && record.bankId !== 'POSTBANK'
        ? 'Dieser Abruf ist für diese Bank nicht verfügbar.'
        : 'Die Bankverbindung ist nicht verfügbar.', true);
      return;
    }

    const ui = {};
    const auth = {tan_method: null, tan_medium: null};
    let targets = [];
    let bankAccounts = [];
    let periodJobId = null;
    let reviewToken = null;
    let busy = false;
    const accountAction = node('button', `${bankName}-Buchungen abrufen`, 'pbt-primary');
    accountAction.type = 'button';
    accountAction.disabled = true;
    content.append(accountAction);
    const accountPanel = node('div', '', 'pbt-account-panel');
    accountPanel.hidden = true;
    content.append(accountPanel);
    const authPanel = node('div', '', 'pbt-auth-panel');
    authPanel.hidden = true;
    content.append(authPanel);
    const results = node('div', '', 'pbt-results');
    content.append(results);

    const fields = () => ({
      account_fingerprint: ui.source?.value || null,
      account_id: ui.target?.value || null,
      month_start: ui.month?.value ? `${ui.month.value}-01` : null,
      as_of: ui.asOf?.value || null,
    });
    const setBusy = value => {
      busy = value;
      root.querySelectorAll('button,select,input').forEach(control => { control.disabled = value; });
      if (!value) {
        accountAction.disabled = !targets.length;
        ui.updatePeriodButton?.();
        ui.updateCommitButton?.();
        const authSelect = authPanel.querySelector('.pbt-auth-select');
        const authContinue = authPanel.querySelector('.pbt-auth-continue');
        if (authSelect && authContinue) authContinue.disabled = !authSelect.value;
      }
    };
    const request = async (path, data) => {
      try { return {ok: true, result: await window.api(path, data)}; }
      catch (error) {
        const code = typeof error?.code === 'string' ? error.code : '';
        return {ok: false, code, diagnostic: error?.diagnostic};
      }
    };
    const selectField = (labelText, className, items, keyName, valueName) => {
      const label = node('label', '', 'pbt-field');
      label.append(node('span', labelText));
      const select = node('select', '', className);
      const placeholder = node('option', 'Bitte auswählen');
      placeholder.value = '';
      placeholder.disabled = true;
      placeholder.selected = true;
      select.append(placeholder);
      for (const item of items) {
        const key = item?.[keyName];
        const value = item?.[valueName];
        if (typeof key !== 'string' || !key || typeof value !== 'string' || !value) continue;
        const option = node('option', value);
        option.value = key;
        select.append(option);
      }
      label.append(select);
      return {label, select};
    };
    const renderAccounts = accounts => {
      if (!Array.isArray(accounts)) {
        setStatus('Die Bankkonten konnten nicht angezeigt werden.', true);
        return;
      }
      bankAccounts = accounts.filter(item => item && typeof item.fingerprint === 'string'
        && /^[0-9a-f]{64}$/.test(item.fingerprint) && typeof item.masked_account === 'string');
      if (record.bankId === 'POSTBANK' && current() && typeof onSourceAccounts === 'function') {
        onSourceAccounts(bankAccounts.map(item => ({
          fingerprint: item.fingerprint, masked_account: item.masked_account,
        })));
      }
      if (!bankAccounts.length) {
        setStatus('Die Bank hat keine auswählbaren Konten gemeldet.');
        return;
      }
      const targetOptions = targets.map(item => ({id:item.id,
        label:`${safeString(item.name,160)} · ${safeString(item.owner,100) || 'Ohne Zuordnung'} · ${safeString(item.currency,12)}`}));
      const targetField = selectField('Finance-Control-Zielkonto', 'pbt-target-select', targetOptions, 'id', 'label');
      const sourceField = selectField(`${bankName}-Konto`, 'pbt-source-select', bankAccounts.map(item => ({
        fingerprint:item.fingerprint, label:item.masked_account,
      })), 'fingerprint', 'label');
      ui.target = targetField.select;
      ui.source = sourceField.select;
      const monthLabel = node('label', '', 'pbt-field'); monthLabel.append(node('span','Monatsbeginn'));
      ui.month = node('input'); ui.month.type = 'month'; ui.month.value = monthStartISO().slice(0,7); ui.month.max=todayISO().slice(0,7); monthLabel.append(ui.month);
      const asOfLabel = node('label', '', 'pbt-field'); asOfLabel.append(node('span','Bis einschließlich'));
      ui.asOf = node('input'); ui.asOf.type = 'date'; ui.asOf.value = todayISO(); ui.asOf.max=todayISO(); asOfLabel.append(ui.asOf);
      const grid = node('div', '', 'pbt-fields'); grid.append(sourceField.label,targetField.label,monthLabel,asOfLabel);
      const disclosure = node('p',`${bankName} wird direkte Quelle; vorhandene Kategorien und Belege bleiben erhalten.`,'pbt-disclosure');
      const button = node('button','Buchungen prüfen','pbt-primary pbt-period-button'); button.type='button'; button.disabled=true;
      const update = () => {button.disabled=busy || !ui.source.value || !ui.target.value
        || !/^\d{4}-\d{2}$/.test(ui.month.value) || !/^\d{4}-\d{2}-\d{2}$/.test(ui.asOf.value)
        || ui.asOf.value < `${ui.month.value}-01` || ui.asOf.value > todayISO()
        || ui.asOf.value.slice(0,7) !== ui.month.value
        || (new Date(`${todayISO()}T12:00:00Z`)-new Date(`${ui.month.value}-01T12:00:00Z`))/86400000 > 90;};
      for (const control of [ui.source,ui.target,ui.month,ui.asOf]) control.addEventListener('change',update);
      button.addEventListener('click',()=>startRead('period'));
      ui.periodButton=button;
      ui.updatePeriodButton=update;
      accountPanel.replaceChildren(grid,disclosure,button);
      accountPanel.hidden=false;
      update();
      setStatus('Wähle das maskierte Bankkonto und das passende Finance-Control-Konto ausdrücklich aus.');
    };
    const fieldValue = (parent,label,value) => {
      const cell=node('div','','pbt-summary-item');
      cell.append(node('span',label),node('strong',value)); parent.append(cell);
    };
    const renderPreview = preview => {
      if (!preview || typeof preview !== 'object' || typeof preview.review_token !== 'string'
          || !preview.review_token || !Array.isArray(preview.rows)
          || (preview.legacy_text_differences !== undefined
            && (!Array.isArray(preview.legacy_text_differences)
              || preview.legacy_text_differences.length > 100
              || preview.legacy_text_differences.some(item => !item || typeof item !== 'object'
                || typeof item.external_id !== 'string' || !item.external_id
                || typeof item.booked_on !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(item.booked_on)
                || typeof item.amount !== 'string' || !/^-?\d+\.\d{2}$/.test(item.amount)
                || typeof item.currency !== 'string' || !/^[A-Z]{3}$/.test(item.currency)
                || ['local_description','bank_description','bank_booking_text','local_counterparty','bank_counterparty']
                  .some(key => typeof item[key] !== 'string' || item[key].length > 8192))))) {
        setStatus('Die Vorschau konnte nicht sicher angezeigt werden.',true);
        return;
      }
      reviewToken=preview.review_token;
      results.replaceChildren();
      const card=node('section','','pbt-preview');
      card.append(node('h4',`Vorschau der ${bankName}-Buchungen`));
      const summary=node('div','','pbt-summary');
      const accountName=targets.find(item=>item.id===preview.account_id)?.name || 'Zielkonto';
      fieldValue(summary,'Bankkonto',`${safeString(preview.masked_account,64)||'Konto'} · ${safeString(preview.currency,12)}`);
      fieldValue(summary,'Zielkonto',safeString(accountName,160));
      fieldValue(summary,'Zeitraum',`${dateValue(preview.month_start)} bis ${dateValue(preview.as_of)}`);
      fieldValue(summary,'Buchungen',String(Number.isSafeInteger(preview.count)?preview.count:preview.rows.length));
      fieldValue(summary,'Neu / bereits vorhanden',`${Number.isSafeInteger(preview.inserted)?preview.inserted:0} / ${Number.isSafeInteger(preview.skipped)?preview.skipped:0}`);
      fieldValue(summary,'Saldo zum Zeitraum',`${safeString(preview.opening_balance,80)||'—'} → ${safeString(preview.closing_balance,80)||'—'} ${safeString(preview.currency,12)}`.trim());
      fieldValue(summary,'Bankkontostand',`${safeString(preview.bank_balance?.amount,80)||'—'} ${safeString(preview.bank_balance?.currency,12)}`.trim());
      fieldValue(summary,'Saldo gebucht am',dateValue(preview.bank_balance?.booked_on));
      fieldValue(summary,'Kontrollmonat',`${dateValue(preview.control_month?.start)} bis ${dateValue(preview.control_month?.end)}`);
      fieldValue(summary,'Monatlicher Kontrollsaldo',`${safeString(preview.control_month?.opening_balance,80)||'—'} → ${safeString(preview.control_month?.closing_balance,80)||'—'} ${safeString(preview.currency,12)}`.trim());
      fieldValue(summary,'Monatsbuchungen',String(Number.isSafeInteger(preview.control_month?.count)?preview.control_month.count:0));
      card.append(summary,node('p',`${bankName} wird direkte Quelle; vorhandene Kategorien und Belege bleiben erhalten.`,'pbt-source-change'));
      const list=node('div','','pbt-row-list');
      const visible=preview.rows.slice(0,50);
      visible.forEach((row,index)=>{
        const item=node('article','','pbt-row');
        const top=node('div','','pbt-row-top');
        const left=node('div','','pbt-row-main');
        left.append(node('span',dateValue(row?.booked_on),'pbt-row-date'),node('strong',safeString(row?.counterparty,500)||'Ohne Gegenpartei'));
        top.append(left,node('strong',`${safeString(row?.amount,80)||'—'} ${safeString(row?.currency,12)}`.trim()));
        item.append(top);
        item.append(node('p',safeString(row?.description,4000)||'Kein Verwendungszweck','pbt-description'));
        const details=node('details','','pbt-row-details');
        details.append(node('summary','Buchungstext und Wertstellung'));
        details.append(node('p',`Wertstellung: ${dateValue(row?.value_on)}`));
        details.append(node('pre',safeString(row?.booking_text,12000)||'Kein weiterer Buchungstext'));
        item.append(details);
        list.append(item);
      });
      card.append(list);
      if(preview.rows.length>visible.length) card.append(node('p',`Es werden die ersten ${visible.length} von ${preview.rows.length} Buchungen angezeigt.`,'pbt-limit'));
      const differences = preview.legacy_text_differences || [];
      ui.legacyConfirmation = null;
      if (differences.length) {
        const review = node('section', '', 'pbt-legacy-review');
        review.append(node('h4', `${differences.length} abweichende Alttexte prüfen`));
        review.append(node('p', 'Datum, Betrag und Währung passen jeweils eindeutig zur Banklieferung. Prüfe anhand der Gegenparteien und Texte, ob es dieselben Buchungen sind. Vorhandene Texte, Kategorien und Beleglinks bleiben erhalten.'));
        for (const difference of differences) {
          const item = node('article', '', 'pbt-legacy-item');
          item.append(node('strong', `${dateValue(difference?.booked_on)} · ${safeString(difference?.amount,80) || '—'} ${safeString(difference?.currency,12)}`));
          const comparison = node('div', '', 'pbt-legacy-comparison');
          for (const [label, counterparty, description, bookingText] of [
            ['Vorhandener Bestand', difference?.local_counterparty, difference?.local_description, null],
            ['Direkter Bankabruf', difference?.bank_counterparty, difference?.bank_description, difference?.bank_booking_text],
          ]) {
            const side = node('section', '', 'pbt-legacy-side');
            side.append(node('h5', label), node('strong', safeString(counterparty,8192) || 'Ohne Gegenpartei'));
            side.append(node('pre', safeString(description,8192) || 'Kein Verwendungszweck vorhanden'));
            if (bookingText && bookingText !== description) side.append(node('pre', safeString(bookingText,8192)));
            comparison.append(side);
          }
          item.append(comparison);
          review.append(item);
        }
        const confirmation = node('label', '', 'pbt-legacy-confirmation');
        ui.legacyConfirmation = node('input');
        ui.legacyConfirmation.type = 'checkbox';
        confirmation.append(ui.legacyConfirmation, node('span', 'Ich habe alle Gegenüberstellungen geprüft und bestätige, dass es dieselben Buchungen sind.'));
        review.append(confirmation);
        card.append(review);
      }
      const commit=node('button','Geprüfte Buchungen übernehmen','pbt-primary pbt-commit');
      commit.type='button';
      ui.updateCommitButton = () => {commit.disabled = busy || Boolean(ui.legacyConfirmation && !ui.legacyConfirmation.checked);};
      ui.legacyConfirmation?.addEventListener('change',ui.updateCommitButton);
      ui.updateCommitButton();
      commit.addEventListener('click',commitImport);
      card.append(commit);
      results.append(card);
      setStatus('Prüfe Konto, Zeitraum, Vorschau und Kontrollsalden vor der Übernahme.');
    };
    const handleResult = (action,result,jobId) => {
      if(!result||typeof result!=='object') {setStatus('Der Abruf lieferte kein gültiges Ergebnis.',true);return;}
      if(result.status==='needs_method'||result.status==='needs_medium') {
        if(record.bankId==='ING') {
          setStatus('Für diese ING-Verbindung ist die angebotene Freigabeart nicht verfügbar. Es wurde keine Auswahl geraten.',true);
          return;
        }
        const method=result.status==='needs_method';
        const options=Array.isArray(result.options)?result.options:[];
        authPanel.replaceChildren(); authPanel.hidden=false;
        const select=selectField(method?'TAN-Methode auswählen':'TAN-Gerät auswählen','pbt-auth-select',
          method?options.filter(item=>item&&typeof item.key==='string'&&typeof item.label==='string').map(item=>({key:item.key,label:item.label}))
            :options.filter(item=>item&&typeof item.label==='string').map(item=>({key:item.label,label:item.label})), 'key','label');
        const continueButton=node('button','Auswahl übernehmen','pbt-secondary pbt-auth-continue'); continueButton.type='button';continueButton.disabled=true;
        select.select.addEventListener('change',()=>{continueButton.disabled=!select.select.value;});
        continueButton.addEventListener('click',()=>{
          if(!select.select.value)return;
          if(method) auth.tan_method=select.select.value; else auth.tan_medium=select.select.value;
          authPanel.hidden=true;
          setStatus(record.bankId==='NASPA'
            ? 'Die Auswahl wird verwendet. Falls NASPA eine Freigabe anfordert, bestätige sie in der S-pushTAN-App.'
            : 'Die Auswahl wird verwendet. Falls die Bank eine Handyfreigabe verlangt, bestätige sie in der Banking-App.');
          startRead(action);
        });
        authPanel.append(select.label,node('p','Es wird keine TAN in dieser Oberfläche eingegeben.','pbt-auth-note'),continueButton);
        setStatus(method?'Wähle eine der von der Bank angebotenen TAN-Methoden.':'Wähle das von der Bank angebotene TAN-Gerät.');
        return;
      }
      authPanel.hidden=true;
      if(action==='accounts'&&result.status==='ok') {renderAccounts(result.accounts);return;}
      if(action==='period'&&result.status==='preview') {periodJobId=jobId;renderPreview(result);return;}
      setStatus('Der Abruf lieferte ein unerwartetes Ergebnis.',true);
    };
    async function poll(jobId,action) {
      const started=Date.now();
      while(current()&&Date.now()-started<330000) {
        await new Promise(resolve=>window.setTimeout(resolve,1000));
        if(!current())return;
        const response=await request('/api/administration/postbank-state',{job_id:jobId});
        if(!current())return;
        if(!response.ok) {setStatus(errorText(response.code,record.bankId,response.diagnostic),true);return;}
        const state=response.result;
        if(!state||state.job_id!==jobId) {setStatus('Der Abrufstatus ist nicht verfügbar.',true);return;}
        if(state.status==='running')continue;
        if(state.status==='error') {setStatus(errorText(state.code,record.bankId,state.diagnostic),true);return;}
        if(state.status==='complete') {handleResult(action,state.result,jobId);return;}
        setStatus('Der Abrufstatus ist nicht verfügbar.',true);return;
      }
      if(current())setStatus('Der Abruf dauert länger als erwartet. Es wurde kein neuer Abruf gestartet.',true);
    }
    async function startRead(action) {
      if(busy||!current())return;
      if(action==='period') {
        if(!ui.source?.value||!ui.target?.value||!/^\d{4}-\d{2}$/.test(ui.month.value)
            ||!/^\d{4}-\d{2}-\d{2}$/.test(ui.asOf.value)||ui.asOf.value<`${ui.month.value}-01`) {
          setStatus('Wähle ein Bankkonto, ein Zielkonto und einen gültigen Zeitraum.',true);return;
        }
      }
      setBusy(true);
      ui.legacyConfirmation = null;
      ui.updateCommitButton = null;
      results.replaceChildren();
      setStatus(record.bankId==='NASPA'
        ? `${bankName}-${action==='accounts'?'Konten':'Buchungen'} werden gelesen. Falls NASPA eine Freigabe anfordert, bestätige sie in der S-pushTAN-App.`
        : action==='accounts'?`${bankName}-Konten werden gelesen.`
        : `${bankName}-Buchungen werden gelesen. Falls nötig, bestätige die Freigabe in Deiner Banking-App.`);
      const selected=fields();
      const data={id:record.id,revision:record.revision,confirmed:true,
        tan_method:record.bankId==='ING'?null:auth.tan_method,
        tan_medium:record.bankId==='ING'?null:auth.tan_medium,action,account_fingerprint:action==='period'?selected.account_fingerprint:null,
        account_id:action==='period'?selected.account_id:null,month_start:action==='period'?selected.month_start:null,
        as_of:action==='period'?selected.as_of:null};
      const response=await request('/api/administration/postbank-read',data);
      if(!current())return;
      if(!response.ok) {setStatus(errorText(response.code,record.bankId,response.diagnostic),true);setBusy(false);return;}
      const job=response.result;
      if(!job||job.status!=='running'||typeof job.job_id!=='string'||!job.job_id) {
        setStatus(errorText(job?.code,record.bankId,job?.diagnostic),true);setBusy(false);return;
      }
      await poll(job.job_id,action);
      if(current())setBusy(false);
    }
    async function commitImport() {
      if(busy||!current()||!periodJobId||!reviewToken)return;
      if (ui.legacyConfirmation && !ui.legacyConfirmation.checked) return;
      setBusy(true);
      setStatus('Geprüfte Buchungen werden übernommen.');
      const data = {
        job_id:periodJobId,review_token:reviewToken,confirmed:true,
      };
      if (ui.legacyConfirmation) data.confirmed_legacy_text_differences = true;
      const response=await request('/api/administration/postbank-commit',data);
      if(!current())return;
      if(!response.ok) {setStatus(errorText(response.code,record.bankId),true);setBusy(false);return;}
      const result=response.result;
      if(!result||result.status!=='imported') {setStatus('Die Übernahme konnte nicht bestätigt werden.',true);setBusy(false);return;}
      const summary=node('div','','pbt-imported');
      summary.append(node('strong',`${Number.isSafeInteger(result.inserted)?result.inserted:0} neue Buchungen übernommen.`));
      summary.append(node('p',`${Number.isSafeInteger(result.skipped)?result.skipped:0} bereits vorhandene Buchungen übersprungen.`));
      if (result.refresh_configured === false) {
        summary.append(node('p','Import erfolgreich; automatische Aktualisierung nicht eingerichtet. Einrichtung erneut bestätigen.','pbt-import-warning'));
      }
      results.replaceChildren(summary);
      ui.legacyConfirmation = null;
      ui.updateCommitButton = null;
      reviewToken=null;
      setStatus(result.refresh_configured === false
        ? 'Buchungen übernommen. Die automatische Aktualisierung ist nicht eingerichtet.'
        : `Die geprüften ${bankName}-Buchungen wurden übernommen.`);
      setBusy(false);
      try {const updated=refresh();if(updated&&typeof updated.catch==='function')updated.catch(()=>{});}catch(_){/* Ignore refresh failure after a completed import. */}
    }

    accountAction.addEventListener('click',()=>startRead('accounts'));
    const setup=bankName==='ING'
      ? node('p','Für den ING-Abruf ist eine Freigabe in der ING-App alle 90 Tage nötig. Zusätzlich muss der Zugang einmal auf dem Server hinterlegt sein.','pbt-disclosure')
      : null;
    if(setup) content.prepend(setup);
    window.api('/api/administration/postbank-targets',{id:record.id,revision:record.revision})
      .then(response=>{
        if(!current())return;
        if(!response||!Array.isArray(response.accounts)) {setStatus('Zielkonten konnten nicht geladen werden.',true);return;}
        targets=response.accounts.filter(item=>item&&typeof item.id==='string'&&item.id
          &&typeof item.name==='string'&&typeof item.currency==='string');
        if(!targets.length) {setStatus('Es gibt kein verfügbares Finance-Control-Zielkonto.',true);return;}
        accountAction.disabled=false;
        setStatus('Bereit. Der Abruf startet erst nach Deiner Auswahl.');
      }).catch(()=>{if(current())setStatus('Zielkonten konnten nicht geladen werden.',true);});
  }

    window.postbankTransactions={bind};
})();
