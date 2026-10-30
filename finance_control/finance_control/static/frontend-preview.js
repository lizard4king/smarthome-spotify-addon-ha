'use strict';
(() => {
const frontendViews=new Set(['overview','transactions','contracts','analysis','more']);
const frontendAliases={home:'overview',dashboard:'transactions',classification:'transactions',recurring:'contracts','plan-actual':'transactions',analytics:'analysis','category-outflows':'analysis'};
function frontendRoute(){
  const query=new URLSearchParams(location.search).get('view');
  if(frontendViews.has(query))return query;
  const area=location.hash.slice(1);
  return area?(Object.hasOwn(frontendAliases,area)?frontendAliases[area]:'more'):'overview';
}
const tabs=[['overview','Konten','wallet'],['transactions','Buchungen','arrow-left-right'],['contracts','Verträge','receipt-text'],['analysis','Analysen','chart-no-axes-combined'],['more','Mehr','ellipsis']];
const $=s=>document.querySelector(s), esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const localDate=d=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
const today=localDate(new Date()), addDays=(date,n)=>{const d=new Date(date+'T12:00:00');d.setDate(d.getDate()+n);return localDate(d)};
const monthRange=()=>({date_from:today.slice(0,7)+'-01',date_to:localDate(new Date(new Date().getFullYear(),new Date().getMonth()+1,0))});
const emptyFilter=()=>({page:0,query:'',account_id:'',account_ids:[],account_mode:'all',category:'',reviewed:'',direction:'',amount_min:'',amount_max:'',...monthRange()});
const fmtDate=s=>/^\d{4}-\d{2}-\d{2}$/.test(s||'')?s.split('-').reverse().join('.'):'unbekannt';
const validAmount=v=>v!==null&&v!==undefined&&/^[+-]?\d+(?:\.\d{1,2})?$/.test(String(v))&&Number.isFinite(Number(v));
const money=(v,c)=>{if(!validAmount(v)||!c)return 'Nicht verfügbar';try{return new Intl.NumberFormat('de-DE',{style:'currency',currency:c}).format(Number(v))}catch{return 'Nicht verfügbar'}};
// Integer cents preserve exact decimal subtraction; no floating-point finance calculation.
function amountDifference(end,start){
  if(!validAmount(end)||!validAmount(start))return null;
  const cents=value=>{const text=String(value),negative=text.startsWith('-'),parts=text.replace(/^[+-]/,'').split('.');return BigInt(parts[0]+(parts[1]||'').padEnd(2,'0'))*(negative?-1n:1n)};
  const delta=cents(end)-cents(start),abs=delta<0n?-delta:delta;
  return `${delta<0n?'-':''}${abs/100n}.${String(abs%100n).padStart(2,'0')}`;
}
const state={page:frontendRoute(),asOf:today,data:null,recurring:null,list:null,catalog:null,analysis:null,errors:{},busy:{},epoch:{},filter:emptyFilter(),month:today.slice(0,7),analysisAccount:''};
let dialogEpoch=0,savePending=false;
const accountPickerState={open:false,historicalOpen:false};
const isObject=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
function validPayload(route,value){
  if(!isObject(value))return false;
  switch(route.split('?')[0]){
    case '/api/state':return Array.isArray(value.accounts)&&isObject(value.dashboard);
    case '/api/classification-list':return Array.isArray(value.rows)&&['page','pages','total'].every(k=>Number.isInteger(value[k])&&value[k]>=0);
    case '/api/classification-catalog':return Array.isArray(value.categories);
    case '/api/recurring-view':return Array.isArray(value.events)&&Array.isArray(value.schedules);
    case '/api/analytics-summary':return isObject(value.totals)&&Array.isArray(value.categories)&&(value.currency===null||typeof value.currency==='string')&&isObject(value.notices)&&['card_double_count','one_sided_source_transfers'].every(k=>isObject(value.notices[k])&&Number.isInteger(value.notices[k].count)&&value.notices[k].count>=0&&typeof value.notices[k].note==='string');
    default:return true;
  }
}
const api=async(route,data)=>{const result=await window.financeFrontendApi.request(route,data);if(!validPayload(route,result))throw new Error('invalid_response');return result};
const accounts=()=>state.data?.accounts||[];
const account=id=>accounts().find(a=>String(a.id)===String(id));
function accountDisplayName(a){
  if(!a)return '';
  // Owner, institution and account kind identify the shared account, never a private ID.
  if(a.owner==='JOINT'&&a.kind==='CHECKING'&&/\bing\b/i.test(a.institution||''))return 'ING Gemeinschaftskonto'+(a.closed_on&&a.closed_on<=state.asOf?' (geschlossen)':'');
  return a.display_name||a.id;
}
const accountName=id=>accountDisplayName(account(id))||id||'Konto unbekannt';
const currency=id=>account(id)?.currency||null;
const categoryName=id=>{const c=(state.catalog?.categories||state.list?.categories||[]).find(x=>x.id===id);return c?.label||id};
const statusLabel=s=>({booked:'Gebucht',expected:'Erwartet',unresolved:'Zu klären'})[s]||'Status offen';
const phrase=value=>({ledger_completeness_unverified:'Berechnung setzt vollständige importierte Buchungen voraus.',conflicting_finanzguru_balances_on_same_date:'Importierte Kontostände desselben Tages widersprechen sich.',budgets_not_payment_events:'Budgets sind keine Zahlungsereignisse.',balance_rollforward_ledger_completeness_unverified:'Fortschreibung setzt vollständige importierte Buchungen voraus.',paypal_transaction_date_not_booking_date:'Transaktionsdatum ist kein belegtes Buchungsdatum.',same_day_projection_order_assumed:'Reihenfolge gleichzeitiger Zahlungen angenommen.',conflicting_controls:'Widersprüchliche Vergleichsstände.',currency_conflict:'Währungen stimmen nicht überein.',no_control:'Kein Vergleichsstand vorhanden.',large_amount_variation:'Beträge schwanken stark.',end_date_unknown:'Enddatum unbekannt.',insufficient_recurring_history:'Zu wenige passende Buchungen.',ambiguous_source_history:'Buchungsreihe nicht eindeutig.',competing_payment_schedule:'Möglicherweise doppelt erfasste Zahlungsreihe.',ambiguous_booking_match:'Mehrere Buchungen passen; Zuordnung offen.',partial_payment_conflict:'Teilzahlung oder Zuordnung prüfen.',past_payment_unresolved:'Erwarteter Termin ohne eindeutige Buchung.',recorded_balance_ambiguous:'Kontostände widersprechen sich.',recorded_balance_missing:'Ein belegter Kontostand fehlt.',stale_recorded_balance:'Der belegte Kontostand ist älter als sieben Tage.',unresolved_payments_excluded:'Ungeklärte Zahlungen sind nicht in der Prognose enthalten.',transfers_not_inferred:'Umbuchungen werden nicht automatisch ergänzt.',projection_conditional_on_imported_ledger:'Prognose setzt vollständige importierte Buchungen voraus.'})[typeof value==='string'?value:value?.code]||'Datenbasis prüfen.';
const errorBlock=(key)=>state.errors[key]?`<div class="notice error" role="alert">${esc(state.errors[key])} ${((key==='state'&&state.data)||(key==='list'&&state.list)||(key==='recurring'&&state.recurring)||(key==='analysis'&&state.analysis))?'Angezeigte Daten sind möglicherweise veraltet.':''} <button class="link" data-retry="${key}">Erneut versuchen</button></div>`:'';
const loading=key=>state.busy[key]?'<p class="sub">Daten werden geladen; vorhandene Angaben können veraltet sein.</p>':'';
const {icon,tile,accountTile,categoryIcon}=window.financeFrontendIcons;
function bookingIdentity(t){
  const display=window.financeCounterparties?.describe(t);
  const label=display?.label||t.counterparty||t.description||'Buchung';
  const src=display?.src;
  return {label,icon:typeof src==='string'&&/^\/[\w.-]+$/.test(src)?`<span class="tile logo"><img src="${esc(src)}" alt="" loading="lazy"></span>`:tile(t.is_transfer?'transfer':categoryIcon(displayCategory(t).label))};
}
const categoryStatus=s=>({imported:'Import ohne Bestätigung',confirmed:'Bestätigt',explicit:'Bestätigt',proposal:'Unbestätigter Vorschlag',proposed:'Unbestätigter Vorschlag',open:'Offen'})[s]||'Offen';
function displayCategory(t){
  if(t.is_transfer)return {label:t.transfer_display||'Umbuchung',status:'confirmed',source:'transfer'};
  if(isObject(t.category_display))return t.category_display;
  return {label:t.category_label||(t.category_status?t.category:categoryName(t.category))||'Kategorie offen',id:t.category_id||t.category,status:t.category_status||(t.confirmed?'confirmed':'open'),source:t.category_source||''};
}
const sourceLabel=s=>({imported:'Import',override:'Eigene Zuordnung',rule:'Eigene Regel',model:'Modellvorschlag',history:'Bisherige Bestätigungen',none:'Keine Quelle',local_rule_exact_counterparty_and_direction:'Eigene Regel',local_merchant_family_rule:'Händlerregel',confirmed:'Eigene Bestätigung'})[s]||(/^(ollama:|assistant:)/.test(s||'')?'KI-Vorschlag':'Quelle nicht näher erläutert');
const paymentWarning=p=>p.match_status==='conflict'?'Zuordnung inzwischen mehrdeutig':p.match_status==='stale'?'Buchung nicht mehr im Abgleich':'';
const balanceSourceLabel=source=>({bank:'Bankabruf',finanzguru_file:'Importierter Auszug',confirmed_reference:'Berechnet aus bestätigter Referenz',recorded:'Erfasster Kontostand',statement:'Kontoauszug'})[source]||'Quelle nicht näher dokumentiert';
const movementWarning=value=>({
  same_day_order_assumed:'Reihenfolge der Buchungen am selben Tag angenommen; die Bankreihenfolge ist nicht belegt.',
  owner_snapshot_sequence_assumed:'Reihenfolge zur bestätigten Referenz angenommen; kein belegter Tagesendstand.',
  ledger_completeness_unverified:'Vollständigkeit der importierten Buchungen ist nicht bestätigt.',
  incomplete_source_coverage:'Die vorhandenen Quellen decken den Buchungszeitraum nicht vollständig ab.',
  owner_reference_not_day_end:'Die bestätigte Referenz belegt keinen Tagesendstand.',
  bank_balance_not_day_end:'Der Bankstand belegt keinen Tagesendstand.',
  file_balance_not_day_end:'Der importierte Stand belegt keinen Tagesendstand.',
  invalid_balance_anchor:'Die Referenz für die Berechnung ist nicht verwendbar.',
  conflicting_statement_balances_on_same_date:'Kontoauszüge enthalten widersprüchliche Stände am selben Tag.',
  ledger_currency_conflict:'Die Buchungen enthalten widersprüchliche Währungen.',
})[typeof value==='string'?value:value?.code]||phrase(value);
const balanceAvailable=b=>!!b&&b.status!=='ambiguous'&&validAmount(b.amount)&&!!b.currency;
function runningBalance(t){
  const b=t.account_movement_balance;
  const warnings=(b?.warnings||[]).map(w=>typeof w==='string'?w:w?.code);
  if(balanceAvailable(b))return `<span class="booking-running-balance"><span>Saldo · berechnet</span><strong>${money(b.amount,b.currency)}</strong>${warnings.some(w=>['same_day_order_assumed','owner_snapshot_sequence_assumed'].includes(w))?'<small>Reihenfolge angenommen</small>':''}</span>`;
  const reason=warnings.some(w=>['recorded_balance_ambiguous','conflicting_statement_balances_on_same_date','conflicting_finanzguru_balances_on_same_date'].includes(w))?'Referenz widersprüchlich'
    :warnings.some(w=>['ledger_currency_conflict','currency_conflict'].includes(w))?'Währung klären'
    :warnings.includes('incomplete_source_coverage')?'Quellenlücke'
    :warnings.some(w=>['paypal_transaction_date_not_booking_date','owner_reference_not_day_end','bank_balance_not_day_end','file_balance_not_day_end','invalid_balance_anchor'].includes(w))?'Datum unklar':'Referenz fehlt';
  return `<span class="booking-running-balance"><span>Saldo offen</span><strong>${reason}</strong></span>`;
}
function currentAccountBalance(a){
  const b=a?.current_balance;
  return `<strong>${balanceAvailable(b)?money(b.amount,b.currency):'Nicht verfügbar'}</strong><small>${esc(balanceSourceLabel(b?.source))} · Stand ${fmtDate(b?.booked_on)}${b?.reference_booked_on?' · Referenz '+fmtDate(b.reference_booked_on):''}</small>${b?.warning?'<small>'+esc(movementWarning(b.warning))+'</small>':''}${!balanceAvailable(b)?'<small>'+esc(b?.status==='ambiguous'?'Kontostände widersprechen sich.':'Ein belegter Kontostand fehlt.')+'</small>':''}`;
}
function bookingBalanceDetails(t){
  const b=t.account_movement_balance,a=account(t.account_id);
  return `<section class="booking-balance-details"><h3>Kontostand · ${esc(accountName(t.account_id))}</h3><div class="balance-detail"><span>Aktueller erfasster Stand</span>${currentAccountBalance(a)}</div><div class="balance-detail"><span>Berechneter Saldo nach dieser Buchung</span><strong>${balanceAvailable(b)?money(b.amount,b.currency):'Nicht verfügbar'}</strong><small>Buchung: ${fmtDate(b?.date||t.date)} · Referenz: ${fmtDate(b?.anchor_booked_on)}</small>${(b?.warnings||[]).map(w=>'<p class="sub">'+esc(movementWarning(w))+'</p>').join('')}${!balanceAvailable(b)&&!b?.warnings?.length?'<p class="sub">Ein belegter Bezug für die Berechnung fehlt.</p>':''}</div></section>`;
}
const bookingRow=(t,i)=>{const identity=bookingIdentity(t),c=displayCategory(t);return `<button type="button" class="row row-button booking-row" data-transaction="${i}" data-source="${state.page==='overview'?'dashboard':'list'}">${identity.icon}<span class="row-main"><strong>${esc(identity.label)}</strong><small>${esc(c.label)} · ${esc(categoryStatus(c.status))}</small><small>${esc(accountDisplayName(account(t.account_id))||t.account_label||accountName(t.account_id))}${(t.payment_statuses||[]).some(paymentWarning)?' · Zuordnung prüfen':''}</small></span><span class="booking-values"><span class="row-end"><span class="booking-amount">${money(t.amount,t.currency)}</span><small>${fmtDate(t.date)}</small></span>${runningBalance(t)}</span><span class="chev">${icon('chevron-right')}</span></button>`};
// Labels are matched only against the shared local merchant catalog, never account IDs.
function recurringIdentity(item,fallback){
  const original=item.label||'Zahlung';
  const identity=window.financeCounterparties?.identify({counterparty:original});
  const label=identity?.label||window.financeCounterparties?.displayName(original)||original;
  const src=identity?.src;
  return {label,icon:typeof src==='string'&&/^\/[\w.-]+$/.test(src)?`<span class="tile logo"><img src="${esc(src)}" alt="" loading="lazy"></span>`:tile(fallback)};
}
const recurringAccount=item=>item.account_id?accountName(item.account_id):'Kontozuordnung offen';
function recurringStatus(item){
  if(item.requires_contract_setup)return ['Vertragsangaben offen',item.active===false?'Inaktiv':''].filter(Boolean);
  const missing=Array.isArray(item.missing_fields)?item.missing_fields:[];
  return [
    item.inferred?'erkannt':'',
    missing.includes('currency')?'Zahlungswährung offen':'',
    missing.includes('day_of_month')?'Zahlungstermin offen':(item.date_estimated??item.estimated)?'Termin geschätzt':'',
    item.amount_estimated?'Betrag geschätzt':'',
    item.active===false?'Inaktiv':'',
  ].filter(Boolean);
}
const documentPhases=item=>item.document_id&&Array.isArray(item.phases)?item.phases:[];
// Contract phase prices are positive EUR costs (contracts._phases / recurring.save).
// This is a price at the cutoff, not a claim that collection occurs that day.
function phaseAt(item,date){
  if(!/^\d{4}-\d{2}-\d{2}$/.test(date||''))return null;
  if((item.valid_from&&date<item.valid_from)||(item.valid_to&&date>item.valid_to))return null;
  return documentPhases(item).find(phase=>date>=phase.valid_from&&(!phase.valid_to||date<=phase.valid_to))||null;
}
const phaseMoney=(item,phase)=>item.currency&&item.currency!=='EUR'?'Währung klären':validAmount(phase.amount)&&Number(phase.amount)>0?money(phase.amount,'EUR'):'Betrag offen';
function recurringAmount(item){
  if(documentPhases(item).length){const phase=phaseAt(item,state.asOf);return phase?phaseMoney(item,phase):'Vertragsphasen'}
  if(item.document_id&&!validAmount(item.amount))return 'Betrag offen';
  return money(item.amount,item.currency);
}
const phaseRhythm=phase=>phase.interval_months===1?'monatlich':`alle ${phase.interval_months} Monate`;
const eventRow=(e,i)=>{const identity=recurringIdentity(e,'calendar-days');return `<button type="button" class="row row-button" data-event="${i}">${identity.icon}<span class="row-main"><strong>${esc(identity.label)}</strong><small><span class="row-date">${fmtDate(e.expected_date)}</span> · ${esc(statusLabel(e.status))}${recurringStatus(e).map(label=>' · '+esc(label)).join('')}</small><small>${esc(recurringAccount(e))}</small></span><span class="row-end">${money(e.amount,e.currency)}</span><span class="chev">${icon('chevron-right')}</span></button>`};
const scheduleRow=(s,i)=>{const identity=recurringIdentity(s,'repeat-2'),phase=phaseAt(s,state.asOf);return `<button type="button" class="row row-button" data-schedule="${i}">${identity.icon}<span class="row-main"><strong>${esc(identity.label)}</strong><small>${esc(recurringAccount(s))}${recurringStatus(s).map(label=>' · '+esc(label)).join('')}</small>${phase?`<small>Kosten je Zahlung · ${esc(phaseRhythm(phase))}</small>`:''}</span><span class="row-end">${esc(recurringAmount(s))}</span><span class="chev">${icon('chevron-right')}</span></button>`};
function nav(){for(const id of ['nav','bottom'])$('#'+id).innerHTML=tabs.map(([key,label,icon])=>`<button type="button" data-page="${key}" class="${state.page===key?'active':''}" ${state.page===key?'aria-current="page"':''}><span class="nav-icon" aria-hidden="true">${window.financeFrontendIcons.icon(icon)}</span><span>${label}</span></button>`).join('')}
function overview(){const d=state.data?.dashboard;if(!d)return `<section class="card">${errorBlock('state')}${loading('state')}</section>`;if(d.error)return `<section class="card"><p role="alert">${esc(d.error)}</p></section>`;
const balances=(d.balance_summary||[]).map(b=>`<div class="balance-line"><strong>${b.known_count?money(b.amount,b.currency):'Nicht verfügbar'}</strong><small>${esc(b.currency)} · ${b.known_count} von ${b.account_count} Konten · ${fmtDate(b.oldest_booked_on)} bis ${fmtDate(b.newest_booked_on)}</small></div>`).join('')||'<p>Keine Kontostände vorhanden.</p>';
const rows=(d.accounts||[]).map(a=>{const b=a.current_balance,rec=a.reconciliation;return `<button type="button" class="row row-button" data-account="${esc(a.id)}">${accountTile(a)}<span class="row-main"><strong>${esc(accountDisplayName(a))}</strong><small>${esc(a.institution||'')} ${rec?`· Abgleich: ${esc(({matched:'passend',different:'abweichend',missing:'nicht verfügbar'})[rec.status]||'offen')}${rec.reason?` · ${esc(phrase(rec.reason))}`:''}`:''}</small></span><span class="row-end">${b?.amount!=null?money(b.amount,b.currency):b?.status==='ambiguous'?'Stand mehrdeutig':'Stand fehlt'}<small>${b?.source==='confirmed_reference'?`Berechnet · Referenz ${fmtDate(b.reference_booked_on)}`:b?fmtDate(b.booked_on):'nicht als Null gewertet'}</small></span><span class="chev">${icon('chevron-right')}</span></button>`}).join('')||'<p class="empty">Keine Konten vorhanden.</p>';
const events=(state.recurring?.events||[]).map((e,i)=>({e,i})).filter(x=>x.e.status==='expected').slice(0,3).map(x=>eventRow(x.e,x.i)).join('');
return `${errorBlock('state')}${loading('state')}<details class="cutoff-details"><summary>Datenstichtag ${fmtDate(state.asOf)} ändern</summary><form id="cutoff-form" class="filters"><label>Datenstichtag<input id="cutoff" type="date" value="${esc(state.asOf)}" required></label><button class="action">Anzeigen</button></form></details><div class="grid"><div class="stack"><section class="card hero"><div class="section-label">ERFASSTE KONTOSTÄNDE</div>${balances}<p class="sub">Fehlende Stände sind nicht Null. Stände können unterschiedliche Daten haben.</p></section><section class="card"><div class="section-head"><h2>Deine Konten</h2></div>${rows}</section></div><div class="stack"><section class="card"><div class="section-head"><h2>Demnächst fällig</h2><button class="link" data-page="contracts">Alle ansehen →</button></div>${errorBlock('recurring')}${loading('recurring')}${events||(!state.busy.recurring&&!state.errors.recurring?'<p class="sub">Keine erwarteten Zahlungen im Zeitraum.</p>':'')}</section><section class="card"><div class="section-head"><h2>Letzte Buchungen</h2><button class="link" data-page="transactions">Alle ansehen →</button></div>${(d.bookings||[]).slice(0,5).map(bookingRow).join('')||'<p class="sub">Keine Buchungen im gewählten Monat.</p>'}</section></div></div>`}
function options(items,value,all){return `<option value="">${all}</option>`+items.map(x=>`<option value="${esc(x.id)}" ${String(x.id)===String(value)?'selected':''}>${esc(x.label||(x.currency?accountDisplayName(x):x.display_name)||x.id)}</option>`).join('')}
const categoryOptions=items=>items.map(c=>({...c,label:[(state.catalog?.parents||[]).find(p=>p.id===c.parent_id)?.label,c.label].filter(Boolean).join(' / ')}));
function filteredSummary(list){
  const summary=list?.summary;
  if(!summary||!Array.isArray(summary.currencies))return '<p class="sub">Gesamtsummen nicht verfügbar. Die angezeigte Seite ist keine Gesamtauswertung.</p>';
  const signedMoney=(value,currency)=>`${validAmount(value)&&Number(value)>0?'+':''}${money(value,currency)}`;
  return `<details class="filtered-summary"><summary><span class="summary-total">${esc(summary.total)} gefilterte Buchungen</span><span class="summary-net-label">Nettoänderung inkl. Umbuchungen</span><span class="summary-nets">${summary.currencies.map(c=>`<span class="summary-currency"><span>${esc(c.currency)}</span> <strong>${signedMoney(c.net,c.currency)}</strong></span>`).join('')}</span><span class="summary-expand">Bilanzdetails anzeigen</span></summary><div class="summary-breakdown"><p class="sub">Alle gefilterten Treffer, keine Seitensumme. Währungen werden getrennt ausgewertet.</p>${summary.currencies.map(c=>`<div class="summary-detail-currency"><strong>${esc(c.currency)} · ${esc(c.count)} Buchungen</strong><div class="summary-metrics"><span>Einnahmen <b>${money(c.income,c.currency)}</b></span><span>Ausgaben <b>${money(c.outflow,c.currency)}</b></span><span>Umbuchungen <b>${money(c.transfer,c.currency)}</b></span><span>Nettoänderung inkl. Umbuchungen <b>${signedMoney(c.net,c.currency)}</b></span></div></div>`).join('')}</div></details>`;
}
// An explicit all mode distinguishes all accounts from an empty individual selection.
const selectedBookingAccounts=()=>state.filter.account_id?[String(state.filter.account_id)]:state.filter.account_ids;
const allBookingAccounts=()=>!state.filter.account_id&&state.filter.account_mode==='all';
function bookingAccountPicker(){
  const all=allBookingAccounts(),selected=selectedBookingAccounts(),items=accounts();
  const closed=a=>!!a.closed_on&&a.closed_on<=state.asOf;
  const active=items.filter(a=>!closed(a)),historical=items.filter(closed);
  const row=a=>`<label class="account-choice"><input type="checkbox" id="booking-account-${esc(a.id)}" data-booking-account="${esc(a.id)}" ${!all&&selected.includes(String(a.id))?'checked':''}><span>${esc(accountDisplayName(a))}</span></label>`;
  const label=all?'Alle Konten':selected.length===1?accountName(selected[0]):selected.length?`${selected.length} Konten`:'Bitte Konto wählen';
  return `<details class="account-picker" ${accountPickerState.open?'open':''}><summary aria-label="Konten filtern: ${esc(label)}"><span>${esc(label)}</span></summary><div class="account-picker-options"><button type="button" class="action account-all" id="booking-all-accounts" aria-pressed="${all}">Alle Konten${all?' · ausgewählt':''}</button><fieldset><legend>Aktive Konten</legend>${active.map(row).join('')||'<p class="sub">Keine aktiven Konten.</p>'}</fieldset>${historical.length?`<details class="historical-accounts" ${accountPickerState.historicalOpen?'open':''}><summary>Historische Konten (geschlossen) · ${historical.length}</summary><fieldset><legend class="sr-only">Historische Konten (geschlossen)</legend>${historical.map(row).join('')}</fieldset></details>`:''}</div></details>`;
}
function selectedAccountBalances(){
  if(allBookingAccounts())return '';
  const selected=selectedBookingAccounts();
  if(!selected.length)return '';
  const b=selected.length===1?account(selected[0])?.current_balance:null;
  const label=selected.length===1?`Kontostand: ${balanceAvailable(b)?money(b.amount,b.currency):'offen'} · Stand ${fmtDate(b?.booked_on)}`:`Erfasste Kontostände · ${selected.length} gewählte Konten`;
  return `<details class="selected-account-balances"><summary>${esc(label)}</summary><div>${selected.map(id=>`<div class="selected-account-balance"><span>${esc(accountName(id))}</span>${currentAccountBalance(account(id))}</div>`).join('')}</div></details>`;
}
function emptyBookingMessage(list){
  const fallback='<p class="empty">Keine passenden Buchungen.</p>';
  if(list.total!==0||state.busy.list||state.errors.list)return fallback;
  const coverage=(list.account_coverage||[]).filter(c=>c.count>0);
  if(!coverage.length||!coverage.every(c=>/^\d{4}-\d{2}-\d{2}$/.test(c.first_date||'')&&/^\d{4}-\d{2}-\d{2}$/.test(c.last_date||'')))return fallback;
  const first=coverage.map(c=>c.first_date).sort()[0],last=coverage.map(c=>c.last_date).sort().at(-1),f=state.filter;
  if((f.date_from&&f.date_to&&f.date_from>f.date_to)||!((f.date_from&&f.date_from>last)||(f.date_to&&f.date_to<first)))return fallback;
  return `<div class="empty booking-coverage"><p>Für diesen Zeitraum keine Buchungen. Vorhanden vom ${fmtDate(first)} bis ${fmtDate(last)}.</p><button class="action" type="button" data-booking-coverage>Vorhandene Buchungen anzeigen</button><p class="sub">Hebt nur den Datumsfilter auf; Konten und weitere Filter bleiben erhalten.</p></div>`;
}
function transactions(){
  const f=state.filter,l=state.list;
  const period=f.date_from===monthRange().date_from&&f.date_to===monthRange().date_to?'Aktueller Monat':!f.date_from&&!f.date_to?'Alle Zeiträume':'Eigener Zeitraum';
  return `<section class="card transactions-card"><div class="section-head"><h2>${period}</h2><div class="quick-actions"><button class="link" data-period="month">Aktueller Monat</button><button class="link" data-period="all">Alle Zeiträume</button></div></div><div class="filters booking-filters"><div class="search-field">${icon('search')}<input type="search" id="search" aria-label="Buchung suchen" placeholder="Buchung suchen" value="${esc(f.query)}"></div>${bookingAccountPicker()}<select id="reviewed-filter" aria-label="Prüfstatus filtern">${options([{id:'false',label:'Offen / ungeprüft'},{id:'true',label:'Bestätigt'}],f.reviewed,'Alle Prüfstände')}</select></div>${selectedAccountBalances()}<details class="filter-details"><summary>Weitere Filter · Richtung, Kategorie, Datum, Betrag</summary><div class="filters"><select id="direction-filter" aria-label="Richtung filtern">${options([{id:'income',label:'Einnahmen'},{id:'expense',label:'Ausgaben'},{id:'transfer',label:'Umbuchungen'}],f.direction,'Alle Richtungen')}</select><select id="category-filter" aria-label="Kategorie filtern">${options(categoryOptions(state.catalog?.categories||l?.categories||[]),f.category,'Alle Kategorien')}</select><label>Von<input type="date" id="date-from" value="${esc(f.date_from)}"></label><label>Bis<input type="date" id="date-to" value="${esc(f.date_to)}"></label><label>Betrag mindestens (absolut)<input inputmode="decimal" id="amount-min" placeholder="0,00" value="${esc(f.amount_min)}"></label><label>Betrag höchstens (absolut)<input inputmode="decimal" id="amount-max" placeholder="100,00" value="${esc(f.amount_max)}"></label></div><p class="sub">Beträge ohne Vorzeichen; mindestens und höchstens einschließlich. Währungen bleiben getrennt.</p></details>${errorBlock('list')}${loading('list')}${l?filteredSummary(l):''}<div id="transaction-list" aria-busy="${!!state.busy.list}">${l?(l.rows.map(bookingRow).join('')||emptyBookingMessage(l)):''}</div>${l?`<div class="pager"><button class="action" id="transactions-prev" ${l.page===0||state.busy.list?'disabled':''}>Zurück</button><span>Seite ${l.page+1} von ${Math.max(1,l.pages)} · ${l.total} Buchungen</span><button class="action" id="transactions-next" ${l.page+1>=l.pages||state.busy.list?'disabled':''}>Weiter</button></div>`:''}</section>`;
}
function contracts(){const r=state.recurring;return `<div class="grid"><section class="card"><div class="section-head"><h2>Fälligkeiten</h2><span class="pill">${fmtDate(r?.as_of||state.asOf)} – ${fmtDate(r?.until||addDays(state.asOf,90))}</span></div>${errorBlock('recurring')}${loading('recurring')}${r?(r.events||[]).map(eventRow).join('')||'<p class="empty">Keine Termine im Zeitraum.</p>':''}</section><section class="card"><h2>Regelmäßige Zahlungen</h2><p class="sub">Gespeicherte Regeln und erkannte Reihen; Schätzungen sind gekennzeichnet.</p>${(r?.schedules||[]).map(scheduleRow).join('')||(!state.busy.recurring?'<p class="sub">Keine Regeln vorhanden.</p>':'')}${(r?.warnings||[]).map(w=>`<p class="notice">${esc(phrase(w))}</p>`).join('')}<p class="sub">Die Bearbeitung von Vertragsangaben und Quellen ist in dieser Ansicht noch nicht eingebunden.</p></section></div>`}
function categoryChart(categories,currency){
  const amounts=categories.filter(c=>validAmount(c.amount)).map(c=>Math.abs(Number(c.amount)));
  const max=Math.max(0,...amounts);
  return categories.map(c=>{const valid=validAmount(c.amount), n=valid?Number(c.amount):0, width=max&&valid?Math.min(50,Math.abs(n)/max*50):0;
    return `<div class="category-chart-row"><div class="metric-row"><span>${esc(c.label)} <small>(${c.transaction_type==='income'?'Einnahmen':'Ausgaben'}${valid&&n<0?' · negativer Betrag':''})</small></span><strong>${money(c.amount,currency)}</strong></div><div class="category-track" aria-hidden="true"><span class="category-zero"></span><span class="category-bar ${n<0?'negative':''}" style="${n<0?'right':'left'}:50%;width:${width}%"></span></div></div>`;
  }).join('');
}
function analysisNotices(notices){
  if(!isObject(notices))return '';
  return [['card_double_count','Mögliche doppelte Kartenerfassung'],['one_sided_source_transfers','Umbuchungen ohne Gegenbuchung']].map(([key,label])=>{
    const item=notices[key];
    if(!isObject(item)||!Number.isInteger(item.count)||item.count<=0)return '';
    return `<div class="notice analysis-notice"><strong>${esc(label)}: ${item.count}</strong>${typeof item.note==='string'?`<p>${esc(item.note)}</p>`:''}</div>`;
  }).join('');
}
function analysis(){const a=state.analysis, selected=account(state.analysisAccount);return `<div class="filters"><label>Monat<input type="month" id="analysis-month" value="${esc(state.month)}"></label><label>Konto<select id="analysis-account">${options(accounts(),state.analysisAccount,'Konto auswählen')}</select></label></div><p class="sub">Nur bestätigte Kategorien. Offene Buchungen und Umbuchungen werden getrennt gezählt. Die Auswertung verlangt eine einheitliche Buchungswährung.</p>${errorBlock('analysis')}${loading('analysis')}${!selected?'<p class="notice">Bitte ein Konto auswählen.</p>':''}${a&&selected&&a.currency?`<div class="analysis-grid"><section class="card"><h2>Monatsergebnis · ${esc(a.currency)}</h2><div class="metric-row"><span>Einnahmen</span><strong>${money(a.totals.income,a.currency)}</strong></div><div class="metric-row"><span>Ausgaben</span><strong>${money(a.totals.outflow,a.currency)}</strong></div><div class="metric-row"><span>Saldo</span><strong>${money(a.totals.net,a.currency)}</strong></div><p class="sub">${a.totals.confirmed_count} bestätigt · ${a.totals.unreviewed_count} offen · ${a.totals.transfer_count} Umbuchungen</p>${analysisNotices(a.notices)}</section><section class="card"><h2>Kategorien</h2>${categoryChart(a.categories||[],a.currency)||'<p class="sub">Keine bestätigten Kategorien im Zeitraum.</p>'}</section></div>`:a&&selected?'<p class="notice">Keine Buchungen mit auswertbarer Währung im Zeitraum.</p>':''}`}
function more(){return `<section class="card"><h2>Einstellungen & Information</h2><div class="setting"><span><strong>Darstellung</strong><small>Helles oder dunkles Erscheinungsbild</small></span><button class="action" id="theme">${document.body.classList.contains('light')?'Dunkel':'Hell'} wählen</button></div><div class="setting"><span><strong>Daten</strong><small>Ansicht erneut aus dem Backend laden</small></span><button class="action" id="reload">Neu laden</button></div><p class="sub">Die Einbindung der Verwaltung von Profil, Bankzugängen und Benutzern sowie der Vermögensansicht ist noch offen.</p></section>`}
// Add decorative local SVGs without changing control labels or accessible names.
function decorateIcons(root=document){
  root.querySelectorAll('[data-static-icon]').forEach(node=>{if(!node.querySelector('svg'))node.innerHTML=icon(node.dataset.staticIcon)});
  root.querySelectorAll('button, a.action, a.link, summary').forEach(node=>{
    if(node.querySelector('svg, .tile, img'))return;
    const id=node.id,text=node.textContent.trim();
    let name='chevron-right';
    if(node.tagName==='SUMMARY')name=node.closest('.filter-details')?'sliders-horizontal':'chevron-down';
    else if(node.matches('[aria-label="Schließen"]'))name='x';
    else if(node.matches('[data-close]')||id==='transactions-prev'||id==='return-recurring')name='arrow-left';
    else if(node.matches('[data-retry]')||id==='reload')name='refresh-cw';
    else if(id==='theme')name='sun';
    else if(id==='reset-classification')name='rotate-ccw';
    else if(node.matches('[id^="confirm-"]')||node.type==='submit'&&node.closest('#edit-form'))name='check';
    else if(node.matches('[data-related-search]'))name='search';
    else if(node.matches('[data-period]')||node.closest('#cutoff-form'))name='calendar-days';
    else if(node.closest('form.logout'))name='log-out';
    else if(node.tagName==='A')name='arrow-up-right';
    for(const child of node.childNodes)if(child.nodeType===Node.TEXT_NODE)child.textContent=child.textContent.replace(/[→↗×]/g,'').trimEnd();
    node.insertAdjacentHTML('afterbegin',icon(name));node.classList.add('with-icon');
  });
}
// Removing a focused input can synchronously dispatch change. Finish the current
// DOM replacement before applying the latest state requested by that handler.
let renderActive=false,renderQueued=false;
function render(){
  if(renderActive){renderQueued=true;return}
  renderActive=true;
  const focused=document.activeElement,focusId=focused?.id,start=focused?.selectionStart,end=focused?.selectionEnd;
  try{
    do{
      renderQueued=false;renderView();
      if(!renderQueued&&(['search','amount-min','amount-max'].includes(focusId)||focusId?.startsWith('booking-account-')||focusId==='booking-all-accounts')&&document.getElementById(focusId)){
        const field=document.getElementById(focusId);field.focus();if(start!=null)field.setSelectionRange(start,end);
      }
    }while(renderQueued)
  }finally{renderActive=false}
}
function renderView(){if($('.account-picker'))accountPickerState.open=$('.account-picker').open;if($('.historical-accounts'))accountPickerState.historicalOpen=$('.historical-accounts').open;const filterOpen=$('.filter-details')?.open, summaryOpen=$('.filtered-summary')?.open;const labels={overview:['Konten','Kontostände und Buchungen zum gewählten Stichtag.'],transactions:['Buchungen','Buchungen suchen und Kategorien bestätigen.'],contracts:['Verträge','Regelmäßige Zahlungen und erwartete Termine.'],analysis:['Analysen','Bestätigte Kategorien im gewählten Monat.'],more:['Mehr','Darstellung und weitere Bereiche.']};$('#title').textContent=labels[state.page][0];$('#subtitle').textContent=labels[state.page][1];$('#crumb').textContent=labels[state.page][0].toUpperCase();$('#mode').textContent=state.data?`${state.data.demo?'Demobestand':'Dein Finanzbestand'}${state.errors.state?' · nicht aktualisiert':state.busy.state?' · wird aktualisiert':''}`:(state.errors.state?'Daten nicht verfügbar':'Daten werden geladen …');$('#side-status').textContent=state.data?`Datenstichtag ${fmtDate(state.data.as_of)}`:'Noch kein Datenstand';$('#footer').textContent=state.data?`Datenstichtag ${fmtDate(state.data.as_of)} · Kontostände mit eigenem Datum`:'Datenstand nicht verfügbar';$('#content').innerHTML=({overview,transactions,contracts,analysis,more})[state.page]();nav();decorateIcons();if(filterOpen&&$('.filter-details'))$('.filter-details').open=true;if(summaryOpen&&$('.filtered-summary'))$('.filtered-summary').open=true}
async function load(key,fn){const epoch=state.epoch[key]=(state.epoch[key]||0)+1;let success=false;state.busy[key]=true;delete state.errors[key];render();try{const data=await fn();if(epoch!==state.epoch[key])return false;state[key==='state'?'data':key]=data;success=true}catch(e){if(epoch!==state.epoch[key])return false;state.errors[key]=key==='analysis'&&e.code==='mixed_currency_analytics'?'Mehrere Buchungswährungen im Zeitraum. Bitte ein anderes Konto oder einen anderen Zeitraum wählen.':'Aktualisierung fehlgeschlagen. Bitte erneut versuchen.'}finally{if(epoch===state.epoch[key]){state.busy[key]=false;render()}}return success}
const loadState=async()=>{const success=await load('state',()=>api('/api/state?as_of='+encodeURIComponent(state.asOf)));if(success)loadPage();return success};
const loadRecurring=()=>load('recurring',()=>api('/api/recurring-view',{as_of:state.asOf,until:addDays(state.asOf,90),account_id:''}));
const loadList=()=>{
  const f=state.filter,p={page:f.page,order_column:'date',order_direction:'desc'};
  if(!allBookingAccounts()){
    const selected=selectedBookingAccounts();
    if(!selected.length){state.epoch.list=(state.epoch.list||0)+1;state.list=null;state.busy.list=false;state.errors.list='Bitte Konto wählen oder ausdrücklich Alle Konten auswählen.';render();return Promise.resolve(false)}
    if(selected.length===1)p.account_id=selected[0];else p.account_ids=[...selected];
  }
  for(const k of ['query','category','date_from','date_to','direction'])if(f[k])p[k]=f[k];
  if(f.reviewed!=='')p.reviewed=f.reviewed==='true';
  for(const k of ['amount_min','amount_max'])if(f[k]){
    const value=f[k].trim().replace(',','.');
    if(!/^\d+(?:\.\d{1,2})?$/.test(value)){state.busy.list=false;state.errors.list='Betrag bitte ohne Vorzeichen mit höchstens zwei Nachkommastellen eingeben.';render();return Promise.resolve(false)}
    p[k]=value.split('.')[0]+'.'+(value.split('.')[1]||'').padEnd(2,'0');
  }
  if(p.amount_min&&p.amount_max&&BigInt(p.amount_min.replace('.',''))>BigInt(p.amount_max.replace('.',''))){state.busy.list=false;state.errors.list='Der Mindestbetrag darf den Höchstbetrag nicht überschreiten.';render();return Promise.resolve(false)}
  return load('list',()=>api('/api/classification-list',p));
};
const loadCatalog=async()=>{const epoch=state.epoch.catalog=(state.epoch.catalog||0)+1;try{const catalog=await api('/api/classification-catalog',{});if(epoch!==state.epoch.catalog)return;state.catalog=catalog;if(state.page==='transactions')render()}catch(e){if(epoch!==state.epoch.catalog)return;state.errors.list='Kategorien konnten nicht geladen werden.';render()}};
const loadAnalysis=()=>{const a=account(state.analysisAccount);state.analysis=null;if(!a){++state.epoch.analysis;state.busy.analysis=false;delete state.errors.analysis;render();return}const [y,m]=state.month.split('-').map(Number);if(!y||!m)return;const from=`${state.month}-01`,to=localDate(new Date(y,m,0));return load('analysis',()=>api('/api/analytics-summary',{date_from:from,date_to:to,account_id:state.analysisAccount,require_single_currency:true}))};
let dialogReturnFocus=null;
function openDialog(html){if(!$('#dialog').open)dialogReturnFocus=document.activeElement;dialogEpoch++;$('#dialog-content').innerHTML=html;$('#dialog').showModal();decorateIcons($('#dialog-content'))}
$('#dialog').addEventListener('close',()=>{dialogEpoch++;if(dialogReturnFocus?.isConnected)dialogReturnFocus.focus()});
$('#dialog').addEventListener('cancel',e=>{if(savePending)e.preventDefault()});
function evidenceDetails(x){
  return `${x.transfer_display?`<p class="notice">${esc(x.transfer_display)}</p>`:''}${(x.payment_statuses||[]).map(p=>`<p class="notice ${paymentWarning(p)?'error':''}"><strong>${esc(p.provider||'Zahlungsdienst')} · ${esc(({paid:'Bezahlt',pending:'Ausstehend',cancelled:'Storniert',refunded:'Erstattet',completed:'Abgeschlossen'})[p.event_status]||p.event_status||'Status offen')}</strong>${fmtDate(p.event_date)}${p.amount!=null?' · '+money(p.amount,p.currency||x.currency):''}${paymentWarning(p)?' · '+esc(paymentWarning(p)):''}${p.provider_reference?' · Referenz: '+esc(p.provider_reference):''}${p.evidence?' · Nachweis: '+esc(typeof p.evidence==='string'?p.evidence:JSON.stringify(p.evidence)):''}</p>`).join('')}${(x.links||[]).map(l=>`<p class="notice"><strong>${esc(l.evidence_type==='order_confirmation'?'Bestellbestätigung':l.kind==='invoice'?'Rechnung':l.kind==='contract'?'Vertrag':l.kind||'Beleg')}</strong>${esc(l.vendor||'')} · ${esc(l.title||'')}${l.document_date?' · '+fmtDate(l.document_date):''}${l.allocation_type?' · '+esc(({evidence:'Zahlungsnachweis',refund:'Erstattung',payment:'Zahlung'})[l.allocation_type]||l.allocation_type):''}<br><span class="sub">Die Bearbeitung der Belegzuordnung ist in dieser Ansicht noch nicht eingebunden.</span></p>`).join('')}`;
}
async function transactionDialog(t,returnItem=null,feedback=''){
  if(!t)return;
  openDialog('<h2 id="dialog-title">Buchungsdetails</h2><p>Daten werden geladen …</p><button class="action" data-close>Schließen</button>');
  const epoch=dialogEpoch;
  try{
    const [res,catalog]=await Promise.all([api('/api/classification-transaction-get',{account_id:t.account_id,external_id:t.external_id}),state.catalog?Promise.resolve(state.catalog):api('/api/classification-catalog',{})]);
    if(!$('#dialog').open||epoch!==dialogEpoch)return;
    state.catalog=catalog;
    const x=res.transaction,c=displayCategory(x),identity=bookingIdentity(x),proposal=!x.confirmed&&!x.is_transfer?x.category_proposal:null;
    const cats=(catalog.categories||[]).filter(c=>c.transaction_type===x.direction);
    const proposalAvailable=proposal&&cats.some(c=>c.id===proposal.category);
    const displayProposalAvailable=c.status==='proposal'&&cats.some(category=>category.id===c.id)&&(!proposal||proposal.category!==c.id);
    $('#dialog-content').innerHTML=`<button class="icon-button close" aria-label="Schließen" data-close>${icon('x')}</button><h2 id="dialog-title">${identity.icon}${esc(identity.label)}</h2><p class="sub">Buchungsdetails · ${fmtDate(x.date)} · ${esc(accountName(x.account_id))}</p><div class="detail-amount">${money(x.amount,x.currency)}</div>${bookingBalanceDetails(x)}<p><strong>${esc(c.label)}</strong> · ${esc(categoryStatus(c.status))}<br><small>Herkunft: ${esc(sourceLabel(c.source))}</small></p><details><summary>Originalangaben und Referenz</summary><p>Gegenpartei: ${esc(x.counterparty||'Keine Angabe')}</p><p>Verwendungszweck: ${esc(x.description||'Keine Angabe')}</p><p>Konto-ID: ${esc(x.account_id)}</p><p>Referenz: ${esc(x.external_id)}</p>${x.date_basis?`<p>Datumsbasis: ${esc(x.date_basis)}</p>`:''}</details>${evidenceDetails(x)}${returnItem?'<button class="link" id="return-recurring">Zurück zur Fälligkeit</button>':''}${x.is_transfer?'<p class="notice">Umbuchung: keine Kategoriezuordnung. Gegenkonto und Gegenbuchung werden nur angezeigt, wenn sie im Bestand belegt sind.</p>':`${displayProposalAvailable?`<section class="notice" id="display-proposal-section"><strong>Angezeigter Vorschlag: ${esc(c.label)}</strong><p>Herkunft: ${esc(sourceLabel(c.source))} · Sicherheit nicht ausgewiesen.</p><button class="action" type="button" id="confirm-display-proposal">Angezeigten Vorschlag ausdrücklich bestätigen</button></section>`:''}${proposal?`<section class="notice"><strong>Separater, unbestätigter Vorschlag: ${esc(categoryName(proposal.category))}</strong><p>Herkunft: ${esc(sourceLabel(proposal.provenance))} · Sicherheit: ${esc(({high:'hoch',medium:'mittel',low:'niedrig'})[proposal.confidence]||'offen')}</p>${proposalAvailable?'<button class="action" type="button" id="confirm-proposal">Vorschlag ausdrücklich bestätigen</button>':''}</section>`:''}<form id="edit-form" class="detail-grid"><label>Kategorie<select id="edit-category" required>${options(categoryOptions(cats),x.category,'Kategorie auswählen')}</select></label><p class="sub">${x.confirmed?'Kategorie bestätigt':'Kategorie noch nicht bestätigt'}</p><p id="edit-status" role="status">${esc(feedback)}</p><div class="dialog-actions"><button type="button" class="action" data-close>Abbrechen</button><button class="action primary" type="submit">Speichern</button></div>${x.confirmed?'<details><summary>Eigene Prüfung zurücksetzen</summary><p>Die eigene Bestätigung wird aufgehoben. Originalangaben bleiben erhalten. Erst die folgende Aktion speichert.</p><button type="button" class="action" id="reset-classification">Prüfung ausdrücklich zurücksetzen</button></details>':''}</form>`}`;
    decorateIcons($('#dialog-content'));
    if($('#return-recurring'))$('#return-recurring').onclick=()=>recurringDialog(returnItem);
    const form=$('#edit-form');
    if(!form)return;
    let stale=false,completed=false;
    const write=async(reset=false,proposalCategory=null)=>{
      if(savePending||stale||completed)return;
      const category=proposalCategory||$('#edit-category').value;
      if(!reset&&!category){$('#edit-category').reportValidity();return}
      const controls=[...form.querySelectorAll('button,select'),...$('#dialog-content').querySelectorAll('#confirm-proposal, #confirm-display-proposal')];
      controls.forEach(b=>b.disabled=true);savePending=true;
      const status=$('#edit-status');status.textContent=reset?'Prüfung wird zurückgesetzt …':'Wird gespeichert …';
      try{
        await api(reset?'/api/classification-reset':'/api/classification-save',{account_id:x.account_id,external_id:x.external_id,revision:x.revision,...(reset?{confirmed:true}:{category})});
        completed=true;
        status.textContent=reset?'Prüfung zurückgesetzt. Daten werden aktualisiert …':'Kategorie gespeichert. Daten werden aktualisiert …';
        await Promise.all([loadState(),loadList()]);
        const feedback=(reset?'Prüfung zurückgesetzt.':'Kategorie gespeichert.')+(state.errors.state||state.errors.list?' Aktualisierung der Übersicht fehlgeschlagen; angezeigte Daten können veraltet sein.':'');
        if($('#dialog').open&&epoch===dialogEpoch)await transactionDialog(x,returnItem,feedback);
      }catch(err){
        stale=err.code==='stale_revision'||String(err.message).includes('stale_revision')||err.status===409;
        status.textContent=stale?'Versionskonflikt: Die Buchung wurde zwischenzeitlich geändert. Bitte schließen und erneut öffnen.':'Nicht gespeichert. Bitte erneut versuchen.';
      }finally{
        savePending=false;
        controls.forEach(b=>b.disabled=stale||completed);
        form.querySelectorAll('[data-close]').forEach(b=>b.disabled=false);
      }
    };
    form.onsubmit=e=>{e.preventDefault();void write()};
    if($('#confirm-proposal'))$('#confirm-proposal').onclick=()=>void write(false,proposal.category);
    if($('#confirm-display-proposal'))$('#confirm-display-proposal').onclick=()=>void write(false,c.id);
    if($('#reset-classification'))$('#reset-classification').onclick=()=>void write(true);
  }catch(e){if($('#dialog').open&&epoch===dialogEpoch)$('#dialog-content').innerHTML=`<h2 id="dialog-title">Buchungsdetails</h2><p role="alert">${feedback?esc(feedback)+' Details konnten nicht aktualisiert werden. Weitere Änderungen sind gesperrt. Bitte schließen und erneut öffnen.':'Buchung konnte nicht geladen werden. Bitte erneut öffnen.'}</p><button class="action" data-close>Schließen</button>`}
  decorateIcons($('#dialog-content'));
}
let relatedItem=null;
function projectionDetails(a){
  const r=state.recurring,p=(r?.accounts||[]).find(x=>x.account_id===a.id);
  if(!p)return '<p class="sub">Kontoprojektion wird nach dem Laden der Fälligkeiten verfügbar.</p>';
  return `<details><summary>Bedingte Kontoprojektion · ${fmtDate(r.as_of)} bis ${fmtDate(r.until)}</summary><p>Ausgangssaldo am ${fmtDate(r.as_of)}: ${money(p.as_of_balance,p.currency)}<br>Erwartete Zahlungen zusammen: ${money(amountDifference(p.projected_balance,p.as_of_balance),p.currency)}<br>Endstand am ${fmtDate(r.until)}: ${money(p.projected_balance,p.currency)}</p><p class="sub">Rechnung: Ausgangssaldo plus erwartete Zahlungen dieses Kontos nach dem Stichtag. Ungeklärte Zahlungen sind ausgeschlossen; Umbuchungen werden nicht ergänzt. Budgets sind keine Zahlungsereignisse. Vollständige Buchungen werden vorausgesetzt.</p>${(p.points||[]).map(x=>`<div class="metric-row"><span>${fmtDate(x.date)}</span><strong>${money(x.amount,p.currency)}</strong></div>`).join('')}${[...(r.warnings||[]),...(p.warnings||[])].map(w=>`<p class="notice">${esc(phrase(w))}</p>`).join('')}<button class="link" data-page="contracts" data-close>Fälligkeiten und Annahmen prüfen →</button></details>`;
}
function accountDialog(id){
  const a=(state.data?.dashboard?.accounts||[]).find(a=>String(a.id)===String(id))||account(id);if(!a)return;
  const b=a.current_balance;
  openDialog(`<button class="icon-button close" aria-label="Schließen" data-close>${icon('x')}</button><h2 id="dialog-title">${accountTile(a)}${esc(accountDisplayName(a))}</h2><div class="detail-amount">${money(b?.amount,b?.currency||a.currency)}</div><p>Herkunft: ${esc(({bank:'Bankabruf',finanzguru_file:'Importierter Auszug',confirmed_reference:'Berechnet aus bestätigter Referenz',recorded:'Erfasster Kontostand',statement:'Kontoauszug'})[b?.source]||'Nicht näher dokumentiert')}</p><p>Standdatum: ${fmtDate(b?.booked_on)}${b?.reference_booked_on?'<br>Referenzdatum: '+fmtDate(b.reference_booked_on):''}${b?.reference_amount!=null?'<br>Rechnung: '+money(b.reference_amount,b.currency)+' Referenzbetrag + '+money(amountDifference(b.amount,b.reference_amount),b.currency)+' Buchungsänderung = '+money(b.amount,b.currency):''}</p>${b?.warning?'<p class="notice">'+esc(phrase(b.warning))+'</p>':''}<p class="sub">Erfasste Stände können unterschiedliche Daten haben. Fehlende oder mehrdeutige Angaben gelten nicht als Null.</p>${a.reconciliation?`<p class="notice">Abgleich: ${esc(({matched:'passend',different:'abweichend',missing:'nicht verfügbar'})[a.reconciliation.status]||'offen')}${a.reconciliation.reason?' · '+esc(phrase(a.reconciliation.reason)):''}</p>`:''}${projectionDetails(a)}<div class="dialog-actions"><button class="action" data-account-bookings="${esc(a.id)}">Buchungen dieses Kontos</button></div>`);
}
function relatedBookings(item){
  if(savePending)return;
  $('#dialog').close();clearTimeout(searchTimer);
  const date=item.expected_date||item.valid_from;
  const range=date?{date_from:date.slice(0,7)+'-01',date_to:localDate(new Date(Number(date.slice(0,4)),Number(date.slice(5,7)),0))}:{date_from:'',date_to:''};
  state.filter={...emptyFilter(),...range,account_id:item.account_id||'',query:item.reference||item.label||''};
  state.list=null;navigate('transactions');
}
function recurringDialog(item){
  if(!item)return;relatedItem=item;
  const bookings=item.matched_bookings||item.bookings||[],phases=documentPhases(item),identity=recurringIdentity(item,'calendar-days');
  const phaseDetails=phases.length?`<section aria-label="Vertragsphasen"><h3>Vertragsphasen</h3><p class="sub">Kosten je Zahlung; der Rhythmus belegt keinen genauen Abbuchungstag.</p><ol class="contract-phases">${phases.map(phase=>`<li><strong>${esc(phaseMoney(item,phase))}</strong><span>${esc(phaseRhythm(phase))}</span><span>Ab <span class="row-date">${fmtDate(phase.valid_from)}</span>${phase.valid_to?` bis einschließlich <span class="row-date">${fmtDate(phase.valid_to)}</span>`:' · Ende offen'}</span></li>`).join('')}</ol></section>`:'';
  openDialog(`<button class="icon-button close" aria-label="Schließen" data-close>${icon('x')}</button><h2 id="dialog-title">${identity.icon}${esc(item.label||'Zahlung')}</h2><div class="detail-amount">${esc(recurringAmount(item))}</div><p>${esc(recurringAccount(item))}</p>${item.status?`<p>${esc(statusLabel(item.status))} · ${fmtDate(item.expected_date)}${item.observed_date?' · gebucht am '+fmtDate(item.observed_date):''}</p>`:''}${recurringStatus(item).map(label=>`<p class="notice">${esc(label)}</p>`).join('')}${phaseDetails}${item.interval_months?`<p>Abstand: ${esc(item.interval_months)} Monate</p>`:''}${item.valid_from?`<p>Gültig ab ${fmtDate(item.valid_from)}${item.valid_to?' bis einschließlich '+fmtDate(item.valid_to):''}</p>`:''}${(item.warnings||[]).map(w=>`<p class="notice">${esc(phrase(w))}</p>`).join('')}${bookings.map((b,i)=>`<button class="row row-button" data-related-booking="${i}">${tile('receipt-text')}<span class="row-main"><strong>Zugehörige Buchung öffnen</strong><small>${esc(accountName(b.account_id))} · ${fmtDate(b.date)}</small></span><span>${money(b.amount,b.currency||item.currency)}</span></button>`).join('')}<button class="action" data-related-search>Buchungen im zugehörigen Zeitraum suchen</button><p class="sub">Suche nach Konto, Zeitraum und Bezeichnung; die Treffer sind keine automatische Zuordnungsbestätigung.</p><p class="sub">Die Bearbeitung von Vertragsangaben ist in dieser Ansicht noch nicht eingebunden.</p><div class="dialog-actions"><button class="action" data-close>Zurück zur Übersicht</button></div>`);
}
function loadPage(){
  if(!state.data)return;
  if(state.page==='transactions'&&!state.list&&!state.busy.list){void loadList();void loadCatalog()}
  if((state.page==='contracts'||state.page==='overview')&&!state.recurring&&!state.busy.recurring)void loadRecurring();
  if(state.page==='analysis'&&state.analysisAccount&&!state.analysis&&!state.busy.analysis)void loadAnalysis();
}
function navigate(page){
  state.page=frontendViews.has(page)?page:'overview';
  const url=new URL(location.href);url.pathname='/';url.searchParams.delete('legacy');url.searchParams.set('view',state.page);url.hash='';
  history.replaceState(null,'',url.pathname+url.search);
  render();window.scrollTo(0,0);loadPage();
}
document.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.page){if($('#dialog').open&&!savePending)$('#dialog').close();navigate(b.dataset.page);}else if(b.hasAttribute('data-close')){if(!savePending)$('#dialog').close()}else if(b.dataset.account!==undefined){accountDialog(b.dataset.account)}else if(b.dataset.accountBookings!==undefined){$('#dialog').close();state.filter={...emptyFilter(),account_id:b.dataset.accountBookings};state.list=null;navigate('transactions')}else if(b.hasAttribute('data-related-search')){relatedBookings(relatedItem)}else if(b.dataset.relatedBooking!==undefined){void transactionDialog((relatedItem?.matched_bookings||relatedItem?.bookings||[])[Number(b.dataset.relatedBooking)],relatedItem)}else if(b.id==='booking-all-accounts'){clearTimeout(searchTimer);state.filter={...state.filter,page:0,account_id:'',account_ids:[],account_mode:'all'};void loadList()}else if(b.hasAttribute('data-booking-coverage')){clearTimeout(searchTimer);state.filter={...state.filter,page:0,date_from:'',date_to:''};void loadList()}else if(b.dataset.period){clearTimeout(searchTimer);state.filter={...state.filter,page:0,...(b.dataset.period==='month'?monthRange():{date_from:'',date_to:''})};void loadList()}else if(b.dataset.transaction!==undefined){const rows=b.dataset.source==='dashboard'?state.data?.dashboard?.bookings:state.list?.rows;void transactionDialog(rows?.[Number(b.dataset.transaction)])}else if(b.dataset.event!==undefined)recurringDialog(state.recurring?.events?.[Number(b.dataset.event)]);else if(b.dataset.schedule!==undefined)recurringDialog(state.recurring?.schedules?.[Number(b.dataset.schedule)]);else if(b.dataset.retry){({state:loadState,list:loadList,recurring:loadRecurring,analysis:loadAnalysis})[b.dataset.retry]?.()}else if(b.id==='transactions-prev'||b.id==='transactions-next'){state.filter.page+=b.id==='transactions-next'?1:-1;void loadList()}else if(b.id==='theme'){window.financeTheme.set(window.financeTheme.get()==='light'?'dark':'light')}else if(b.id==='reload'){clearTimeout(searchTimer);for(const key of ['state','list','catalog','recurring','analysis'])state.epoch[key]=(state.epoch[key]||0)+1;state.busy={};state.errors={};state.list=null;state.catalog=null;state.recurring=null;state.analysis=null;void loadState();}});
document.addEventListener('submit',e=>{if(e.target.id==='cutoff-form'){e.preventDefault();state.asOf=$('#cutoff').value;state.epoch.recurring=(state.epoch.recurring||0)+1;state.busy.recurring=false;delete state.errors.recurring;state.recurring=null;void loadState()}});
let searchTimer;document.addEventListener('input',e=>{if(e.target.id==='search'){state.filter.query=e.target.value;state.filter.page=0;clearTimeout(searchTimer);++state.epoch.list;state.busy.list=true;render();searchTimer=setTimeout(()=>void loadList(),350)}});
document.addEventListener('toggle',e=>{if(!e.target.isConnected)return;if(e.target.matches('.account-picker'))accountPickerState.open=e.target.open;else if(e.target.matches('.historical-accounts'))accountPickerState.historicalOpen=e.target.open},true);
document.addEventListener('change',e=>{if(e.target.matches('[data-booking-account]')){clearTimeout(searchTimer);const selected=new Set(allBookingAccounts()?[]:selectedBookingAccounts());if(e.target.checked)selected.add(e.target.dataset.bookingAccount);else selected.delete(e.target.dataset.bookingAccount);state.filter={...state.filter,page:0,account_id:'',account_mode:'selected',account_ids:[...selected]};void loadList();return}const map={'category-filter':'category','date-from':'date_from','date-to':'date_to','reviewed-filter':'reviewed','direction-filter':'direction','amount-min':'amount_min','amount-max':'amount_max'};if(map[e.target.id]){clearTimeout(searchTimer);++state.epoch.list;state.filter[map[e.target.id]]=e.target.value;state.filter.page=0;void loadList()}else if(e.target.id==='analysis-month'){state.month=e.target.value;void loadAnalysis()}else if(e.target.id==='analysis-account'){state.analysisAccount=e.target.value;void loadAnalysis()}});
window.financeTheme.applyCurrent();window.addEventListener('finance-theme-change',render);
window.addEventListener('popstate',()=>navigate(frontendRoute()));
window.addEventListener('hashchange',()=>navigate(frontendRoute()));
navigate(state.page);void loadState();
})();
