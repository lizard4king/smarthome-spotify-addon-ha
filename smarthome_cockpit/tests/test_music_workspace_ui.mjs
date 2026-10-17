import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import vm from 'node:vm';
import test from 'node:test';

const directory = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(directory, '../dashboard/index.html'), 'utf8');
const facade = readFileSync(join(directory, '../dashboard/music-workspace.js'), 'utf8');
const inline = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const tick = () => new Promise(resolve => setImmediate(resolve));

class Element {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.attributes={}; this.listeners={}; this.dataset={}; this.style={}; this.textContent=''; this.value=''; this.hidden=false; this.disabled=false; this.checked=false; }
  get parentElement() { return this.parent; }
  get className() { return this.attributes.class || ''; }
  set className(value) { this.attributes.class=value; }
  get classList() { return {toggle:(name,on)=>{const set=new Set(this.className.split(/\s+/).filter(Boolean));on?set.add(name):set.delete(name);this.className=[...set].join(' ');}}; }
  matches(selector) {
    if(selector.startsWith('.'))return this.className.split(/\s+/).includes(selector.slice(1));
    if(selector.startsWith('[')) { const [,key,value]=selector.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/)||[];return key in this.attributes && (value===undefined||this.attributes[key]===value); }
    return this.tag===selector;
  }
  querySelectorAll(selector) { const choices=selector.split(',').map(x=>x.trim());const found=[];const walk=node=>{for(const child of node.children){if(choices.some(choice=>child.matches(choice)))found.push(child);walk(child);}};walk(this);return found; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { for(let node=this;node;node=node.parent)if(node.matches(selector))return node;return null; }
  append(...nodes) { for(const node of nodes){node.remove();node.parent=this;this.children.push(node);if(this.tag==='select'&&this.children.length===1)this.value=node.value;} }
  prepend(node) { node.remove();node.parent=this;this.children.unshift(node); }
  before(node) { node.remove();const index=this.parent.children.indexOf(this);node.parent=this.parent;this.parent.children.splice(index,0,node); }
  remove() { if(this.parent)this.parent.children=this.parent.children.filter(node=>node!==this);this.parent=null; }
  replaceChildren(...nodes) { for(const node of this.children)node.parent=null;this.children=[];if(this.tag==='select')this.value='';this.append(...nodes); }
  setAttribute(key,value) { this.attributes[key]=String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  removeAttribute(key) { delete this.attributes[key]; }
  get options() { return this.children; }
  get selectedOptions() { return this.children.filter(node=>node.value===this.value); }
  set innerHTML(markup) { this.replaceChildren();parse(markup,this); }
  addEventListener(type,listener) { (this.listeners[type] ||= []).push(listener); }
  dispatchEvent(event) { for(const listener of this.listeners[event.type]||[])listener(event); }
  fire(type) { if(type==='click'&&this.disabled)return;return Promise.all((this.listeners[type]||[]).map(fn=>fn({type,target:this,currentTarget:this,preventDefault(){},stopPropagation(){}}))); }
  click() { return this.fire('click'); }
  focus() { this.focused=(this.focused||0)+1; }
}
function parse(markup, root=new Element('main')) {
  const stack=[root];
  for(const token of markup.match(/<[^>]+>|[^<]+/g)||[]) {
    if(token.startsWith('</')) { if(stack.length>1)stack.pop();continue; }
    if(!token.startsWith('<')) { stack.at(-1).textContent+=token.trim();continue; }
    const tag=token.match(/^<([a-z0-9]+)/i)?.[1];if(!tag)continue;
    const node=new Element(tag);
    for(const match of token.matchAll(/\s([a-zA-Z-]+)(?:="([^"]*)")?/g)) {
      const key=match[1], value=match[2]||'';node.attributes[key]=value;
      if(key.startsWith('data-'))node.dataset[key.slice(5).replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase())]=value;
      if(key==='value')node.value=value;
      if(key==='disabled')node.disabled=true;
      if(key==='hidden')node.hidden=true;
    }
    stack.at(-1).append(node);
    if(!['input','img','br','meta','link'].includes(tag))stack.push(node);
  }
  return root;
}
const publicStatus = () => ({available:true, profiles:[
  {profile_id:'opaque_mine',display_name:'Mein Spotify',status:'idle',targets:['office','living'],available_targets:['office','living'],active_target_id:null,is_playing:false,track:null},
  {profile_id:'opaque_erlene',display_name:'Erlene Spotify',status:'idle',targets:['office','living'],available_targets:['office','living'],active_target_id:null,is_playing:false,track:null},
], targets:[{target_id:'office',display_name:'Büro'}, {target_id:'living',display_name:'Wohnzimmer'}]});

