'use strict';
const $ = id => document.getElementById(id);
let state;
let cockpitDataGeneration = 0;
let cockpitFeaturesReady = document.readyState === 'complete';
const cockpitAreaLoaded = new Map();
const cockpitAreaLoading = new Map();

function cockpitAreaKey(id) {
  return ['planning', 'scenarios'].includes(id) ? 'planning' : id;
}

async function cockpitLoadArea(id, {force = false} = {}) {
  if (!state?.csrf || !cockpitFeaturesReady) return false;
  const key = cockpitAreaKey(id);
  if (cockpitAreaLoading.has(key)) return cockpitAreaLoading.get(key);
  if (!force && cockpitAreaLoaded.get(key) === cockpitDataGeneration) return true;
  const loaders = {
    dashboard: async () => {
      if (typeof window.financeDashboardRender === 'function') window.financeDashboardRender(state.dashboard);
      if (typeof window.financeBankRefresh?.activate === 'function') void window.financeBankRefresh.activate();
    },
    'category-outflows': async () => { if (typeof window.financeDashboardRender === 'function') window.financeDashboardRender(state.dashboard, 'category-outflows'); },
    purchases: async () => { if (typeof window.financePurchasesLoad === 'function') await window.financePurchasesLoad(); },
    administration: async () => { if (typeof window.administrationLoad === 'function') await window.administrationLoad(); },
    'plan-actual': async reload => {
      if (typeof planActualEnsureLoaded !== 'function') return;
      await planActualEnsureLoaded(reload);
    },
    classification: async () => { if (typeof classificationLoad === 'function') await classificationLoad(); },
    documents: async reload => {
      if (typeof classificationCatalog !== 'undefined' && (!classificationCatalog.length || reload)) {
        const catalog = await api('/api/classification-catalog', {});
        classificationCatalog = catalog.categories || [];
        classificationCatalogParents = catalog.parents || [];
      }
      await Promise.all([
        typeof classificationLoadDocuments === 'function' ? classificationLoadDocuments() : undefined,
        typeof bonsyCashLoad === 'function' ? bonsyCashLoad() : undefined,
      ]);
    },
    planning: async reload => {
      if (typeof budgetLoad === 'function') await budgetLoad(reload && !(typeof budgetDirty !== 'undefined' && budgetDirty));
    },
    intake: async () => { if (typeof intakeRefresh === 'function') await intakeRefresh(); },
    analytics: async () => { if (typeof analyticsLoad === 'function') await analyticsLoad(); },
    approvals: async () => { if (typeof approvalsLoad === 'function') await approvalsLoad(); },
    finanzguru: async () => { if (typeof fgRefreshAccounts === 'function') fgRefreshAccounts(); },
    monthly: async () => {
      if (typeof monthlyPreview !== 'function') return;
      if (!$('monthly-period').value) $('monthly-period').value = state.as_of.slice(0, 7);
      await monthlyPreview();
    },
    wealth: async () => {
      if (typeof wealthLoadLatest === 'function' && !(typeof wealthInitialized !== 'undefined' && wealthInitialized))
        await wealthLoadLatest();
    },
  };
  const promise = Promise.resolve().then(async () => {
    let reload = cockpitAreaLoaded.has(key);
    do {
      const generation = cockpitDataGeneration;
      if (loaders[key]) await loaders[key](reload);
      cockpitAreaLoaded.set(key, generation);
      if (generation === cockpitDataGeneration || cockpitAreaKey(location.hash.slice(1)) !== key) return true;
      reload = true;
    } while (state?.csrf);
    return false;
  }).catch(error => {
    if (cockpitAreaKey(location.hash.slice(1)) === key)
      message(`Dieser Bereich konnte nicht geladen werden: ${error.message}`, true);
    return false;
  }).finally(() => cockpitAreaLoading.delete(key));
  cockpitAreaLoading.set(key, promise);
  return promise;
}

