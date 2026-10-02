'use strict';
let approvalItems=[];
let approvalVisible=40;
let approvalCatalog={parents:[],categories:[]};

const approvalTypeLabels={transfer_correction:'Umbuchungen',classification:'Kategorien',document:'Belege',budget_item:'Planung',intake_mapping:'Import und Konten'};
const approvalConfidenceLabels={high:'hohe Sicherheit',medium:'mittlere Sicherheit',low:'niedrige Sicherheit',unknown:'noch zu prüfen'};
function approvalConfidenceText(item){const label=approvalConfidenceLabels[item.confidence]||item.confidence;return item.model_name?`Modell meldet ${label}`:label;}

function approvalCategoryLabel(id){
  const leaf=approvalCatalog.categories.find(item=>item.id===id);
  if(!leaf)return id;
  const parent=approvalCatalog.parents.find(item=>item.id===leaf.parent_id);
  return `${parent?parent.label+' > ':''}${leaf.label}`;
}

function approvalGroups(items){
  const result=[],seen=new Set();
  for(const item of items){
    if(item.type==='monthly_review')continue;
    if(item.type!=='classification'||!item.group_key){result.push(item);continue;}
    if(seen.has(item.group_key))continue;
    seen.add(item.group_key);
    const members=items.filter(other=>other.group_key===item.group_key);
    const actionable=Boolean(item.group_direct_action);
    result.push({...item,_members:members,direct_action:item.group_direct_action||null,
      title:actionable?`${approvalCategoryLabel(item.direct_action?.payload?.category)} für ${item.group_counterparty||'diese Gegenpartei'} bestätigen`:`Kategorie für ${item.group_counterparty||'diese Gegenpartei'} festlegen`,
      detail:`${item.group_count} Buchungen · ${item.group_period_start} bis ${item.group_period_end} · ${eur(item.group_absolute_amount)} absolut`});
  }
  const order={transfer_correction:1,classification:2,document:3,budget_item:4,intake_mapping:5};
  return result.sort((a,b)=>(Number(Boolean(b.direct_action))-Number(Boolean(a.direct_action)))||((order[a.type]??9)-(order[b.type]??9))||((b.group_count||1)-(a.group_count||1))||a.id.localeCompare(b.id));
}

function approvalNeedsUserDecision(item){return item.type!=='monthly_review'&&(Boolean(item.direct_action)||['budget_item','intake_mapping'].includes(item.type));}

function approvalRenderSummary(items){
  const box=$('approvals-summary');box.replaceChildren();
  const grouped=approvalGroups(items);
  const priority=grouped.filter(approvalNeedsUserDecision).length;
  const values=[['Jetzt zu entscheiden',priority],['Arbeitsbestand',grouped.length-priority],['Umbuchungen',grouped.filter(item=>item.type==='transfer_correction').length],['Kategorie-Gruppen',grouped.filter(item=>item.type==='classification').length],['Belege',grouped.filter(item=>item.type==='document').length]];
  for(const [label,value] of values){const card=document.createElement('article'),span=document.createElement('span'),strong=document.createElement('strong');span.textContent=label;strong.textContent=value;card.append(span,strong);box.append(card);}
}

async function approvalRunAction(action,label,successMessage=''){
  if(!action)throw new Error('Für diesen Punkt ist eine Detailprüfung nötig.');
  await api(action.route,action.payload);
  await refresh();
  message(successMessage||`${label} gespeichert. Die Freigaben wurden neu geladen.`);
}

async function approvalConfirmDocumentPair(item,doc,tx){
  if(!item.direct_action)throw new Error('Die Belegdaten müssen vor der Zuordnung geändert oder ergänzt werden.');
  const payload={...item.direct_action.payload,account_id:tx.account_id,external_id:tx.external_id};
  await api(item.direct_action.route,payload);
  await refresh();
  message('Zuordnung bestätigt. Der erledigte Fall wurde aus der offenen Liste entfernt.');
}

async function approvalRejectDocumentPair(item,doc,tx){
  await api('/api/classification-link-reject',{account_id:tx.account_id,external_id:tx.external_id,document_id:doc.id,rejected:true});
  await refresh();
  message('Zuordnung abgelehnt. Dieses Paar wird nicht erneut vorgeschlagen.');
}