async function rig({libraryAvailable=true}={}) {
  const root=parse(html.slice(html.indexOf('<main '),html.indexOf('<script>')));
  const document={hidden:true,querySelectorAll:selector=>root.querySelectorAll(selector),
    querySelector:selector=>root.querySelector(selector),createElement:tag=>new Element(tag),
    getElementById(id) {const walk=node=>node.attributes.id===id?node:node.children.map(walk).find(Boolean);return walk(root)||null;}};
  const calls=[],targets=[],scrolls=[],timers=new Map();let sequence=0;
  let status=publicStatus(),searchResponder=null,libraryOptions;
  const queue=player=>({player_id:player,state:'idle',active:true,own_music:false,items:0,tracks:[],current_track:null,elapsed_time:0,current_index:null,
    controls:Object.fromEntries(['pause','resume','stop','previous','next','seek'].map(name=>[name,false]))});
  const context=vm.createContext({document,AbortController,Event,URL,console,
    scrollTo:value=>scrolls.push(value),
    navigator:{clipboard:{writeText:async()=>{}}},
    setTimeout:(fn,delay)=>{timers.set(++sequence,{fn,delay});return sequence;},clearTimeout:id=>timers.delete(id),
    initMusicLibrary:libraryAvailable ? (container,options)=>{libraryOptions=options;return {ready:Promise.resolve(),setPlaybackTarget:target=>targets.push(target),destroy(){throw new Error('navigation destroyed audio');}};} : undefined,
    fetch:async(path,options={})=>{
      calls.push({path,options});let payload;
      if(path==='/api/capabilities')payload={mode:'live',home_assistant:true};
      else if(path==='/api/ha/entities')payload={entities:[]};
      else if(path==='/api/music/status')payload={configured:true,available:true,allow_playback:true};
      else if(path==='/api/music/players')payload={players:[{id:'speaker',name:'Büro',available:true}]};
      else if(path.startsWith('/api/music/queue?'))payload=queue('speaker');
      else if(path==='/api/music/play')payload={status:'ok',mode:'live'};
      else if(path==='/api/spotify/status')payload=status;
      else if(path==='/api/spotify')payload={status:'accepted',assignments:JSON.parse(options.body).assignments.map(item=>({...item,status:'accepted'}))};
      else if(path==='/api/spotify/search') {
        if(searchResponder)return searchResponder(options);
        payload={results:[]};
      } else throw new Error('Unexpected endpoint');
      return {ok:true,json:async()=>payload};
    }});
  vm.runInContext(facade,context);vm.runInContext(inline,context);
  await tick();await tick();
  const controller=vm.runInContext('spotifyController',context);
  controller.applyStatus(status);
  return {context,root,document,calls,targets,scrolls,timers,controller,workspace:context.musicWorkspace,
    library:()=>libraryOptions,byId:id=>document.getElementById(id),
    setStatus(value){status=value;controller.applyStatus(value);},
    setSearch(fn){searchResponder=fn;}, async debounce(){const jobs=[...timers.values()].filter(job=>job.delay===300);for(const job of jobs)await job.fn();await tick();}};
}

test('Start zeigt genau drei Quellen, Zuhause bleibt erhalten und Navigation sendet keine Wiedergabe', async()=>{
  const r=await rig();assert.equal(r.document.querySelectorAll('[data-music-source]').length,3);
  assert.equal(r.byId('musicSources').hidden,false);
  assert.equal(r.byId('workspaceTitle').focused,undefined,'no focus frame on initial load');
  assert.equal(r.scrolls.length,0);
  for(const id of ['spotifyWorkspace','ownMusicWorkspace','homeWorkspace'])assert.equal(r.byId(id).hidden,true);
  for(const source of ['erlene','mine','own','home',null])r.workspace.open(source);
  assert.equal(r.scrolls.length,5);assert.ok(r.scrolls.every(scroll=>scroll.top===0));
  assert.equal(r.calls.filter(call=>call.options.method==='POST').length,0);
  assert.ok(r.byId('alexaSpeak')&&r.byId('cameraGrid')&&r.byId('systemState'));
});

