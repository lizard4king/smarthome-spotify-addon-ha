'use strict';

// A separate confirmation boundary: contract terms and evidence links never
// trigger the invoice/category review workflow.
let contractsEpoch=0,contractsRequest=0,contractsDocument=null,contractsData=null,contractsDirty=false;
function contractsNode(tag,text){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;return node;}
function contractsStatus(text,error=false){const node=$('contracts-status');node.textContent=text;node.className=error?'error':'muted';}
function contractsClear(){contractsEpoch++;contractsRequest++;contractsDocument=null;contractsData=null;contractsDirty=false;$('contracts-panel').hidden=true;$('contracts-phases').replaceChildren();$('contracts-matches').replaceChildren();}
function contractsCurrent(epoch){return epoch===contractsEpoch&&contractsDocument!==null;}
function contractsChanged(){contractsDirty=true;contractsRequest++;$('contracts-matches').replaceChildren();contractsStatus('Vertragsangaben geändert. Erst ausdrücklich bestätigen und speichern, dann Vorschläge prüfen.');}
function contractsField(parent,label,key,value,type='text'){
  const wrapper=contractsNode('label',label),input=contractsNode('input');input.type=type;input.value=value??'';input.dataset.field=key;
  if(type==='number'){input.min='1';input.max='120';input.step='1';input.required=true;}
  if(key==='amount'){input.inputMode='decimal';input.required=true;}
  input.addEventListener('input',contractsChanged);wrapper.append(input);parent.append(wrapper);return input;
}
function contractsAddPhase(phase={}){
  if($('contracts-phases').children.length>=24){contractsStatus('Höchstens 24 Betragsphasen sind möglich.',true);return false;}
  const row=contractsNode('fieldset');row.className='contracts-phase';row.append(contractsNode('legend','Betragsphase'));
  const grid=contractsNode('div');grid.className='contracts-grid';
  contractsField(grid,'Gültig von','valid_from',phase.valid_from,'date').required=true;
  contractsField(grid,'Gültig bis (optional)','valid_to',phase.valid_to,'date');
  contractsField(grid,'Betrag EUR je Zahlung','amount',phase.amount);
  contractsField(grid,'Intervall in Monaten (1–120)','interval_months',phase.interval_months??1,'number');row.append(grid);
  const remove=contractsNode('button','Betragsphase entfernen');remove.type='button';remove.className='secondary';remove.addEventListener('click',()=>{row.remove();contractsChanged();});row.append(remove);$('contracts-phases').append(row);
}
function contractsRender(data){
  contractsData=data;contractsDirty=false;
  $('contracts-provider').textContent=data.provider||contractsDocument.vendor||'Anbieter im Belegfeld oben pflegen.';
  for(const field of ['reference','valid_from','valid_to','origin'])$('contracts-'+field).value=data[field]??(field==='origin'?'manual':'');
  $('contracts-phases').replaceChildren();for(const phase of data.phases||[])contractsAddPhase(phase);
  const confirmed=contractsDocument.status==='confirmed';$('contracts-fields').disabled=!confirmed;$('contracts-save').disabled=!confirmed;
  contractsStatus(confirmed?(data.configured?'Gespeicherte Vertragsangaben. Jede Zuordnung separat prüfen und bestätigen.':'Vertragsangaben prüfen und ausdrücklich bestätigen.'): 'Zuerst die Belegdaten oben anhand der Quelle bestätigen. Danach Vertragsangaben separat bestätigen.');
}
async function contractsLoadDocument(doc){
  contractsClear();if(doc.kind!=='contract')return;
  contractsDocument=doc;const epoch=contractsEpoch;$('contracts-panel').hidden=false;$('contracts-fields').disabled=true;$('contracts-save').disabled=true;contractsStatus('Vertragsangaben werden geladen …');
  try{const data=await api('/api/contracts-load',{document_id:doc.id});if(!contractsCurrent(epoch))return;contractsRender(data);if(data.configured&&doc.status==='confirmed')await contractsLoadMatches(epoch,0);}
  catch(error){if(contractsCurrent(epoch))contractsStatus(error.message||'Vertragsangaben konnten nicht geladen werden.',true);}
}
async function contractsSave(event){
  event.preventDefault();if(!contractsDocument||contractsDocument.status!=='confirmed'||!contractsData)return;
  const form=$('contracts-form');if(!form.reportValidity())return;
  const epoch=contractsEpoch,documentId=contractsDocument.id,revision=contractsData.revision;
  const phases=[...$('contracts-phases').querySelectorAll('.contracts-phase')].map(row=>{const value=key=>row.querySelector(`[data-field="${key}"]`).value.trim();return {valid_from:value('valid_from'),valid_to:value('valid_to')||null,amount:value('amount').replace(',','.'),interval_months:Number(value('interval_months'))};});
  const payload={document_id:documentId,revision,reference:$('contracts-reference').value.trim(),valid_from:$('contracts-valid_from').value||null,valid_to:$('contracts-valid_to').value||null,origin:$('contracts-origin').value,phases,confirmed:true};
  $('contracts-save').disabled=true;$('contracts-fields').disabled=true;contractsRequest++;$('contracts-matches').replaceChildren();contractsStatus('Bestätigte Vertragsangaben werden gespeichert …');
  try{const data=await api('/api/contracts-save',payload);if(!contractsCurrent(epoch))return;contractsDocument={...contractsDocument,revision:data.revision};if(classificationSelectedDocument?.id===documentId)classificationSelectedDocument.revision=data.revision;contractsRender(data);contractsStatus('Vertragsangaben bestätigt und gespeichert. Die Buchungszuordnung bleibt eine eigene Entscheidung.');await contractsLoadMatches(epoch,0);}
  catch(error){if(contractsCurrent(epoch))contractsStatus(error.message||'Speichern fehlgeschlagen. Angaben bleiben zur Prüfung erhalten.',true);}
  finally{if(contractsCurrent(epoch)){$('contracts-save').disabled=false;$('contracts-fields').disabled=false;}}
}
async function contractsLoadMatches(epoch=contractsEpoch,page=0){
  if(!contractsCurrent(epoch)||contractsDirty)return;const request=++contractsRequest,documentId=contractsDocument.id;
  $('contracts-matches').textContent='Vertragsvorschläge werden gesucht …';
  try{const data=await api('/api/contracts-matches',{document_id:documentId,page});if(contractsCurrent(epoch)&&request===contractsRequest&&!contractsDirty)contractsRenderMatches(data,epoch);}
  catch(error){if(contractsCurrent(epoch)&&request===contractsRequest)$('contracts-matches').textContent=error.message||'Vertragsvorschläge konnten nicht geladen werden.';}
}
function contractsRenderMatches(data,epoch){
  const box=$('contracts-matches');box.replaceChildren();box.append(contractsNode('h4','Vertragsvorschläge zur Prüfung'));
  box.append(contractsNode('p','Eine Bestätigung ergänzt ausschließlich einen Vertragsnachweis. Betrag, Saldo, Kategorie und Planung bleiben unverändert.'));
  for(const note of data.limit_notes||[])box.append(contractsNode('p',note));
  if(!(data.suggestions||[]).length)box.append(contractsNode('p','Keine Vertragsvorschläge für diesen Stand.'));
  for(const suggestion of data.suggestions||[]){
    const card=contractsNode('article');card.className='contracts-match';
    const accountName=typeof accountDisplayById==='function'?accountDisplayById(suggestion.account_id):suggestion.account_id;
    card.append(contractsNode('p',`Konto: ${accountName||suggestion.account_id}`));
    card.append(contractsNode('p',`${suggestion.date} · ${eur(suggestion.amount)} · ${suggestion.currency||'EUR'}`),contractsNode('p',suggestion.counterparty||'Keine Gegenpartei'));
    const details=contractsNode('details');details.append(contractsNode('summary','Vollständigen Verwendungszweck anzeigen'),contractsNode('p',suggestion.description||'Kein Verwendungszweck'));card.append(details);
    if(suggestion.phase)card.append(contractsNode('p',`Betragsphase: ${suggestion.phase.valid_from||'offen'} bis ${suggestion.phase.valid_to||'offen'} · alle ${suggestion.phase.interval_months||'?'} Monate`));
    card.append(contractsNode('p',`Erwarteter Betrag: ${eur(suggestion.expected_amount)} · Einschätzung: ${suggestion.confidence??'offen'}`));
    for(const [label,values] of [['Gründe',suggestion.reasons],['Unsicherheiten',suggestion.uncertainties],['Mehrdeutige Vertrags-IDs',suggestion.competing_document_ids]])card.append(contractsNode('p',`${label}: ${(values||[]).join(' · ')||'keine gemeldet'}`));
    const confirm=contractsNode('button','Vertragszuordnung bestätigen');confirm.type='button';confirm.addEventListener('click',async()=>{
      if(!contractsCurrent(epoch)||contractsDirty)return;confirm.disabled=true;
      try{await api('/api/contracts-link',{document_id:data.document_id,document_revision:suggestion.document_revision,account_id:suggestion.account_id,external_id:suggestion.external_id,revision:suggestion.revision,expected_date:suggestion.date,expected_amount:suggestion.amount,confirmed:true});if(!contractsCurrent(epoch))return;contractsStatus('Vertragszuordnung ausdrücklich bestätigt. Buchungswerte bleiben unverändert.');await contractsLoadMatches(epoch,data.page);}
      catch(error){if(contractsCurrent(epoch)){contractsStatus(error.message||'Zuordnung fehlgeschlagen; Vorschlag erneut prüfen.',true);confirm.disabled=false;}}
    });card.append(confirm);box.append(card);
  }
  if(data.pages>1){const controls=contractsNode('div');controls.className='contracts-pagination';for(const [label,page,disabled] of [['Vorige Vorschlagsseite',data.page-1,data.page===0],['Nächste Vorschlagsseite',data.page+1,data.page+1>=data.pages]]){const control=contractsNode('button',label);control.type='button';control.disabled=disabled;control.addEventListener('click',()=>contractsLoadMatches(epoch,page));controls.append(control);}controls.append(contractsNode('p',`Seite ${data.page+1} von ${data.pages} · ${data.total} Vorschläge`));box.append(controls);}
}
document.addEventListener('DOMContentLoaded',()=>{if(!$('contracts-panel'))return;$('contracts-form').addEventListener('submit',contractsSave);$('contracts-add-phase').addEventListener('click',()=>{if(contractsAddPhase()!==false)contractsChanged();});for(const field of ['reference','valid_from','valid_to','origin'])$('contracts-'+field).addEventListener('input',contractsChanged);});