function approvalOpenType(type){
  $('approvals-filter').value=type;approvalVisible=40;cockpitNavigate('approvals');approvalRender();
}

function approvalTransaction(item){return {account_id:item.transaction_account,external_id:item.transaction_external_id,date:item.transaction_date,amount:item.transaction_amount,currency:item.transaction_currency,counterparty:item.transaction_counterparty,description:item.transaction_description,direction:Number(item.transaction_amount)<0?'expense':'income',is_transfer:false,category:null,source_category:null,confirmed:false,revision:Number(item.source_revision)||0,links:[]};}
function approvalAlternativeCategory(item,actions){
  const select=document.createElement('select');select.setAttribute('aria-label','Alternative Kategorie wählen');
  select.append(new Option('Alternative Kategorie wählen',''));
  const direction=Number(item.transaction_amount)<0?'expense':'income';
  for(const parent of approvalCatalog.parents.filter(entry=>entry.transaction_type===direction)){
    const group=document.createElement('optgroup');group.label=parent.label;
    for(const category of approvalCatalog.categories.filter(entry=>entry.transaction_type===direction&&entry.parent_id===parent.id))group.append(new Option(category.label,category.id));
    if(group.children.length)select.append(group);
  }
  select.addEventListener('change',()=>run(async()=>{
    if(!select.value)return;
    select.disabled=true;
    try{
      await approvalRunAction({...item.direct_action,payload:{...item.direct_action.payload,category:select.value}},'Alternative Kategorie');
    }catch(error){select.disabled=false;throw error;}
  }));
  actions.append(select);
}
function approvalCompareArticle(title,lines){const article=document.createElement('article'),heading=document.createElement('h5');heading.textContent=title;article.append(heading);for(const line of lines){const p=document.createElement('p');p.textContent=line;article.append(p);}return article;}
function approvalTransactionLine(tx){return `${tx.date} · ${tx.account_label||accountDisplayById(tx.account_id)} · ${tx.counterparty||'Keine Gegenpartei'} · ${tx.description||'Kein Verwendungszweck'} · ${eur(tx.amount)}`;}
function approvalSourceExcerpt(text,doc){
  const lines=String(text||'').split(/\r?\n/).map(line=>line.replace(/[\u200B-\u200F\u2060\uFEFF]/g,'').replace(/\s+/g,' ').trim()).filter(line=>line&&!/^<?https?:\/\//i.test(line));
  const terms=[doc.vendor,doc.title,doc.amount,'rechnung','bestellnr','summe','gesamt','zahlungsart'].flatMap(value=>String(value||'').toLowerCase().match(/[a-z0-9äöüß]{4,}/g)||[]);
  const indexes=[];for(let index=0;index<lines.length;index++){const value=lines[index].toLowerCase();if(terms.some(term=>value.includes(term)))indexes.push(index);}
  const selected=[],seen=new Set();for(const hit of indexes){for(let index=Math.max(0,hit-1);index<=Math.min(lines.length-1,hit+2);index++){if(!seen.has(index)){seen.add(index);selected.push(lines[index]);}}if(selected.length>=36)break;}
  return (selected.length?selected:lines.slice(0,24)).join('\n');
}
async function approvalLoadExactContext(item,detail){
  if(detail.dataset.loaded==='true'||!['document','classification'].includes(item.type))return;
  detail.dataset.loaded='true';
  const comparison=document.createElement('div');comparison.className='approval-comparison';detail.append(comparison);
  if(item.type==='document'){
    const [documentResult,sourceResult,transactionResult]=await Promise.all([
      api('/api/classification-document-get',{id:item.document_id}),
      api('/api/classification-document-source',{id:item.document_id}).catch(()=>({text:'Kein lokal hinterlegter Quelltext.'})),
      api('/api/classification-transaction-get',{account_id:item.transaction_account,external_id:item.transaction_external_id}),
    ]);
    const selectedDocument=documentResult.document,tx=transactionResult.transaction;
    const documentCard=approvalCompareArticle(`Beleg #${selectedDocument.id}`,[`${selectedDocument.vendor} · ${selectedDocument.title}`,`${selectedDocument.document_date||'Datum offen'} · ${selectedDocument.amount===null?'Betrag offen':eur(selectedDocument.amount)} · ${selectedDocument.status==='confirmed'?'bestätigt':'ungeprüft'}`]);
    const source=document.createElement('pre');source.className='approval-source-preview';source.textContent=approvalSourceExcerpt(sourceResult.text,selectedDocument)||'Kein lokaler Quelltext.';documentCard.append(source);comparison.append(documentCard);
    const bookingCard=approvalCompareArticle('Genau diese Buchung',[approvalTransactionLine(tx)]),row=document.createElement('div');row.className='approval-compare-row';
    if(item.direct_action)button(row,'Zuordnung stimmt',()=>approvalConfirmDocumentPair(item,selectedDocument,tx));button(row,'Zuordnung stimmt nicht',()=>approvalRejectDocumentPair(item,selectedDocument,tx));button(row,'Andere Buchung wählen',()=>classificationOpenComparison(selectedDocument.id,tx));bookingCard.append(row);
    comparison.append(bookingCard);
    const actions=document.createElement('div');actions.className='approval-actions';
    button(actions,'Belegdaten ändern',()=>classificationOpenComparison(selectedDocument.id));
    detail.append(actions);
  }else{
    const tx=(await api('/api/classification-transaction-get',{account_id:item.transaction_account,external_id:item.transaction_external_id})).transaction,bookingCard=approvalCompareArticle('Ausgewählte Buchung',[approvalTransactionLine(tx)]);comparison.append(bookingCard);
    const matches=await api('/api/classification-matches',{account_id:tx.account_id,external_id:tx.external_id,max_days:45});
    const documentCard=approvalCompareArticle('Dazu passende Belege',[]);
    if(!matches.suggestions.length){const p=document.createElement('p');p.textContent='Kein konkreter Beleg vorgeschlagen. Es wird nichts zugeordnet.';documentCard.append(p);}
    for(const match of matches.suggestions){const result=await api('/api/classification-document-get',{id:match.document_id}),row=document.createElement('div');row.className='approval-compare-row';const p=document.createElement('p');p.textContent=`Beleg #${result.document.id} · ${result.document.vendor} · ${result.document.title} · ${result.document.document_date||'Datum offen'} · ${result.document.amount===null?'Betrag offen':eur(result.document.amount)}`;row.append(p);button(row,'Genau dieses Paar öffnen',()=>classificationOpenComparison(result.document.id,tx));documentCard.append(row);}
    comparison.append(documentCard);
    const actions=document.createElement('div');actions.className='approval-actions';button(actions,'Genau diese Buchung öffnen',()=>classificationOpenTransaction(tx));
    if(item.direct_action&&!item._members?.length)button(actions,'Genau diese Buchung bestätigen',()=>approvalRunAction(item.direct_action,item.title));
    detail.append(actions);
  }
}

function approvalContext(item){
  const detail=document.createElement('div');detail.className='approval-context';detail.hidden=true;
  const heading=document.createElement('h4');heading.textContent='Nur dieser Freigabeposten';detail.append(heading);
  const summary=document.createElement('p');summary.textContent=item.detail;detail.append(summary);
  if(item.type==='transfer_correction'){
    const note=document.createElement('p');note.className='muted';note.textContent='Geprüft werden genau diese beiden Gegenbuchungen. Gleicher Betrag und zeitliche Nähe sind ein Vorschlag, kein alleiniger Nachweis.';detail.append(note);
  }else if(item.type==='budget_item'){
    const note=document.createElement('p');note.className='muted';note.textContent='Die Bestätigung betrifft nur diese Planposition. Dabei entsteht eine neue unveränderliche Planrevision.';detail.append(note);
  }else if(item._members?.length){
    const note=document.createElement('p');note.className='muted';note.textContent=`Die Gruppe enthält nur Buchungen der lokalen Regel für „${item.group_counterparty}“. Stichprobe:`;detail.append(note);
    const list=document.createElement('ul');
    for(const member of item._members.slice(0,8)){const entry=document.createElement('li');entry.textContent=`${member.transaction_date} · ${member.transaction_account} · ${eur(Math.abs(Number(member.transaction_amount)))}`;list.append(entry);}
    if(item._members.length>8){const entry=document.createElement('li');entry.textContent=`… ${item._members.length-8} weitere Buchungen derselben Regel.`;list.append(entry);}
    detail.append(list);
  }else{
    const note=document.createElement('p');note.className='muted';note.textContent='Die Entscheidung betrifft ausschließlich den oben genannten Eintrag.';detail.append(note);
  }
  if(!['document','classification'].includes(item.type)){const actions=document.createElement('div');actions.className='approval-actions';button(actions,'Diesen Fall im Bereich öffnen',()=>cockpitNavigate(item.target));detail.append(actions);}
  return detail;
}

function approvalRender(){
  const type=$('approvals-filter').value;
  const grouped=approvalGroups(approvalItems);
  const filtered=type==='priority'?grouped.filter(approvalNeedsUserDecision):grouped.filter(item=>!type||item.type===type);
  const shown=filtered.slice(0,approvalVisible),box=$('approvals-list');box.replaceChildren();
  $('approvals-scope').textContent=type==='priority'?`${filtered.length} Entscheidungen für Dich`:`${filtered.length} gebündelte offene ${filtered.length===1?'Position':'Positionen'}`;
  if(!shown.length){const empty=document.createElement('p');empty.className='overview-done';empty.textContent='In diesem Bereich ist aktuell keine Freigabe offen.';box.append(empty);}
  for(const item of shown){
    const card=document.createElement('article');card.className=`approval-item${item.direct_action?' direct':''}`;
    const content=document.createElement('div'),heading=document.createElement('h3'),detail=document.createElement('p'),meta=document.createElement('p');
    heading.textContent=item.title;
    if(item._members?.length>1){const count=document.createElement('span');count.className='approval-group-count';count.textContent=`${item._members.length} zusammen`;heading.append(count);}
    detail.textContent=item.detail;meta.className='approval-meta';meta.textContent=`${approvalTypeLabels[item.type]||item.type} · ${approvalConfidenceText(item)}`;content.append(heading,detail,meta);
    if(item.transaction_provisional){const pending=document.createElement('p');pending.className='approval-decision-note';pending.textContent='Vorgemerkte Buchung: Händlerangabe und Buchungskennung können sich bei der endgültigen Buchung noch ändern.';content.append(pending);}
    if(item.decision_note){const note=document.createElement('p');note.className='approval-decision-note';note.textContent=item.decision_note;content.append(note);}
    const actions=document.createElement('div');actions.className='approval-actions';
    if(item.type==='classification'&&item.direct_action&&!item._members?.length){
      button(actions,'Vorschlag bestätigen',()=>approvalRunAction(item.direct_action,item.title));
      if(item.model_name){
        button(actions,'Vorschlag ablehnen',()=>approvalRunAction(
          item.reject_action,'Modellvorschlag','Modellvorschlag abgelehnt. Diese Kategorie wird für diese Buchung nicht erneut vom Modell vorgeschlagen. Die Buchung bleibt ungeprüft.'));
        approvalAlternativeCategory(item,actions);
      }
    }
    if(item.direct_action&&!['document','classification'].includes(item.type)){button(actions,item.type==='transfer_correction'?'Umbuchung bestätigen':item.type==='budget_item'?'Position bestätigen':'Freigeben',()=>approvalRunAction(item.direct_action,item.title));}
    const context=approvalContext(item),toggle=document.createElement('button');toggle.type='button';toggle.textContent='Details';toggle.addEventListener('click',()=>run(async()=>{context.hidden=!context.hidden;toggle.textContent=context.hidden?'Details':'Details schließen';if(!context.hidden)await approvalLoadExactContext(item,context);}));actions.append(toggle);card.append(content,actions,context);box.append(card);
  }
  $('approvals-more').hidden=shown.length>=filtered.length;
}

async function approvalsLoad(){
  const [queue,catalog]=await Promise.all([api('/api/approvals',{as_of:$('cutoff').value}),api('/api/classification-catalog',{})]);
  approvalItems=queue.items||[];approvalCatalog=catalog;approvalVisible=40;approvalRenderSummary(approvalItems);approvalRender();
}

document.addEventListener('DOMContentLoaded',()=>{
  if(!$('approvals'))return;
  $('approvals-reload').addEventListener('click',()=>run(approvalsLoad));
  $('approvals-filter').addEventListener('change',()=>{approvalVisible=40;approvalRender();});
  $('approvals-more').addEventListener('click',()=>{approvalVisible+=40;approvalRender();});
});
