import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmdirSync, unlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const sourceDir = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(sourceDir, '..', 'dashboard', 'index.html'), 'utf8');
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(scriptMatch, 'Dashboard muss ein Inline-Script enthalten.');
const musicStart = scriptMatch[1].indexOf('const ownMusic = (() => {');
const musicEnd = scriptMatch[1].indexOf('\n    function showSpotifyEmbed', musicStart);
assert.ok(musicStart >= 0 && musicEnd > musicStart, 'Eigene-Musik-Code muss eindeutig isolierbar sein.');
const ownMusicSource = scriptMatch[1].slice(musicStart, musicEnd);
const spotifyRenderStart = scriptMatch[1].indexOf('function renderSpotifyResults(');
const spotifyRenderEnd = scriptMatch[1].indexOf('\n    let spotifySearchGeneration', spotifyRenderStart);
assert.ok(spotifyRenderStart >= 0 && spotifyRenderEnd > spotifyRenderStart, 'Spotify-Renderer muss eindeutig isolierbar sein.');
const spotifyRenderSource = scriptMatch[1].slice(spotifyRenderStart, spotifyRenderEnd);

function runNodeFile(source, args = []) {
  const tempDir = mkdtempSync(join(tmpdir(), 'own-music-ui-'));
  const scriptPath = join(tempDir, 'ui-check.cjs');
  try {
    writeFileSync(scriptPath, source, 'utf8');
    return spawnSync(process.execPath, [...args, scriptPath], { encoding: 'utf8', timeout: 10000 });
  } finally {
    unlinkSync(scriptPath);
    rmdirSync(tempDir);
  }
}

test('Finanzcockpit-Link nutzt öffentliche HTTPS-Adresse und neutralen Tooltip', () => {
  const link = html.match(/<a\b[^>]*>Finanzcockpit ↗<\/a>/)?.[0];
  assert.ok(link, 'Finanzcockpit-Quicklink muss vorhanden sein.');
  assert.equal(link.match(/\bhref="([^"]+)"/)?.[1], 'https://finance.pistelok.de/');
  assert.equal(link.match(/\btitle="([^"]+)"/)?.[1], 'Finanzcockpit öffnen');
  assert.doesNotMatch(link, /HAL9000|tailscale|\.ts\.net|:8785/i);
  assert.match(link, /\brel="noopener"/);
});

test('Dashboard-JavaScript besteht node --check', () => {
  const result = runNodeFile(scriptMatch[1], ['--check']);
  assert.equal(result.status, 0, result.stderr || result.stdout);
});

test('Spotify-Fremdtext bleibt Text und unsichere Bild-URLs werden verworfen', () => {
  const harness = String.raw`
const assert = require('node:assert/strict');
class Element {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.listeners={}; this.style={}; this.textContent=''; this.hidden=false; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=[]; this.append(...nodes); }
  addEventListener(name, fn) { this.listeners[name]=fn; }
}
const track=new Element('input'); const nowPlaying=new Element();
global.document={createElement:tag=>new Element(tag),getElementById:id=>id==='track'?track:nowPlaying};
global.showSpotifyEmbed=()=>{}; global.selectSpotifyTrack=()=>{}; global.log=()=>{};
${spotifyRenderSource}
const results=new Element();
renderSpotifyResults(results,[{title:'<img src=x onerror=alert(1)>',subtitle:'<svg onload=alert(1)>',uri:'spotify:track:abc',image_url:'javascript:alert(1)'}]);
assert.equal(results.children.length,1);
const [button]=results.children; const [art,meta]=button.children; const [title,subtitle]=meta.children;
assert.equal(title.textContent,'<img src=x onerror=alert(1)>');
assert.equal(subtitle.textContent,'<svg onload=alert(1)>');
assert.equal(art.children.length,0,'javascript: artwork is rejected while gradient placeholder remains');
for (const image_url of ['http://images.example/cover.jpg','data:image/svg+xml,<svg onload=alert(1)>','https://user:pass@images.example/cover.jpg']) {
  renderSpotifyResults(results,[{title:'Unsafe cover',image_url}]);
  assert.equal(results.children[0].children[0].children.length,0,'non-HTTPS or credential-bearing artwork URL is rejected');
}
renderSpotifyResults(results,[{title:'Cover',subtitle:'Artist',uri:'spotify:track:def',image_url:'https://images.example/cover.jpg'}]);
assert.equal(results.children[0].children[0].children[0].src,'https://images.example/cover.jpg');
`;
  const result = runNodeFile(harness);
  assert.equal(result.status, 0, result.stderr || result.stdout);
});

