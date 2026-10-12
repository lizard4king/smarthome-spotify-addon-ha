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
  const phrases = {
    confirmed_manual:'Bestätigte manuelle Regel', linked_history:'Termin aus verknüpften Zahlungen geschätzt', confirmed_document_setting:'Bestätigte Vertragskonfiguration',
    projection_conditional_on_imported_ledger:'Die Prognose setzt vollständige importierte Buchungen voraus.', transfers_not_inferred:'Umbuchungen werden nicht automatisch ergänzt.', budgets_not_payment_events:'Budgets sind Ausgabengrenzen und werden nicht als zusätzliche Zahlungen eingeplant.',
    balance_rollforward_ledger_completeness_unverified:'Kontostand anhand vorhandener Buchungen fortgeschrieben; Vollständigkeit nicht bestätigt.', stale_recorded_balance:'Der belegte Kontostand ist älter als sieben Tage.', recorded_balance_missing:'Ein belegter Kontostand fehlt.', unresolved_payments_excluded:'Ungeklärte Zahlungen sind nicht in der Prognose enthalten.',
    ambiguous_booking_match:'Mehrere Buchungen passen; Zuordnung offen.', partial_payment_conflict:'Teilzahlung oder abweichende Zuordnung muss geprüft werden.', past_payment_unresolved:'Erwarteter Zahlungstag liegt zurück; keine eindeutige Buchung zugeordnet.', booking_date_proximity_estimate:'Buchung am Monatswechsel anhand von Betrag, Referenz und Datumsnähe zugeordnet; Terminzuordnung geschätzt.',
    stale_recurring_revision:'Versionskonflikt: Die Regel wurde inzwischen geändert.', stale_document_revision:'Versionskonflikt: Der Vertragsbeleg wurde inzwischen geändert.',
    account_id:'Konto', currency:'Währung', day_of_month:'Zahlungstag', phases:'Vertragsphasen',
  };
  const phrase = value => phrases[value] || value;
  const sourceLabel = source => ({bank:'Bankabruf',statement:'Kontoauszug',bank_balance:'Bankabruf',ing_period_import:'Importierter Kontoauszug',monthly_adoption:'Bestätigter Monatsstand'})[source] || source;
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
    const heading=node('h2','',doc?'Vertrag konfigurieren':item?'Regel bearbeiten':'Regelmäßige Zahlung');heading.id='recurring-editor-title';form.append(heading,node('p','muted',doc?'Betrag und Rhythmus stammen aus den bestätigten Vertragsphasen. Konto und Zahlungstag ergänzen.':'Erfasst eine erwartete Zahlung. Es wird keine Bankzahlung ausgelöst.'));
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
  function render() {
    const root=document.getElementById('recurring-root'), fragment=document.createDocumentFragment();
    const controls=node('form','rc-filters');const start=field(controls,'as_of','Prognose ab','date',view.as_of,true), until=field(controls,'until','Prognose bis','date',view.until,true), account=field(controls,'filter-account','Konto','select','');accountOptions(account,true);account.value=view.account_id;
    const show=node('button','','Anzeigen');show.type='submit';controls.append(show);controls.addEventListener('submit',event=>{event.preventDefault();view.as_of=start.value;view.until=until.value;view.account_id=account.value;void window.financeRecurringLoad();});const filters=node('details','rc-filter-details');filters.open=view.filterOpen;filters.append(node('summary','',`Zeitraum und Konto · ${date(view.as_of)} – ${date(view.until)} · ${view.account_id?accountLabel(view.account_id):'Alle Konten'}`),controls);filters.addEventListener('toggle',()=>{view.filterOpen=filters.open;});fragment.append(filters);
    const intro=node('div','rc-intro');intro.append(node('p','fc-caption',`Teilprognose vom ${date(view.as_of)} bis ${date(view.until)}. Nur erfasste regelmäßige Zahlungen; ungeplante variable Ausgaben und fehlende Verträge sind nicht enthalten.`),button('Zahlung erfassen',()=>openEditor()));fragment.append(intro);
    if(view.data.warnings?.length){const limits=node('details','rc-limits');limits.append(node('summary','','Datenbasis und Grenzen'));for(const warning of view.data.warnings)limits.append(node('p','fc-caption',phrase(warning)));fragment.append(limits);}
    const contracts=node('section','rc-contracts');contracts.append(node('h3','','Verträge & regelmäßige Zahlungen'));const grid=node('div','rc-card-grid');
    for(const item of view.data.schedules||[]){const card=node('article','fc-home-panel rc-contract');card.dataset.ruleId=item.id??'';card.append(node('span','eyebrow',item.document_id?'BESTÄTIGTER VERTRAG':'MANUELLE REGEL'),node('h4','',item.label),node('p','rc-amount',money(item.amount,item.currency)),node('p','muted',accountLabel(item.account_id)),node('p','fc-caption',`${item.interval_months?`Alle ${item.interval_months} Monate`:'Rhythmus offen'} · Tag ${item.day_of_month||'offen'} · ${date(item.valid_from)}${item.valid_to?' – '+date(item.valid_to):''}`));
      if(item.document_id){for(const phase of item.phases||[])card.append(node('p','rc-phase',`Kosten ${money(phase.amount,'EUR')} · alle ${phase.interval_months} Monate · ab ${date(phase.valid_from)}${phase.valid_to?' bis '+date(phase.valid_to):''}`));card.querySelector('.rc-amount').textContent='Vertragsphasen';}
      if(item.date_estimated??item.estimated)card.append(node('span','rc-badge','Termin geschätzt'));if(item.active===false)card.append(node('span','rc-badge','Inaktiv'));if(item.missing_fields?.length)card.append(node('p','notice','Angaben fehlen: '+item.missing_fields.map(phrase).join(', ')));
      if(item.basis)card.append(node('p','fc-caption',phrase(item.basis)));
      const actions=node('div','rc-actions');actions.append(button(item.document_id?'Konfigurieren':'Bearbeiten',()=>openEditor(item)));if(item.id && item.active!==false)actions.append(button('Deaktivieren',()=>disableRule(item)));if(item.document_id)actions.append(link('Vertragsbeleg','documents'));card.append(actions);grid.append(card);
    }if(!grid.children.length)grid.append(node('p','muted','Noch keine regelmäßigen Zahlungen. Erfasse eine Zahlung oder bestätige einen Vertragsbeleg.'));contracts.append(grid,link('Vertrag aus Beleg erfassen','documents'));fragment.append(contracts);
    const timeline=node('section','fc-home-panel rc-timeline');timeline.append(node('h3','','Zahlungskalender'));const status=field(timeline,'status','Zahlungen anzeigen','select','');for(const [value,text] of [['all','Alle'],['expected','Erwartet'],['booked','Gebucht'],['unresolved','Offen']]){const o=node('option','',text);o.value=value;status.append(o);}status.value=view.status;status.addEventListener('change',()=>{view.status=status.value;renderEvents(list);});const list=node('div','rc-events');timeline.append(node('p','fc-caption','Gebuchte Zahlungen des laufenden Monats und erwartete Zahlungen im Prognosezeitraum. Termine können geschätzt sein.'),list);renderEvents(list);fragment.append(timeline);
    const forecasts=node('section','rc-forecasts');forecasts.append(node('h3','','Kontostände vorausgeschaut'));
    for(const account of view.data.accounts||[]){const card=node('article','fc-home-panel');card.append(node('h4','',account.label),node('p','fc-caption',account.recorded_balance?`Basis: ${money(account.recorded_balance.amount,account.currency)} · ${date(account.recorded_balance.booked_on)} · ${sourceLabel(account.recorded_balance.source)}`:'Kein belegter Kontostand. Fehlend bedeutet nicht Null.'));
      const metrics=node('div','rc-metrics');for(const [label,value] of [[`Am ${date(view.as_of)}`,account.as_of_balance],[`Am ${date(view.until)}`,account.projected_balance]]){const metric=node('div');metric.append(node('span','muted',label),node('strong','',money(value,account.currency)));metrics.append(metric);}card.append(metrics,chart(account));for(const warning of account.warnings||[])card.append(node('p','notice',phrase(warning)));forecasts.append(card);
    }fragment.append(forecasts);root.replaceChildren(fragment);root.dataset.loaded='true';
  }
  function renderEvents(list) {
    list.replaceChildren();for(const event of view.data.events||[]){if(view.status!=='all'&&event.status!==view.status)continue;const row=node('article','rc-event'), text=node('div');text.append(node('strong','',event.label),node('small','muted',`${date(event.expected_date)}${(event.date_estimated??event.estimated)?' · Termin geschätzt':''}${event.amount_estimated?' · Betrag geschätzt':''} · ${accountLabel(event.account_id)}`),node('small','fc-caption',phrase(event.basis)||''));
      if(event.observed_date)text.append(node('small','muted','Gebucht am '+date(event.observed_date)));const amount=node('div','rc-event-value');amount.append(node('strong',String(event.amount).startsWith('-')?'':'fc-positive',money(event.amount,event.currency)),node('span','rc-badge rc-'+event.status,({booked:'Gebucht',expected:'Erwartet',unresolved:'Zu klären'})[event.status]||event.status));row.append(text,amount);for(const warning of event.warnings||[])row.append(node('small','notice',phrase(warning)));list.append(row);}if(!list.children.length)list.append(node('p','muted','Keine Zahlungen für diese Auswahl.'));
  }
  window.financeRecurringLoad = async () => {
    const root=document.getElementById('recurring-root');if(!root)return;const request=++view.request;root.setAttribute('aria-busy','true');if(!view.data&&!root.children.length)root.append(node('p','muted','Zahlungen und Kontoprognose werden geladen …'));
    try{const data=await api('/api/recurring-view',{as_of:view.as_of,until:view.until,account_id:view.account_id});if(request!==view.request)return;view.data=data;render();}
    catch(error){if(request===view.request)errorBox(root,error,()=>void window.financeRecurringLoad());}finally{if(request===view.request)root.setAttribute('aria-busy','false');}
  };
  document.getElementById('recurring-reload')?.addEventListener('click',()=>void window.financeRecurringLoad());
})();
