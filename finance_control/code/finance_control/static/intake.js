'use strict';
let intakeDraft=null, intakeDirty=false, intakeRowsPage=0, intakeRowsRequest=0;
const intakeStatusNames={bracketed_by_source:'Aus Quelle abgeleitet',last_source_before_cutoff_only:'Quelle endet vor Stichtag',missing_cutoff_coverage:'Quelle beginnt nach Stichtag',before_or_on_opening:'Historie · nicht nochmals buchen',after_opening:'Zur Buchungsprüfung'};
function intakeSetStatus(text,error=false){$('intake-status').textContent=text;$('intake-status').className=error?'error':'';}
function intakeInputs(){return Array.from($('intake-account-rows').rows).map(row=>({key:row.dataset.key,opening:row.querySelector('input[type="text"]').value.trim().replace(',','.')||null,opening_confirmed:row.querySelector('input[type="checkbox"]').checked}));}
function intakeUpdateButtons(){
  const ready=intakeDraft&&intakeInputs().every(a=>a.opening!==null&&a.opening_confirmed);
  $('intake-save').disabled=!intakeDirty||Boolean(intakeDraft?.activated);
  $('intake-activate').disabled=!ready||intakeDirty||Boolean(intakeDraft?.activated);
}
function intakeMarkDirty(){intakeDirty=true;intakeUpdateButtons();intakeSetStatus('Ungespeicherte Änderungen. Bitte Entwurf speichern.');}
function intakeRenderSummary(draft){
  const confirmed=draft.accounts.filter(a=>a.opening_confirmed).length;
  const candidate=draft.accounts.filter(a=>!a.opening_confirmed&&a.opening!==null).length;
  const missing=draft.accounts.length-confirmed-candidate;
  $('intake-source').textContent=`${draft.source_label} · Eröffnungsstichtag: ${draft.opening_date} · ${draft.source_rows} Quellzeilen · ${draft.original_rows} Originalbuchungen`;
  $('intake-counts').textContent=`${draft.accounts.length} Konten · ${confirmed} Salden bestätigt · ${candidate} Werte zu prüfen · ${missing} Salden fehlen`;
  const split=draft.split_summary||{};
  $('intake-splits').textContent=`${split.balanced||0} von ${split.groups||0} Split-Gruppen ausgeglichen · ${draft.excluded_split_children} Teilzeilen werden nicht doppelt gebucht.`;
  $('intake-unresolved').textContent=draft.activated?'Konten angelegt. Buchungen müssen noch gesondert geprüft werden.':`${draft.unresolved} Salden noch nicht bestätigt. Keine Buchungen übernommen.`;
  const chart=$('intake-chart');chart.replaceChildren();
  for(const [kind,label,count] of [['confirmed','Bestätigt',confirmed],['candidate','Zu prüfen',candidate],['missing','Fehlt',missing]]){
    const item=document.createElement('div');item.className='intake-bar-item';
    const title=document.createElement('span');title.textContent=`${label}: ${count}`;
    const track=document.createElement('div'),bar=document.createElement('div');bar.className=`intake-bar ${kind}`;bar.style.width=`${count/draft.accounts.length*100}%`;track.append(bar);item.append(title,track);chart.append(item);
  }
}
function intakeRenderAccounts(draft){
  const body=$('intake-account-rows');body.replaceChildren();
  for(const account of draft.accounts){
    const tr=document.createElement('tr');tr.dataset.key=account.key;
    cell(tr,`${account.key} · ${account.source_name}\n${account.institution}`).className='intake-account-name';
    cell(tr,Object.entries(account.shares).map(([p,v])=>`${profilePersonLabel(p)} ${Number(v)*100} %`).join(' / '));
    cell(tr,account.opening_candidate===null?'Offen':amount(account.opening_candidate),true);
    const input=document.createElement('input');input.type='text';input.inputMode='decimal';input.value=account.opening??'';input.placeholder='Saldo fehlt';input.disabled=draft.activated;input.setAttribute('aria-label',`Anfangssaldo ${account.key}`);cell(tr,'').append(input);
    const check=document.createElement('input');check.type='checkbox';check.checked=account.opening_confirmed;check.disabled=draft.activated;check.setAttribute('aria-label',`Saldo ${account.key} bestätigen`);cell(tr,'').append(check);
    input.addEventListener('input',()=>{check.checked=false;intakeMarkDirty();});check.addEventListener('change',intakeMarkDirty);
    const details=document.createElement('details'),summary=document.createElement('summary'),note=document.createElement('p');summary.textContent=intakeStatusNames[account.balance_status]||'Quellangabe';note.textContent=`Quelltag: ${account.source_day||'offen'}. ${account.note}`;details.append(summary,note);cell(tr,'').append(details);body.append(tr);
  }
}
async function intakeRender(draft){
  intakeDraft=draft;intakeDirty=false;intakeRowsPage=0;
  $('intake-empty').hidden=Boolean(draft);$('intake-panel').hidden=!draft;
  if(!draft)return;
  if(!state.accounts.length)$('empty').textContent=`${draft.accounts.length} Konten sind als Importentwurf vorbereitet. Salden und Buchungen kannst Du unter „Vorbereiteter Import“ prüfen.`;
  $('intake-continue').hidden=!draft.activated;
  intakeRenderSummary(draft);intakeRenderAccounts(draft);intakeUpdateButtons();
  const filter=$('intake-account-filter');filter.replaceChildren();const all=document.createElement('option');all.value='';all.textContent='Alle Konten';filter.append(all);
  for(const a of draft.accounts){const option=document.createElement('option');option.value=a.key;option.textContent=`${a.key} · ${a.source_name}`;filter.append(option);}
  await intakeRowsRefresh();
}
async function intakeRefresh(){
  if(intakeDirty){intakeSetStatus('Ungespeicherte Änderungen bleiben erhalten. Speichern oder gespeicherten Stand laden.',true);return;}
  const result=await api('/api/intake-load',{});await intakeRender(result.draft);
}
async function intakeSave(){
  const result=await api('/api/intake-save',{revision:intakeDraft.revision,accounts:intakeInputs()});
  await intakeRender(result.draft);intakeSetStatus('Entwurf gespeichert. Noch keine Konten oder Buchungen übernommen.');
}
async function intakeActivate(){
  if(intakeDirty)throw new Error('Bitte den Entwurf zuerst speichern.');
  await api('/api/intake-activate',{revision:intakeDraft.revision,confirmed:true});
  await refresh();await fgResumePrepared();intakeSetStatus('Konten angelegt. Weiter mit der Buchungsprüfung.');
}
async function intakeRowsRefresh(){
  if(!intakeDraft)return;
  const request=++intakeRowsRequest,account=$('intake-account-filter').value;
  const result=await api('/api/intake-rows',{page:intakeRowsPage,...(account?{account_key:account}:{})});
  if(request!==intakeRowsRequest)return;
  $('intake-rows-page').textContent=`Seite ${result.pages?result.page+1:0} / ${result.pages} · ${result.total} Zeilen`;
  $('intake-rows-prev').disabled=result.page<=0;$('intake-rows-next').disabled=result.page+1>=result.pages;
  $('intake-row-rows').replaceChildren();
  for(const row of result.rows){const tr=document.createElement('tr');cell(tr,row.reference);cell(tr,row.account_key);cell(tr,row.date);cell(tr,amount(row.amount),true);cell(tr,row.category);cell(tr,intakeStatusNames[row.status]||row.status);$('intake-row-rows').append(tr);}
}
$('intake-save').addEventListener('click',()=>run(intakeSave));
$('intake-reload').addEventListener('click',()=>run(async()=>{intakeDirty=false;await intakeRefresh();intakeSetStatus('Gespeicherten Stand geladen.');}));
$('intake-activate').addEventListener('click',()=>run(intakeActivate));
$('intake-continue').addEventListener('click',()=>run(fgResumePrepared));
$('intake-rows-prev').addEventListener('click',()=>run(async()=>{intakeRowsPage--;await intakeRowsRefresh();}));
$('intake-rows-next').addEventListener('click',()=>run(async()=>{intakeRowsPage++;await intakeRowsRefresh();}));
$('intake-account-filter').addEventListener('change',()=>run(async()=>{intakeRowsPage=0;await intakeRowsRefresh();}));
