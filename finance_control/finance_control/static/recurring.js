'use strict';
// Presentation only: schedules, booked matches and all financial values come from the server.
(() => {
  const today = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; };
  const initial = today(), end = new Date(initial+'T12:00:00'); end.setDate(end.getDate()+90);
  const view = {as_of:initial, until:`${end.getFullYear()}-${String(end.getMonth()+1).padStart(2,'0')}-${String(end.getDate()).padStart(2,'0')}`, account_id:'', status:'all', filterOpen:false, data:null, request:0};
  const node = (tag, cls, text) => { const n=document.createElement(tag); if(cls)n.className=cls; if(text!==undefined)n.textContent=text; return n; };
  const date = value => value ? new Date(value+'T12:00:00').toLocaleDateString('de-DE') : 'offen';
  const money = (value,currency='EUR') => value===null || value===undefined ? 'Unbekannt' : new Intl.NumberFormat('de-DE',{style:'currency',currency}).format(value);
  const button = (text, handler, cls='secondary') => { const n=node('button',cls,text); n.type='button'; n.addEventListener('click',handler); return n; };
  const link = (text, target) => { const n=node('a','fc-text-link',text);n.href='#'+target;return n; };
  const inventory = {page:0, request:0, data:null, error:null, loading:false, open:false};
  const inventoryRoot = node('details','rc-inventory');
  inventoryRoot.id='contract-inventory';
  inventoryRoot.addEventListener('toggle',()=>{inventory.open=inventoryRoot.open;});
  function renderInventory() {
    inventoryRoot.open=inventory.open;
    inventoryRoot.setAttribute('aria-busy',String(inventory.loading));
    inventoryRoot.replaceChildren(node('summary','',`Quelleninventar bestätigter Vertragsbelege${inventory.data?' · '+inventory.data.total:''}`));
    inventoryRoot.append(node('p','fc-caption','Gespeicherte Bestätigungen und offene Angaben. Buchungsnachweise ergänzen keine Vertragsbedingungen.'));
    if(inventory.loading)inventoryRoot.append(node('p','muted','Quelleninventar wird geladen …'));
    if(inventory.error){const error=node('div','rc-inventory-error');error.setAttribute('role','alert');error.append(node('p','','Quelleninventar konnte nicht aktualisiert werden. Angezeigte Angaben stammen gegebenenfalls aus dem letzten Abruf.'),button('Inventar erneut laden',()=>void loadInventory()));inventoryRoot.append(error);}
    if(!inventory.data)return;
    if(!inventory.data.contracts.length)inventoryRoot.append(node('p','muted','Keine bestätigten Vertragsbelege auf dieser Seite.'));
    for(const item of inventory.data.contracts){
      const detail=node('details','rc-inventory-item');detail.dataset.documentId=item.document_id;
      detail.append(node('summary','',`${item.provider} · ${item.title} · ${item.contract.configured?'Vertragsangaben gespeichert':'Vertragsangaben offen'}`));
      const body=node('div','rc-inventory-body');
      body.append(node('p','fc-caption',`Beleg #${item.document_id} · Revision ${item.document_revision} · Beleg bestätigt`),node('p','rc-inventory-source','Quelle: '+item.source_reference));
      body.append(button('Quelle und Beleg öffnen',async()=>{try{await classificationOpenComparison(item.document_id);}catch(error){errorBox(body,error);}}));
      const contract=item.contract;
      if(contract.configured){
        body.append(node('p','',`Bestätigter Vertragsbeginn: ${date(contract.valid_from)} · Ende: ${contract.valid_to?date(contract.valid_to):'offen'}`));
        if(contract.reference)body.append(node('p','fc-caption','Vertragsreferenz: '+contract.reference));
      }
      for(const phase of contract.phases||[])body.append(node('p','rc-phase',`Bestätigte Phase: ${money(phase.amount,'EUR')} je Zahlung · alle ${phase.interval_months} Monate · ${date(phase.valid_from)} bis ${phase.valid_to?date(phase.valid_to):'offen'}`));
      if(item.rule_conflict)body.append(node('p','notice','Mehrere gespeicherte Zahlungsregeln: Zuordnung prüfen.'));
      else if(item.rule){
        const rule=item.rule;
        body.append(node('p','rc-inventory-rule',`Bestätigte Zahlungsregel #${rule.id} · ${rule.active?'aktiv':'inaktiv'} · ${accountLabel(rule.account_id)} · ${rule.currency} · Zahlungstag ${rule.day_of_month}`));
      }else body.append(node('p','muted','Keine bestätigte Zahlungsregel hinterlegt.'));
      const missing=item.missing_fields||[];
      body.append(node('p',missing.length?'notice':'fc-caption',missing.length?'Offene Pflichtfelder: '+missing.map(key=>item.missing_field_labels?.[key]||phrase(key)).join(', '):'Keine offenen Pflichtfelder laut gespeichertem Stand.'));
      const evidence=node('details','rc-inventory-evidence');
      evidence.append(node('summary','',`Bestätigte Buchungsnachweise (${item.booking_total})`));
      const bookings=node('ol','rc-evidence');
      for(const booking of item.bookings||[])bookings.append(node('li','',`${date(booking.date)} · ${money(booking.amount,booking.currency)} · ${accountLabel(booking.account_id)} · ${booking.counterparty||'Gegenpartei offen'} · ${booking.external_id}`));
      evidence.append(bookings);
      if(!item.booking_total)evidence.append(node('p','muted','Keine wirksamen Buchungsnachweise hinterlegt.'));
      if(item.bookings_truncated)evidence.append(node('p','fc-caption',`Angezeigt: ${item.bookings.length} von ${item.booking_total} Nachweisen.`));
      body.append(evidence);detail.append(body);inventoryRoot.append(detail);
    }
    const navigation=node('div','rc-inventory-navigation');
    const previous=button('Vorherige Belege',()=>void loadInventory(inventory.data.page-1));
    const next=button('Weitere Belege',()=>void loadInventory(inventory.data.page+1));
    previous.disabled=inventory.loading||inventory.data.page===0;
    next.disabled=inventory.loading||inventory.data.page+1>=inventory.data.pages;
    navigation.append(previous,node('span','fc-caption',`Seite ${inventory.data.pages?inventory.data.page+1:0} von ${inventory.data.pages} · ${inventory.data.page_size} Belege je Seite`),next);inventoryRoot.append(navigation);
  }
  async function loadInventory(page=inventory.page) {
    const request=++inventory.request;inventory.page=page;inventory.loading=true;inventory.error=null;renderInventory();
    try{const data=await api('/api/contracts-inventory',{page});if(request!==inventory.request)return;inventory.data=data;}
    catch(error){if(request===inventory.request)inventory.error=error;}
    finally{if(request===inventory.request){inventory.loading=false;renderInventory();}}
  }
  const phrases = {
    paypal_transaction_date_not_booking_date:'PayPal-Buchungsdatum fehlt.',
    confirmed_paypal_credit:'Bestätigter PayPal-Ratenplan · Originaltermine und Beträge',
    source_revision:'Aktuellen Planstand erneut bestätigen',
    paypal_credit_source_stale:'Der PayPal-Planstand wurde geändert. Die Kontozuordnung muss vom Eigentümer erneut bestätigt werden; Raten sind bis dahin nicht eingerechnet.',
    paypal_credit_account_ineligible:'Das Zahlungskonto passt nicht mehr zur Laufzeit des PayPal-Ratenplans. Raten sind bis zur Klärung nicht eingerechnet.',
    competing_payment_schedule:'Möglicherweise doppelt erfasste Zahlungsreihe. Ungeklärte Raten sind nicht eingerechnet.',
    paypal_credit_booking_assignment_conflict:'Eine passende Buchung wurde bereits ausdrücklich anders eingeordnet. Die Zuordnung zum PayPal-Ratenplan bleibt offen.',
    invalid_forecast_range:'Wähle einen Zeitraum von 0 bis höchstens 366 Tagen; das Enddatum muss am oder nach dem Startdatum liegen.', unsupported_cadence:'Der Zahlungsrhythmus aus der Quelle wird nicht unterstützt. Bitte vor einer Übernahme prüfen.', unsupported_source_cadence:'Der Zahlungsrhythmus aus der Quelle wird nicht unterstützt. Bitte vor einer Übernahme prüfen.', insufficient_recurring_history:'Es liegen noch zu wenige passende Buchungen für eine verlässliche Fortschreibung vor.', insufficient_source_history:'Es liegen noch zu wenige passende Buchungen für eine verlässliche Fortschreibung vor.', conflicting_contract_markers:'Die Vertragskennzeichnungen der Quelle widersprechen sich. Keine automatische Fortschreibung.', conflicting_source_markers:'Die Vertragskennzeichnungen der Quelle widersprechen sich. Keine automatische Fortschreibung.', ambiguous_source_history:'Die Buchungshistorie lässt sich keiner eindeutigen Vertragsreihe zuordnen.', large_amount_variation:'Die Buchungsbeträge schwanken stark; Betrag vor einer Übernahme prüfen.', missing_recurring_period:'Eine erwartete Periode fehlt. Diese Reihe wird nicht fortgeschrieben.', end_date_unknown:'Ein Enddatum ist nicht bekannt. Weitere Zahlungen bleiben eine Annahme.', auto_detected:'Automatisch aus wiederkehrenden Buchungen erkannt · geschätzt', confirmed_manual:'Bestätigte manuelle Regel', linked_history:'Termin aus verknüpften Zahlungen geschätzt', confirmed_document_setting:'Bestätigte Vertragskonfiguration',
    projection_conditional_on_imported_ledger:'Die Prognose setzt vollständige importierte Buchungen voraus.', transfers_not_inferred:'Umbuchungen werden nicht automatisch ergänzt.', budgets_not_payment_events:'Budgets sind Ausgabengrenzen und werden nicht als zusätzliche Zahlungen eingeplant.',
    balance_rollforward_ledger_completeness_unverified:'Kontostand anhand vorhandener Buchungen fortgeschrieben; Vollständigkeit nicht bestätigt.', stale_recorded_balance:'Der belegte Kontostand ist älter als sieben Tage.', recorded_balance_missing:'Ein belegter Kontostand fehlt.', recorded_balance_ambiguous:'Die ausgewiesenen Kontostände widersprechen sich. Bis zur Klärung bleibt die Kontoprognose unbekannt.', unresolved_payments_excluded:'Ungeklärte Zahlungen sind nicht in der Prognose enthalten.',
    ambiguous_booking_match:'Mehrere Buchungen passen; Zuordnung offen.', partial_payment_conflict:'Teilzahlung oder abweichende Zuordnung muss geprüft werden.', past_payment_unresolved:'Erwarteter Zahlungstag liegt zurück; keine eindeutige Buchung zugeordnet.', booking_date_proximity_estimate:'Buchung am Monatswechsel anhand von Betrag, Referenz und Datumsnähe zugeordnet; Terminzuordnung geschätzt.',
    stale_recurring_revision:'Versionskonflikt: Die Regel wurde inzwischen geändert.', stale_document_revision:'Versionskonflikt: Der Vertragsbeleg wurde inzwischen geändert.',
    account_id:'Konto', currency:'Währung', day_of_month:'Zahlungstag', phases:'Vertragsphasen', contract_terms:'Vertrags- und Zahlungsangaben', unconfigured_document:'Noch keine Vertragsangaben hinterlegt.',
  };
  const phrase = value => phrases[value] || (typeof value==='string' && /^[a-z]+(?:_[a-z]+)+$/.test(value)?'Die Datenbasis muss geprüft werden.':value);
  const sourceLabel = source => ({finanzguru_file:'Finanzguru-Datei · ausgewiesener Buchungsstand',bank:'Bankabruf',statement:'Kontoauszug',bank_balance:'Bankabruf',ing_period_import:'Importierter Kontoauszug',monthly_adoption:'Bestätigter Monatsstand'})[source] || source;
  const accountLabel = id => (state.accounts||[]).find(a=>a.id===id)?.display_name || view.data?.accounts.find(a=>a.account_id===id)?.label || id || 'Konto fehlt';
  const field = (form, name, label, type, value, required=false) => { const l=node('label','',label), n=node(type==='select'?'select':'input');n.name=name;n.id='recurring-'+name;if(type!=='select')n.type=type;n.value=value??'';n.required=required;l.append(n);form.append(l);return n; };
  function accountOptions(select, includeAll) {
    const accounts = new Map((state.accounts||[]).map(a=>[a.id,{id:a.id,label:a.display_name,currency:a.currency}]));
    for(const a of view.data?.accounts||[]) if(!accounts.has(a.account_id))accounts.set(a.account_id,{id:a.account_id,label:a.label,currency:a.currency});
    if(includeAll){const o=node('option','','Alle Konten');o.value='';select.append(o);}
    else {const o=node('option','','Konto wählen');o.value='';select.append(o);}
    for(const a of accounts.values()){const o=node('option','',a.label+' · '+a.currency);o.value=a.id;o.dataset.currency=a.currency;select.append(o);}
  }
  function errorBox(root,error,retry) {
    root.querySelector('.rc-error')?.remove();const box=node('div','fc-retry-notice rc-error');box.setAttribute('role','alert');box.append(node('p','',phrase(error.message)||'Aktualisierung fehlgeschlagen.'));if(retry)box.append(button('Erneut versuchen',retry));root.prepend(box);
  }
  function chart(account) {
    const points=(account.points||[]).filter(p=>Number.isFinite(Number(p.amount)) && p.date);
    const figure=node('figure','rc-chart'); const caption=node('figcaption','fc-caption','Kontoverlauf · '+account.currency);figure.append(caption);
    if(!points.length){figure.append(node('p','muted','Ohne belegten Kontostand keine Kontoprognose.'));return figure;}
    // Geometry only. The browser neither accumulates payments nor derives balances.
    const values=points.map(p=>Number(p.amount)), min=Math.min(...values), max=Math.max(...values), span=max-min||1;
    const dates=points.map(p=>new Date(p.date+'T12:00:00').getTime()), first=Math.min(...dates), duration=Math.max(...dates)-first||1;
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 600 180');svg.setAttribute('role','img');svg.setAttribute('aria-label',`Gelieferter Kontoverlauf ${date(points[0].date)} bis ${date(points.at(-1).date)}`);
    // Balances change on payment dates, not continuously between them.
    const xy=(i,amount)=>`${20+(dates[i]-first)/duration*560},${150-(Number(amount)-min)/span*120}`;
    const steps=points.flatMap((p,i)=>i?[xy(i,points[i-1].amount),xy(i,p.amount)]:[xy(i,p.amount)]);
    const line=document.createElementNS(svg.namespaceURI,'polyline');line.setAttribute('points',steps.join(' '));line.setAttribute('fill','none');line.setAttribute('stroke','currentColor');line.setAttribute('stroke-width','3');svg.append(line);figure.append(svg);
    const axis=node('div','rc-axis');axis.append(node('span','',date(points[0].date)),node('span','',date(points.at(-1).date)));figure.append(axis,node('p','fc-caption',`Verlaufsspanne: ${money(String(min),account.currency)} bis ${money(String(max),account.currency)}`));return figure;
  }
  function openEditor(item=null) {
    const doc=!!item?.document_id, dialog=node('dialog','rc-dialog'), form=node('form','rc-form');form.method='dialog';dialog.setAttribute('aria-labelledby','recurring-editor-title');
    const heading=node('h2','',doc?'Vertrag konfigurieren':item?.inferred?'Erkannte Zahlung übernehmen':item?'Regel bearbeiten':'Regelmäßige Zahlung');heading.id='recurring-editor-title';form.append(heading,node('p','muted',doc?'Betrag und Rhythmus stammen aus den bestätigten Vertragsphasen. Konto und Zahlungstag ergänzen.':'Erfasst eine erwartete Zahlung. Es wird keine Bankzahlung ausgelöst.'));
    const label=field(form,'label','Bezeichnung','text',item?.label,true);label.maxLength=160;if(doc)label.disabled=true;
    const account=field(form,'account_id','Konto','select','',true);accountOptions(account,false);account.value=item?.account_id||'';
    const currency=field(form,'currency','Währung','text',item?.currency||'');currency.readOnly=true;
    const updateCurrency=()=>{currency.value=account.selectedOptions[0]?.dataset.currency||item?.currency||'';};account.addEventListener('change',updateCurrency);if(account.value)updateCurrency();
    const amount=field(form,'amount','Betrag mit Vorzeichen (+ Eingang / − Ausgabe)','text',item?.amount, !doc);amount.inputMode='decimal';amount.pattern='[+-]?[0-9]+([.,][0-9]{1,2})?';amount.placeholder='−49,90';amount.disabled=doc;
    const from=field(form,'valid_from','Gültig ab','date',item?.valid_from||view.as_of,true), to=field(form,'valid_to','Gültig bis (optional)','date',item?.valid_to);from.disabled=doc;to.disabled=doc;
    const day=field(form,'day_of_month','Voraussichtlicher Zahlungstag im Monat','number',item?.day_of_month,true);day.min=1;day.max=31;
    const interval=field(form,'interval_months','Abstand in Monaten','number',item?.interval_months||1,true);interval.min=1;interval.max=120;interval.disabled=doc;
    const reference=field(form,'reference','Referenz (optional)','text',item?.reference);reference.disabled=doc;reference.maxLength=240;
    if(doc){for(const phase of item.phases||[])form.append(node('p','fc-caption',`Vertragsphase: Kosten ${money(phase.amount,'EUR')} · alle ${phase.interval_months} Monate · ${date(phase.valid_from)}${phase.valid_to?' – '+date(phase.valid_to):''}`));}
    if(doc)form.append(link('Vertragsphasen im Beleg bearbeiten','documents'));
    const confirmLabel=node('label','rc-confirm'), confirmed=node('input');confirmed.type='checkbox';confirmed.name='confirmed';confirmed.required=true;confirmLabel.append(confirmed,node('span','','Ich bestätige diese lokale Planung.'));form.append(confirmLabel);
    const status=node('p','rc-form-status');status.setAttribute('role','alert');form.append(status);
    const actions=node('div','rc-actions'), save=node('button','','Speichern');save.type='submit';actions.append(button('Abbrechen',()=>dialog.close()),save);form.append(actions);dialog.append(form);document.body.append(dialog);
    dialog.addEventListener('close',()=>dialog.remove());
    form.addEventListener('submit',async event=>{event.preventDefault();if(!form.reportValidity())return;save.disabled=true;status.textContent='Wird gespeichert …';
      try{await api('/api/recurring-save',{id:item?.id??null,revision:item?.revision||0,document_id:item?.document_id??null,document_revision:item?.document_revision??null,label:label.value,account_id:account.value,currency:currency.value,amount:doc?null:amount.value.replace(',','.'),valid_from:from.value,valid_to:to.value||null,day_of_month:Number(day.value),interval_months:Number(interval.value),reference:reference.value,confirmed:confirmed.checked});dialog.close();await window.financeRecurringLoad();}
      catch(error){status.textContent=`Nicht gespeichert: ${phrase(error.message)} Deine Eingaben bleiben erhalten. Bei einem Versionskonflikt abbrechen und aktualisieren.`;}finally{save.disabled=false;}
    });dialog.showModal();
  }
  function disableRule(item) {
    const dialog=node('dialog','rc-dialog'), heading=node('h2','','Regel deaktivieren?');heading.id='recurring-disable-title';dialog.setAttribute('aria-labelledby',heading.id);dialog.append(heading,node('p','',`„${item.label}“ wird künftig nicht mehr eingeplant. Bisherige Buchungen und die Historie bleiben erhalten.`));
    const status=node('p');status.setAttribute('role','alert');const yes=button('Deaktivierung bestätigen',async()=>{yes.disabled=true;try{await api('/api/recurring-disable',{id:item.id,revision:item.revision,confirmed:true});dialog.close();await window.financeRecurringLoad();}catch(error){status.textContent=phrase(error.message);}finally{yes.disabled=false;}});
    const actions=node('div','rc-actions');actions.append(button('Abbrechen',()=>dialog.close()),yes);dialog.append(status,actions);document.body.append(dialog);dialog.addEventListener('close',()=>dialog.remove());dialog.showModal();
  }
  const automatic = item => item.inferred || item.basis==='auto_detected' || String(item.id||'').startsWith('auto:');
  function adopt(candidate) {
    // A proposal becomes a new manual draft. Automatic IDs are never sent to mutation APIs.
    openEditor({...candidate,id:null,revision:0,document_id:null,document_revision:null,inferred:true,reference:candidate.reference||''});
  }
  function provenance(candidate, includeWarnings=true) {
    const details=node('details','rc-provenance');details.append(node('summary','','Herleitung und Buchungen'));
    const sources={budget_allocations:'Zuordnungen einer Budgetposition',budget_category_mapping:'Budgetposition über Kategorien zugeordnet',booking_history:'Wiederkehrende Buchungshistorie',finanzguru_contract_marker:'Vertragskennzeichnung aus Finanzguru',source_contract_marker:'Vertragskennzeichnung aus Finanzguru'};
    details.append(node('p','fc-caption','Quelle: '+(sources[candidate.source]||'Erkannte Buchungsreihe')));
    if(candidate.derivation?.amount_method==='median'&&Number.isInteger(candidate.derivation.sample_count))details.append(node('p','fc-caption',`Betrag: Median der letzten ${candidate.derivation.sample_count} gebuchten Beträge · geschätzt.`));if(candidate.derivation?.date_method==='month_end')details.append(node('p','fc-caption','Voraussichtlicher Zahlungstag: Monatsende.'));
    for(const ref of candidate.source_refs||[]){
      if(['finanzguru_contract','source_contract'].includes(ref.type)){
        details.append(node('p','fc-caption',typeof ref.contract_label==='string'&&ref.contract_label?`Vertrag der Quelle: ${ref.contract_label}`:'Vertrag in der Finanzguru-Datei gekennzeichnet.'));
        details.append(node('p','fc-caption',candidate.interval_months?`Rhythmus aus der Quelle: alle ${candidate.interval_months} Monate`:'Rhythmus aus der Quelle nicht verlässlich zugeordnet.'));
      }else details.append(node('p','fc-caption',ref.type==='budget_item'?`Planstand ${ref.revision} · Position ${ref.item_id}`:ref.type==='category'?`Kategorie ${ref.category_id||ref.id||'offen'}`:'Verknüpfte Buchungsquelle'));
    }
    const bookings=node('ol','rc-evidence');for(const booking of candidate.bookings||[])bookings.append(node('li','',`${date(booking.date)} · ${money(booking.amount,candidate.currency)} · ${accountLabel(booking.account_id)} · ${booking.external_id}`));
    if(!bookings.children.length)details.append(node('p','muted','Keine Einzelbuchungen in dieser Antwort verfügbar.'));else details.append(bookings);
    if(includeWarnings)for(const warning of candidate.warnings||[])details.append(node('p','notice',phrase(warning)));return details;
  }
  function candidateCard(candidate) {
    const blocked=candidate.status==='blocked';
    const card=node('article','fc-home-panel rc-candidate');card.dataset.candidateId=candidate.id;card.append(node('span','eyebrow',blocked?'KONTOZUORDNUNG PRÜFEN':candidate.status==='covered'?'BEREITS ABGEDECKT':'BUCHUNGSREIHE PRÜFEN'),node('h4','',candidate.label),node('p','rc-amount',money(candidate.amount,candidate.currency)),node('p','muted',accountLabel(candidate.account_id)),node('p','fc-caption',blocked?'Stand des PayPal-Ratenplans oder Zahlungskonto muss vom Eigentümer erneut bestätigt werden. Keine geschätzte Ersatzreihe.':candidate.status==='covered'?'Bereits durch einen Vertrag oder eine Regel berücksichtigt.':'Unbestätigter Vorschlag. Keine Zahlung aus dieser Reihe ist in der Prognose enthalten.'),provenance(candidate,false));for(const warning of candidate.warnings||[])card.append(node('p','notice',phrase(warning)));
    if(blocked)card.append(link('Eigentümerverwaltung','administration'));
    else if(candidate.status!=='covered')card.append(button('Als Regel übernehmen',()=>adopt(candidate)));return card;
  }
  function creditScheduleCard(item) {
    const card=node('article','fc-home-panel rc-contract');
    card.dataset.ruleId=item.id;
    card.append(node('span','eyebrow','BESTÄTIGTER PAYPAL-RATENPLAN'),node('h4','',item.label),
      node('p','muted',accountLabel(item.account_id)),
      node('p','fc-caption','Einzeln bestätigte Kontozuordnung. Termine und Beträge stammen aus dem gespeicherten Restplan; die Schlussrate bleibt erhalten.'));
    for(const warning of item.warnings||[])card.append(node('p','notice',phrase(warning)));
    const dates=node('details','rc-provenance');dates.append(node('summary','','Gespeicherte Raten'));
    const list=node('ol','rc-evidence');
    for(const payment of item.exact_payments||[])list.append(node('li','',`${date(payment.date)} · ${money(payment.amount,item.currency)}`));
    if(list.children.length)dates.append(list);else dates.append(node('p','muted','Keine offenen Raten im gespeicherten Restplan.'));
    card.append(dates,link('Eigene Kontozuordnung verwalten','administration'));return card;
  }
  function render() {
    const root=document.getElementById('recurring-root'), fragment=document.createDocumentFragment();
    const controls=node('form','rc-filters');const start=field(controls,'as_of','Prognose ab','date',view.as_of,true), until=field(controls,'until','Prognose bis (max. 366 Tage)','date',view.until,true), account=field(controls,'filter-account','Konto','select','');accountOptions(account,true);account.value=view.account_id;
    const limitDates=()=>{until.min=start.value;if(start.value){const maximum=new Date(start.value+'T12:00:00');maximum.setDate(maximum.getDate()+366);until.max=`${maximum.getFullYear()}-${String(maximum.getMonth()+1).padStart(2,'0')}-${String(maximum.getDate()).padStart(2,'0')}`;}};start.addEventListener('input',limitDates);limitDates();
    const show=node('button','','Anzeigen');show.type='submit';controls.append(show);controls.addEventListener('submit',event=>{event.preventDefault();const days=(Date.parse(until.value+'T00:00:00Z')-Date.parse(start.value+'T00:00:00Z'))/86400000;if(!Number.isFinite(days)||days<0||days>366){errorBox(root,new Error('invalid_forecast_range'));return;}view.as_of=start.value;view.until=until.value;view.account_id=account.value;void window.financeRecurringLoad();});const filters=node('details','rc-filter-details');filters.open=view.filterOpen;filters.append(node('summary','',`Zeitraum und Konto · ${date(view.as_of)} – ${date(view.until)} · ${view.account_id?accountLabel(view.account_id):'Alle Konten'}`),controls);filters.addEventListener('toggle',()=>{view.filterOpen=filters.open;});fragment.append(filters);
    const intro=node('div','rc-intro');intro.append(node('p','fc-caption',`Teilprognose vom ${date(view.as_of)} bis ${date(view.until)}. Nur erfasste regelmäßige Zahlungen; ungeplante variable Ausgaben und fehlende Verträge sind nicht enthalten.`),button('Zahlung erfassen',()=>openEditor()));fragment.append(intro);
    if(view.data.warnings?.length){const limits=node('details','rc-limits');limits.append(node('summary','','Datenbasis und Grenzen'));for(const warning of view.data.warnings)limits.append(node('p','fc-caption',phrase(warning)));fragment.append(limits);}
    const contracts=node('section','rc-contracts');contracts.append(node('h3','','Verträge & regelmäßige Zahlungen'));const grid=node('div','rc-card-grid');
    for(const item of view.data.schedules||[]){if(item.basis==='confirmed_paypal_credit'){grid.append(creditScheduleCard(item));continue;}const card=node('article','fc-home-panel rc-contract');card.dataset.ruleId=item.id??'';card.append(node('span','eyebrow',automatic(item)?'AUTOMATISCH ERKANNT · GESCHÄTZT':item.requires_contract_setup?'BESTÄTIGTER VERTRAGSBELEG':item.document_id?'BESTÄTIGTER VERTRAG':'MANUELLE REGEL'),node('h4','',item.label),node('p','rc-amount',money(item.amount,item.currency)),node('p','muted',accountLabel(item.account_id)),node('p','fc-caption',`${item.document_id && item.phases?.length?'Rhythmus laut Vertragsphasen':item.interval_months?`Alle ${item.interval_months} Monate`:'Rhythmus offen'} · Tag ${item.day_of_month||'offen'} · ${date(item.valid_from)}${item.valid_to?' – '+date(item.valid_to):''}`));
      if(item.document_id){for(const phase of item.phases||[])card.append(node('p','rc-phase',`Kosten ${money(phase.amount,'EUR')} · alle ${phase.interval_months} Monate · ab ${date(phase.valid_from)}${phase.valid_to?' bis '+date(phase.valid_to):''}`));card.querySelector('.rc-amount').textContent=item.phases?.length?'Vertragsphasen':'Zahlungsbetrag unbekannt';}
      if(item.date_estimated??item.estimated)card.append(node('span','rc-badge','Termin geschätzt'));if(item.active===false)card.append(node('span','rc-badge','Inaktiv'));if(item.missing_fields?.length)card.append(node('p','notice','Angaben fehlen: '+item.missing_fields.map(phrase).join(', ')));
      if(item.basis)card.append(node('p','fc-caption',phrase(item.basis)));
      const candidate=(view.data.candidates||[]).find(c=>c.id===(item.candidate_id||item.id));if(automatic(item))card.append(provenance(candidate||item));
      const actions=node('div','rc-actions');if(automatic(item)){actions.append(button('Als Regel übernehmen',()=>adopt(candidate||item)));}else if(item.requires_contract_setup){actions.append(button('Vertragsangaben ergänzen',async()=>{try{await classificationOpenComparison(item.document_id);}catch(error){errorBox(card,error);}}));}else actions.append(button(item.document_id?'Konfigurieren':'Bearbeiten',()=>openEditor(item)));if(!automatic(item)&&item.id && item.active!==false)actions.append(button('Deaktivieren',()=>disableRule(item)));if(item.document_id&&!item.requires_contract_setup)actions.append(link('Vertragsbeleg','documents'));card.append(actions);grid.append(card);
    }if(!grid.children.length)grid.append(node('p','muted','Noch keine regelmäßigen Zahlungen. Erfasse eine Zahlung oder bestätige einen Vertragsbeleg.'));contracts.append(grid,link('Vertrag aus Beleg erfassen','documents'));fragment.append(contracts,inventoryRoot);
    const scheduled=new Set((view.data.schedules||[]).map(item=>item.candidate_id||item.id));const candidates=(view.data.candidates||[]).filter(item=>!scheduled.has(item.id));const reviews=candidates.filter(item=>['needs_review','blocked'].includes(item.status));if(reviews.length){const review=node('section','rc-candidate-review');review.append(node('h3','','Erkannte Reihen zur Prüfung'));const cards=node('div','rc-card-grid');for(const candidate of reviews)cards.append(candidateCard(candidate));review.append(cards);fragment.append(review);}const covered=candidates.filter(item=>item.status==='covered');if(covered.length){const details=node('details','rc-limits');details.append(node('summary','',`${covered.length} bereits abgedeckte Buchungsreihen`));for(const candidate of covered)details.append(candidateCard(candidate));fragment.append(details);}
    const timeline=node('section','fc-home-panel rc-timeline');timeline.append(node('h3','','Zahlungskalender'));const status=field(timeline,'status','Zahlungen anzeigen','select','');for(const [value,text] of [['all','Alle'],['expected','Erwartet'],['booked','Gebucht'],['unresolved','Offen']]){const o=node('option','',text);o.value=value;status.append(o);}status.value=view.status;status.addEventListener('change',()=>{view.status=status.value;renderEvents(list);});const list=node('div','rc-events');timeline.append(node('p','fc-caption','Gebuchte Zahlungen des laufenden Monats und erwartete Zahlungen im Prognosezeitraum. Termine können geschätzt sein.'),list);renderEvents(list);fragment.append(timeline);
    const forecasts=node('section','rc-forecasts');forecasts.append(node('h3','','Kontostände vorausgeschaut'));
    for(const account of view.data.accounts||[]){const card=node('article','fc-home-panel');card.append(node('h4','',account.label),node('p','fc-caption',account.recorded_balance?`Basis: ${money(account.recorded_balance.amount,account.currency)} · ${date(account.recorded_balance.booked_on)} · ${sourceLabel(account.recorded_balance.source)}`:'Kein belegter Kontostand. Fehlend bedeutet nicht Null.'));
      const metrics=node('div','rc-metrics');for(const [label,value] of [[`Am ${date(view.as_of)}`,account.as_of_balance],[`Kontostand am gewählten Datum · ${date(view.until)}`,account.projected_balance]]){const metric=node('div');metric.append(node('span','muted',label),node('strong','',money(value,account.currency)));metrics.append(metric);}card.append(metrics,chart(account));for(const warning of account.warnings||[])card.append(node('p','notice',phrase(warning)));forecasts.append(card);
    }fragment.append(forecasts);root.replaceChildren(fragment);root.dataset.loaded='true';
  }
  function renderEvents(list) {
    list.replaceChildren();for(const event of view.data.events||[]){if(view.status!=='all'&&event.status!==view.status)continue;const row=node('article','rc-event'), text=node('div');text.append(node('strong','',event.label),node('small','muted',`${date(event.expected_date)}${(event.date_estimated??event.estimated)?' · Termin geschätzt':''}${event.amount_estimated?' · Betrag geschätzt':''} · ${accountLabel(event.account_id)}`),node('small','fc-caption',phrase(event.basis)||''));
      if(event.observed_date)text.append(node('small','muted','Gebucht am '+date(event.observed_date)));const amount=node('div','rc-event-value');amount.append(node('strong',String(event.amount).startsWith('-')?'':'fc-positive',money(event.amount,event.currency)),node('span','rc-badge rc-'+event.status,({booked:'Gebucht',expected:'Erwartet',unresolved:'Zu klären'})[event.status]||event.status));row.append(text,amount);for(const warning of event.warnings||[])row.append(node('small','notice',phrase(warning)));list.append(row);}if(!list.children.length)list.append(node('p','muted','Keine Zahlungen für diese Auswahl.'));
  }
  window.financeRecurringLoad = async () => {
    const root=document.getElementById('recurring-root');if(!root)return;const request=++view.request;root.setAttribute('aria-busy','true');if(!view.data&&!root.children.length)root.append(node('p','muted','Zahlungen und Kontoprognose werden geladen …'));
    if(!root.contains(inventoryRoot))root.append(inventoryRoot);
    void loadInventory();
    try{const data=await api('/api/recurring-view',{as_of:view.as_of,until:view.until,account_id:view.account_id});if(request!==view.request)return;view.data=data;render();}
    catch(error){if(request===view.request)errorBox(root,error,()=>void window.financeRecurringLoad());}finally{if(request===view.request)root.setAttribute('aria-busy','false');}
  };
  document.getElementById('recurring-reload')?.addEventListener('click',()=>void window.financeRecurringLoad());
})();