test('Leere Spotify-Auswahl zeigt keinen Startblock; Ziel und Fortschritt bleiben einklappbar', async()=>{
  const r=await rig();r.workspace.open('erlene');
  assert.equal(r.byId('spotifyPlay').parentElement.hidden,true);
  const card=r.document.querySelectorAll('[data-spotify-slot]').find(node=>!node.hidden);
  assert.equal(card.querySelector('.spotify-selection').hidden,true);
  const details=card.querySelector('.spotify-device-details');
  assert.notEqual(details.open,true);assert.ok(details.querySelector('.spotify-seek'));
  assert.ok(details.querySelector('.spotify-session-target-select'));
  assert.equal(card.querySelector('.spotify-previous').textContent,'⏮');
  r.controller.selectTrack({title:'Auswahl',uri:'spotify:track:ABC'},'opaque_erlene');
  assert.equal(r.byId('spotifyPlay').parentElement.hidden,false);
  assert.equal(card.querySelector('.spotify-session-track').textContent,'Bereit: Auswahl');
});

test('Opake IDs werden über echte Profilnamen gebunden; Reihenfolge und fehlendes Konto vertauschen nichts', async()=>{
  const r=await rig();r.workspace.open('erlene');assert.equal(r.byId('spotifyProfile').value,'opaque_erlene');
  r.workspace.open('mine');assert.equal(r.byId('spotifyProfile').value,'opaque_mine');
  const reversed=publicStatus();reversed.profiles.reverse();r.setStatus(reversed);
  assert.equal(r.byId('spotifyProfile').value,'opaque_mine');
  reversed.profiles=reversed.profiles.filter(row=>row.profile_id!=='opaque_mine');r.setStatus(reversed);
  assert.equal(r.byId('spotifyProfile').value,'');assert.equal(r.byId('spotifyPlay').disabled,true);
  assert.equal(r.byId('workspaceNotice').hidden,false);
  reversed.profiles.push({...publicStatus().profiles[0],profile_id:'replacement'});r.setStatus(reversed);
  assert.equal(r.byId('spotifyProfile').value,'','pinned account is never replaced by equal display name');
});

test('Mehrdeutige Namen und fremde Zusätze werden nicht auf ein Konto geraten', async()=>{
  const r=await rig();const resolve=r.context.resolveSpotifySource;
  assert.equal(resolve([{id:'one',name:'Erlene Spotify'},{id:'two',name:'Erlene Spotify'}],'erlene'),null);
  assert.equal(resolve([{id:'one',name:'Erlene Spotify · fremd'}],'erlene'),null);
  assert.equal(resolve([{id:'one',name:'Fremdes Konto'}],'mine'),null);
});

test('Primärer Start betrifft ausschließlich geöffnete Quelle; getrennte Auswahl und Ziele bleiben erhalten', async()=>{
  const r=await rig();const track=id=>({title:id,uri:'spotify:track:'+id});
  r.workspace.open('mine');r.controller.selectTrack(track('AAA'),'opaque_mine');
  r.workspace.open('erlene');r.controller.selectTrack(track('BBB'),'opaque_erlene');
  r.controller.selectTrack(track('WRONG'),'opaque_mine');
  await r.byId('spotifyPlay').click();
  const body=JSON.parse(r.calls.find(call=>call.path==='/api/spotify').options.body);
  assert.equal(body.assignments.length,1);assert.equal(body.assignments[0].profile,'opaque_erlene');assert.equal(body.assignments[0].track,'spotify:track:BBB');
  r.workspace.open('mine');await r.byId('spotifyPlay').click();
  const second=JSON.parse(r.calls.filter(call=>call.path==='/api/spotify')[1].options.body);
  assert.equal(second.assignments[0].profile,'opaque_mine');assert.equal(second.assignments[0].track,'spotify:track:AAA');
  assert.notEqual(body.assignments[0].target,second.assignments[0].target);
});

test('Eigene Musik startet im Browserziel; Zielwechsel sendet nichts, Speakerstart benutzt vorhandene MA-Sperre', async()=>{
  const r=await rig();r.workspace.open('own');const target=r.byId('workspaceMusicTarget');
  assert.equal(target.value,'');assert.equal(r.byId('musicPlayer').value,'');
  assert.equal(r.targets.at(-1).kind,'browser');assert.equal(r.library().compact,true);
  assert.equal(r.library().playerContainer,r.byId('musicBrowserDock'));
  target.value='speaker';await target.fire('change');await tick();
  assert.equal(r.byId('musicPlayer').value,'speaker');assert.equal(r.targets.at(-1).label,'Büro');
  assert.equal(r.byId('musicBrowserDock').hidden,false,'Browserplayer remains accessible under More');
  assert.equal(r.calls.filter(call=>call.options.method==='POST').length,0);
  await r.library().onPlayTrack({title:'Titel',uri:'filesystem_local://track/song.flac'});
  const commands=r.calls.filter(call=>call.path==='/api/music/play');assert.equal(commands.length,1);
  assert.deepEqual(JSON.parse(commands[0].options.body),{uri:'filesystem_local://track/song.flac',player_id:'speaker',option:'replace'});
  target.value='';await target.fire('change');
  await r.library().onPlayTrack({title:'Falsch',uri:'filesystem_local://track/other.flac'});
  assert.equal(r.calls.filter(call=>call.path==='/api/music/play').length,1,'no speaker command from browser context');
});