test('Eigene Musik: Pagination, POST-Sperre, Artwork-Origin und Lesemodus', () => {
  const harness = String.raw`
const assert = require('node:assert/strict');
class Element {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.listeners={}; this.style={}; this.attributes={}; this.textContent=''; this.hidden=false; this.disabled=false; this.value=''; }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  append(...nodes) { for (const node of nodes) { node.parent=this; this.children.push(node); if (this.tag==='select' && this.children.length===1) this.value=node.value; } }
  replaceChildren(...nodes) { this.children=[]; if (this.tag==='select') this.value=''; this.append(...nodes); }
  setAttribute(name, value) { this.attributes[name]=String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  remove() { if (this.parent) this.parent.children=this.parent.children.filter(node => node !== this); }
  querySelectorAll(selector) { return selector === '.music-result' ? this.children.filter(node => node.className === 'music-result') : []; }
  get options() { return this.children; }
  get selectedOptions() { const option=this.children.find(item => item.value===this.value); return option ? [option] : []; }
  fire(name, event={}) { return this.listeners[name]?.[0]?.(event); }
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function createRig(allowPlayback, artworkPresent, overrides={}) {
  const ids='musicAvailability musicPlayer musicResults musicMessage musicMore musicPlay musicAdd musicToggle musicPrevious musicNext musicStop musicSeek musicElapsed musicDuration musicCurrentArt musicCurrentTitle musicCurrentSubtitle musicLiveStatus musicQueue musicLibrary musicSearchButton musicQuery musicPlayerHint musicSelected musicSelectedArt musicSelectedTitle musicSelectedSubtitle musicSearchForm'.split(' ');
  const elements=Object.fromEntries(ids.map(id=>[id,new Element(id==='musicPlayer'?'select':'div')]));
  elements.musicSelectedArt.hidden=true; elements.musicMore.hidden=true;
  const calls=[]; let resolvePost;
  const fetch=async (url, options={}) => {
    calls.push({url,options});
    if (url==='/api/music/status') return overrides.statusResponse || {ok:true,json:async()=>({configured:true,available:true,allow_playback:allowPlayback})};
    if (url==='/api/music/players') {
      if (overrides.playerFailure) throw new Error('synthetischer API-Ausfall');
      return overrides.playerResponse || {ok:true,json:async()=>({players:[{id:'player-1',name:'Testplayer',available:true}]})};
    }
    if (url.startsWith('/api/music/queue?')) {
      if (overrides.queueFailure) throw new Error('Warteschlange offline');
      if (overrides.queueFetch) return overrides.queueFetch(url, options);
      const playerId = new URL(url,'http://local').searchParams.get('player_id');
      return {ok:true,json:async()=>overrides.queuePayload || ({player_id:playerId,queue_id:playerId,active:true,state:'playing',own_music:true,items:2,current_index:0,elapsed_time:14,current_track:{title:'Aktueller Titel',artist:'Aktueller Interpret',album:'Aktuelles Album',duration:120,uri:'library:track/current',artwork_url:'https://example.invalid/cover.jpg'},tracks:[{title:'Aktueller Titel',artist:'Aktueller Interpret',index:0},{title:'Nächster Titel',index:1}],controls:{pause:true,resume:false,stop:true,previous:false,next:true,seek:true}})};
    }
    if (url.startsWith('/api/music/tracks?')) {
      if (overrides.trackFailure) throw new Error('Musiksuche offline');
      const parsed=new URL(url,'http://local'); const offset=Number(parsed.searchParams.get('offset'));
      const track={uri:'library:track/1',title:'Treffer',artist:'Interpret',album:'Album'};
      if (artworkPresent) track.artwork_url='https://example.invalid/cover.jpg';
      return {ok:true,json:async()=>offset<2?{tracks:[],next_offset:offset+1}:{tracks:[track],next_offset:3}};
    }
    if (options.method==='POST') {
      if (overrides.postResponse) return overrides.postResponse;
      return new Promise(resolve=>{ resolvePost=()=>resolve({ok:true,json:async()=>({status:'ok'})}); });
    }
    throw new Error('Unerwarteter API-Pfad: '+url);
  };
  const document={hidden:false,listeners:{},getElementById:id=>elements[id],createElement:tag=>new Element(tag),querySelectorAll:()=>[],addEventListener(name,fn){this.listeners[name]=fn;},fire(name){this.listeners[name]?.();}};
  const log=()=>{};
  ${ownMusicSource}
  await tick(); await tick();
  return {elements,calls,document,ownMusic,finishPost:()=>resolvePost?.()};
}
(async()=>{
  const active=await createRig(true,true);
  active.elements.musicQuery.value='Original Query';
  await active.ownMusic.search(false);
  assert.equal(active.elements.musicMore.hidden,false,'next_offset hält Weitere Treffer sichtbar, auch bei leerer Seite');
  active.elements.musicQuery.value='Neue Eingabe';
  await active.ownMusic.search(true);
  const pageTwo=new URL(active.calls.filter(call=>call.url.startsWith('/api/music/tracks?'))[1].url,'http://local');
  assert.equal(pageTwo.searchParams.get('q'),'Original Query','Pagination bleibt an die aktive Suche gebunden');
  assert.equal(pageTwo.searchParams.get('offset'),'1');
  await active.ownMusic.search(true);
  const result=active.elements.musicResults.children.find(node=>node.className==='music-result');
  assert.ok(result);
  assert.equal(result.children[0].children[0].src,'/api/music/artwork?uri=library%3Atrack%2F1','beliebige artwork_url wird nie direkt geladen');
  result.fire('click');
  active.elements.musicPlay.fire('click');
  await tick();
  assert.equal(active.elements.musicPlayer.disabled,true,'Zielauswahl ist während POST gesperrt');
  assert.equal(result.disabled,true,'Titelauswahl ist während POST gesperrt');
  assert.equal(active.elements.musicPlay.disabled,true,'Aktionen sind während POST gesperrt');
  assert.equal(active.elements.musicMore.disabled,true,'Pagination ist während POST gesperrt');
  active.elements.musicPlay.fire('click');
  assert.equal(active.calls.filter(call=>call.options.method==='POST').length,1,'kein paralleler zweiter POST');
  active.finishPost(); await tick(); await tick();
  assert.equal(active.elements.musicMore.disabled,false,'Pagination wird nach POST wieder aktiv');

  const pageErrorOptions={}; const pageError=await createRig(true,false,pageErrorOptions);
  pageError.elements.musicQuery.value='Query'; await pageError.ownMusic.search(false);
  assert.equal(pageError.elements.musicMore.hidden,false);
  pageErrorOptions.trackFailure=true; await pageError.ownMusic.search(true);
  assert.equal(pageError.elements.musicMore.hidden,true,'fehlgeschlagene Seite behält keine alte Pagination');
  assert.equal(pageError.elements.musicMore.disabled,true);

  for (const result of [{status:'failed'}, {status:'ok',mode:'simulation'}, {}]) {
    const rejected=await createRig(true,false,{postResponse:{ok:true,json:async()=>result}});
    rejected.elements.musicToggle.fire('click'); await tick(); await tick();
    assert.match(rejected.elements.musicMessage.textContent,/Befehl fehlgeschlagen/,'HTTP 200 ist kein bestätigter Musikbefehl');
  }

  const readOnly=await createRig(false,false);
  assert.equal(readOnly.elements.musicPlay.disabled,true,'allow_playback=false sperrt Play');
  assert.equal(readOnly.elements.musicToggle.disabled,true,'allow_playback=false sperrt Steueraktionen');
  assert.equal(readOnly.elements.musicSearchButton.disabled,false,'Suche bleibt im Lesemodus verfügbar');
  readOnly.elements.musicQuery.value='Lesbarer Titel';
  await readOnly.ownMusic.search(false);
  await readOnly.ownMusic.search(true); await readOnly.ownMusic.search(true);
  const noCover=readOnly.elements.musicResults.children.find(node=>node.className==='music-result');
  assert.ok(noCover.children[0],'Cover-Placeholder bleibt im Layout erhalten');
  assert.equal(noCover.children[0].children[0].hidden,true,'fehlendes artwork_url lädt kein Bild');
  assert.equal(readOnly.calls.filter(call=>call.url.startsWith('/api/music/tracks?')).length,3,'Lesesuche und Pagination bleiben trotz gesperrter Aktionen nutzbar');

  const playerApiError=await createRig(true,false,{playerResponse:{ok:false,json:async()=>({error:'MA timeout'})}});
  assert.equal(playerApiError.elements.musicPlayer.options[0].textContent,'Playerliste nicht abrufbar','API-Fehler darf nicht als leere Liste erscheinen');
  assert.doesNotMatch(playerApiError.elements.musicPlayer.options[0].textContent,/Kein Player verfügbar/);
  assert.equal(playerApiError.elements.musicPlayer.disabled,true,'ohne bestätigte Player bleibt die Zielauswahl gesperrt');
  assert.equal(playerApiError.elements.musicPlay.disabled,true,'API-Fehler darf Wiedergabe nicht freigeben');

  const playerNetworkError=await createRig(true,false,{playerFailure:true});
  assert.equal(playerNetworkError.elements.musicPlayer.options[0].textContent,'Playerliste nicht abrufbar','Netzwerkfehler darf nicht als leere Liste erscheinen');
  assert.equal(playerNetworkError.elements.musicPlayer.disabled,true);

  const malformedPlayers=await createRig(true,false,{playerResponse:{ok:true,json:async()=>({players:'keine-liste'})}});
  assert.equal(malformedPlayers.elements.musicPlayer.options[0].textContent,'Playerliste nicht abrufbar','malformed payload ist ein Abfragefehler');
  assert.equal(malformedPlayers.elements.musicPlayer.disabled,true);

  const emptyPlayers=await createRig(true,false,{playerResponse:{ok:true,json:async()=>({players:[]})}});
  assert.equal(emptyPlayers.elements.musicPlayer.options[0].textContent,'Kein Player verfügbar','nur eine erfolgreiche leere Antwort bedeutet keine Player');
  assert.equal(emptyPlayers.elements.musicPlayer.disabled,true);

  const unconfigured=await createRig(true,false,{statusResponse:{ok:true,json:async()=>({configured:false,available:false,error:'Nicht konfiguriert'})}});
  assert.equal(unconfigured.elements.musicPlayer.options[0].textContent,'Musikdienst nicht verfügbar','nicht konfiguriert ist keine bestätigte leere Playerliste');
  assert.equal(unconfigured.elements.musicPlayer.disabled,true);
  assert.equal(unconfigured.calls.some(call=>call.url==='/api/music/players'),false,'nicht konfigurierte Instanz fragt keine Player ab');

  const unavailable=await createRig(true,false,{statusResponse:{ok:true,json:async()=>({configured:true,available:false,error:'MA offline'})}});
  assert.equal(unavailable.elements.musicPlayer.options[0].textContent,'Musikdienst nicht verfügbar','Status-Ausfall ist keine bestätigte leere Playerliste');
  assert.equal(unavailable.elements.musicPlayer.disabled,true);
  assert.equal(unavailable.calls.some(call=>call.url==='/api/music/players'),false,'nicht verfügbarer Dienst fragt keine Player ab');

  const invalidPlayerEntry=await createRig(true,false,{playerResponse:{ok:true,json:async()=>({players:[{id:'player-2',name:'Unbekannt',available:'true'}]})}});
  assert.equal(invalidPlayerEntry.elements.musicPlayer.options[0].textContent,'Playerliste nicht abrufbar','ungültiger Datensatz wird nicht als geladene leere Liste dargestellt');
  const invalidPlayerCases=[];
  for (const player of [
    {id:'',name:'Ohne ID',available:true},
    {id:'player-3',name:'',available:true},
    {id:'player-4',name:'Ohne Availability',available:null},
  ]) {
    const rig=await createRig(true,false,{playerResponse:{ok:true,json:async()=>({players:[player]})}});
    assert.equal(rig.elements.musicPlayer.options[0].textContent,'Playerliste nicht abrufbar','ungültige id/name/available werden als Payload-Fehler behandelt');
    invalidPlayerCases.push(rig);
  }

  const strictPlayback=await createRig(true,false,{statusResponse:{ok:true,json:async()=>({configured:true,available:true})}});
  assert.equal(strictPlayback.elements.musicToggle.disabled,true,'fehlendes allow_playback darf nicht standardmäßig freigeben');

  const unavailablePlayers=await createRig(true,false,{playerResponse:{ok:true,json:async()=>({players:[
    {id:'offline-1',name:'Echo Küche',available:false},
    {id:'offline-2',name:'Echo Büro',available:false},
  ]})}});
  assert.equal(unavailablePlayers.elements.musicPlayer.options.length,2,'gemeldete Offline-Player bleiben sichtbar');
  assert.ok(unavailablePlayers.elements.musicPlayer.options.every(option=>option.disabled),'alle nicht verfügbaren Player sind deaktiviert');

  const noSelection=await createRig(true,false);
  noSelection.elements.musicPlayer.value='not-a-player';
  noSelection.elements.musicPlayer.fire('change');
  assert.deepEqual(noSelection.elements.musicPlayer.selectedOptions,[],'nicht passende Auswahl hat kein selectedOptions-Fallback');

  function assertNoTargetActions(rig, label) {
    for (const id of ['musicPlay','musicAdd','musicToggle','musicPrevious','musicNext','musicStop','musicSeek']) {
      assert.equal(rig.elements[id].disabled,true,label + ': ' + id + ' gesperrt');
      rig.elements[id].fire('click');
    }
    assert.equal(rig.calls.filter(call=>call.options.method==='POST').length,0,label + ': kein Wiedergabe-POST');
  }
  for (const [label,rig] of [
    ['HTTP-Fehler',playerApiError], ['Netzwerkfehler',playerNetworkError],
    ['ungültige Payload',malformedPlayers], ['leere Liste',emptyPlayers],
    ['nicht konfiguriert',unconfigured], ['nicht verfügbar',unavailable],
    ['ungültiger Player',invalidPlayerEntry], ...invalidPlayerCases.map((rig,index)=>['ungültiger Player '+index,rig]),
    ['fehlende Playback-Freigabe',strictPlayback], ['keine Auswahl',noSelection],
  ]) assertNoTargetActions(rig,label);
  assertNoTargetActions(unavailablePlayers,'alle Player nicht verfügbar');
  assert.equal(playerApiError.elements.musicSearchButton.disabled,false,'bei erfolgreichem Status bleibt Bibliothekssuche trotz Playerlistenfehler verfügbar');

  assert.equal(active.elements.musicCurrentTitle.textContent,'Aktueller Titel','aktuelle Wiedergabe kommt aus gelesener Queue');
  assert.equal(active.elements.musicSelectedTitle.textContent,'Treffer','Titelauswahl ist unabhängig von aktueller Wiedergabe');
  assert.equal(active.elements.musicToggle.textContent,'Ⅱ Pause');
  assert.equal(active.elements.musicPrevious.disabled,true,'fehlende previous-Freigabe sperrt Zurück');
  assert.equal(active.elements.musicNext.disabled,false);
  assert.equal(active.elements.musicSeek.disabled,false);
  assert.equal(active.elements.musicElapsed.textContent,'0:14');
  assert.equal(active.elements.musicDuration.textContent,'2:00');
  assert.equal(active.elements.musicQueue.children[0].attributes['aria-current'],'true');
  const playPayload=JSON.parse(active.calls.find(call=>call.url==='/api/music/play').options.body);
  assert.equal(playPayload.option,'replace','Start ersetzt die Warteschlange explizit');
  await active.ownMusic.search(false);
  assert.equal(active.elements.musicCurrentTitle.textContent,'Aktueller Titel','neue Suche verändert keine beobachtete Wiedergabe');

  const browse=await createRig(true,false);
  browse.elements.musicLibrary.fire('click'); await tick(); await tick();
  const browseUrl=new URL(browse.calls.find(call=>call.url.startsWith('/api/music/tracks?')).url,'http://local');
  assert.equal(browseUrl.searchParams.get('q'),'','Bibliothek darf ohne Suchtext geladen werden');
  assert.equal(browseUrl.searchParams.get('limit'),'50');
  await browse.ownMusic.search(true); await browse.ownMusic.search(true);
  browse.elements.musicResults.children.find(node=>node.className==='music-result').fire('click');
  browse.elements.musicAdd.fire('click'); await tick();
  assert.equal(JSON.parse(browse.calls.find(call=>call.url==='/api/music/play').options.body).option,'add','Queue-Hinzufügen nutzt add');
  browse.finishPost(); await tick(); await tick();
  browse.elements.musicSeek.fire('pointerdown'); browse.elements.musicSeek.value='42'; browse.elements.musicSeek.fire('change'); await tick();
  assert.deepEqual(JSON.parse(browse.calls.filter(call=>call.options.method==='POST').at(-1).options.body),{player_id:'player-1',command:'seek',position:42});
  browse.finishPost(); await tick(); await tick();

  const paused=await createRig(true,false,{queuePayload:{player_id:'player-1',queue_id:'player-1',active:true,state:'paused',own_music:true,items:1,current_index:0,elapsed_time:5,current_track:{title:'Pause',duration:100},tracks:[{title:'Pause',index:0}],controls:{pause:false,resume:true,stop:true,previous:false,next:false,seek:false}}});
  assert.equal(paused.elements.musicToggle.textContent,'▶ Fortsetzen');
  assert.equal(paused.elements.musicToggle.disabled,false);
  assert.equal(paused.elements.musicSeek.disabled,true,'seek wird nur bei expliziter Freigabe angeboten');
  paused.elements.musicToggle.fire('click'); await tick();
  assert.equal(JSON.parse(paused.calls.find(call=>call.options.method==='POST').options.body).command,'resume');
  paused.finishPost(); await tick(); await tick();
  assert.match(paused.elements.musicMessage.textContent,/Befehl angenommen/,'Befehlserfolg behauptet keine tatsächliche Wiedergabe');
  assert.equal(paused.elements.musicCurrentTitle.textContent,'Pause','Befehlserfolg ersetzt beobachteten Titel nicht');

  const queueFailureOptions={}; const formerlyPlaying=await createRig(true,false,queueFailureOptions);
  assert.equal(formerlyPlaying.elements.musicCurrentTitle.textContent,'Aktueller Titel');
  queueFailureOptions.queueFailure=true; await formerlyPlaying.ownMusic.refresh();
  assert.equal(formerlyPlaying.elements.musicCurrentTitle.textContent,'Keine bestätigte Wiedergabe','Fehler nach erfolgreichem Lesen löscht alte Wiedergabe');
  assert.equal(formerlyPlaying.elements.musicCurrentArt.hidden,true);
  assert.equal(formerlyPlaying.elements.musicStop.disabled,true);
  const otherSource=await createRig(true,false,{queuePayload:{player_id:'player-1',queue_id:'player-1',active:true,state:'playing',own_music:false,items:1,current_index:0,elapsed_time:5,current_track:{title:'Andere Quelle',duration:100},tracks:[{title:'Andere Quelle',index:0}],controls:{pause:true,resume:false,stop:true,previous:true,next:true,seek:true}}});
  for(const id of ['musicToggle','musicPrevious','musicNext','musicStop','musicSeek']) {
    assert.equal(otherSource.elements[id].disabled,true,'andere Musikquelle sperrt '+id);
    otherSource.elements[id].fire(id==='musicSeek'?'change':'click');
  }
  assert.equal(otherSource.calls.filter(call=>call.options.method==='POST').length,0,'eigene Musik verändert keine fremde Wiedergabequelle');
  assert.match(otherSource.elements.musicLiveStatus.textContent,/Andere Musikquelle aktiv/,'beobachtete andere aktive Quelle darf benannt werden');
  const emptyIdle=await createRig(true,false,{queuePayload:{player_id:'player-1',queue_id:null,active:false,state:'idle',own_music:false,items:0,current_index:null,elapsed_time:0,current_track:null,tracks:[],controls:{pause:false,resume:false,stop:false,previous:false,next:false,seek:false}}});
  assert.equal(emptyIdle.elements.musicLiveStatus.textContent,'Keine eigene Wiedergabe','leere idle Queue behauptet keine andere aktive Quelle');
  const unknown=await createRig(true,false,{queuePayload:{player_id:'player-1',queue_id:'player-1',active:true,state:'unknown',own_music:true,items:1,current_index:0,elapsed_time:5,current_track:{title:'Unbekannter Zustand',duration:100,uri:'library:track/unknown'},tracks:[{title:'Unbekannter Zustand',index:0}],controls:{pause:true,resume:true,stop:true,previous:true,next:true,seek:true}}});
  assert.equal(unknown.elements.musicLiveStatus.textContent,'Status unbekannt','unknown ist kein gestoppter Zustand');
  assert.equal(unknown.elements.musicToggle.textContent,'Wiedergabe','unbekannter Zustand behauptet keine Pause oder Fortsetzung');
  for(const id of ['musicToggle','musicPrevious','musicNext','musicStop','musicSeek']) {
    assert.equal(unknown.elements[id].disabled,true,'unknown sperrt '+id);
    unknown.elements[id].fire(id==='musicSeek'?'change':'click');
  }
  assert.equal(unknown.calls.filter(call=>call.options.method==='POST').length,0,'unknown verhindert Transportbefehle auch bei ungültiger Freigabe');

  const idle=await createRig(true,false,{queuePayload:{player_id:'player-1',queue_id:'player-1',active:true,state:'idle',own_music:true,items:1,current_index:0,elapsed_time:5,current_track:{title:'Gestoppt',duration:100,uri:'library:track/stopped'},tracks:[{title:'Gestoppt',index:0}],controls:{pause:false,resume:true,stop:false,previous:false,next:false,seek:false}}});
  assert.equal(idle.elements.musicToggle.textContent,'▶ Fortsetzen','beobachtete resume-Freigabe gilt auch bei gestoppter Queue');
  assert.equal(idle.elements.musicToggle.disabled,false);
  idle.elements.musicToggle.fire('click'); await tick();
  assert.equal(JSON.parse(idle.calls.find(call=>call.options.method==='POST').options.body).command,'resume');
  idle.finishPost(); await tick(); await tick();

  const changingTrackOptions={}; const changingTrack=await createRig(true,false,changingTrackOptions);
  changingTrack.elements.musicSeek.fire('pointerdown'); changingTrack.elements.musicSeek.value='42';
  changingTrackOptions.queuePayload={player_id:'player-1',queue_id:'player-1',active:true,state:'playing',own_music:true,items:1,current_index:0,elapsed_time:5,current_track:{title:'Neuer Titel',duration:100,uri:'library:track/new'},tracks:[{title:'Neuer Titel',index:0}],controls:{pause:true,resume:false,stop:true,previous:false,next:false,seek:true}};
  await changingTrack.ownMusic.refresh();
  changingTrack.elements.musicSeek.fire('change'); await tick();
  assert.equal(changingTrack.calls.filter(call=>call.options.method==='POST').length,0,'Positionswahl des alten Titels wird nicht auf neuen Titel übertragen');
  assert.match(changingTrack.elements.musicMessage.textContent,/erneut wählen/);
  const beforeVisibility=changingTrack.calls.filter(call=>call.url.startsWith('/api/music/queue?')).length;
  changingTrack.document.hidden=true; changingTrack.document.fire('visibilitychange');
  changingTrack.document.hidden=false; changingTrack.document.fire('visibilitychange');
  assert.equal(changingTrack.elements.musicToggle.disabled,true,'Rückkehr zur Ansicht entfernt alte Bedienfreigabe sofort');
  await tick(); await tick();
  assert.equal(changingTrack.calls.filter(call=>call.url.startsWith('/api/music/queue?')).length,beforeVisibility+1,'Rückkehr zur Ansicht liest aktuellen Zustand neu');

  const queueError=await createRig(true,false,{queueFailure:true});
  assert.equal(queueError.elements.musicCurrentTitle.textContent,'Keine bestätigte Wiedergabe');
  assert.match(queueError.elements.musicLiveStatus.textContent,/nicht abrufbar/);
  for(const id of ['musicToggle','musicPrevious','musicNext','musicStop','musicSeek']) assert.equal(queueError.elements[id].disabled,true,'Queuefehler sperrt '+id);
  const malformedQueue=await createRig(true,false,{queuePayload:{player_id:'player-1',state:'playing',controls:{pause:true}}});
  assert.equal(malformedQueue.elements.musicToggle.disabled,true,'ungültige Statusantwort schaltet keine Bedienung frei');

  let resolveOldQueue; let queueRequests=0;
  const changed=await createRig(true,false,{playerResponse:{ok:true,json:async()=>({players:[{id:'player-1',name:'Raum Eins',room:'Raum Eins',available:true},{id:'player-2',name:'Raum Zwei',room:'Raum Zwei',available:true}]})},queueFetch:(url)=>{
    queueRequests+=1;
    if(queueRequests===1) return new Promise(resolve=>{resolveOldQueue=resolve;});
    return Promise.resolve({ok:true,json:async()=>({player_id:'player-2',queue_id:'player-2',active:true,state:'idle',own_music:true,items:0,current_index:null,elapsed_time:0,current_track:null,tracks:[],controls:{pause:false,resume:false,stop:false,previous:false,next:false,seek:false}})});
  }});
  assert.equal(changed.elements.musicPlayer.options[0].textContent,'Raum Eins','Raumname wird nicht doppelt angezeigt');
  changed.elements.musicPlayer.value='player-2'; changed.elements.musicPlayer.fire('change');
  assert.equal(queueRequests,1,'Zielwechsel startet keinen überlappenden Queue-Request');
  assert.equal(changed.elements.musicToggle.disabled,true,'alte Bedienung wird bei Zielwechsel sofort gesperrt');
  resolveOldQueue({ok:true,json:async()=>({player_id:'player-1'})}); await tick(); await tick();
  assert.equal(queueRequests,2,'nach altem Request wird das neue Ziel gelesen');
  assert.equal(changed.elements.musicCurrentTitle.textContent,'Kein aktueller Titel','späte Antwort des alten Ziels überschreibt neuen Zustand nicht');
  assert.doesNotMatch(changed.elements.musicLiveStatus.textContent,/nicht abrufbar/,'alte ungültige Antwort wird ignoriert');
})().catch(error=>{console.error(error);process.exitCode=1;});
`;
  const result = runNodeFile(harness);
  assert.equal(result.status, 0, result.stderr || result.stdout);
});
