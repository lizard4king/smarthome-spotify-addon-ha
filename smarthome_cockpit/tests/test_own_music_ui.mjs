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
const spotifyRenderStart = scriptMatch[1].indexOf('function renderSpotifyResults(results, items) {');
const spotifyRenderEnd = scriptMatch[1].indexOf('\n    document.getElementById(\'spotifySearch\')', spotifyRenderStart);
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
global.showSpotifyEmbed=()=>{}; global.log=()=>{};
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
  const ids='musicAvailability musicPlayer musicResults musicMessage musicMore musicPlay musicPause musicResume musicStop musicSearchButton musicQuery musicPlayerHint musicSelected musicSelectedArt musicSelectedTitle musicSelectedSubtitle musicSearchForm'.split(' ');
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
    if (url.startsWith('/api/music/tracks?')) {
      const parsed=new URL(url,'http://local'); const offset=Number(parsed.searchParams.get('offset'));
      const track={uri:'library:track/1',title:'Treffer',artist:'Interpret',album:'Album'};
      if (artworkPresent) track.artwork_url='https://example.invalid/cover.jpg';
      return {ok:true,json:async()=>offset<2?{tracks:[],next_offset:offset+1}:{tracks:[track],next_offset:3}};
    }
    if (options.method==='POST') return new Promise(resolve=>{ resolvePost=()=>resolve({ok:true,json:async()=>({})}); });
    throw new Error('Unerwarteter API-Pfad: '+url);
  };
  const document={getElementById:id=>elements[id],createElement:tag=>new Element(tag),querySelectorAll:()=>[]};
  const log=()=>{};
  ${ownMusicSource}
  await tick(); await tick();
  return {elements,calls,ownMusic,finishPost:()=>resolvePost?.()};
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

  const readOnly=await createRig(false,false);
  assert.equal(readOnly.elements.musicPlay.disabled,true,'allow_playback=false sperrt Play');
  assert.equal(readOnly.elements.musicPause.disabled,true,'allow_playback=false sperrt Steueraktionen');
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
  assert.equal(strictPlayback.elements.musicPause.disabled,true,'fehlendes allow_playback darf nicht standardmäßig freigeben');

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
    for (const id of ['musicPlay','musicPause','musicResume','musicStop']) {
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
})().catch(error=>{console.error(error);process.exitCode=1;});
`;
  const result = runNodeFile(harness);
  assert.equal(result.status, 0, result.stderr || result.stdout);
});
