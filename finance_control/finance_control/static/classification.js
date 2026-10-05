'use strict';
let classificationPage=0, classificationDocPage=0, classificationDocumentMatchPage=0, classificationSelected=null, classificationSelectedDocument=null, classificationCatalog=[], classificationCatalogParents=[], classificationDocumentSelectionEpoch=0;
let classificationSortColumn=null, classificationSortDirection='asc', classificationLoadEpoch=0;
const classificationColumns={date:'Datum',account:'Konto',detail:'Gegenpartei und Zweck',amount:'Betrag EUR',detected:'Erkannte eigene Kategorie',category:'Kategorie festlegen',status:'Status und Belege'};
function classificationUpdateSortButtons(){
  for(const head of document.querySelectorAll('#classification-table th[data-column]')){
    const column=head.dataset.column,active=column===classificationSortColumn;
    head.setAttribute('aria-sort',active?(classificationSortDirection==='asc'?'ascending':'descending'):'none');
    const button=head.querySelector('button');
    button.textContent=classificationColumns[column]+(active?(classificationSortDirection==='asc'?' ▲':' ▼'):' ↕');
    button.title=`${classificationColumns[column]} sortieren`;
  }
  classificationSyncMobileControls();
}
function classificationColumnFilters(){
  const filters={};
  for(const input of document.querySelectorAll('#classification-table [data-column-filter]')){
    const value=input.value.trim();if(value)filters[input.dataset.columnFilter]=value;
  }
  for(const input of document.querySelectorAll('#classification-mobile-controls [data-mobile-column-filter]')){
    if(!input.dataset.mobileColumnFilter)continue;
    const value=input.value.trim();if(value)filters[input.dataset.mobileColumnFilter]=value;else delete filters[input.dataset.mobileColumnFilter];
  }
  return filters;
}
function classificationSyncMobileControls(){
  if(typeof $!=='function')return;
  const controls=$('classification-mobile-controls');if(!controls)return;
  for(const input of controls.querySelectorAll('[data-mobile-column-filter]')){
    const desktop=document.querySelector(`#classification-table [data-column-filter="${input.dataset.mobileColumnFilter}"]`);
    if(desktop&&input.value!==desktop.value)input.value=desktop.value;
  }
  const sortColumn=$('classification-mobile-sort-column'),sortDirection=$('classification-mobile-sort-direction');
  if(sortColumn){
    sortColumn.value=classificationSortColumn||'';
    const standard=sortColumn.querySelector('option[value=""]');
    if(standard)standard.textContent=$('classification-order').value==='amount_desc'
      ?'Standardsortierung: größter Betrag zuerst':'Standardsortierung: neueste zuerst';
  }
  if(sortDirection){sortDirection.value=classificationSortDirection;sortDirection.disabled=!classificationSortColumn;}
}
function classificationCreateMobileControls(){
  // Card mode hides the table header; generate accessible controls from the same column map.
  if($('classification-mobile-controls'))return;
  const tableWrap=document.querySelector('.classification-table-wrap');if(!tableWrap)return;
  const details=document.createElement('details');details.id='classification-mobile-controls';details.className='classification-mobile-controls';details.open=true;
  const summary=document.createElement('summary');summary.textContent='Spaltenfilter';details.append(summary);
  const content=document.createElement('div');content.className='classification-mobile-control-grid';
  const sortLabel=document.createElement('label');sortLabel.textContent='Sortierung nach';
  const sortColumn=document.createElement('select');sortColumn.id='classification-mobile-sort-column';
  sortColumn.setAttribute('aria-label','Buchungen nach Spalte sortieren');
  sortColumn.append(new Option('Standardsortierung: neueste zuerst',''));
  for(const [key,label] of Object.entries(classificationColumns))sortColumn.append(new Option(label,key));
  sortLabel.append(sortColumn);content.append(sortLabel);
  const directionLabel=document.createElement('label');directionLabel.textContent='Reihenfolge';
  const sortDirection=document.createElement('select');sortDirection.id='classification-mobile-sort-direction';
  sortDirection.setAttribute('aria-label','Sortierreihenfolge');
  sortDirection.append(new Option('Aufsteigend','asc'),new Option('Absteigend','desc'));
  directionLabel.append(sortDirection);content.append(directionLabel);
  for(const [key,label] of Object.entries(classificationColumns)){
    const field=document.createElement('label');field.textContent=`${label} filtern`;
    const input=document.createElement('input');input.type='search';input.dataset.mobileColumnFilter=key;input.setAttribute('aria-label',`${label} filtern`);field.append(input);content.append(field);
  }
  details.append(content);
  const advanced=$('classification-range-controls');
  if(advanced)advanced.append(details);else tableWrap.before(details);
  for(const input of content.querySelectorAll('[data-mobile-column-filter]'))input.addEventListener('change',()=>run(async()=>{
    const desktop=document.querySelector(`#classification-table [data-column-filter="${input.dataset.mobileColumnFilter}"]`);if(desktop)desktop.value=input.value;
    classificationPage=0;await classificationLoad();
  }));
  sortColumn.addEventListener('change',()=>run(async()=>{
    classificationSortColumn=sortColumn.value||null;classificationPage=0;classificationUpdateSortButtons();await classificationLoad();
  }));
  sortDirection.addEventListener('change',()=>run(async()=>{
    classificationSortDirection=sortDirection.value;classificationPage=0;classificationUpdateSortButtons();await classificationLoad();
  }));
  classificationSyncMobileControls();
}
function classificationMessage(text,error=false){const node=$('classification-status');node.textContent=text;node.className=error?'error':'';$('documents-status').textContent=text;}
function classificationCategoryOptions(select,direction,value=''){
  select.replaceChildren();const empty=document.createElement('option');empty.value='';empty.textContent='Kategorie wählen';select.append(empty);
  const parents=classificationCatalogParents.filter(parent=>parent.transaction_type===direction);
  for(const parent of parents){const group=document.createElement('optgroup');group.label=parent.label;for(const cat of classificationCatalog.filter(c=>c.transaction_type===direction&&c.parent_id===parent.id)){const o=document.createElement('option');o.value=cat.id;o.textContent=cat.label;group.append(o);}if(group.children.length)select.append(group);}
  if(direction==='income'||direction==='expense'){const create=document.createElement('option');create.value='__create__';create.textContent='+ Neue Haupt- und Unterkategorie anlegen …';select.append(create);}
  select.value=value;
}
function classificationPrepareCategoryCreator(tx){
  const type=tx.direction==='income'?'Einnahme':'Ausgabe';$('classification-category-type').value=type;
  const list=$('classification-category-parent-list');list.replaceChildren();for(const parent of classificationCatalogParents.filter(item=>item.transaction_type===tx.direction))list.append(new Option(parent.label));
  $('classification-category-parent').value='';$('classification-category-child').value='';$('classification-category-create-status').textContent='';
  const details=$('classification-category-create');details.open=true;details.scrollIntoView({block:'center'});
}
function classificationKey(tx){return {account_id:tx.account_id,external_id:tx.external_id};}
function classificationAllocationType(tx){return Number(tx.amount)<0?'payment':'refund';}
function classificationLinkAmount(link){const value=Number(link.allocated_amount);return Number.isFinite(value)?Math.abs(value):null;}
function classificationDocumentAllocated(doc,type=null){return (doc.links||[]).filter(link=>!type||link.allocation_type===type).reduce((sum,link)=>sum+(classificationLinkAmount(link)||0),0);}
function classificationAllocationDefault(){if(!classificationSelected||!classificationSelectedDocument)return '';const type=classificationAllocationType(classificationSelected),documentAmount=Number(classificationSelectedDocument.amount),bookingAmount=Math.abs(Number(classificationSelected.amount)),voucherAmount=type==='payment'?(Number(classificationSelectedDocument.voucher_amount)||0):0;const documentRest=Number.isFinite(documentAmount)?Math.max(0,documentAmount-classificationDocumentAllocated(classificationSelectedDocument,type)-voucherAmount):Infinity;const bookingRest=Math.max(0,bookingAmount-(classificationSelected.links||[]).reduce((sum,link)=>sum+(classificationLinkAmount(link)||0),0));const value=Math.min(documentRest,bookingRest);return Number.isFinite(value)&&value>0?value.toFixed(2):'';}
function classificationUpdatePairActions(){
  const ready=Boolean(classificationSelected&&classificationSelectedDocument);
  const linked=ready&&(classificationSelected.links||[]).some(link=>link.id===classificationSelectedDocument.id);
  const documentLinked=Boolean(classificationSelectedDocument&&(classificationSelectedDocument.links||[]).length);
  $('classification-doc-not-invoice').disabled=!classificationSelectedDocument||documentLinked;
  $('classification-link').disabled=!ready||linked;$('classification-reject-link').disabled=!ready||linked;$('classification-allocate').disabled=!ready||linked;
  $('classification-link').textContent=linked?'Zuordnung ist bestätigt':'Zuordnung stimmt';
  $('classification-reject-link').textContent=linked?'Keine Ablehnung nötig':'Zuordnung stimmt nicht';
  $('classification-allocate').textContent=linked?'Keine weitere Zuordnung nötig':'Zuordnungsbetrag für dieses Paar übernehmen';
}
function classificationClearSelectedTransaction(){
  classificationSelected=null;
  $('classification-selected').textContent='Keine Buchung ausgewählt.';
  $('documents-transaction').textContent='Keine Buchung ausgewählt. Wähle im Tab Kategorien eine Buchung und „Belege zuordnen“.';
  $('document-selected-transaction').textContent='Noch keine Buchung ausgewählt.';
  $('classification-suggestions').replaceChildren();
  $('classification-rule-source').value='';
  $('classification-rule-own').replaceChildren();
  $('classification-single-save').disabled=true;
  $('classification-rule-save').disabled=true;
  $('classification-local-model').disabled=true;
  $('classification-allocation-amount').value='';
  $('classification-allocation-type').value='payment';
  classificationUpdatePairActions();
}
function classificationMatchAmount(match){for(const key of ['allocated_amount','suggested_allocation_amount','proposed_allocation_amount','allocation_amount','suggested_amount']){const value=Number(match[key]);if(Number.isFinite(value)&&value>0)return value.toFixed(2);}return '';}
function classificationMatchMeta(match,data=null){const type=match.allocation_type||(Number(match.amount)>=0?'refund':'payment'),rest=Number(data?.[type==='refund'?'remaining_refund':'remaining_payment']);return {type,proposed:classificationMatchAmount(match),rest:Number.isFinite(rest)&&rest>=0?rest:null};}
function classificationLabel(id){return classificationCatalog.find(c=>c.id===id)?.label||id;}
function classificationHierarchy(category,parent){const parentLabel=classificationCatalogParents.find(item=>item.id===parent)?.label||parent;return `${parent?parentLabel+' > ':''}${classificationLabel(category)}`;}
function classificationReason(reason){return ({exact_counterparty_and_direction:'gleiche Gegenpartei und Zahlungsrichtung',local_rule_exact_counterparty_and_direction:'lokale Regel: gleiche Gegenpartei und Zahlungsrichtung',local_model:'lokales Modell'})[reason]||String(reason||'lokale Regel');}
function classificationConfidence(value){return ({high:'hoch',medium:'mittel',low:'niedrig'})[value]||String(value||'unbekannt');}
function classificationPaymentStatus(value){return ({authorization:'autorisiert',processing:'in Bearbeitung',paid:'erfolgreich bezahlt',payment_plan:'Zahlungsplan'})[value]||String(value||'offen');}
function classificationProposal(box,tx,proposal){const p=document.createElement('p');if(String(proposal.provenance||'').startsWith('ollama:'))p.dataset.localModel='true';const rationale=proposal.rationale?` · Erläuterung: ${proposal.rationale.replace(/[.\s]+$/,'')}`:'';p.textContent=`Vorschlag: ${classificationHierarchy(proposal.category,proposal.parent_category)} · Begründung: ${classificationReason(proposal.reason)} · Sicherheit: ${classificationConfidence(proposal.confidence)}${rationale}. `;button(p,'Kategorie bestätigen',async()=>{await api('/api/classification-save',{...classificationKey(tx),category:proposal.category,revision:tx.revision});await classificationLoad();});box.append(p);}
function classificationRenderSummary(data){
  const counts=data.totals;$('classification-summary').replaceChildren();
  for(const [label,key] of [['Buchungen','total'],['Eigene Kategorie noch offen','unreviewed'],['Mailstatus offen (gesamt)','payment_status_open'],['Mit Beleg verknüpft','linked'],['Einnahmen EUR','income'],['Ausgaben EUR','outflow'],['Umbuchungsnetto EUR','transfer']]){const a=document.createElement('article'),s=document.createElement('span'),n=document.createElement('strong');s.textContent=label;n.textContent=['income','outflow','transfer'].includes(key)?eur(counts[key]):counts[key];a.append(s,n);$('classification-summary').append(a);}
  $('classification-bars').replaceChildren();
  for(const bucket of data.categories){const row=document.createElement('div'),label=document.createElement('span'),track=document.createElement('div'),bar=document.createElement('div');row.className='classification-bar-row';label.textContent=`${bucket.label}: ${bucket.count} Buchungen · ${bucket.transaction_type==='transfer'?'Umbuchungsnetto '+eur(bucket.transfer):bucket.transaction_type==='income'?eur(bucket.income):eur(bucket.outflow)}`;track.className='classification-bar-track';bar.className='classification-bar';bar.style.width=`${counts.total?100*bucket.count/counts.total:0}%`;track.append(bar);row.append(label,track);$('classification-bars').append(row);}
}
// Fixed semantic pictograms: never infer a merchant or category from a name.
function classificationPictogram(kind){
  const paths={income:'M12 4v16m-6-6 6 6 6-6',expense:'M12 20V4m-6 6 6-6 6 6',transfer:'M4 8h16m-4-4 4 4-4 4M20 16H4m4-4-4 4 4 4',category:'M4 4h7l9 9-7 7-9-9V4Zm4 4h.01',check:'m5 12 4 4 10-10',reset:'M4 11a8 8 0 1 1 2 7M4 4v7h7',details:'M8 4h8l4 4v12H4V4h4m0 7h8m-8 4h8',receipt:'M6 3h12v18l-3-2-3 2-3-2-3 2V3Zm3 5h6m-6 4h6'};
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');svg.setAttribute('focusable','false');svg.setAttribute('width','18');svg.setAttribute('height','18');svg.classList.add('classification-pictogram');
  const path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d',paths[kind]||paths.details);path.setAttribute('fill','none');path.setAttribute('stroke','currentColor');path.setAttribute('stroke-width','1.7');path.setAttribute('stroke-linecap','round');path.setAttribute('stroke-linejoin','round');svg.append(path);return svg;
}
function classificationActionPictogram(control,kind){control.classList.add('classification-icon-action');control.append(classificationPictogram(kind));return control;}
function classificationRenderRows(data){
  $('classification-rows').replaceChildren();
  for(const tx of data.rows){
    const tr=document.createElement('tr');const movement=tx.is_transfer?'transfer':Number(tx.amount)>0?'income':Number(tx.amount)<0?'expense':'neutral';tr.dataset.movement=movement;const dateCell=cell(tr,tx.date);dateCell.dataset.label='Datum';const accountCell=cell(tr,tx.account_label||accountDisplayById(tx.account_id));accountCell.dataset.label='Konto';const detail=cell(tr,'');detail.dataset.label='Gegenpartei und Zweck';const name=document.createElement('strong');name.textContent=tx.counterparty||'Keine Gegenpartei';const movementIcon=classificationPictogram(movement);movementIcon.classList.add('classification-booking-icon');name.prepend(movementIcon);const desc=document.createElement('p');desc.className=(tx.source_context_complete===false?'notice':'muted')+' classification-desktop-purpose';desc.textContent=tx.source_context_complete===false?'Quelldetails fehlen: Empfänger und Verwendungszweck sind nicht enthalten.':(tx.description||'');detail.append(name,desc);if(tx.source_context_complete!==false&&tx.description){const purpose=document.createElement('details');purpose.className='classification-mobile-purpose';const summary=document.createElement('summary');summary.textContent='Verwendungszweck ansehen';const text=document.createElement('p');text.textContent=tx.description;purpose.append(summary,text);detail.append(purpose);}const amountCell=cell(tr,amount(tx.amount),true);amountCell.dataset.label='Betrag';if(Number(tx.amount)>0){const sign=document.createElement('span');sign.className='classification-mobile-positive-sign';sign.textContent='+';amountCell.prepend(sign);}const movementLabel=document.createElement('span');movementLabel.className='classification-mobile-movement';movementLabel.textContent=(tx.is_transfer?'Umbuchung':Number(tx.amount)>0?'Einnahme':Number(tx.amount)<0?'Ausgabe':'Nullbetrag')+' · EUR';amountCell.append(movementLabel);
    const proposal=tx.category_proposal;const detected=cell(tr,'');detected.dataset.label='Erkannte Kategorie';
    if(proposal){const label=document.createElement('strong');label.textContent=classificationHierarchy(proposal.category,proposal.parent_category);detected.append(label);const meta=document.createElement('p');meta.className=proposal.confidence==='low'?'notice':'muted';const source=proposal.provenance==='local_rule_exact_counterparty_and_direction'?'Vorschlag aus Deiner Regel':proposal.provenance==='local_merchant_family_rule'?'Vorschlag aus Händlerregel':String(proposal.provenance||'').startsWith('ollama:')?'Vorschlag des lokalen Modells':'Bestätigt';meta.textContent=tx.confirmed||tx.is_transfer?'Bestätigt':`${source} · Sicherheit ${classificationConfidence(proposal.confidence)}`;detected.append(meta);if(tx.transfer_display){const path=document.createElement('p');path.className='muted';path.textContent=tx.transfer_display;detected.append(path);}}else detected.textContent='Noch kein eigener Vorschlag';
    const exactProposal=proposal?.provenance==='local_rule_exact_counterparty_and_direction';const own=cell(tr,'');own.dataset.label='Kategorie';let select=null;const currentCategory=document.createElement('p');currentCategory.className='classification-mobile-current-category';currentCategory.textContent=tx.is_transfer?(tx.transfer_display||'Bestätigte Umbuchung'):tx.category?`Kategorie: ${classificationHierarchy(tx.category,proposal?.parent_category)}`:proposal?`Vorschlag: ${classificationHierarchy(proposal.category,proposal.parent_category)} · ${classificationConfidence(proposal.confidence)}`:'Noch keine eigene Kategorie';own.append(currentCategory);
    const status=cell(tr,tx.is_transfer?'Bestätigte Umbuchung':tx.confirmed?'Geprüft':'Kategorie offen');status.dataset.label='Status und Belege';if(tx.links.length){const p=document.createElement('p');p.textContent=tx.links.map(d=>`${d.evidence_type==='order_confirmation'?'Bestellbestätigung':d.kind==='invoice'?'Rechnung':'Vertrag'}: ${d.title} · ${d.allocation_type==='evidence'?'Zahlungsnachweis':d.allocation_type==='refund'?'Erstattung':'Zahlung'}${classificationLinkAmount(d)!==null?' · '+eur(classificationLinkAmount(d)):' · Vollbetrag'}`).join(' · ');status.append(p);}for(const event of tx.payment_statuses||[]){const warning=event.match_status==='conflict'?'Zuordnung inzwischen mehrdeutig':event.match_status==='stale'?'passende Buchung nicht mehr im Abgleich':'';const p=document.createElement('p');p.className=warning?'notice':'muted';p.textContent=`${event.provider==='paypal'?'PayPal':'Klarna'} · ${classificationPaymentStatus(event.event_status)} · ${event.event_date}${event.amount?' · '+eur(event.amount):''}${warning?' · '+warning:''}`;status.append(p);}
    const actions=cell(tr,'');actions.dataset.label='Aktionen';if(!tx.is_transfer){let confirm=null;const updateConfirm=()=>{const value=select?.value||'';const changed=value&&value!=='__create__'&&(!tx.confirmed||value!==tx.category);if(confirm){confirm.disabled=!changed;confirm.title=changed?'Ausgewählte Kategorie bestätigen':tx.confirmed?'Eine andere Kategorie auswählen, um die Zuordnung zu ändern':'Zuerst links eine eigene Kategorie auswählen';}};const openCategoryPicker=()=>{if(select)return;select=document.createElement('select');select.setAttribute('aria-label','Eigene Kategorie für '+(tx.counterparty||tx.external_id));classificationCategoryOptions(select,tx.direction,tx.category||(exactProposal?proposal.category:''));own.replaceChildren(select);select.addEventListener('change',()=>run(async()=>{if(select.value==='__create__'){select.value='';updateConfirm();await classificationSelect(tx);classificationPrepareCategoryCreator(tx);return;}updateConfirm();status.firstChild.textContent=tx.confirmed?(select.value===tx.category?'Geprüft':'Geprüft · Änderung noch nicht gespeichert'):(select.value?'Auswahl noch nicht bestätigt':'Kategorie auswählen, dann bestätigen');}));updateConfirm();select.focus();};const choose=button(own,exactProposal?'Regelvorschlag prüfen':'Kategorie wählen',openCategoryPicker);classificationActionPictogram(choose,'category');choose.title='Kategorien erst bei Bedarf laden';confirm=button(actions,tx.confirmed?'Kategorie ändern und bestätigen':'Als geprüft bestätigen',async()=>{if(!select)openCategoryPicker();if(!select.value)return;confirm.disabled=true;try{const result=await api('/api/classification-save',{...classificationKey(tx),category:select.value,revision:tx.revision});tx.category=result.classification.category;tx.confirmed=true;tx.revision=result.classification.revision;status.firstChild.textContent='Geprüft';classificationMessage($('classification-unreviewed').checked?'Kategorie bestätigt; die Buchung wurde aus der offenen Arbeitsliste entfernt.':'Kategorie bestätigt; die Buchung ist jetzt geprüft.');await classificationLoad();}catch(error){updateConfirm();throw error;}});classificationActionPictogram(confirm,'check');updateConfirm();if(tx.confirmed)classificationActionPictogram(button(actions,'Prüfung zurücksetzen',async()=>{await api('/api/classification-reset',{...classificationKey(tx),revision:tx.revision,confirmed:true});await classificationLoad();}),'reset');}
    classificationActionPictogram(button(actions,'Buchung auswählen',()=>classificationSelect(tx)),'details');if(!tx.is_transfer)classificationActionPictogram(button(actions,'Belege zuordnen',async()=>{await classificationSelect(tx);cockpitNavigate('documents');}),'receipt');$('classification-rows').append(tr);
  }
  $('classification-page').textContent=`Seite ${data.page+1} / ${Math.max(1,data.pages)} · ${data.total} Buchungen`;$('classification-prev').disabled=data.page===0;$('classification-next').disabled=data.page+1>=data.pages;
}
async function classificationLoad(){
  const epoch=++classificationLoadEpoch;
  if($('classification-from').value && $('classification-to').value && $('classification-from').value>$('classification-to').value)throw new Error('Das Von-Datum darf nicht nach dem Bis-Datum liegen.');
  const select=$('classification-account'),chosen=select.value;select.replaceChildren(new Option('Alle Konten',''));for(const account of state.accounts)select.append(new Option(`${accountDisplay(account)} · ${account.institution}`,account.id));select.value=chosen;
  const request={page:classificationPage,query:$('classification-query').value,order:$('classification-order').value};if($('classification-from').value)request.date_from=$('classification-from').value;if($('classification-to').value)request.date_to=$('classification-to').value;if(chosen)request.account_id=chosen;if($('classification-unreviewed').checked)request.reviewed=false;
  const columnFilters=classificationColumnFilters();if(Object.keys(columnFilters).length)request.column_filters=columnFilters;if(classificationSortColumn){request.order_column=classificationSortColumn;request.order_direction=classificationSortDirection;}
  const [catalog,data]=await Promise.all([api('/api/classification-catalog',{}),api('/api/classification-list',request)]);if(epoch!==classificationLoadEpoch)return;if(classificationPage>0&&!data.rows.length){classificationPage=Math.max(0,data.pages-1);return classificationLoad();}classificationCatalogParents=catalog.parents||[];classificationCatalog=catalog.categories||data.categories||[];classificationRenderRows(data);const {page,order,reviewed,column_filters,order_column,order_direction,...filters}=request;const summary=await api('/api/classification-aggregates',filters);if(epoch!==classificationLoadEpoch)return;classificationRenderSummary(summary);$('classification-scope').textContent=`Auswertung: ${request.date_from||'Erster Importtag'} bis ${request.date_to||'Letzter Importtag'} · ${chosen||'Alle Konten'}${request.reviewed===false?' · Tabelle: geprüfte Buchungen ausgeblendet':''}${request.query?' · Suche: '+request.query:''}${Object.keys(columnFilters).length?' · Spaltenfilter aktiv':''}. Kennzahlen umfassen den gesamten gewählten Zeitraum. Nur vorhandene Buchungen; Umbuchungen sind keine Einnahmen/Ausgaben. Sortierung: ${classificationSortColumn?classificationColumns[classificationSortColumn]+' '+(classificationSortDirection==='asc'?'aufsteigend':'absteigend'):request.order==='amount_desc'?'absoluter Betrag absteigend':'Datum absteigend'}.`;
  if(classificationSelected){const fresh=data.rows.find(t=>t.account_id===classificationSelected.account_id&&t.external_id===classificationSelected.external_id);if(fresh)await classificationSelect(fresh);else classificationClearSelectedTransaction();}
}
async function classificationSelect(tx,match=null){
  classificationSelected=tx;$('classification-selected').textContent=`${tx.date} · ${tx.account_label||accountDisplayById(tx.account_id)} · ${tx.counterparty||'—'} · ${eur(tx.amount)} · ${tx.description||''}`;
  if(classificationSelectedDocument){const meta=match&&classificationMatchMeta(match);$('classification-allocation-type').value=meta?.type||classificationAllocationType(tx);$('classification-allocation-amount').value=meta?.proposed||classificationAllocationDefault();}
  $('documents-transaction').textContent='Ausgewählte Buchung: '+$('classification-selected').textContent;
  $('document-selected-transaction').textContent=`${tx.date} · ${tx.account_label||accountDisplayById(tx.account_id)} · ${tx.counterparty||'Keine Gegenpartei'} · ${tx.description||'Kein Verwendungszweck'} · ${eur(tx.amount)}`;
  $('classification-rule-source').value=tx.counterparty||'';classificationCategoryOptions($('classification-rule-own'),tx.direction,tx.category||'');$('classification-rule-save').disabled=tx.is_transfer;$('classification-single-save').disabled=tx.is_transfer;$('classification-local-model').disabled=tx.is_transfer;$('classification-category-type').value=tx.is_transfer?'Umbuchung':tx.direction==='income'?'Einnahme':'Ausgabe';classificationUpdatePairActions();
  const box=$('classification-suggestions');box.replaceChildren();
  if(tx.is_transfer&&!tx.transfer_id){
    const correction=await api('/api/transfer-correction-get',classificationKey(tx));
    if(correction.pair){
      const p=document.createElement('p');p.textContent=`Additive Umbuchungskorrektur #${correction.pair.id}. Die Originalbuchungen bleiben unverändert. `;
      button(p,'Umbuchungskorrektur zurücknehmen',async()=>{await api('/api/transfer-correction-revoke',{id:correction.pair.id,revision:correction.pair.revision,confirmed:true});await classificationLoad();classificationMessage('Umbuchungskorrektur zurückgenommen; Originalbuchungen blieben unverändert.');});box.append(p);
    }
  }
  const proposals=await api('/api/classification-suggestions',classificationKey(tx));
  for(const proposal of proposals.suggestions)classificationProposal(box,tx,proposal);
  const matches=await api('/api/classification-matches',{...classificationKey(tx),max_days:30});
  for(const match of matches.suggestions){const p=document.createElement('p');p.textContent=`Belegvorschlag #${match.document_id}: ${match.vendor} · ${match.title}. Betrag und Datum passen; kein Nachweis der Zusammengehörigkeit. `;box.append(p);}
  for(const group of matches.suggestion_groups||[]){const section=document.createElement('section');section.className='classification-match-group';const title=document.createElement('h5');title.textContent=`Sammelvorschlag: ${group.documents.length} Belege ergeben zusammen ${eur(group.allocated_amount)}`;section.append(title);const explanation=document.createElement('p');explanation.className='muted';explanation.textContent='Empfänger, Zeitraum und Restsumme passen. Jeder Beleg muss einzeln geprüft und zugeordnet werden.';section.append(explanation);for(const doc of group.documents){const p=document.createElement('p');p.textContent=`#${doc.document_id} · ${doc.document_date} · ${doc.vendor} · ${doc.title} · Anteil ${eur(doc.remaining_amount)} `;button(p,'Beleg prüfen',()=>classificationOpenComparison(doc.document_id,tx));section.append(p);}box.append(section);}
  for(const link of tx.links){const p=document.createElement('p');p.textContent=`Zugeordnet: #${link.id} ${link.title} · ${link.allocation_type==='evidence'?'Zahlungsnachweis':link.allocation_type==='refund'?'Erstattung':'Zahlung'}${classificationLinkAmount(link)!==null?' · '+eur(classificationLinkAmount(link)):' · Vollbetrag'} `;button(p,'Diese Zuordnung lösen',async()=>{await api('/api/classification-unlink',{...classificationKey(tx),document_id:link.id,confirmed:true});await classificationLoad();await classificationLoadDocuments();});box.append(p);}
  if(!box.childNodes.length)box.textContent='Keine Regeln, Belegvorschläge oder Verknüpfungen vorhanden.';
}
async function classificationLoadDocuments(){
  await documentCoverageLoad();
  const request={page:classificationDocPage,query:$('classification-doc-search').value};
  const year=$('document-year').value,status=$('document-status').value,linked=$('document-linked').value;
  if(year)request.year=year==='unknown'?year:Number(year);if(status)request.status=status;if(linked)request.linked=linked==='yes';
  const data=await api('/api/classification-documents',request);
  $('document-scope').textContent=`Gefiltert: ${year==='unknown'?'Datum fehlt':year||'Alle Jahre'} · ${status==='unreviewed'?'ungeprüft':status==='confirmed'?'bestätigt':'alle Prüfstände'} · ${linked==='yes'?'mit Buchungsverknüpfung':linked==='no'?'ohne Buchungsverknüpfung':'alle Zuordnungen'} · ${data.total} Dokumente. Suche: ${request.query||'keine'}.`;
  const panel=$('document-comparison-panel'),home=$('document-comparison-home');home.append(panel);
  const box=$('classification-documents');box.replaceChildren();
  for(const doc of data.documents){const item=document.createElement('div');item.className='classification-document-item';item.dataset.documentId=String(doc.id);const row=document.createElement('div');row.className='classification-document-row';const label=document.createElement('span');const paid=classificationDocumentAllocated(doc,'payment'),refunded=classificationDocumentAllocated(doc,'refund'),voucher=Number(doc.voucher_amount)||0,complete=doc.status==='confirmed'&&(doc.kind==='contract'||doc.amount!==null&&paid+voucher>=Number(doc.amount)),type=doc.evidence_type==='order_confirmation'?'Bestellbestätigung':doc.kind==='invoice'?'Rechnung':'Vertrag';label.textContent=`#${doc.id} · ${type} · ${doc.document_date||'Datum offen'} · ${doc.vendor} · ${doc.title} · ${doc.amount===null?'Betrag offen':eur(doc.amount)} · ${doc.status==='confirmed'?'bestätigt':'ungeprüft'} · ${doc.links.length} Zuordnungen${paid>0?' · '+eur(paid)+' bezahlt':''}${voucher>0?' · Gutschein '+eur(voucher):''}${refunded>0?' · '+eur(refunded)+' erstattet':''}`;row.append(label);const open=button(row,complete?'Details ansehen':'Beleg prüfen',async()=>{classificationPlaceDocumentPanel(doc.id,true);await classificationSelectDocument(doc);});open.dataset.defaultLabel=complete?'Details ansehen':'Beleg prüfen';open.setAttribute('aria-expanded','false');open.setAttribute('aria-controls','document-comparison-panel');const slot=document.createElement('div');slot.className='classification-document-review-slot';item.append(row,slot);box.append(item);}
  if(classificationSelectedDocument)classificationPlaceDocumentPanel(classificationSelectedDocument.id,false);
  $('classification-doc-page').textContent=`Seite ${data.page+1} / ${Math.max(1,data.pages)} · ${data.total} Dokumente`;$('classification-doc-prev').disabled=data.page===0;$('classification-doc-next').disabled=data.page+1>=data.pages;
}
function classificationPlaceDocumentPanel(documentId=null,scroll=false){
  const panel=$('document-comparison-panel'),home=$('document-comparison-home');let target=home,selected=null;
  for(const item of $('classification-documents').querySelectorAll('.classification-document-item')){const active=documentId!==null&&item.dataset.documentId===String(documentId);item.classList.toggle('selected',active);const control=item.querySelector('button');if(control){control.textContent=active?'Details geöffnet':control.dataset.defaultLabel||'Beleg prüfen';control.setAttribute('aria-expanded',String(active));}if(active){selected=item;target=item.querySelector('.classification-document-review-slot');}}
  target.append(panel);panel.hidden=false;if(scroll)(selected||panel).scrollIntoView({block:'nearest'});
}
function classificationClearDocument(){classificationDocumentSelectionEpoch++;classificationDocumentMatchPage=0;classificationSelectedDocument=null;classificationPlaceDocumentPanel(null,true);for(const id of ['title','vendor','date','total','source'])$('classification-doc-'+id).value='';$('classification-doc-source').readOnly=false;$('classification-doc-kind').disabled=false;$('classification-doc-warnings').textContent='Neuer Entwurf: erst speichern, dann anhand der Quelle bestätigen.';$('document-selected-document').textContent='Noch kein Beleg ausgewählt.';$('classification-doc-source-text').textContent='';$('classification-document-matches').replaceChildren();$('classification-doc-save').textContent='Dokumententwurf speichern';classificationUpdatePairActions();}
async function classificationSelectDocument(doc){
  const selection=++classificationDocumentSelectionEpoch;
  classificationDocumentMatchPage=0;
  classificationSelectedDocument=doc;for(const [id,key] of [['title','title'],['vendor','vendor'],['date','document_date'],['total','amount'],['source','source_reference']])$('classification-doc-'+id).value=doc[key]??'';$('classification-allocation-type').value=classificationAllocationType(classificationSelected||{amount:-1});$('classification-allocation-amount').value=classificationAllocationDefault();
  $('document-selected-document').textContent=`Beleg #${doc.id} · ${doc.vendor} · ${doc.title} · ${doc.document_date||'Datum offen'} · ${doc.amount===null?'Betrag offen':eur(doc.amount)}${Number(doc.voucher_amount)>0?' · Gutschein '+eur(doc.voucher_amount):''}`;
  classificationUpdatePairActions();
  $('classification-doc-source').readOnly=true;$('classification-doc-kind').value=doc.kind;$('classification-doc-kind').disabled=true;$('classification-doc-save').textContent='Belegdaten bestätigen (ohne Buchungszuordnung)';$('classification-doc-warnings').textContent=`#${doc.id}: ${doc.status==='confirmed'?'Bestätigt':'Ungeprüft'}${Number(doc.voucher_amount)>0?` · Gutschein separat bestätigt: ${eur(doc.voucher_amount)}`:''}. Hinweise: ${doc.warnings.length?doc.warnings.join(', '):'Keine Extraktionswarnungen; trotzdem anhand der Quelle prüfen.'}`;
  $('classification-doc-source-text').textContent='Quelle wird geladen …';$('classification-document-matches').textContent='Passende Buchungen werden gesucht …';
  try{const source=await api('/api/classification-document-source',{id:doc.id});if(selection===classificationDocumentSelectionEpoch)$('classification-doc-source-text').textContent=source.text;}catch(error){if(selection===classificationDocumentSelectionEpoch)$('classification-doc-source-text').textContent='Kein lokal hinterlegter Quelltext. Bestätigung nur anhand des vorhandenen Originalbelegs.';}
  await classificationLoadDocumentMatches(doc,selection,0);
}
async function classificationOpenTransaction(tx){
  cockpitNavigate('classification');
  await classificationSelect(tx);
  $('classification-selected').scrollIntoView({block:'center'});
}
async function classificationOpenComparison(documentId,tx=null){
  const result=await api('/api/classification-document-get',{id:Number(documentId)});
  cockpitNavigate('documents');
  if(tx)await classificationSelect(tx);else $('document-selected-transaction').textContent='Noch keine konkrete Buchung ausgewählt.';
  await classificationSelectDocument(result.document);
  $('document-comparison-panel').scrollIntoView({block:'start'});
}
async function classificationLoadDocumentMatches(doc,selection,page){try{const matches=await api('/api/classification-document-matches',{document_id:doc.id,max_days:45,page});if(selection===classificationDocumentSelectionEpoch){classificationDocumentMatchPage=page;classificationRenderDocumentMatches(matches);}}catch(error){if(selection===classificationDocumentSelectionEpoch)$('classification-document-matches').textContent='Suche nach passenden Buchungen fehlgeschlagen.';}}
function classificationRenderDocumentMatches(data){
  const box=$('classification-document-matches');box.replaceChildren();
  const heading=document.createElement('h4');heading.textContent='Passende Buchungen';box.append(heading);
  const note=document.createElement('p');note.textContent=`Betrag und Datum im 45-Tage-Fenster sind nur ein Vorschlag, kein Nachweis der Zusammengehörigkeit. Offener Zahlungsrest ${eur(data.remaining_payment||0)} · offener Erstattungsrest ${eur(data.remaining_refund||0)}.`;box.append(note);
  if(data.linked_transactions.length){const linked=document.createElement('h5');linked.textContent='Bereits verknüpfte Buchungen';box.append(linked);for(const tx of data.linked_transactions){const row=document.createElement('p'),allocation=(tx.links||[]).find(link=>link.id===classificationSelectedDocument?.id);row.textContent=`${tx.date} · ${tx.account_label||accountDisplayById(tx.account_id)} · ${tx.counterparty||'Keine Gegenpartei'} · ${tx.description||'Kein Verwendungszweck'} · ${eur(tx.amount)}${allocation?' · '+(allocation.allocation_type==='refund'?'Erstattung':'Zahlung')+' · '+eur(classificationLinkAmount(allocation)):''} `;button(row,'Buchung auswählen',()=>classificationSelect(tx));box.append(row);}}
  for(const group of data.suggestion_groups||[]){const section=document.createElement('section');section.className='classification-match-group';const title=document.createElement('h5');title.textContent=`Gemeinsamer ${group.allocation_type==='refund'?'Erstattungs':'Zahlungs'}vorschlag · exakt ${eur(group.allocated_amount)}`;section.append(title);const explanation=document.createElement('p');explanation.className='muted';explanation.textContent='Erst die Summe dieser Buchungen deckt den offenen Rest. Jede Zuordnung muss einzeln bestätigt werden.';section.append(explanation);for(const tx of group.transactions){const row=document.createElement('p');row.textContent=`${tx.date} · ${tx.account_label||accountDisplayById(tx.account_id)} · ${tx.counterparty||'Keine Gegenpartei'} · ${tx.description||'Kein Verwendungszweck'} · Anteil ${eur(tx.allocated_amount)} `;button(row,'Buchung auswählen',()=>classificationSelect(tx,tx));section.append(row);}box.append(section);}
  if(!data.suggestions.length&&!(data.suggestion_groups||[]).length){const empty=document.createElement('p');empty.textContent=data.match_status==='document_not_eligible'?'Bitte erst Belegdaten prüfen oder bestätigen; für diesen Stand gibt es keine Buchungsvorschläge.':'Keine einzelne Buchung oder exakte Zahlungsgruppe deckt einen offenen Restbetrag im Zeitfenster.';box.append(empty);return;}
  for(const tx of data.suggestions){const row=document.createElement('p'),meta=classificationMatchMeta(tx,data);row.textContent=`${tx.date} · ${tx.account_label||accountDisplayById(tx.account_id)} · ${tx.counterparty||'Keine Gegenpartei'} · ${tx.description||'Kein Verwendungszweck'} · ${eur(tx.amount)} · ${meta.type==='refund'?'Erstattung':'Zahlung'} · Vorschlag ${eur(meta.proposed)} `;button(row,'Buchung auswählen',()=>classificationSelect(tx,tx));box.append(row);}
  if(data.pages>1){const controls=document.createElement('p');button(controls,'Vorige Vorschlagsseite',()=>classificationLoadDocumentMatches(classificationSelectedDocument,classificationDocumentSelectionEpoch,classificationDocumentMatchPage-1)).disabled=data.page===0;const page=document.createElement('span');page.textContent=` Seite ${data.page+1} von ${data.pages} · ${data.total} Einzelvorschläge `;controls.append(page);button(controls,'Nächste Vorschlagsseite',()=>classificationLoadDocumentMatches(classificationSelectedDocument,classificationDocumentSelectionEpoch,classificationDocumentMatchPage+1)).disabled=data.page+1>=data.pages;box.append(controls);}
}
async function classificationSaveDocument(){
  const value=$('classification-doc-total').value.trim().replace(',','.');const common={vendor:$('classification-doc-vendor').value,title:$('classification-doc-title').value,document_date:$('classification-doc-date').value||null,amount:value||null,currency:value?'EUR':null,source_reference:$('classification-doc-source').value};let result;
  if(classificationSelectedDocument)result=await api('/api/classification-document-save',{...common,id:classificationSelectedDocument.id,revision:classificationSelectedDocument.revision,confirmed:true,status:'confirmed'});
  else result=await api('/api/classification-document-create',{...common,kind:$('classification-doc-kind').value,warnings:[],status:'unreviewed'});
  await classificationSelectDocument(result.document);await classificationLoadDocuments();classificationMessage(result.document.status==='confirmed'?'Belegdaten bestätigt; die Buchungszuordnung ist noch nicht bestätigt.':'Dokumententwurf gespeichert; noch nicht bestätigt.');
}
async function classificationDismissDocument(){
  if(!classificationSelectedDocument)throw new Error('Bitte zuerst ein Dokument auswählen.');
  const document=classificationSelectedDocument;
  await api('/api/classification-document-dismiss',{id:document.id,revision:document.revision,not_invoice:true});
  classificationClearDocument();
  await classificationLoadDocuments();
  classificationMessage('Als „keine Rechnung“ eingeordnet. Die Quelle bleibt im lokalen Archiv, der Eintrag ist aus der Prüfliste entfernt.');
}
async function classificationLink(){if(!classificationSelected||!classificationSelectedDocument)throw new Error('Bitte zuerst Buchung und Beleg auswählen.');const selected=classificationSelected;await api('/api/classification-link',{...classificationKey(selected),document_id:classificationSelectedDocument.id,confirmed:true});const review=await api('/api/classification-model-review',classificationKey(selected));await classificationLoad();await classificationLoadDocuments();await classificationLoadDocumentMatches(classificationSelectedDocument,classificationDocumentSelectionEpoch,0);classificationMessage(review.reviewed?'Zuordnung gespeichert und Kategorie-Vorschlag aus dem Beleg erzeugt.':'Vollzuordnung gespeichert. Es wurde keine zusätzliche Buchung erzeugt.');}
async function classificationRejectLink(){if(!classificationSelected||!classificationSelectedDocument)throw new Error('Bitte zuerst Buchung und Beleg auswählen.');await api('/api/classification-link-reject',{...classificationKey(classificationSelected),document_id:classificationSelectedDocument.id,rejected:true});const document=classificationSelectedDocument;classificationClearSelectedTransaction();await classificationLoadDocumentMatches(document,classificationDocumentSelectionEpoch,0);classificationMessage('Zuordnung abgelehnt. Dieses Paar wird nicht erneut vorgeschlagen.');}
async function classificationAllocate(){if(!classificationSelected||!classificationSelectedDocument)throw new Error('Bitte zuerst Buchung und Beleg auswählen.');const selected=classificationSelected,value=$('classification-allocation-amount').value.trim().replace(',','.');const amount=Number(value);if(!Number.isFinite(amount)||amount<=0)throw new Error('Bitte einen positiven Zuordnungsbetrag eingeben.');await api('/api/classification-allocate',{...classificationKey(selected),document_id:classificationSelectedDocument.id,allocated_amount:value,allocation_type:$('classification-allocation-type').value,confirmed:true});const review=await api('/api/classification-model-review',classificationKey(selected));await classificationLoad();await classificationLoadDocuments();await classificationLoadDocumentMatches(classificationSelectedDocument,classificationDocumentSelectionEpoch,0);classificationMessage(review.reviewed?'Teilzuordnung gespeichert und Kategorie-Vorschlag aus dem Beleg erzeugt.':'Zuordnungsbetrag gespeichert. Es wurde keine zusätzliche Buchung erzeugt.');}
document.addEventListener('DOMContentLoaded',()=>{
  if(!$('classification'))return;
  const compactViewport=window.matchMedia?.('(max-width:640px)').matches??false;
  $('classification-range-controls').open=!compactViewport;
  document.querySelector('.classification-scope-details').open=!compactViewport;
  $('classification-summary-details').open=!compactViewport;
  for(const head of document.querySelectorAll('#classification-table th[data-column]'))head.querySelector('button').addEventListener('click',()=>run(async()=>{const column=head.dataset.column;classificationSortDirection=classificationSortColumn===column&&classificationSortDirection==='asc'?'desc':'asc';classificationSortColumn=column;classificationPage=0;classificationUpdateSortButtons();await classificationLoad();}));
  for(const input of document.querySelectorAll('#classification-table [data-column-filter]')){
    input.addEventListener('change',()=>run(async()=>{classificationSyncMobileControls();classificationPage=0;await classificationLoad();}));
    input.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();input.blur();}});
  }
  $('classification-order').addEventListener('change',()=>run(async()=>{classificationSortColumn=null;classificationPage=0;classificationUpdateSortButtons();await classificationLoad();}));
  classificationUpdateSortButtons();
  $('classification-load').addEventListener('click',()=>run(async()=>{classificationPage=0;await classificationLoad();}));
  $('classification-unreviewed').addEventListener('change',()=>run(async()=>{classificationPage=0;await classificationLoad();}));
  classificationCreateMobileControls();
  for(const [id,step] of [['prev',-1],['next',1]])$('classification-'+id).addEventListener('click',()=>run(async()=>{classificationPage+=step;await classificationLoad();}));
  $('classification-single-save').addEventListener('click',()=>run(async()=>{if(!classificationSelected||classificationSelected.is_transfer)throw new Error('Zuerst eine Einnahme oder Ausgabe auswählen.');const category=$('classification-rule-own').value;if(!category)throw new Error('Bitte eine eigene Kategorie wählen.');const result=await api('/api/classification-save',{...classificationKey(classificationSelected),category,revision:classificationSelected.revision});classificationSelected={...classificationSelected,category,confirmed:true,revision:result.classification.revision};await classificationSelect(classificationSelected);classificationMessage('Kategorie für genau diese Buchung bestätigt.');}));
  $('classification-rule-save').addEventListener('click',()=>run(async()=>{if(!classificationSelected||classificationSelected.is_transfer)throw new Error('Zuerst eine Einnahme oder Ausgabe auswählen.');await api('/api/classification-rule',{counterparty:$('classification-rule-source').value,direction:classificationSelected.direction,category:$('classification-rule-own').value});await classificationSelect(classificationSelected);classificationMessage('Regel gespeichert. Sie erzeugt Vorschläge und ändert keine Buchung automatisch.');}));
  $('classification-rule-own').addEventListener('change',()=>{if($('classification-rule-own').value==='__create__'&&classificationSelected){$('classification-rule-own').value='';classificationPrepareCategoryCreator(classificationSelected);}});
  $('classification-category-create-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{if(!classificationSelected||classificationSelected.is_transfer)throw new Error('Zuerst eine Einnahme oder Ausgabe auswählen.');const selected=classificationSelected;const result=await api('/api/classification-category-create',{parent_label:$('classification-category-parent').value,label:$('classification-category-child').value,transaction_type:selected.direction});classificationCatalogParents=result.parents;classificationCatalog=result.categories;await api('/api/classification-save',{...classificationKey(selected),category:result.category.id,revision:selected.revision});$('classification-category-create').open=false;$('classification-category-create-form').reset();$('classification-category-create-status').textContent='';classificationClearSelectedTransaction();await classificationLoad();classificationMessage(`Kategorie ${result.parent.label} > ${result.category.label} angelegt, zugeordnet und bestätigt.`);$('classification-status').scrollIntoView({block:'nearest'});});});
  $('classification-local-model').addEventListener('click',()=>run(async()=>{if(!classificationSelected||classificationSelected.is_transfer)throw new Error('Zuerst eine Einnahme oder Ausgabe auswählen.');const selected=classificationSelected,control=$('classification-local-model'),box=$('classification-suggestions');control.disabled=true;control.textContent='Lokales Modell arbeitet …';try{const result=await api('/api/classification-local-suggestion',classificationKey(selected));if(!classificationSelected||classificationSelected.account_id!==selected.account_id||classificationSelected.external_id!==selected.external_id)return;for(const old of box.querySelectorAll('[data-local-model]'))old.remove();if(result.status==='ok'){if(box.textContent.trim()==='Keine Regeln, Belegvorschläge oder Verknüpfungen vorhanden.')box.replaceChildren();for(const proposal of result.suggestions)classificationProposal(box,selected,proposal);classificationMessage(`Lokaler Vorschlag von ${result.model} geladen; noch nicht bestätigt.`);}else classificationMessage(result.message||'Kein lokaler Modellvorschlag verfügbar.',true);}finally{control.textContent='Lokales Modell erneut fragen';control.disabled=!classificationSelected||classificationSelected.is_transfer;}}));
  $('classification-doc-load').addEventListener('click',()=>run(async()=>{classificationDocPage=0;await classificationLoadDocuments();}));
  for(const [id,step] of [['prev',-1],['next',1]])$('classification-doc-'+id).addEventListener('click',()=>run(async()=>{classificationDocPage+=step;await classificationLoadDocuments();}));
  for(const id of ['document-year','document-status','document-linked'])$(id).addEventListener('change',()=>run(async()=>{classificationDocPage=0;await classificationLoadDocuments();}));
  $('document-current').addEventListener('click',()=>run(async()=>{const year=String(new Date().getFullYear());if(![...$('document-year').options].some(o=>o.value===year))$('document-year').append(new Option(year,year));$('document-year').value=year;$('document-status').value='unreviewed';$('document-linked').value='';$('classification-doc-search').value='';classificationDocPage=0;await classificationLoadDocuments();}));
  $('classification-doc-new').addEventListener('click',classificationClearDocument);$('classification-doc-save').addEventListener('click',()=>run(classificationSaveDocument));$('classification-doc-not-invoice').addEventListener('click',()=>run(classificationDismissDocument));$('classification-link').addEventListener('click',()=>run(classificationLink));$('classification-reject-link').addEventListener('click',()=>run(classificationRejectLink));$('classification-allocate').addEventListener('click',()=>run(classificationAllocate));
});