test('Livevorschläge gruppieren echte Treffer; Filter und Künstlerauswahl spielen niemals automatisch', async()=>{
  const r=await rig();r.workspace.open('erlene');
  r.setSearch(async()=>({ok:true,json:async()=>({results:['artist','album','track','playlist'].map(type=>({uri:'spotify:'+type+':ABC',title:type,subtitle:'Treffer'}))})}));
  r.byId('spotifyQuery').value='A';await r.byId('spotifyQuery').fire('input');
  assert.equal(r.calls.filter(call=>call.path==='/api/spotify/search').length,0);
  await r.debounce();
  assert.deepEqual(r.byId('spotifyResults').children.map(node=>node.children[0].textContent),['Künstler','Alben','Titel','Playlists']);
  const chips=r.document.querySelectorAll('.workspace-filter-chips')[0];await chips.children[1].click();
  assert.equal(r.byId('spotifyResults').children.length,1);
  await r.byId('spotifyResults').querySelector('.spotify-result').click();
  assert.equal(r.byId('track').value,'spotify:artist:ABC');
  assert.equal(r.calls.filter(call=>call.path==='/api/spotify').length,0);
});

test('Neue Eingabe bricht alte Suche sofort ab; alte Antwort überschreibt neue Quelle nicht', async()=>{
  const r=await rig();r.workspace.open('erlene');let finish;let signal;
  r.setSearch(options=>{signal=options.signal;return new Promise(resolve=>{finish=()=>resolve({ok:true,json:async()=>({results:[{uri:'spotify:track:OLD',title:'Veraltet'}]})});});});
  r.byId('spotifyQuery').value='alt';const pending=r.byId('spotifySearch').click();await tick();
  r.byId('spotifyQuery').value='neu';await r.byId('spotifyQuery').fire('input');assert.equal(signal.aborted,true);
  r.workspace.open('mine');finish();await pending;
  assert.equal(r.byId('spotifyResults').querySelector('.spotify-result'),null);
  assert.equal(r.byId('spotifyProfile').value,'opaque_mine');
});

test('Quellenwechsel erhält laufende separate Spotify-Vorschau ohne iframe-Neuladen', async()=>{
  const r=await rig();r.workspace.open('erlene');
  r.controller.selectTrack({title:'Vorschau',uri:'spotify:track:PREVIEW'},'opaque_erlene');
  const frame=r.byId('spotifyEmbed'), original=frame.src;
  let writes=0;Object.defineProperty(frame,'src',{get:()=>original,set:()=>{writes++;}});
  for(const source of ['mine','home','own',null,'erlene'])r.workspace.open(source);
  assert.equal(writes,0);assert.equal(r.byId('spotifyEmbedWrap').hidden,false);
  assert.match(r.byId('spotifyEmbedWrap').querySelector('summary').textContent,/Separate.*Browserwiedergabe.*unabhängig/);
  assert.match(frame.attributes.title,/keinem Cockpit-Profil zugeordnet/);
});

test('Fehlende Bibliothekskomponente erhält sichtbaren Legacy-Start und vorhandene MA-Gates', async()=>{
  const r=await rig({libraryAvailable:false});r.workspace.open('own');
  assert.equal(r.byId('musicLegacyLibrary').hidden,false);
  assert.equal(r.byId('musicPlay').hidden,false);
  assert.equal(r.byId('musicPlay').parentElement.hidden,false);
  assert.equal(r.byId('musicPlay').disabled,true,'ohne Titel und Lautsprecher kein Start');
  const target=r.byId('workspaceMusicTarget');target.value='speaker';await target.fire('change');
  vm.runInContext('ownMusic.playSelectedTrack({title:"Legacy",uri:"filesystem_local://track/legacy.flac"})',r.context);
  await tick();
  assert.equal(r.byId('musicPlay').disabled,false);
  const before=r.calls.filter(call=>call.path==='/api/music/play').length;
  await r.byId('musicPlay').click();await tick();
  assert.equal(r.calls.filter(call=>call.path==='/api/music/play').length,before+1);
});
