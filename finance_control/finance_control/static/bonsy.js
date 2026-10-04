function bonsyBase64(file){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onerror=()=>reject(new Error('Die Bonsy-Datei konnte nicht gelesen werden.'));reader.onload=()=>resolve(String(reader.result).split(',',2)[1]);reader.readAsDataURL(file);});}

const bonsyEur=value=>Number(value).toLocaleString('de-DE',{style:'currency',currency:'EUR'});
function bonsyPaymentAssessment(item){
  const assessment=item.payment_assessment;
  const explicitCash=assessment?.method==='cash'&&assessment.confidence==='very_high'&&assessment.confirmed===false;
  if(item.reason==='pending_bank_posting')return 'pending_bank_posting';
  return explicitCash||['likely_cash','likely_cash_no_direct_payment_found'].includes(item.reason)?'likely_cash':'payment_review';
}
function bonsyOpenDescription(item){
  if(bonsyPaymentAssessment(item)==='pending_bank_posting')return ' · Bankbuchung vermutlich noch ausstehend';
  if(bonsyPaymentAssessment(item)==='likely_cash')return ' · sehr wahrscheinlich bar bezahlt · unbestätigt';
  if(Number(item.covered)>0)return item.reason==='partial_legacy_cash_allocation'
    ?` · historische Bargeldzuordnung ${bonsyEur(item.covered)}, offen ${bonsyEur(item.remaining)} · Zahlung prüfen`
    :` · direkt belegt ${bonsyEur(item.covered)}, offen ${bonsyEur(item.remaining)} · Zahlung prüfen`;
  if(item.reason==='evidence_link_requires_manual_review')return ' · Zahlungsnachweis vorhanden · Zahlung prüfen';
  return ' · Zahlung prüfen';
}
async function bonsyCashLoad(){
  const summary=$('bonsy-cash-summary'),pending=$('bonsy-cash-pending'),proposals=$('bonsy-cash-proposals'),unresolved=$('bonsy-cash-unresolved'),confirmed=$('bonsy-cash-confirmed');
  if(!summary||!pending||!proposals||!unresolved||!confirmed)return;
  try{
    const data=await api('/api/bonsy-cash',{});
    const pendingItems=data.unresolved.filter(item=>bonsyPaymentAssessment(item)==='pending_bank_posting');
    const likelyCashItems=data.unresolved.filter(item=>bonsyPaymentAssessment(item)==='likely_cash');
    const paymentReviewItems=data.unresolved.filter(item=>bonsyPaymentAssessment(item)==='payment_review');
    const pendingCount=data.counts.pending_posting??pendingItems.length;
    const likelyCash=data.counts.likely_cash??likelyCashItems.length;
    const paymentReview=data.counts.payment_review??paymentReviewItems.length;
    summary.textContent=`Buchung vermutlich noch ausstehend: ${pendingCount} · Sehr wahrscheinlich bar (unbestätigt): ${likelyCash} · Zahlung prüfen: ${paymentReview} · Bereits zugeordnet: ${data.counts.confirmed}.`;
    pending.replaceChildren();
    proposals.replaceChildren();
    unresolved.replaceChildren();
    confirmed.replaceChildren();
    const waiting=document.createElement('p');waiting.textContent=`${pendingCount} Bons sind höchstens fünf Kalendertage alt. Eine Lastschrift oder Kartenzahlung kann noch ausstehen; bis dahin wird keine Zahlungsart angenommen.`;pending.append(waiting);
    for(const item of pendingItems.slice(0,25)){const p=document.createElement('p');p.textContent=`${item.date} · ${item.vendor} · ${bonsyEur(item.amount)}${bonsyOpenDescription(item)}`;pending.append(p);}
    const review=document.createElement('p');review.textContent=`${paymentReview} Bons haben einen möglichen direkten Zahlungstreffer, einen offenen Zahlungsanteil oder kosten mindestens 50 €. Diese Fälle bleiben „Zahlung prüfen“.`;proposals.append(review);
    for(const item of paymentReviewItems.slice(0,25)){const p=document.createElement('p');p.textContent=`${item.date} · ${item.vendor} · ${bonsyEur(item.amount)}${bonsyOpenDescription(item)}`;proposals.append(p);}
    const open=document.createElement('p');open.textContent=`${likelyCash} ältere Bons unter 50 € haben keinen direkten Zahlungstreffer und sind deshalb mit sehr hoher Wahrscheinlichkeit bar bezahlt. Die Einschätzung bleibt unbestätigt und erzeugt weder eine Buchung noch eine Zuordnung.`;unresolved.append(open);
    for(const item of likelyCashItems.slice(0,25)){const p=document.createElement('p');p.textContent=`${item.date} · ${item.vendor} · ${bonsyEur(item.amount)}${bonsyOpenDescription(item)}`;unresolved.append(p);}
    const done=document.createElement('p');done.textContent=`${data.counts.confirmed} Bons sind bereits zugeordnet.`;confirmed.append(done);
    for(const item of data.confirmed.slice(0,25)){const p=document.createElement('p'),kind=item.reason==='legacy_cash_allocation'?'historische Bargeldzuordnung':'direkte Zahlung';p.textContent=`${item.date} · ${item.vendor} · ${bonsyEur(item.amount)} · ${kind}`;confirmed.append(p);}
  }catch(error){summary.textContent='Bargeldabgleich konnte nicht geladen werden: '+error.message;}
}

