'use strict';
// Planning metadata only. Original bookings and financial calculations stay on the server.
(() => {
  let active = null;
  let epoch = 0;
  const node = (tag, text, cls = '') => {
    const n = document.createElement(tag);
    if (text !== undefined) n.textContent = String(text);
    if (cls) n.className = cls;
    return n;
  };
  const button = (text, handler, cls = '') => {
    const n = node('button', text, cls); n.type = 'button';
    n.addEventListener('click', handler); return n;
  };
  const money = (value, currency) => {
    if (typeof value !== 'string' || !/^[+-]?\d+(?:\.\d{1,2})?$/.test(value)
        || !/^[A-Z]{3}$/.test(currency || '') || currency === 'XXX') return 'Unbekannt';
    return new Intl.NumberFormat('de-DE', {style:'currency', currency}).format(value);
  };
  const date = value => /^\d{4}-\d{2}-\d{2}$/.test(value || '')
    ? value.split('-').reverse().join('.') : 'unbekannt';
  const phrase = error => ({
    stale_booking_contract_revision:'Die Vertragszuordnung wurde inzwischen geändert. Lade sie neu; Deine Eingaben werden nicht automatisch gespeichert.',
    stale_recurring_revision:'Die Zahlungsregel wurde inzwischen geändert. Bitte neu laden.',
    stale_document_revision:'Der Vertragsbeleg wurde inzwischen geändert. Bitte neu laden.',
    duplicate_recurring_schedule:'Diese Zahlungsregel existiert bereits. Wähle die vorhandene Regel.',
    invalid_recurring_account_currency:'Konto oder Währung passen nicht zur Buchung.',
    contract_phase_amount_authoritative:'Der Betrag stammt aus den Vertragsphasen. Öffne den Vertragsbeleg zum Bearbeiten.',
    recurring_setting_outside_contract:'Die Planung liegt außerhalb der belegten Vertragslaufzeit.',
    zero_recurring_amount:'Bitte einen Vertragsbetrag ungleich null eingeben.',
    booking_contract_transfer_not_supported:'Eine Umbuchung lässt sich hier nicht als Vertrag bestätigen.',
    booking_not_suitable_for_contract:'Eine Umbuchung oder Nullbuchung lässt sich hier nicht als Vertrag bestätigen.',
    booking_contract_account_currency_mismatch:'Diese Zahlungsregel gehört zu einem anderen Konto oder einer anderen Währung.',
    booking_contract_document_authoritative:'Für diese Buchung liegt ein maßgeblicher Vertragsbeleg vor. Wähle dessen Zahlungsregel und prüfe die Vertragsphasen.',
  })[error?.message] || 'Die Vertragsangaben konnten nicht gespeichert werden. Prüfe die Eingaben oder lade den aktuellen Stand neu.';
  const current = session => active === session && session.dialog.isConnected;
  const call = (action, payload) => api('/api/booking-contract-' + action, payload);
  function documentButton(parent, id, label = 'Beleg öffnen') {
    if (!Number.isInteger(id) || id < 1) return null;
    const b = button(label, () => document.dispatchEvent(new CustomEvent('finance-purchase-edit', {detail:{id}})), 'bc-document');
    parent.append(b); return b;
  }
  function documentList(parent, documents) {
    if (!Array.isArray(documents)) return;
    for (const doc of documents) documentButton(parent, doc.id,
      `${doc.kind === 'contract' ? 'Vertrag' : 'Beleg'}: ${doc.vendor || doc.title || '#' + doc.id}`);
  }
  function decorate(parent, booking) {
    const context = booking?.contract_context;
    if (!context) return;
    const labels = {confirmed:'Vertrag bestätigt', matched:'Vertrag · Zuordnung prüfen',
      ambiguous:'Vertragszuordnung offen', rejected:'Kein Vertrag bestätigt'};
    if (labels[context.decision]) parent.append(button(labels[context.decision], () => void open(booking), 'bc-marker'));
    if (context.documents?.length) {
      const links = node('div', undefined, 'bc-document-links');
      documentList(links, context.documents); parent.append(links);
    }
  }
  function mount(parent, booking) {
    const box = node('section', undefined, 'bc-inline');
    box.append(node('strong', 'Vertrag & regelmäßige Zahlung'));
    box.append(button(booking?.contract_context?.decision === 'confirmed'
      ? 'Vertrag und Betrag prüfen' : 'Vertrag und Betrag bestätigen', () => void open(booking), 'bc-open'));
    parent.append(box);
  }
  function field(form, name, label, type, value, required = true) {
    const wrap = node('label', undefined, 'bc-field'), control = document.createElement('input');
    wrap.append(node('span', label)); control.name = name; control.type = type;
    control.value = value ?? ''; control.required = required; wrap.append(control); form.append(wrap);
    return control;
  }
  async function open(booking) {
    if (!booking || typeof booking.account_id !== 'string' || typeof booking.external_id !== 'string') return false;
    if (active?.busy) return false;
    active?.dialog.close();
    const session = {key:{account_id:booking.account_id, external_id:booking.external_id}, busy:false,
      dialog:node('dialog', undefined, 'bc-dialog'), opener:document.activeElement, epoch:++epoch};
    active = session;
    session.dialog.setAttribute('aria-labelledby', 'bc-title');
    session.dialog.addEventListener('cancel', e => {if (session.busy) e.preventDefault();});
    session.dialog.addEventListener('close', () => {
      if (active === session) active = null;
      session.dialog.remove();
      if (session.opener?.isConnected) session.opener.focus({preventScroll:true});
    });
    document.body.append(session.dialog); session.dialog.showModal();
    await load(session); return true;
  }
  async function load(session) {
    session.dialog.replaceChildren(node('p', 'Vertragsangaben werden geladen …'));
    try {
      const data = await call('view', session.key);
      if (!current(session)) return;
      session.data = data; render(session);
    } catch (_) {
      if (!current(session)) return;
      session.dialog.replaceChildren(node('p', 'Vertragsangaben konnten nicht geladen werden.', 'bc-error'),
        button('Erneut laden', () => void load(session)), button('Schließen', () => session.dialog.close()));
    }
  }
  function render(session) {
    const {data, dialog} = session, tx = data.booking;
    dialog.replaceChildren();
    const heading = node('h2', 'Vertrag direkt an der Buchung'); heading.id = 'bc-title';
    const close = button('Schließen', () => {if (!session.busy) dialog.close();}, 'bc-close');
    const header = node('div', undefined, 'bc-header'); header.append(heading, close); dialog.append(header);
    dialog.append(node('p', `${tx.counterparty || 'Buchung'} · ${date(tx.date)} · Original ${money(tx.amount, tx.currency)}`, 'bc-original'));
    const documents = node('div', undefined, 'bc-documents'); documentList(documents, data.documents); dialog.append(documents);
    const choiceLabel = node('label', undefined, 'bc-field'); choiceLabel.append(node('span', 'Vorhandene Regel oder neue Zahlung'));
    const choice = document.createElement('select'); choice.name = 'rule_choice';
    const fresh = node('option', 'Neue regelmäßige Zahlung'); fresh.value = 'new'; choice.append(fresh);
    const rules = new Map();
    const ruleKey = r => r.document_id ? 'document:' + r.document_id : 'rule:' + r.id;
    for (const r of [data.rule, ...(data.suggested_rules || [])].filter(Boolean)) {
      const key = ruleKey(r); if (rules.has(key)) continue;
      rules.set(key, r); const option = node('option', r.label || 'Vorhandene Regel'); option.value = key; choice.append(option);
    }
    choice.value = data.rule ? ruleKey(data.rule) : 'new';
    choiceLabel.append(choice); dialog.append(choiceLabel);
    const fields = node('div', undefined, 'bc-editor-fields'); dialog.append(fields);
    const show = () => {
      const rule = rules.get(choice.value) || null, doc = Boolean(rule?.document_id);
      fields.replaceChildren();
      const form = node('form', undefined, 'bc-form'); fields.append(form);
      const label = field(form,'label','Vertragsbezeichnung','text',rule?.label || tx.counterparty || '',true); label.maxLength = 160;
      const currency = field(form,'currency','Währung','text',tx.currency); currency.readOnly = true;
      const amount = field(form,'amount','Vertragsbetrag (+ Eingang / − Ausgabe)','text',doc ? '' : rule?.amount ?? tx.amount,!doc);
      amount.inputMode = 'decimal'; amount.pattern = '(?:\\+|−|-)?[0-9]+([.,][0-9]{1,2})?'; amount.disabled = doc;
      const day = field(form,'day_of_month','Voraussichtlicher Zahlungstag','number',rule?.day_of_month ?? Number(tx.date.slice(8,10)));
      day.min = 1; day.max = 31;
      const interval = field(form,'interval_months','Abstand in Monaten','number',rule?.interval_months ?? 1); interval.min=1; interval.max=120;
      const from = field(form,'valid_from','Gültig ab','date',rule?.valid_from || tx.date);
      const to = field(form,'valid_to','Gültig bis (falls bekannt)','date',rule?.valid_to || '',false);
      const reference = field(form,'reference','Erkennungsmerkmal im Buchungstext','text',rule?.reference || tx.counterparty || '',false); reference.maxLength=240;
      if (doc) {
        label.disabled = true; from.disabled = true; to.disabled = true; interval.disabled = true; reference.disabled = true;
        form.append(node('p', 'Betrag, Rhythmus und Laufzeit folgen den bestätigten Vertragsphasen.', 'bc-help'));
        for (const phase of rule.phases || []) form.append(node('p', `${date(phase.valid_from)} – ${date(phase.valid_to)}: ${money(phase.amount,tx.currency)} · alle ${phase.interval_months} Monate`, 'bc-help'));
        documentButton(form, rule.document_id, 'Vertragsbeleg und Phasen öffnen');
      } else form.append(node('p', 'Ohne belegtes Enddatum ist die Fortsetzung eine Annahme. Der Originalbetrag der Buchung bleibt erhalten.', 'bc-help'));
      const confirmLabel = node('label', undefined, 'bc-confirm'), confirmed = document.createElement('input');
      confirmed.type='checkbox'; confirmed.name='confirmed'; confirmed.required=true;
      confirmLabel.append(confirmed,node('span','Ich bestätige Betrag, Rhythmus, Zahlungstag und Laufzeit für diese Planung.')); form.append(confirmLabel);
      const status = node('p', undefined, 'bc-status'); status.setAttribute('role','alert'); form.append(status);
      const actions = node('div', undefined, 'bc-actions'), save = node('button','Vertrag und Betrag bestätigen'); save.type='submit';
      const reject = button('Diese Buchung ist kein Vertrag', () => {
        if (session.busy || form.querySelector('.bc-reject-confirm')) return;
        const review = node('div',undefined,'bc-reject-confirm');
        review.append(node('p','Nur die Vertragszuordnung dieser Buchung wird abgelehnt. Eine gemeinsame Zahlungsregel und Originalbuchungen bleiben erhalten.'),
          button('Ablehnung bestätigen',()=>void write('reject',{confirmed:true})),
          button('Abbrechen',()=>review.remove(),'secondary'));
        form.append(review);
      },'secondary');
      actions.append(save,reject,button('Neu laden',()=>{if(!session.busy)void load(session);},'secondary')); form.append(actions);
      const lock = value => {session.busy=value; choice.disabled=value; close.disabled=value;
        for (const control of form.querySelectorAll('button')) control.disabled=value;};
      async function write(action, extra) {
        if (session.busy || !current(session)) return;
        lock(true); status.textContent='Angaben werden gespeichert …';
        try {
          await call(action,{...session.key,revision:data.revision,...extra});
          if (!current(session)) return;
          dialog.close(); document.dispatchEvent(new CustomEvent('finance-booking-contract-changed',{detail:session.key}));
        } catch (error) {if(current(session))status.textContent=phrase(error);}
        finally {if(current(session))lock(false);}
      }
      form.addEventListener('submit', e => {
        e.preventDefault(); if (session.busy || !form.reportValidity()) return;
        void write('save',{confirmed:confirmed.checked,rule:{
          id:rule?.id ?? null,revision:rule?.revision || 0,
          document_id:rule?.document_id ?? null,document_revision:rule?.document_revision ?? null,
          label:label.value,account_id:tx.account_id,currency:tx.currency,
          amount:doc ? null : amount.value.trim().replace('−','-').replace(',','.'),
          valid_from:from.value,valid_to:to.value || null,day_of_month:Number(day.value),
          interval_months:Number(interval.value),reference:reference.value,confirmed:confirmed.checked,
        }});
      });
    };
    choice.addEventListener('change',show); show();
  }
  window.financeBookingContracts = {decorate,mount,documentButton,open};
})();