document.addEventListener('cockpit-area-changed', event => {
  if (event.detail.id !== 'dashboard' && typeof window.financeBankRefresh?.deactivate === 'function')
    window.financeBankRefresh.deactivate();
  // Both views share selection state and must repaint when becoming visible.
  const sharedAccountView = ['dashboard', 'category-outflows'].includes(event.detail.id);
  void cockpitLoadArea(event.detail.id, {force: sharedAccountView});
});
document.addEventListener('finance-purchase-edit', event => {
  if (Number.isInteger(event.detail?.id) && typeof classificationOpenComparison === 'function')
    void run(() => classificationOpenComparison(event.detail.id));
});
document.addEventListener('DOMContentLoaded', () => {
  cockpitFeaturesReady = true;
  void cockpitLoadArea(location.hash.slice(1) || 'plan-actual');
});
const accountSaveMessages = new Map();
const eur = value => new Intl.NumberFormat('de-DE', {style:'currency', currency:'EUR'}).format(Number(value));
const amount = value => new Intl.NumberFormat('de-DE', {minimumFractionDigits:2, maximumFractionDigits:2}).format(Number(value));
function accountBalanceLabel(balance) {
  if (!balance || typeof balance !== 'object' || balance.amount === null || balance.amount === undefined)
    return 'Kontostand nicht verfügbar';
  const bookedOn = String(balance.booked_on || '').match(/^(\d{4})-(\d{2})-(\d{2})/);
  const stand = bookedOn ? `${bookedOn[3]}.${bookedOn[2]}.${bookedOn[1]}` : 'Datum nicht verfügbar';
  return `${amount(balance.amount)} ${balance.currency || 'EUR'} · Stand: ${stand}`;
}
function message(text, error=false) { $('message').textContent=text; $('message').className=error?'error':''; }
async function api(route, data, retryAfterTokenRefresh=true) {
  const options = data === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Finance-Token':state.csrf},body:JSON.stringify(data)};
  const response = await fetch(route, options); const result = await response.json();
  if(response.status===403&&data!==undefined&&retryAfterTokenRefresh&&result.error==='Anfrage nicht freigegeben. Seite neu laden.'){
    const fresh=await api('/api/state',undefined,false);
    state={...state,...fresh};
    return api(route,data,false);
  }
  if (!response.ok) {
    const error = new Error(result.error || 'Anfrage fehlgeschlagen.');
    if (typeof result.code === 'string') error.code = result.code;
    throw error;
  }
  return result;
}
function cell(row, text, numeric=false) { const td=document.createElement('td'); td.textContent=text; if(numeric) td.className='numeric'; row.append(td); return td; }
function accountDisplay(account) { return account.display_name ? `${account.id} · ${account.display_name}` : account.id; }
function accountDisplayById(id) { const account=(state?.accounts||[]).find(item=>item.id===id); return account ? accountDisplay(account) : id; }
function profilePeople() {
  const configured=state?.profile_people;
  if(Array.isArray(configured)&&configured.length)return configured.map((person,index)=>typeof person==='string'
    ? {id:index===0?'ANDREAS':index===1?'ERLENE':`PERSON_${index+1}`,label:person}
    : person);
  return [{id:'ANDREAS',label:'Person 1'},{id:'ERLENE',label:'Person 2'}];
}
function profilePersonLabel(id, fallback=id) {
  if(id==='JOINT')return 'Gemeinsam';
  return profilePeople().find(person=>person.id===id)?.label||fallback;
}
function percentageToShare(value) { const match=/^(\d{1,3})(?:\.(\d{1,2}))?$/.exec(String(value).trim().replace(',','.'));if(!match)throw new Error('Anteile als Prozentwert mit höchstens zwei Nachkommastellen angeben.');const basisPoints=Number(match[1])*100+Number((match[2]||'').padEnd(2,'0'));if(basisPoints<=0||basisPoints>10000)throw new Error('Jeder Anteil muss größer als 0 und höchstens 100 Prozent sein.');return `${Math.floor(basisPoints/10000)}.${String(basisPoints%10000).padStart(4,'0')}`; }
function accountSharesFromInputs() { const shares=Object.fromEntries([...$('share-inputs').querySelectorAll('[data-person-share]')].map(input=>[input.dataset.personShare,percentageToShare(input.value)]));const total=Object.values(shares).reduce((sum,value)=>sum+Math.round(Number(value)*10000),0);if(total!==10000)throw new Error('Die Anteile müssen zusammen genau 100 Prozent ergeben.');return shares; }
function renderAccountOwnerControls() {
  const people=profilePeople(), owner=$('account-form').elements.owner, current=owner.value;
  owner.replaceChildren();
  const prompt=document.createElement('option');prompt.value='';prompt.disabled=true;prompt.textContent='Bitte zuordnen';owner.append(prompt);
  for(const person of people){const option=document.createElement('option');option.value=person.id;option.textContent=person.label;owner.append(option);}
  if(people.length>1){const option=document.createElement('option');option.value='JOINT';option.textContent='Gemeinsam';owner.append(option);}
  owner.value=[...owner.options].some(option=>option.value===current)?current:'';
  renderAccountShareFields();
}
function renderAccountShareFields() {
  const fieldset=$('share-fields'), container=$('share-inputs'), people=profilePeople();
  container.replaceChildren();
  const shared=$('account-form').elements.owner.value==='JOINT';
  fieldset.hidden=!shared;
  if(!shared)return;
  const basisPoints=Math.floor(10000/people.length), remainder=10000-basisPoints*people.length;
  people.forEach((person,index)=>{
    const label=document.createElement('label');label.textContent=person.label+' (%)';
    const input=document.createElement('input');input.type='text';input.inputMode='decimal';input.required=true;
    input.dataset.personShare=person.id;input.value=((basisPoints+(index===people.length-1?remainder:0))/100).toFixed(2);
    input.pattern='\\d+(?:[.,]\\d{1,2})?';input.placeholder='0,00';label.append(input);container.append(label);
  });
}
function button(parent, text, action) { const b=document.createElement('button'); b.type='button'; b.textContent=text; b.addEventListener('click',()=>run(action)); parent.append(b); return b; }
async function run(action) { try { await action(); } catch(error) { message(error.message, true); } }
function download(url) {const link=document.createElement('a');link.href=url;link.download='';document.body.append(link);link.click();link.remove();}
function invalidate() { $('projection').hidden=true; }
function renderOverview(overview) {
  const actuals=overview?.actuals;
  if(actuals){
    const incomeLabel=$('overview-income-label'), expenseLabel=$('overview-expenses-label');
    if(incomeLabel)incomeLabel.textContent=`Kontenzuflüsse ${actuals.year}`;
    if(expenseLabel)expenseLabel.textContent=`Kontenabflüsse ${actuals.year}`;
    $('income-total').textContent=eur(actuals.income);
    $('expense-total').textContent=eur(actuals.expenses);
  }
  const plan=overview?.plan||null, values=$('overview-plan-values');
  const primaryAction=overview?.primary_action||null, primary=$('overview-primary-action'), primaryOpen=$('overview-primary-action-open');
  primary.classList.toggle('done',!primaryAction);
  $('overview-primary-action-label').textContent=primaryAction?.label||'Keine unmittelbare Aktion nötig';
  $('overview-primary-action-detail').textContent=primaryAction?.detail||'Der aktuelle Stand enthält keine vorrangige Entscheidung.';
  primaryOpen.hidden=!primaryAction;
  primaryOpen.dataset.target=primaryAction?.target||'';
  $('overview-plan-revision').textContent=plan?`Revision ${plan.revision}`:'Noch ohne Plan';
  $('overview-plan-title').textContent=plan?`${plan.title} · ${plan.start_month} bis ${plan.end_period}`:'Speichere unter Planung einen Budgetentwurf.';
  values.hidden=!plan;
  if(plan){
    $('overview-plan-ending').textContent=plan.expected_end===null?'—':eur(plan.expected_end);
    $('overview-plan-minimum').textContent=plan.minimum_balance===null?'—':`${eur(plan.minimum_balance)} · ${plan.minimum_period}`;
    $('overview-plan-critical').textContent=plan.connected?(plan.first_negative_period||'Keiner im Zeitraum'):'—';
    $('overview-plan-change').textContent=eur(plan.total_change);
    $('overview-plan-unconfirmed-income').textContent=plan.unconfirmed_income===null?'—':eur(plan.unconfirmed_income);
    $('overview-plan-unconfirmed-expenses').textContent=plan.unconfirmed_expenses===null?'—':eur(plan.unconfirmed_expenses);
  }
  $('overview-plan-note').textContent=plan?.connection_reason||'Noch keine gespeicherte Vorschau vorhanden.';
  const tasks=overview?.tasks||[], list=$('overview-task-list'); list.replaceChildren();
  $('overview-task-count').textContent=tasks.length===1?'1 Bereich offen':`${tasks.length} Bereiche offen`;
  if(!tasks.length){const done=document.createElement('p');done.className='overview-done';done.textContent='Für diesen Stand sind keine nächsten Schritte offen.';list.append(done);return;}
  for(const task of tasks){
    const item=document.createElement('div');item.className='overview-task';
    const text=document.createElement('div'), title=document.createElement('strong'), detail=document.createElement('span');
    title.textContent=task.label;detail.textContent=task.detail;text.append(title,detail);
    const action=document.createElement('button');action.type='button';action.className='secondary';action.textContent='Öffnen';action.dataset.target=task.target;
    item.append(text,action);list.append(item);
  }
}
function formatProfileGoalAmount(value, currency) { return new Intl.NumberFormat('de-DE',{style:'currency',currency,currencyDisplay:'code'}).format(Number(value)); }
function renderProfileGoals(goals) {
  const items=Array.isArray(goals)?goals:[], list=$('profile-goal-list');list.replaceChildren();
  $('profile-goal-count').textContent=items.length===1?'1 Ziel':`${items.length} Ziele`;
  if(!items.length){const empty=document.createElement('p');empty.className='muted profile-goals-empty';empty.textContent='Für dieses Profil sind keine finanziellen Ziele hinterlegt.';list.append(empty);return;}
  for(const goal of items){const item=document.createElement('article');item.className='profile-goal';item.setAttribute('role','listitem');const label=document.createElement('span');label.textContent=goal.label;const target=document.createElement('strong');target.textContent=formatProfileGoalAmount(goal.target_amount,goal.currency);item.append(label,target);list.append(item);}
}
function renderBackupStatus(status) {
  const node=$('backup-sync-status');
  if(status?.error){node.textContent=`Lokale Sicherung aktiv · Drive-Abgleich benötigt technische Prüfung · ${status.pending} Paket(e) warten.`;return;}
  if(!status?.configured){node.textContent=`Lokale Sicherung aktiv · ${status?.pending||0} Paket(e) warten auf ein konfiguriertes Drive-Ziel.`;return;}
  if(!status.reachable){node.textContent=`Offline arbeitsfähig · ${status.pending} Sicherungspaket(e) warten lokal auf Drive.`;return;}
  node.textContent=status.pending?`Drive erreichbar · ${status.pending} Sicherungspaket(e) werden nachsynchronisiert.`:'Lokale Sicherung und Drive-Ablage sind synchron.';
}
async function backupMaintain() {
  const status=await api('/api/backup-sync',{});
  renderBackupStatus(status);
  return status;
}
async function refresh() {
  const query=$('cutoff').value?'?as_of='+encodeURIComponent($('cutoff').value):'';
  state=await api('/api/state'+query); $('cutoff').value=state.as_of;renderAccountOwnerControls();
  $('backup-package-drive').hidden=!state.drive_backup_configured;
  renderBackupStatus(state.backup_sync);
  $('mode').textContent=state.demo?'Synthetische Demo · getrennte Daten':'Dein Finanzbestand';
  $('empty').hidden=state.accounts.length>0; $('status-date').textContent='Angezeigter Stand: '+state.as_of;
  $('liquidity').textContent=state.status?eur(state.status.liquidity):'—';
  renderProfileGoals(state.profile_goals);
  renderOverview(state.overview);
  const kinds={CHECKING:'Girokonto',SAVINGS:'Sparkonto',CREDIT_CARD:'Kreditkarte',DEPOT:'Depot'};
  $('account-rows').replaceChildren();
  $('account-config-rows').replaceChildren();
  for(const a of state.accounts){const tr=document.createElement('tr');cell(tr,a.id);cell(tr,a.display_name||'Noch nicht bezeichnet');const ownerText=a.owner==='JOINT'?'Gemeinsam · '+Object.entries(a.shares).map(([person,share])=>`${profilePersonLabel(person)} ${(Number(share)*100).toLocaleString('de-DE',{maximumFractionDigits:2})} %`).join(' / '):(a.owner_label||profilePersonLabel(a.owner));cell(tr,ownerText);cell(tr,kinds[a.kind]);cell(tr,accountBalanceLabel(a.current_balance),true);$('account-rows').append(tr);const config=document.createElement('tr');cell(config,a.id);const edit=cell(config,'');const input=document.createElement('input');input.value=a.display_name||'';input.maxLength=120;input.placeholder='z. B. Haushaltskonto';input.setAttribute('aria-label',`Bezeichnung für ${a.id}`);const save=document.createElement('button');save.type='button';save.textContent='Speichern';save.disabled=true;const status=document.createElement('span');status.className='account-save-status muted';status.setAttribute('role','status');status.textContent=accountSaveMessages.get(a.id)||'';input.addEventListener('input',()=>{save.disabled=!input.value.trim()||input.value.trim()===(a.display_name||'');status.textContent=save.disabled?'':'Ungespeichert';});save.addEventListener('click',()=>run(async()=>{const value=input.value.trim();save.disabled=true;status.textContent='Wird gespeichert …';try{await api('/api/account-display-name',{id:a.id,display_name:value,revision:a.display_name_revision,confirmed:true});accountSaveMessages.set(a.id,'Gespeichert');await refresh();message(`Bezeichnung für ${a.id} gespeichert.`);}catch(error){status.textContent='Nicht gespeichert';save.disabled=false;throw error;}}));edit.append(input,save,status);$('account-config-rows').append(config);}
  const opening=$('account-form').elements.opening_date; if(state.accounts.length) {opening.value=state.accounts[0].opening_date;opening.readOnly=true;}
  $('scenario-rows').replaceChildren();$('first').replaceChildren();$('second').replaceChildren();
  for(const s of state.scenarios){const tr=document.createElement('tr');cell(tr,s.name);cell(tr,s.as_of);cell(tr,amount(s.opening),true);cell(tr,amount(s.income),true);cell(tr,amount(s.expenses),true);const actions=cell(tr,'');
    button(actions,'Annahmen übernehmen',async()=>{const form=$('plan-form');for(const key of ['income','expenses','reserve'])form.elements[key].value=s[key];form.elements.one_offs.value=s.one_offs.map(x=>x.join('; ')).join('\n');$('scenario-name').value=s.name+' – Alternative';invalidate();message('Annahmen übernommen. Für die neue Berechnung gilt der aktuell ausgewählte Stichtag.');$('legacy-planning').open=true;cockpitNavigate('planning');});
    button(actions,'CSV',async()=>{const result=await api('/api/export-scenario',{name:s.name});download(result.download_url);});
    button(actions,'JSON',async()=>{const result=await api('/api/export-scenario',{name:s.name});download(result.snapshot_url);});
    $('scenario-rows').append(tr);for(const id of ['first','second']){const o=document.createElement('option');o.value=s.name;o.textContent=s.name;$(id).append(o);}}
  if(state.scenarios.length>1)$('second').selectedIndex=1;
  $('comparison').hidden=true;
  cockpitDataGeneration += 1;
  document.dispatchEvent(new Event('finance-refreshed'));
  await cockpitLoadArea(location.hash.slice(1) || 'plan-actual', {force: true});
}
function planInput(){const f=$('plan-form');if(!f.reportValidity())throw new Error('Bitte alle Planbeträge angeben.');const one_offs=[];for(const line of f.elements.one_offs.value.split('\n').filter(x=>x.trim())){const parts=line.split(';');if(parts.length!==2||!/^\s*\d+\s*$/.test(parts[0]))throw new Error('Einmalzahlungen: je Zeile Planmonat; Betrag.');one_offs.push({month:Number(parts[0]),amount:parts[1].trim()});}return {as_of:$('cutoff').value,plan:{income:f.elements.income.value,expenses:f.elements.expenses.value,reserve:f.elements.reserve.value,one_offs}};}
const ns='http://www.w3.org/2000/svg';
function svgNode(parent, tag, attrs, text){const node=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))node.setAttribute(k,v);if(text!==undefined)node.textContent=text;parent.append(node);return node;}
function chart(id, series, labels, reserve) {
  const svg=$(id);svg.replaceChildren();const values=series.flatMap(s=>s.values.map(Number));if(reserve!==undefined)values.push(Number(reserve));
  let lo=Math.min(0,...values),hi=Math.max(0,...values);if(lo===hi)hi=lo+1;const padding=(hi-lo)*.12;lo-=padding;hi+=padding;
  const x=i=>100+i*740/Math.max(1,labels.length-1), y=v=>220-(Number(v)-lo)/(hi-lo)*175;
  for(let i=0;i<5;i++){const v=lo+(hi-lo)*i/4;svgNode(svg,'line',{x1:100,x2:850,y1:y(v),y2:y(v),stroke:'#e2e9ed'});svgNode(svg,'text',{x:90,y:y(v)+4,'text-anchor':'end'},Math.round(v).toLocaleString('de-DE'));}
  svgNode(svg,'text',{x:18,y:22},'EUR');
  const labelStep = Math.max(2, Math.ceil((labels.length - 1) / 6));
  labels.forEach((label,i)=>{if(i===0||i===labels.length-1||(i%labelStep===0&&i<=labels.length-1-labelStep/2))svgNode(svg,'text',{x:x(i),y:248,'text-anchor':'middle'},label);});
  if(reserve!==undefined)svgNode(svg,'line',{x1:100,x2:850,y1:y(reserve),y2:y(reserve),class:'chart-reserve'});
  series.forEach((s,index)=>{svgNode(svg,'polyline',{points:s.values.map((v,i)=>`${x(i)},${y(v)}`).join(' '),class:index?'chart-line chart-alt':'chart-line'});svgNode(svg,'text',{x:120+index*340,y:22},s.name.length>38?s.name.slice(0,35)+'…':s.name);});
  if(reserve!==undefined)svgNode(svg,'text',{x:650,y:22},'Gestrichelt: Mindestreserve');
}
function renderProjection(result){$('projection').hidden=false;const rows=result.rows;$('closing').textContent=eur(rows.at(-1).liquidity);$('minimum').textContent=eur(Math.min(...rows.map(r=>Number(r.liquidity))));$('reserve-months').textContent=rows.filter(r=>r.below_reserve).length+' / 12';$('calculated-for').textContent=`Berechnet ab ${result.as_of}, Anfangsbestand ${eur(result.opening)}. Alle Tabellenwerte in EUR.`;$('forecast-rows').replaceChildren();for(const r of rows){const tr=document.createElement('tr');if(r.below_reserve)tr.className='warning';cell(tr,r.period);for(const key of ['opening','income','expenses','one_offs','cashflow','liquidity','reserve'])cell(tr,amount(r[key]),true);$('forecast-rows').append(tr);}chart('forecast-chart',[{name:'Liquidität zum Monatsende',values:rows.map(r=>r.liquidity)}],rows.map(r=>r.period),rows[0].reserve);}
$('cutoff-form').addEventListener('submit',e=>{e.preventDefault();run(async()=>{invalidate();$('comparison').hidden=true;await refresh();message('Stichtag aktualisiert.');});});
$('cutoff').addEventListener('input',invalidate);
$('account-form').elements.owner.addEventListener('change',renderAccountShareFields);
$('account-form').addEventListener('submit',e=>{e.preventDefault();run(async()=>{const values=Object.fromEntries(new FormData(e.target));if(values.owner==='JOINT')values.shares=accountSharesFromInputs();await api('/api/accounts',values);if(values.opening_date>$('cutoff').value)$('cutoff').value=values.opening_date;invalidate();await refresh();message('Konto gespeichert.');});});
$('import-form').addEventListener('submit',e=>{e.preventDefault();run(async()=>{const file=$('csv-file').files[0];if(!file||file.size>1500000)throw new Error('CSV fehlt oder überschreitet 1,5 MB.');const csv=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer());const result=await api('/api/import',{csv});invalidate();await refresh();message(`${result.inserted} neue Buchungen übernommen.`);});});
$('plan-form').addEventListener('input',invalidate);
$('plan-form').addEventListener('submit',e=>{e.preventDefault();run(async()=>{renderProjection(await api('/api/forecast',planInput()));message('Planung berechnet. Noch nicht als Szenario gespeichert.');});});
$('save-plan').addEventListener('click',()=>run(async()=>{const name=$('scenario-name').value.trim();if(!name)throw new Error('Bitte einen neuen Szenarionamen eingeben.');const result=await api('/api/save',{...planInput(),name});await refresh();renderProjection(result);message('Szenario mit Ausgangsbestand und Annahmen gespeichert.');}));
$('export-plan').addEventListener('click',()=>run(async()=>{const result=await api('/api/export',planInput());renderProjection(result);download(result.download_url);message('CSV lokal im Datenordner unter exports gespeichert; Download gestartet.');}));
$('compare-form').addEventListener('submit',e=>{e.preventDefault();run(async()=>{const first=$('first').value,second=$('second').value;const result=await api('/api/compare',{first,second});$('comparison').hidden=false;$('comparison-rows').replaceChildren();for(const r of result.rows){const tr=document.createElement('tr');cell(tr,r.month);for(const k of ['first','second','difference'])cell(tr,amount(r[k]),true);$('comparison-rows').append(tr);}chart('comparison-chart',[{name:'Basis: '+first,values:result.rows.map(r=>r.first)},{name:'Alternative: '+second,values:result.rows.map(r=>r.second)}],result.rows.map(r=>'M'+r.month));message('Gespeicherte Ergebnisse verglichen; keine neue Berechnung mit veränderten Ist-Daten.');});});
$('backup').addEventListener('click',()=>run(async()=>{const result=await api('/api/backup',{});message(`Lokale Sicherung geprüft: ${result.filename}\nCloud-Upload nicht durchgeführt.`);}));
for(const id of ['first','second'])$(id).addEventListener('change',()=>{$('comparison').hidden=true;});
$('overview-plan-open').addEventListener('click',()=>cockpitNavigate('planning'));
$('overview-primary-action-open').addEventListener('click',event=>{if(event.currentTarget.dataset.target)cockpitNavigate(event.currentTarget.dataset.target);});
$('overview-task-list').addEventListener('click',event=>{const action=event.target.closest('button[data-target]');if(action)cockpitNavigate(action.dataset.target);});
async function backupRetry(){try{const status=await backupMaintain();setTimeout(backupRetry,status.reachable&&!status.error?60000:300000);}catch(error){setTimeout(backupRetry,300000);}}
run(async()=>{await refresh();await backupRetry();});

document.addEventListener('finance-dashboard-cutoff', event => {
  const value = event.detail?.as_of;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return;
  $('cutoff').value = value;
  void run(refresh);
});

document.addEventListener('finance-bank-refreshed', () => {
  void run(refresh);
});

document.addEventListener('finance-dashboard-edit', event => {
  const key = event.detail;
  void run(async () => {
    if (typeof bookingEditorOpen !== 'function') throw new Error('Buchungsbearbeitung ist noch nicht geladen.');
    await bookingEditorOpen(key, {onSaved: async () => { await refresh(); }});
  });
});
