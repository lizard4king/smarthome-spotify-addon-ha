'use strict';

(() => {
  const node = (tag, text = '', className = '') => {
    const element = document.createElement(tag);
    element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const safe = (value, max = 180) => typeof value === 'string' && value.length <= max ? value : '';
  const validDate = value => {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const parsed = Date.parse(`${value}T00:00:00Z`);
    return Number.isFinite(parsed) && new Date(parsed).toISOString().slice(0, 10) === value;
  };
  const canonicalMoney = (raw, optional = false) => {
    const value = String(raw ?? '').trim().replace(',', '.');
    if (optional && !value) return null;
    if (!/^\d+(?:\.\d{1,2})?$/.test(value)) return null;
    return Number(value).toFixed(2);
  };
  const canonicalRate = raw => {
    const value = String(raw ?? '').trim().replace(',', '.');
    if (!value) return null;
    if (!/^\d+(?:\.\d{1,4})?$/.test(value)) return undefined;
    return value;
  };
  const currencyAmount = value => {
    if (typeof value !== 'string' || !/^\d+(?:\.\d{1,2})?$/.test(value)) return 'Nicht erfasst';
    return new Intl.NumberFormat('de-DE', {style: 'currency', currency: 'EUR'}).format(Number(value));
  };
  const cashDifference = (amount, interest) => {
    const payment = canonicalMoney(amount), interestAmount = canonicalMoney(interest);
    if (payment == null || interestAmount == null) return null;
    const principal = Math.round(Number(payment) * 100) - Math.round(Number(interestAmount) * 100);
    return principal < 0 ? null : (principal / 100).toFixed(2);
  };
  const safeError = (error, fallback) => {
    const message = typeof error?.message === 'string' ? error.message.trim() : '';
    return message && message.length <= 240 ? message : fallback;
  };
  const validForecastReference = value => {
    if (typeof value !== 'string' || /\p{C}/u.test(value)) return false;
    const reference = value.trim();
    if (reference.length < 4 || reference.length > 240 || !/\d/.test(reference)) return false;
    if (/^[\d\s.,+\-€]+(?:EUR)?$/i.test(reference)) return false;
    return /\p{L}/u.test(reference.replace(/\bEUR\b|€/gi, ''));
  };

  function rowGroup(title, fields, onRemove) {
    const row = node('fieldset', '', 'pc-row-group');
    row.append(node('legend', title));
    for (const [key, label, type] of fields) {
      const fieldLabel = node('label', label);
      const input = document.createElement('input');
      input.name = key; input.type = type || 'text'; input.autocomplete = 'off';
      if (key === 'date') input.placeholder = 'JJJJ-MM-TT';
      if (key === 'amount' || key === 'interest') input.inputMode = 'decimal';
      fieldLabel.append(input); row.append(fieldLabel);
    }
    const remove = node('button', 'Zeile entfernen', 'pc-remove');
    remove.type = 'button'; remove.addEventListener('click', onRemove);
    row.append(remove);
    return row;
  }

  function makeForm(section, status, current, onSaved) {
    const form = node('form', '', 'pb-card pc-form');
    form.hidden = true;
    const title = node('h3', 'Kreditstand erfassen', 'pb-heading');
    const identity = node('input'); identity.type = 'hidden'; identity.name = 'loan_id';
    const revision = node('input'); revision.type = 'hidden'; revision.name = 'revision';
    form.append(title, identity, revision);
    const fields = [
      ['label', 'Bezeichnung', 'text'], ['as_of', 'Stand am', 'date'],
      ['net_credit', 'Nettodarlehen in Euro', 'text'], ['total_interest', 'Zinsen gesamt in Euro', 'text'],
      ['total_payable', 'Gesamtbetrag in Euro', 'text'], ['paid_total', 'Bisher gezahlt in Euro', 'text'],
      ['remaining_total', 'Restbetrag inkl. künftiger Zinsen in Euro', 'text'],
      ['apr', 'Effektiver Jahreszins in Prozent', 'text'],
      ['outstanding_principal', 'Offene Kreditsumme ohne künftige Zinsen in Euro', 'text'],
    ];
    const inputs = {};
    for (const [name, label, type] of fields) {
      const fieldLabel = node('label', label);
      const input = document.createElement('input');
      input.name = name; input.type = type; input.autocomplete = 'off';
      if (type === 'date') input.placeholder = 'JJJJ-MM-TT';
      if (type === 'text' && name !== 'label') input.inputMode = 'decimal';
      if (['label', 'as_of', 'net_credit', 'total_interest', 'total_payable', 'paid_total',
        'remaining_total'].includes(name)) input.required = true;
      inputs[name] = input; fieldLabel.append(input); form.append(fieldLabel);
    }
    form.append(node('p', 'Währung: Euro (EUR). Geldbeträge können mit Komma oder Punkt eingegeben werden.', 'pb-note'));
    const paymentRows = node('div', '', 'pc-repeating-rows');
    const scheduleRows = node('div', '', 'pc-repeating-rows');
    const addPayment = node('button', 'Zahlung hinzufügen', 'pb-secondary'); addPayment.type = 'button';
    const addSchedule = node('button', 'Planrate hinzufügen', 'pb-secondary'); addSchedule.type = 'button';
    function addRow(target, titleText, fieldsForRow) {
      let row;
      row = rowGroup(titleText, fieldsForRow, () => row.remove());
      target.append(row);
    }
    addPayment.addEventListener('click', () => addRow(paymentRows, 'Bisherige Zahlung', [
      ['date', 'Datum', 'date'], ['amount', 'Rate in Euro'], ['interest', 'Zinsanteil in Euro (optional)'],
    ]));
    addSchedule.addEventListener('click', () => addRow(scheduleRows, 'Künftige Rate', [
      ['date', 'Datum', 'date'], ['amount', 'Rate in Euro'],
    ]));
    const paymentsHeading = node('h4', 'Historische Zahlungen');
    const scheduleHeading = node('h4', 'Verbleibender Ratenplan');
    form.append(paymentsHeading, paymentRows, addPayment, scheduleHeading, scheduleRows, addSchedule);
    const confirmWrap = node('label', '', 'pb-check');
    const confirm = document.createElement('input'); confirm.type = 'checkbox';
    confirmWrap.append(confirm, node('span', 'Ich habe diese Angaben im PayPal-Konto geprüft und bestätige den datierten Stand.'));
    const regularWrap = node('label', '', 'pb-check');
    const regular = document.createElement('input'); regular.type = 'checkbox';
    regularWrap.append(regular, node('span', 'Vollständiger, unveränderter Plan: nur reguläre Raten, keine Sondertilgung, Erstattung, Gebühren oder noch unverarbeiteten Zahlungen.'));
    form.append(confirmWrap, regularWrap);
    const submit = node('button', 'Kreditstand speichern'); submit.type = 'submit';
    const cancel = node('button', 'Abbrechen', 'pb-secondary'); cancel.type = 'button';
    cancel.addEventListener('click', () => { form.hidden = true; form.reset(); current.loanId = null; current.revision = null; });
    form.append(submit, cancel);

    function setValue(name, value) { inputs[name].value = value == null ? '' : String(value); }
    function fill(entry = null) {
      form.reset(); confirm.checked = false; regular.checked = false; paymentRows.replaceChildren(); scheduleRows.replaceChildren();
      current.loanId = entry?.loan_id || null; current.revision = Number.isInteger(entry?.revision) ? entry.revision : null;
      identity.value = current.loanId || ''; revision.value = current.revision == null ? '' : String(current.revision);
      title.textContent = entry ? 'Kreditstand aktualisieren' : 'Kreditstand erfassen';
      submit.textContent = entry ? 'Stand aktualisieren' : 'Kreditstand speichern';
      const plan = entry?.plan || {};
      for (const [name] of fields) setValue(name, name === 'currency' ? 'EUR' : plan[name]);
      for (const payment of (Array.isArray(plan.payments) ? plan.payments : [])) {
        addPayment.click();
        const row = paymentRows.lastElementChild;
        row.querySelector('[name="date"]').value = safe(payment.date, 10);
        row.querySelector('[name="amount"]').value = safe(payment.amount, 32);
        row.querySelector('[name="interest"]').value = payment.interest == null ? '' : safe(payment.interest, 32);
      }
      for (const payment of (Array.isArray(plan.schedule) ? plan.schedule : [])) {
        addSchedule.click();
        const row = scheduleRows.lastElementChild;
        row.querySelector('[name="date"]').value = safe(payment.date, 10);
        row.querySelector('[name="amount"]').value = safe(payment.amount, 32);
      }
    }
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const values = Object.fromEntries(fields.map(([name]) => [name, inputs[name].value.trim()]));
      if (!confirm.checked) { setMessage(status, 'Bitte bestätige den geprüften Stand.'); return; }
      if (!regular.checked) { setMessage(status, 'Bitte bestätige den vollständigen, unveränderten Ratenplan.', true); return; }
      if (!values.label || !validDate(values.as_of)) { setMessage(status, 'Bitte Bezeichnung und gültiges Standdatum prüfen.'); return; }
      const plan = {label: values.label.slice(0, 120), as_of: values.as_of, currency: 'EUR',
        regular_plan_confirmed: true};
      for (const key of ['net_credit', 'total_interest', 'total_payable', 'paid_total', 'remaining_total']) {
        plan[key] = canonicalMoney(values[key]);
        if (plan[key] == null) { setMessage(status, `Bitte den Betrag „${fields.find(field => field[0] === key)[1]}“ prüfen.`, true); return; }
      }
      plan.apr = canonicalRate(values.apr);
      if (plan.apr == null || plan.apr === undefined) { setMessage(status, 'Bitte den Jahreszins angeben und prüfen.', true); return; }
      const optionalMoney = values.outstanding_principal === '' ? null : canonicalMoney(values.outstanding_principal);
      if (values.outstanding_principal !== '' && optionalMoney == null) {
        setMessage(status, 'Bitte die offene Kreditsumme prüfen oder das Feld leer lassen.', true); return;
      }
      plan.outstanding_principal = optionalMoney;
      const readRows = (target, withInterest) => {
        const rows = [];
        for (const row of target.querySelectorAll('.pc-row-group')) {
          const date = row.querySelector('[name="date"]').value.trim();
          const amountRaw = row.querySelector('[name="amount"]').value.trim();
          const interestRaw = withInterest ? row.querySelector('[name="interest"]').value.trim() : '';
          if (!date && !amountRaw && !interestRaw) continue;
          const amount = canonicalMoney(amountRaw);
          if (!validDate(date) || amount == null) return null;
          if (withInterest) {
            const interest = canonicalMoney(interestRaw, true);
            if (interestRaw && interest == null) return null;
            rows.push({date, amount, interest});
          } else rows.push({date, amount});
        }
        return rows;
      };
      plan.payments = readRows(paymentRows, true);
      plan.schedule = readRows(scheduleRows, false);
      if (!plan.payments || !plan.schedule) { setMessage(status, 'Bitte Datum und Betrag in allen angefangenen Zeilen prüfen.', true); return; }
      const payload = {loan_id: current.loanId, revision: current.revision, confirmed: true, plan};
      submit.disabled = true; setMessage(status, 'Stand wird gespeichert.');
      try {
        await window.api('/api/administration/paypal-credit-save', payload);
        form.hidden = true; form.reset(); current.loanId = null; current.revision = null;
        setMessage(status, 'Der manuell geprüfte Stand wurde gespeichert.');
        try { await onSaved(); }
        catch (_) { setMessage(status, 'Der Stand ist gespeichert, die Liste konnte aber nicht aktualisiert werden.', true); }
      } catch (error) { setMessage(status, safeError(error, 'Der Kreditstand konnte nicht gespeichert werden. Bitte prüfe den Serverstatus.'), true); }
      finally { submit.disabled = false; }
    });
    return {form, fill};
  }

  function setMessage(target, text, error = false) {
    target.textContent = text;
    target.classList.toggle('pb-error', error);
  }

  function renderPlan(entry) {
    if (!entry || entry.source !== 'manual_paypal_portal' || !entry.plan || typeof entry.plan !== 'object') return null;
    const plan = entry.plan;
    const card = node('article', '', 'pb-row pc-plan-card');
    const label = safe(plan.label, 120) || 'Manuell erfasster Kredit';
    const asOf = safe(plan.as_of, 10);
    card.append(node('h4', label));
    card.append(node('small', `Manuell geprüft · Stand ${asOf || 'nicht ausgewiesen'}`));
    const remaining = node('strong', `Restbetrag inkl. künftiger Zinsen: ${currencyAmount(plan.remaining_total)}`, 'pc-remaining');
    card.append(remaining);
    const overview = document.createElement('dl');
    const derived = entry.derived && typeof entry.derived === 'object' ? entry.derived : {};
    overview.append(node('dt', 'Offene Kreditsumme ohne künftige Zinsen'), node('dd', plan.outstanding_principal == null
      ? 'Nicht ausgewiesen' : currencyAmount(plan.outstanding_principal)));
    const dueDate = safe(derived.next_due_date, 10), dueAmount = currencyAmount(derived.next_payment_amount);
    overview.append(node('dt', 'Nächste Rate'), node('dd', dueDate
      ? `${dueDate} · ${dueAmount}` : 'Nicht erfasst'));
    const paid = Number.isInteger(derived.paid_installments) ? derived.paid_installments : null;
    const total = Number.isInteger(derived.total_installments) ? derived.total_installments : null;
    overview.append(node('dt', 'Bezahlte Raten'), node('dd', paid != null && total != null
      ? `${paid} von ${total}` : 'Nicht erfasst'));
    card.append(overview);
    const details = node('details', '', 'pc-plan-details');
    details.append(node('summary', 'Finanzierung und Ratenplan'));
    details.append(node('p', 'Nur der bestätigte, unveränderte Plan regulärer Raten ist erfasst. Sondertilgungen, Erstattungen, Gebühren und noch unverarbeitete Zahlungen sind nicht abgebildet.', 'pb-note'));
    const financing = document.createElement('dl');
    const payments = Array.isArray(plan.payments) ? plan.payments : [];
    const canShowDerivedSplit = plan.regular_plan_confirmed === true && payments.length > 0 &&
      payments.every(payment => payment && payment.interest != null);
    for (const [labelText, value] of [
      ['Nettodarlehen', currencyAmount(plan.net_credit)], ['Zinsen gesamt', currencyAmount(plan.total_interest)],
      ['Gesamtbetrag', currencyAmount(plan.total_payable)], ['Effektiver Jahreszins',
        typeof plan.apr === 'string' || typeof plan.apr === 'number' ? `${String(plan.apr)} %` : 'Nicht erfasst'],
      ['Bisher gezahlt', currencyAmount(plan.paid_total)],
      ['Bisherige Zinsen', canShowDerivedSplit ? currencyAmount(derived.paid_interest) : 'Nicht erfasst'],
      ['Bisherige Tilgung', canShowDerivedSplit ? currencyAmount(derived.paid_principal) : 'Nicht erfasst'],
    ]) financing.append(node('dt', labelText), node('dd', value));
    details.append(financing);
    details.append(node('h5', 'Bisherige Zahlungen'));
    if (!payments.length) details.append(node('p', 'Keine historischen Zahlungen erfasst.', 'pb-note'));
    for (const payment of payments) {
      const row = node('div', '', 'pc-payment-row');
      row.append(node('strong', safe(payment.date, 10) || 'Datum nicht erfasst'));
      row.append(node('span', currencyAmount(payment.amount)));
      if (plan.regular_plan_confirmed !== true || payment.interest == null)
        row.append(node('small', 'Zins- und Tilgungsanteil nicht erfasst.'));
      else {
        const principal = cashDifference(payment.amount, payment.interest);
        row.append(node('small', principal == null ? 'Zins- und Tilgungsanteil nicht erfasst.' :
          `Zinsen: ${currencyAmount(payment.interest)} · Tilgung: ${currencyAmount(principal)}`));
      }
      details.append(row);
    }
    details.append(node('h5', 'Verbleibender Ratenplan'));
    const schedule = Array.isArray(plan.schedule) ? plan.schedule : [];
    if (!schedule.length) details.append(node('p', 'Keine künftigen Raten erfasst.', 'pb-note'));
    for (const payment of schedule) {
      const row = node('div', '', 'pc-schedule-row');
      row.append(node('span', safe(payment.date, 10) || 'Datum nicht erfasst'));
      row.append(node('strong', currencyAmount(payment.amount)));
      details.append(row);
    }
    card.append(details);
    return card;
  }

  async function renderBookings(host) {
    if (!host || typeof host.replaceChildren !== 'function') return;
    host.replaceChildren(); host.classList.add('pb-host');
    const section = node('section', '', 'pb-card pc-credit-section');
    section.append(node('h3', 'Manuell geprüfter Kreditstand', 'pb-heading'));
    section.append(node('p', 'Getrennt vom PayPal-Kontoabruf. Hier erfasste Kreditangaben stammen aus einer eigenen Prüfung im PayPal-Konto.', 'pb-note'));
    const status = node('p', '', 'pb-action-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const plans = node('div', '', 'pc-plan-list');
    const add = node('button', 'Kreditstand erfassen'); add.type = 'button';
    const current = {loanId: null, revision: null};
    let formControl;
    const isMounted = () => host.isConnected && host.contains(section) && section.isConnected;
    const forecastView = (loanId) => window.api('/api/administration/paypal-credit-forecast-view', {loan_id: loanId});
    const attachForecastControl = (card, entry) => {
      const area = node('div', '', 'pc-forecast');
      const toggle = node('button', 'Mit Kontoprognose verknüpfen', 'pb-secondary');
      toggle.type = 'button';
      const panel = node('div', '', 'pc-forecast-panel'); panel.hidden = true;
      const feedback = node('p', '', 'pb-action-status'); feedback.setAttribute('role', 'status'); feedback.setAttribute('aria-live', 'polite');
      area.append(toggle, panel); card.append(area);
      let open = false;
      let view = null;
      let displayedSourceRevision = entry.revision;
      let panelGeneration = 0;
      const showMessage = (text, error = false) => {
        feedback.textContent = text; feedback.classList.toggle('pb-error', error);
      };
      const isCurrentPanel = generation => isMounted() && open && generation === panelGeneration;
      const renderPanel = nextView => {
        view = nextView;
        panel.replaceChildren();
        panel.hidden = false; open = true;
        const link = view?.link && typeof view.link === 'object' ? view.link : null;
        const sourceChanged = Number.isInteger(view?.source_revision) && view.source_revision !== displayedSourceRevision;
        const currentSourcePlan = view?.plan && typeof view.plan === 'object' ? view.plan : null;
        toggle.textContent = link?.active ? 'Kontozuordnung ändern' : 'Mit Kontoprognose verknüpfen';
        if (link?.stale === true || sourceChanged) {
          panel.append(node('p', 'Planstand geändert – Kontozuordnung erneut bestätigen.', 'pc-forecast-stale'));
        } else if (link?.active === true) {
          const account = (view.accounts || []).find(item => item.id === link.account_id);
          panel.append(node('p', `Aktuelle Zuordnung: ${safe(account?.label, 180) || safe(link.account_id, 180)} · ${safe(link.reference, 240)}`, 'pc-forecast-current'));
        }
        const accountLabel = node('label', 'EUR-Girokonto');
        const accountSelect = document.createElement('select'); accountSelect.required = true;
        const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = 'Bitte Konto auswählen'; accountSelect.append(placeholder);
        const accounts = Array.isArray(view?.accounts) ? view.accounts.filter(account => account &&
          typeof account.id === 'string' && account.id && account.currency === 'EUR') : [];
        for (const account of accounts) {
          const option = document.createElement('option'); option.value = account.id;
          const label = safe(account.label, 180) || account.id;
          option.textContent = `${label}${safe(account.owner, 120) ? ` · ${safe(account.owner, 120)}` : ''}`;
          accountSelect.append(option);
        }
        accountSelect.value = accounts.some(account => account.id === link?.account_id) ? link.account_id : '';
        accountLabel.append(accountSelect); panel.append(accountLabel);
        if (!accounts.length) panel.append(node('p', 'Kein eigenes EUR-Girokonto für die Kontoprognose verfügbar.', 'pb-note'));
        const referenceLabel = node('label', 'Erkennbarer Buchungstext');
        const reference = document.createElement('input'); reference.type = 'text'; reference.minLength = 4; reference.maxLength = 240;
        reference.autocomplete = 'off'; reference.required = true; reference.value = safe(link?.reference, 240);
        reference.placeholder = 'Zum Beispiel RATEN1234'; referenceLabel.append(reference); panel.append(referenceLabel);
        panel.append(node('small', 'Eindeutige Kennung aus dem Buchungstext, mit Buchstaben und Ziffern (z. B. RATEN1234); Betrag oder „PayPal“ allein reichen nicht.', 'pc-forecast-help'));
        if (currentSourcePlan) {
          panel.append(node('p', `„${safe(currentSourcePlan.label, 120) || 'Manueller PayPal-Kredit'}“, Kennung und diese Raten werden für die gemeinsame Kontoprognose freigegeben und sind für alle Haushaltsmitglieder sichtbar. Kreditsumme und Jahreszins bleiben privat; Termine und Schlussrate unverändert.`, 'pb-note'));
        } else {
          panel.append(node('p', 'Der aktuelle Planstand konnte nicht geladen werden. Bitte schließe diese Zuordnung und lade den Kreditplan neu, bevor du sie bestätigst.', 'pc-forecast-stale'));
        }
        const schedule = Array.isArray(currentSourcePlan?.schedule) ? currentSourcePlan.schedule : [];
        const scheduleList = node('ul', '', 'pc-forecast-schedule');
        if (schedule.length) {
          schedule.forEach((payment, index) => scheduleList.append(node('li',
            `${safe(payment?.date, 10) || 'Datum nicht erfasst'} · ${currencyAmount(payment?.amount)}${index === schedule.length - 1 ? ' · Schlussrate' : ''}`)));
          panel.append(scheduleList);
        } else if (currentSourcePlan) panel.append(node('p', 'Im geprüften Plan sind keine künftigen Termine erfasst.', 'pb-note'));
        const confirmWrap = node('label', '', 'pb-check pc-forecast-confirm');
        const confirm = document.createElement('input'); confirm.type = 'checkbox';
        confirmWrap.append(confirm, node('span', 'Ich bestätige die Freigabe für die gemeinsame Kontoprognose.'));
        const removeConfirmWrap = node('label', '', 'pb-check pc-forecast-confirm');
        const removeConfirm = document.createElement('input'); removeConfirm.type = 'checkbox';
        removeConfirmWrap.append(removeConfirm, node('span', 'Ich bestätige, dass diese Kontozuordnung entfernt wird.'));
        removeConfirmWrap.hidden = !link?.active;
        panel.append(confirmWrap, removeConfirmWrap, feedback);
        const save = node('button', link?.active ? 'Zuordnung speichern' : 'Zuordnung bestätigen'); save.type = 'button'; save.disabled = true;
        const remove = node('button', 'Zuordnung entfernen', 'pb-secondary'); remove.type = 'button';
        remove.hidden = !link?.active; remove.disabled = true;
        const close = node('button', 'Schließen', 'pb-secondary'); close.type = 'button';
        const updateSave = () => {
          save.disabled = !currentSourcePlan || !confirm.checked || !accountSelect.value || !validForecastReference(reference.value);
        };
        confirm.addEventListener('change', updateSave);
        accountSelect.addEventListener('change', () => { confirm.checked = false; updateSave(); });
        reference.addEventListener('input', () => { confirm.checked = false; updateSave(); });
        removeConfirm.addEventListener('change', () => { remove.disabled = !removeConfirm.checked; });
        save.addEventListener('click', async () => {
          updateSave(); if (save.disabled) return;
          const generation = panelGeneration;
          save.disabled = true; remove.disabled = true;
          const payload = {loan_id: entry.loan_id, source_revision: view.source_revision,
            revision: link?.revision ?? 0, account_id: accountSelect.value,
            reference: reference.value.trim(), confirmed: true};
          try {
            await window.api('/api/administration/paypal-credit-forecast-save', payload);
            if (!isCurrentPanel(generation)) return;
            displayedSourceRevision = payload.source_revision;
            showMessage('Kontozuordnung wurde bestätigt.');
            try {
              const latest = await forecastView(entry.loan_id);
              if (isCurrentPanel(generation)) renderPanel(latest);
              } catch (_) { if (isCurrentPanel(generation)) showMessage('Die Zuordnung ist gespeichert. Der aktuelle Stand konnte aber nicht neu geladen werden.', true); }
          } catch (error) {
            if (!isCurrentPanel(generation)) return;
            if (error?.code === 'paypal_credit_forecast_stale') {
              showMessage('Der Kreditplan wurde geändert. Bitte prüfe die aktuelle Zuordnung und bestätige sie erneut.', true);
              confirm.checked = false; updateSave();
              try { const latest = await forecastView(entry.loan_id); if (isCurrentPanel(generation)) { renderPanel(latest); showMessage('Planstand geändert – Kontozuordnung erneut bestätigen.', true); } }
              catch (_) { if (isCurrentPanel(generation)) showMessage('Der Planstand ist veraltet. Bitte Zuordnung schließen und erneut öffnen.', true); }
            } else showMessage(safeError(error, 'Die Kontozuordnung konnte nicht gespeichert werden.'), true);
          } finally { if (isCurrentPanel(generation)) { updateSave(); remove.disabled = !removeConfirm.checked; } }
        });
        remove.addEventListener('click', async () => {
          if (!removeConfirm.checked || !link) return;
          const generation = panelGeneration;
          save.disabled = true; remove.disabled = true;
          try {
            await window.api('/api/administration/paypal-credit-forecast-remove', {
              loan_id: entry.loan_id, source_revision: view.source_revision,
              revision: link.revision, confirmed: true});
            if (!isCurrentPanel(generation)) return;
            showMessage('Kontozuordnung wurde entfernt.');
            try {
              const latest = await forecastView(entry.loan_id);
              if (isCurrentPanel(generation)) renderPanel(latest);
            } catch (_) { if (isCurrentPanel(generation)) showMessage('Die Zuordnung ist entfernt. Der aktuelle Stand konnte aber nicht neu geladen werden.', true); }
          } catch (error) {
            if (!isCurrentPanel(generation)) return;
            if (error?.code === 'paypal_credit_forecast_stale') {
              showMessage('Der Kreditplan oder die Zuordnung wurde geändert. Bitte erneut laden und bestätigen.', true);
              removeConfirm.checked = false;
              try { const latest = await forecastView(entry.loan_id); if (isCurrentPanel(generation)) { renderPanel(latest); showMessage('Planstand geändert – Kontozuordnung erneut bestätigen.', true); } }
              catch (_) { if (isCurrentPanel(generation)) showMessage('Der Planstand ist veraltet. Bitte Zuordnung schließen und erneut öffnen.', true); }
            } else showMessage(safeError(error, 'Die Kontozuordnung konnte nicht entfernt werden.'), true);
          } finally { if (isCurrentPanel(generation)) remove.disabled = !removeConfirm.checked; }
        });
        close.addEventListener('click', () => { panelGeneration++; panel.hidden = true; open = false; });
        panel.append(save, remove, close);
      };
      toggle.addEventListener('click', async () => {
        if (open) { panelGeneration++; panel.hidden = true; open = false; return; }
        const generation = ++panelGeneration;
        open = true;
        toggle.disabled = true; toggle.textContent = 'Zuordnung wird geladen …';
        try {
          const latest = await forecastView(entry.loan_id);
          if (isCurrentPanel(generation)) renderPanel(latest);
        } catch (error) {
          if (!isCurrentPanel(generation)) return;
          panel.replaceChildren(); panel.hidden = false; open = true;
          showMessage(safeError(error, 'Die Kontozuordnung konnte nicht geladen werden.'), true);
          panel.append(feedback);
        } finally {
          toggle.disabled = false;
          toggle.textContent = view?.link?.active ? 'Kontozuordnung ändern' : 'Mit Kontoprognose verknüpfen';
        }
      });
    };
    const reload = async result => {
      if (!result) result = await window.api('/api/administration/paypal-credit-list', {});
      if (!isMounted()) return;
      plans.replaceChildren();
      const entries = Array.isArray(result?.plans) ? result.plans : [];
      for (const entry of entries) {
        const card = renderPlan(entry);
        if (!card) continue;
        attachForecastControl(card, entry);
        const update = node('button', 'Stand aktualisieren', 'pb-secondary'); update.type = 'button';
        update.addEventListener('click', () => {
          formControl.fill(entry); formControl.form.hidden = false;
          formControl.form.scrollIntoView({block: 'nearest', behavior: 'smooth'});
        });
        card.append(update); plans.append(card);
      }
      if (!plans.children.length) plans.append(node('p', 'Noch kein manuell geprüfter Kreditstand erfasst.', 'pb-note'));
    };
    formControl = makeForm(section, status, current, () => reload());
    add.addEventListener('click', () => {
      formControl.fill(null); formControl.form.hidden = false;
      formControl.form.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    });
    section.append(status, plans, add, formControl.form);
    host.append(section);
    setMessage(status, 'Kreditstände werden geladen.');
    try {
      const result = await window.api('/api/administration/paypal-credit-list', {});
      if (!isMounted()) return;
      await reload(result);
      setMessage(status, '');
    } catch (error) {
      if (!isMounted()) return;
      setMessage(status, safeError(error, 'Manuelle Kreditstände konnten nicht geladen werden. Bitte prüfe den Serverstatus.'), true);
      plans.replaceChildren(node('p', 'Kreditstände sind derzeit nicht verfügbar.', 'pb-note'));
    }
  }

  window.PayPalCredit = {renderBookings};
})();