async function documentCoverageLoad(){
  const data=await api('/api/classification-document-coverage',{});
  const box=$('document-coverage');box.replaceChildren();
  for(const [label,key] of [['Dokumente insgesamt','total'],['Noch ungeprüft','unreviewed'],['Bestätigt','confirmed'],['Mit Buchungsverknüpfung','linked'],['Ohne Buchungsverknüpfung','unlinked'],['Ohne Dokumentdatum','undated']]){const a=document.createElement('article'),span=document.createElement('span'),strong=document.createElement('strong');span.textContent=label;strong.textContent=data[key];a.append(span,strong);box.append(a);}
  const select=$('document-year'),chosen=select.value;select.replaceChildren(new Option('Alle Jahre',''));
  const years=$('document-years');years.replaceChildren();
  for(const bucket of data.years){const key=bucket.year===null?'unknown':String(bucket.year),label=bucket.year===null?'Datum fehlt':String(bucket.year);select.append(new Option(label,key));const row=document.createElement('div'),text=document.createElement('span'),track=document.createElement('div'),bar=document.createElement('div');row.className='classification-bar-row';text.textContent=`${label}: ${bucket.count} Dokumente · ${bucket.confirmed} bestätigt · ${bucket.linked} verknüpft`;track.className='classification-bar-track';bar.className='classification-bar';bar.style.width=`${data.total?100*bucket.count/data.total:0}%`;track.append(bar);row.append(text,track);years.append(row);}
  if(chosen && ![...select.options].some(o=>o.value===chosen))select.append(new Option(chosen,chosen));select.value=chosen;
}