document.addEventListener('DOMContentLoaded',()=>{
  const form=$('bonsy-form');if(!form)return;
  const exclusionLabel=document.createElement('label');
  exclusionLabel.className='check';
  const exclusionControl=document.createElement('input');
  exclusionControl.type='checkbox';
  exclusionControl.id='bonsy-confirm-exclusions';
  const exclusionText=document.createElement('span');
  exclusionText.textContent='Ausschlüsse aus diesem Export übernehmen; vorhandene Bons und Zahlungszuordnungen werden entsprechend bereinigt.';
  exclusionLabel.append(exclusionControl,exclusionText);
  form.insertBefore(exclusionLabel,$('bonsy-import'));
  form.addEventListener('submit',event=>{event.preventDefault();run(async()=>{
    const file=$('bonsy-file').files[0];if(!file)throw new Error('Bitte eine Bonsy-XLSX auswählen.');
    const button=$('bonsy-import'),status=$('bonsy-status');button.disabled=true;status.textContent='Bonsy-Belege werden lokal geprüft und übernommen …';
    try{
      const confirmExclusions=exclusionControl.checked;
      const result=await api('/api/bonsy-import',{content:await bonsyBase64(file),confirm_exclusions:confirmExclusions});
      const base=result.status==='duplicate'?`Dieser Export war bereits importiert: ${result.receipts} geprüfte Belege, ${result.products} Positionen. ${result.auto_links.linked.length} inzwischen eindeutige Zahlungen wurden nachträglich verknüpft.`:`${result.new_receipts} neue, bereits geprüfte Belege und ${result.products} Produktpositionen übernommen. ${result.auto_links.linked.length} eindeutige Zahlungen automatisch verknüpft; bei ${result.unlinked_receipts} Belegen ist noch keine eindeutige Zahlung zugeordnet. Die Einschätzung steht im Zahlungsabgleich.`;
      const report=result.exclusion_report||{},reconciliation=result.reconciliation||{};
      const excludedReceipts=(report.excluded_receipt_ids||[]).length,excludedProducts=(report.excluded_product_ids||[]).length;
      const reconciled=(reconciliation.reconciled_receipt_ids||[]).length;
      const unchanged=(reconciliation.unchanged_receipt_ids||[]).length;
      const missing=(reconciliation.missing_receipt_ids||[]).length;
      const exclusionSummary=`Aus Quelle ausgeschlossen: ${excludedReceipts} Bons, ${excludedProducts} Positionen.`;
      const decisionSummary=confirmExclusions
        ?` Ausschlüsse übernommen: ${reconciled} bestehende Bons bereinigt, ${unchanged} bereits markiert, ${missing} im bisherigen Importbestand nicht gefunden.`
        :' Ausschlüsse wurden nicht auf vorhandene Belege angewendet.';
      status.textContent=`${base} ${exclusionSummary}${decisionSummary}`;
      exclusionControl.checked=false;
      await documentCoverageLoad();await classificationLoadDocuments();await bonsyCashLoad();
    }finally{button.disabled=false;}
  });});
});
