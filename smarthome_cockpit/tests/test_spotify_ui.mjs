import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import test from 'node:test';

const sourceDir = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(sourceDir, '..', 'dashboard', 'index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const start = script.indexOf('const spotifyController = (() => {');
const end = script.indexOf('\n    const ownMusic = ', start);
assert.ok(start >= 0 && end > start);
const controllerSource = script.slice(start, end) + '\n globalThis.controller = spotifyController;';

class Element {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.listeners = {}; this.dataset = {}; this.style = {}; this.value = ''; this.textContent = ''; this.checked = false; this.disabled = false; this.nodes = {}; }
  querySelector(selector) { return this.nodes[selector] || null; }
  append(node) { this.children.push(node); if (this.tag === 'select' && this.children.length === 1) this.value = node.value; }
  replaceChildren() { this.children = []; if (this.tag === 'select') this.value = ''; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  setAttribute(name, value) { (this.attributes ||= {})[name] = String(value); }
  fire(type) { return this.listeners[type]?.(); }
}
const publicStatus = () => ({available:true, profiles:[
  {profile_id:'andreas',display_name:'Andreas',status:'idle',targets:['living','office'],available_targets:['living','office'],active_target_id:null,is_playing:false,track:null},
  {profile_id:'erlene',display_name:'Erlene Andreia',status:'idle',targets:['living','office'],available_targets:['living','office'],active_target_id:null,is_playing:false,track:null},
], targets:[
  {target_id:'living',display_name:"Andreas' Echo Show",spotify_device_name:"Andreas' Echo Show",aliases:[]},
  {target_id:'office',display_name:'Echo Büro',spotify_device_name:'Echo Büro',aliases:[]},
]});

function createRig({missingSeek=false}={}) {
  const ids = 'spotifyProfile spotifyPlay spotifyMessage spotifySearch spotifyQuery track nowPlaying'.split(' ');
  const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
  elements.spotifyProfile.value = '';
  const cards = ['1', '2'].map(slot => {
    const card = new Element(); card.dataset.spotifySlot = slot;
    card.nodes = Object.fromEntries(['.spotify-session-enabled','.spotify-session-track','.spotify-session-status','.spotify-session-target-select','.spotify-session-profile-label','.spotify-live-title','.spotify-live-artist','.spotify-live-room','.spotify-live-art','.spotify-previous','.spotify-toggle','.spotify-next','.spotify-seek-input','.spotify-progress','.spotify-duration']
      .map(selector => [selector,new Element(selector.endsWith('-select') ? 'select' : 'div')]));
    return card;
  });
  const rooms = ['living','office'].map(id => {
    const room = new Element(); room.dataset.roomTarget = id;
    room.nodes['.room-name'] = new Element(); room.nodes['.room-name'].textContent = id === 'living' ? 'Wohnzimmer' : 'Büro';
    room.nodes['.room-spotify-state'] = new Element(); return room;
  });
  const calls = []; const timers = []; const logs = [];
  let clockNow=1000000;
  class ClockDate extends Date { static now() { return clockNow; } }
  if (missingSeek) delete cards[0].nodes['.spotify-seek-input'];
  let status = publicStatus(); let nextPost = null; let postResponse = null; let controlResponse = null; const pendingControls = [];
  const context = vm.createContext({
    document: {hidden:true,getElementById:id=>elements[id],createElement:tag=>new Element(tag),
      querySelectorAll:selector=>selector==='[data-spotify-slot]'?cards:selector==='[data-room-target]'?rooms:[]},
    roomDetails:{living:{spotifyTarget:"Andreas' Echo Show"},office:{spotifyTarget:'Echo Büro'},bath:{spotifyTarget:'Echo Badezimmer'},bed:{spotifyTarget:'Echo Spot Schlafzimmer'},kitchen:{spotifyTarget:'Echo Küche'}},
    log:message=>logs.push(message), showSpotifyEmbed:()=>{}, AbortController,
    URL,
    Date:ClockDate,
    setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length;},clearTimeout:()=>{},
    fetch:async(path,options={})=>{
      calls.push({path,options});
      if (path==='/api/spotify/status') {
        if (status instanceof Error) throw status;
        const observed = status;
        return {ok:true,json:async()=>observed};
      }
      if (path==='/api/spotify') {
        if (postResponse) return postResponse;
        return new Promise(resolve=>{nextPost=()=>resolve({ok:true,json:async()=>({status:'accepted',assignments:JSON.parse(options.body).assignments.map(item=>({...item,status:'accepted',playback_verified:false}))})});});
      }
      if (path==='/api/spotify/control') {
        if (controlResponse) return controlResponse;
        return new Promise(resolve => pendingControls.push(() => resolve({ok:true,json:async()=>({status:'accepted',playback_verified:false})})));
      }
      throw new Error('Unexpected URL');
    },
  });
  vm.runInContext(controllerSource, context);
  return {controller:context.controller,elements,cards,rooms,calls,timers,logs,setStatus:value=>{status=value;},setPost:value=>{postResponse=value;},finishPost:()=>nextPost?.(),setControl:value=>{controlResponse=value;},finishControl:()=>pendingControls.shift()?.(),advance:ms=>{clockNow+=ms;}};
}
const tick = () => new Promise(resolve => setImmediate(resolve));

const controllableStatus = () => {
  const state=actualProfileStatus();
  state.profiles=state.profiles.map((profile,index)=>({...profile,status:'playing',is_playing:true,controls_available:true,seek_available:true,disallowed_actions:[],active_target_id:index?'office':'living',progress_ms:63000,track:{uri:'spotify:track:LIVE'+index,title:'Live '+index,artists:['Artist '+index],duration_ms:180000,image_url:'https://images.example/cover.jpg'}}));
  return state;
};

test('Player nutzt nur beobachtetes Ziel, hat echte Metadaten und getrennte nächste Auswahl', async () => {
  const rig=createRig(); rig.controller.applyStatus(controllableStatus());
  rig.cards[0].nodes['.spotify-session-target-select'].value='office';
  rig.controller.selectTrack({title:'Später',uri:'spotify:track:CCC'},'person_a');
  assert.equal(rig.cards[0].nodes['.spotify-live-title'].textContent,'Live 0');
  assert.equal(rig.cards[0].nodes['.spotify-live-artist'].textContent,'Artist 0');
  assert.equal(rig.cards[0].nodes['.spotify-progress'].textContent,'1:03');
  assert.equal(rig.cards[0].nodes['.spotify-duration'].textContent,'3:00');
  assert.equal(rig.cards[0].nodes['.spotify-live-art'].children[0].src,'https://images.example/cover.jpg');
  const pause=rig.controller.control('person_a','pause'); await tick();
  assert.deepEqual(JSON.parse(rig.calls[0].options.body),{profile:'person_a',target:'living',action:'pause'});
  await rig.controller.control('person_a','next');
  assert.equal(rig.calls.filter(call=>call.path==='/api/spotify/control').length,1);
  assert.equal(rig.cards[0].nodes['.spotify-toggle'].disabled,true);
  assert.equal(rig.cards[1].nodes['.spotify-toggle'].disabled,false,'anderes Profil bleibt steuerbar');
  const paused=controllableStatus(); paused.profiles[0].status='paused'; paused.profiles[0].is_playing=false;
  rig.setStatus(paused); rig.finishControl(); await pause;
  assert.equal(rig.cards[0].nodes['.spotify-toggle'].textContent,'▶ Fortsetzen');
  assert.match(rig.cards[0].nodes['.spotify-session-status'].textContent,/derselben Stelle/);
  const resume=rig.controller.control('person_a','resume'); await tick(); rig.finishControl(); await resume;
  assert.equal(JSON.parse(rig.calls.filter(call=>call.path==='/api/spotify/control')[1].options.body).action,'resume');
});

test('Seek sendet integer ms, respektiert Grenzen und Poll überschreibt keine Slider-Auswahl', async () => {
  const rig=createRig(); rig.controller.applyStatus(controllableStatus());
  const slider=rig.cards[0].nodes['.spotify-seek-input'];
  slider.value='97000'; slider.fire('input'); rig.controller.applyStatus(controllableStatus());
  assert.equal(slider.value,'97000');
  slider.fire('change'); await tick();
  assert.deepEqual(JSON.parse(rig.calls[0].options.body),{profile:'person_a',target:'living',action:'seek',position_ms:97000,track_uri:'spotify:track:LIVE0'});
  rig.finishControl(); await tick(); await tick();
  for(const ms of [-1,180000,2.5]) await rig.controller.control('person_a','seek',ms);
  assert.equal(rig.calls.filter(call=>call.path==='/api/spotify/control').length,1);
});

test('Ältere Statusfelder, unbekanntes Ziel und Disallows sperren passende Transportaktionen', async () => {
  const rig=createRig(); const state=controllableStatus();
  delete state.profiles[0].controls_available; rig.controller.applyStatus(state);
  await rig.controller.control('person_a','pause'); assert.equal(rig.calls.length,0);
  const allowed=controllableStatus(); allowed.profiles[0].disallowed_actions=['next']; allowed.profiles[0].seek_available=false;
  rig.controller.applyStatus(allowed);
  assert.equal(rig.cards[0].nodes['.spotify-next'].disabled,true);
  assert.equal(rig.cards[0].nodes['.spotify-previous'].disabled,false);
  assert.equal(rig.cards[0].nodes['.spotify-seek-input'].disabled,true);
  await rig.controller.control('person_a','next'); assert.equal(rig.calls.length,0);
  allowed.profiles[0].active_target_id='not-observed'; rig.controller.applyStatus(allowed);
  assert.equal(rig.cards[0].nodes['.spotify-toggle'].disabled,true);
});

test('Ausgefallenes anderes Profil lässt bestätigte Pause und Seek zu und zeigt Einschränkung', () => {
  const rig=createRig(); const state=controllableStatus();
  state.profiles[1].status='unavailable'; state.profiles[1].controls_available=false;
  state.profiles[0].disallowed_actions=['resume','next','previous'];
  state.profiles[0].degradation_note='Anderes Profil nicht verfügbar · nur Stoppen und Position ändern möglich.';
  rig.controller.applyStatus(state);
  assert.equal(rig.cards[0].nodes['.spotify-toggle'].disabled,false,'Pause bleibt beim bestätigten aktiven Ziel verfügbar');
  assert.equal(rig.cards[0].nodes['.spotify-seek-input'].disabled,false);
  assert.equal(rig.cards[0].nodes['.spotify-next'].disabled,true);
  assert.equal(rig.cards[0].nodes['.spotify-previous'].disabled,true);
  assert.match(rig.cards[0].nodes['.spotify-session-status'].textContent,/Anderes Profil nicht verfügbar/);
  state.profiles[0].status='paused'; state.profiles[0].is_playing=false; rig.controller.applyStatus(state);
  assert.equal(rig.cards[0].nodes['.spotify-toggle'].disabled,true,'Resume bleibt bis unabhängiger Profilprüfung gesperrt');
  assert.equal(rig.cards[0].nodes['.spotify-seek-input'].disabled,false);
});

test('Control wartet auf frischen GET nach altem GET; Ausfall behauptet keine Pause oder Wiedergabe', async () => {
  const rig=createRig(); rig.controller.applyStatus(controllableStatus());
  let finishOld; rig.setStatus(new Promise(resolve=>{finishOld=resolve;}));
  const old=rig.controller.refresh(); await tick();
  const control=rig.controller.control('person_a','pause'); await tick(); rig.finishControl(); await tick();
  assert.equal(rig.cards[0].nodes['.spotify-toggle'].disabled,true);
  rig.setStatus(new Error('offline')); finishOld(controllableStatus()); await old; await control;
  assert.equal(rig.calls.filter(call=>call.path==='/api/spotify/status').length,2);
  assert.equal(rig.cards[0].nodes['.spotify-toggle'].disabled,true);
  assert.match(rig.cards[0].nodes['.spotify-session-status'].textContent,/Ergebnis unklar/);
  assert.equal(rig.cards[0].nodes['.spotify-live-title'].textContent,'Keine bestätigte Wiedergabe');
});

test('Control-Ablehnung bleibt explizit unklar und Browser-Vorschau ist einklappbar', async () => {
  const rig=createRig(); rig.controller.applyStatus(controllableStatus());
  rig.setStatus(new Error('offline'));
  rig.setControl({ok:true,json:async()=>({status:'failed',error:'rejected'})});
  await rig.controller.control('person_a','next');
  assert.match(rig.cards[0].nodes['.spotify-session-status'].textContent,/Ergebnis unklar/);
  assert.match(html,/<details class="spotify-embed-wrap"/);
  assert.match(html,/min-height:48px/);
  rig.setStatus(controllableStatus()); await rig.controller.refresh();
  assert.doesNotMatch(rig.cards[0].nodes['.spotify-session-status'].textContent,/Ergebnis unklar/,'frischer gesunder Status löst alte Fehlermeldung ab');
});

test('Während Seek bleiben Cover, bisheriger Fortschritt und Slider bestehen; spätere Position bestätigt ihn', async () => {
  const rig=createRig(); rig.setStatus(controllableStatus()); rig.controller.applyStatus(controllableStatus());
  const slider=rig.cards[0].nodes['.spotify-seek-input']; slider.value='97000'; slider.fire('input'); slider.fire('change'); await tick();
  rig.controller.applyStatus(controllableStatus());
  assert.equal(slider.value,'97000');
  assert.equal(rig.cards[0].nodes['.spotify-progress'].textContent,'1:37');
  assert.equal(rig.cards[0].nodes['.spotify-live-title'].textContent,'Live 0');
  rig.finishControl(); await tick(); await tick();
  assert.match(rig.cards[0].nodes['.spotify-session-status'].textContent,/Änderung noch nicht bestätigt/);
  const observed=controllableStatus(); observed.profiles[0].progress_ms=97000;
  rig.controller.applyStatus(observed);
  assert.doesNotMatch(rig.cards[0].nodes['.spotify-session-status'].textContent,/noch nicht bestätigt/);
});

test('Track-Wechsel während Slider-Auswahl sendet keinen Seek auf den neuen Titel', async () => {
  const rig=createRig(); rig.controller.applyStatus(controllableStatus());
  const slider=rig.cards[0].nodes['.spotify-seek-input']; slider.value='97000'; slider.fire('input');
  const next=controllableStatus(); next.profiles[0].track.uri='spotify:track:NEW';
  rig.setStatus(next); rig.controller.applyStatus(next); slider.fire('change'); await tick();
  assert.equal(rig.calls.some(call=>call.path==='/api/spotify/control'),false);
  slider.value='42000'; slider.fire('input'); slider.fire('change'); await tick();
  assert.equal(JSON.parse(rig.calls.find(call=>call.path==='/api/spotify/control').options.body).track_uri,'spotify:track:NEW');
  rig.finishControl(); await tick();
});

test('Fehlender Track, Dauer oder Slider verhindern Seek ohne Ausnahme', async () => {
  for (const track of [null,{uri:'spotify:track:AAA'},{uri:'spotify:track:AAA',duration_ms:null}]) {
    const rig=createRig(); const state=controllableStatus(); state.profiles[0].track=track;
    rig.controller.applyStatus(state); await rig.controller.control('person_a','seek',1000);
    assert.equal(rig.calls.length,0);
  }
  const rig=createRig({missingSeek:true});
  rig.controller.applyStatus(controllableStatus()); await rig.controller.control('person_a','seek',1000);
  assert.equal(rig.calls.length,0);
});

test('Angenommene Pause mit noch spielendem GET bleibt unbestätigt bis beobachteter Pause', async () => {
  const rig=createRig(); rig.setStatus(controllableStatus()); rig.controller.applyStatus(controllableStatus());
  const pause=rig.controller.control('person_a','pause'); await tick(); rig.finishControl(); await pause;
  assert.match(rig.cards[0].nodes['.spotify-live-room'].textContent,/Spielt/);
  assert.match(rig.cards[0].nodes['.spotify-session-status'].textContent,/Änderung noch nicht bestätigt/);
  assert.doesNotMatch(rig.cards[0].nodes['.spotify-session-status'].textContent,/Wiedergabestatus bestätigt/);
  const state=controllableStatus(); state.profiles[0].status='paused'; state.profiles[0].is_playing=false;
  rig.controller.applyStatus(state);
  assert.match(rig.cards[0].nodes['.spotify-live-room'].textContent,/Pausiert/);
  assert.doesNotMatch(rig.cards[0].nodes['.spotify-session-status'].textContent,/noch nicht bestätigt/);
});

test('Angenommener nächster Titel bleibt unbestätigt bis Track-Identität wechselt', async () => {
  const rig=createRig(); rig.setStatus(controllableStatus()); rig.controller.applyStatus(controllableStatus());
  const next=rig.controller.control('person_a','next'); await tick(); rig.finishControl(); await next;
  assert.match(rig.cards[0].nodes['.spotify-session-status'].textContent,/Änderung noch nicht bestätigt/);
  const state=controllableStatus(); state.profiles[0].track.uri='spotify:track:NEXT'; state.profiles[0].track.title='Neuer Titel';
  rig.controller.applyStatus(state);
  assert.equal(rig.cards[0].nodes['.spotify-live-title'].textContent,'Neuer Titel');
  assert.doesNotMatch(rig.cards[0].nodes['.spotify-session-status'].textContent,/noch nicht bestätigt/);
});

test('Seek bestätigt zeitversetzte Wiedergabeposition mit kontrollierter Uhr', async () => {
  const rig=createRig(); rig.setStatus(controllableStatus()); rig.controller.applyStatus(controllableStatus());
  const seek=rig.controller.control('person_a','seek',97000); await tick(); rig.finishControl(); await seek;
  assert.match(rig.cards[0].nodes['.spotify-session-status'].textContent,/noch nicht bestätigt/);
  rig.advance(20000); const state=controllableStatus(); state.profiles[0].progress_ms=117000;
  rig.controller.applyStatus(state);
  assert.doesNotMatch(rig.cards[0].nodes['.spotify-session-status'].textContent,/noch nicht bestätigt|Änderung nicht bestätigt/);
});

test('Unveränderter Titel und Fortschritt bestätigen keinen Sprung und Meldung endet nach drei Beobachtungen', async () => {
  const rig=createRig(); rig.setStatus(controllableStatus()); rig.controller.applyStatus(controllableStatus());
  const next=rig.controller.control('person_a','next'); await tick(); rig.finishControl(); await next;
  rig.controller.applyStatus(controllableStatus());
  assert.match(rig.cards[0].nodes['.spotify-session-status'].textContent,/noch nicht bestätigt/);
  const state=controllableStatus(); state.profiles[0].degradation_note='Anderes Profil nicht verfügbar.';
  rig.controller.applyStatus(state);
  assert.doesNotMatch(rig.cards[0].nodes['.spotify-session-status'].textContent,/noch nicht bestätigt/);
  assert.match(rig.cards[0].nodes['.spotify-session-status'].textContent,/Änderung nicht bestätigt/);
  assert.match(rig.cards[0].nodes['.spotify-session-status'].textContent,/Anderes Profil nicht verfügbar/);
  rig.advance(31000); rig.controller.applyStatus(state);
  assert.doesNotMatch(rig.cards[0].nodes['.spotify-session-status'].textContent,/Änderung nicht bestätigt/);
  assert.match(rig.cards[0].nodes['.spotify-session-status'].textContent,/Anderes Profil nicht verfügbar/);
});

test('Unveränderte Pause endet nach Zeitlimit; idle endet sofort und neutraler Hinweis läuft aus', async () => {
  for (const status of ['paused','idle']) {
    const rig=createRig(); const initial=controllableStatus(); initial.profiles[0].status='paused'; initial.profiles[0].is_playing=false;
    rig.setStatus(initial); rig.controller.applyStatus(initial);
    const resume=rig.controller.control('person_a','resume'); await tick(); rig.finishControl(); await resume;
    const observed=controllableStatus(); observed.profiles[0].status=status; observed.profiles[0].is_playing=false;
    if(status==='paused') rig.advance(60001);
    rig.controller.applyStatus(observed);
    assert.doesNotMatch(rig.cards[0].nodes['.spotify-session-status'].textContent,/noch nicht bestätigt/);
    assert.match(rig.cards[0].nodes['.spotify-session-status'].textContent,/Änderung nicht bestätigt/);
    rig.advance(31000); rig.controller.applyStatus(observed);
    assert.doesNotMatch(rig.cards[0].nodes['.spotify-session-status'].textContent,/Änderung nicht bestätigt/);
  }
});

test('Zurück kann beobachteten Fortschrittsreset im selben Titel erkennen', async () => {
  const rig=createRig(); rig.setStatus(controllableStatus()); rig.controller.applyStatus(controllableStatus());
  const previous=rig.controller.control('person_a','previous'); await tick(); rig.finishControl(); await previous;
  const state=controllableStatus(); state.profiles[0].progress_ms=1000;
  rig.controller.applyStatus(state);
  assert.doesNotMatch(rig.cards[0].nodes['.spotify-session-status'].textContent,/noch nicht bestätigt|Änderung nicht bestätigt/);
});

test('Strukturierte Ablehnung bleibt Ablehnung; fehlende Unterscheidung bleibt unklar ohne Retry', async () => {
  for(const outcome of ['not_sent','unknown',undefined]) {
    const rig=createRig(); rig.setStatus(controllableStatus()); rig.controller.applyStatus(controllableStatus());
    rig.setControl({ok:false,status:409,json:async()=>({error:'provider rejected',outcome})});
    await rig.controller.control('person_a','pause');
    assert.match(rig.cards[0].nodes['.spotify-session-status'].textContent,outcome==='not_sent'?/Befehl abgelehnt:/ : /Ergebnis unklar/);
    assert.doesNotMatch(rig.cards[0].nodes['.spotify-session-status'].textContent,/nicht gesendet|nichts gesendet/);
    assert.equal(rig.calls.filter(call=>call.path==='/api/spotify/control').length,1);
    if(outcome==='not_sent') {
      const state=controllableStatus(); state.profiles[0].degradation_note='Anderes Profil nicht verfügbar.';
      rig.controller.applyStatus(state);
      assert.match(rig.cards[0].nodes['.spotify-session-status'].textContent,/Befehl abgelehnt:/);
      assert.match(rig.cards[0].nodes['.spotify-session-status'].textContent,/Anderes Profil nicht verfügbar/);
      rig.advance(10001); rig.controller.applyStatus(state);
      assert.doesNotMatch(rig.cards[0].nodes['.spotify-session-status'].textContent,/Befehl abgelehnt:/);
    }
  }
});

test('Zwei Profile steuern unabhängig; Zurück und Weiter senden genau eine Aktion je Profil', async () => {
  const rig=createRig(); rig.setStatus(controllableStatus()); rig.controller.applyStatus(controllableStatus());
  const first=rig.controller.control('person_a','previous');
  const second=rig.controller.control('person_b','next'); await tick();
  assert.deepEqual(rig.calls.filter(call=>call.path==='/api/spotify/control').map(call=>JSON.parse(call.options.body)),[
    {profile:'person_a',target:'living',action:'previous'},
    {profile:'person_b',target:'office',action:'next'},
  ]);
  rig.finishControl(); await first;
  assert.equal(rig.cards[1].nodes['.spotify-toggle'].disabled,true,'zweites Profil bleibt bis zu seiner eigenen Antwort gesperrt');
  rig.finishControl(); await second;
  assert.equal(rig.cards[0].nodes['.spotify-toggle'].disabled,false);
  assert.equal(rig.cards[1].nodes['.spotify-toggle'].disabled,false);
});

test('Räume haben nur Spotify-Status, keine redundante Spotify-Steuerung', () => {
  assert.doesNotMatch(html, /room-spotify-play|room-spotify-profile|id="pause"|id="stop"/);
  assert.match(html, /room-details\[hidden\]\s*\{\s*display:none/);
  assert.match(html, /event\.target === roomCard/);
  assert.match(html, /room-temperature-controls/);
  assert.match(html, /Spotify-Vorschau im Browser\. Die Wiedergabe hier steuert keine Alexa\./);
  assert.doesNotMatch(html, /iframe[^>]+src="https:\/\/open\.spotify\.com/);
});

test('Mobile Header-Aktionen dürfen umbrechen und lange Verbindungszustände passen in die Breite', () => {
  assert.match(html, /@media \(max-width:620px\) \{ \.header-actions \{ flex-wrap:wrap; min-width:0; \} \.header-actions \.pill \{ max-width:100%; white-space:normal; overflow-wrap:anywhere; \}/);
});

test('Zwei Profile behalten unterschiedliche Titel und Ziele; genau ein batch POST', async () => {
  const rig = createRig(); rig.controller.applyStatus(publicStatus());
  rig.controller.selectTrack({title:'Song A',subtitle:'Artist A',uri:'spotify:track:AAA'},'andreas');
  rig.controller.selectTrack({title:'Song B',subtitle:'Artist B',uri:'spotify:track:BBB'},'erlene');
  assert.equal(rig.cards[0].nodes['.spotify-session-track'].textContent,'Song A · Artist A');
  assert.equal(rig.cards[1].nodes['.spotify-session-track'].textContent,'Song B · Artist B');
  assert.notEqual(rig.cards[0].nodes['.spotify-session-target-select'].value,rig.cards[1].nodes['.spotify-session-target-select'].value);
  assert.equal(rig.elements.spotifyPlay.disabled,false);
  const play = rig.controller.play(); await tick();
  assert.equal(rig.elements.spotifyPlay.disabled,true);
  assert.ok(rig.cards.every(card=>card.nodes['.spotify-session-target-select'].disabled));
  await rig.controller.play();
  assert.equal(rig.calls.filter(call=>call.path==='/api/spotify').length,1);
  const batch = JSON.parse(rig.calls.find(call=>call.path==='/api/spotify').options.body).assignments;
  assert.deepEqual(batch,[{profile:'andreas',target:'living',track:'spotify:track:AAA'},{profile:'erlene',target:'office',track:'spotify:track:BBB'}]);
  rig.finishPost(); await play;
  assert.match(rig.elements.spotifyMessage.textContent,/Befehl angenommen/);
  assert.doesNotMatch(rig.rooms[0].nodes['.room-spotify-state'].textContent,/Spielt:/);
  assert.equal(rig.elements.spotifyPlay.disabled,false);
});

const actualProfileStatus = () => {
  const status=publicStatus();
  status.profiles[0]={...status.profiles[0],profile_id:'person_a',display_name:'Erlene Spotify'};
  status.profiles[1]={...status.profiles[1],profile_id:'person_b',display_name:'Mein Spotify'};
  return status;
};

test('Live-Profilnamen werden dynamisch angezeigt und Start sendet nur stabile IDs', async () => {
  const rig=createRig(); rig.controller.applyStatus(actualProfileStatus());
  assert.deepEqual(rig.cards.map(card=>card.nodes['.spotify-session-profile-label'].textContent),['Erlene Spotify','Mein Spotify']);
  assert.deepEqual(rig.elements.spotifyProfile.children.map(option=>option.value),['person_a','person_b']);
  assert.deepEqual(rig.elements.spotifyProfile.children.map(option=>option.textContent),['Erlene Spotify','Mein Spotify']);
  assert.equal(rig.elements.spotifyProfile.value,'person_a');
  assert.equal(rig.cards[0].nodes['.spotify-session-target-select'].attributes['aria-label'],'Spotify-Ziel für Erlene Spotify');
  assert.equal(rig.controller.searchAllowed('person_a'),true);
  assert.equal(rig.controller.searchAllowed('Andreas'),false,'kein geratenes Personenmapping');
  rig.controller.selectTrack({title:'Song A',uri:'spotify:track:AAA'},'person_a');
  rig.controller.selectTrack({title:'Song B',uri:'spotify:track:BBB'},'person_b');
  const play=rig.controller.play(); await tick();
  assert.deepEqual(JSON.parse(rig.calls[0].options.body).assignments,[
    {profile:'person_a',target:'living',track:'spotify:track:AAA'},
    {profile:'person_b',target:'office',track:'spotify:track:BBB'},
  ]);
  rig.setStatus(actualProfileStatus()); rig.finishPost(); await play;
  assert.match(rig.elements.spotifyMessage.textContent,/Erlene Spotify: Befehl angenommen/);
  assert.match(rig.elements.spotifyMessage.textContent,/Mein Spotify: Befehl angenommen/);
});

test('Profil-Umbenennung und Status-Neuordnung verschieben keine Auswahl zwischen Konten', () => {
  const rig=createRig(); const state=actualProfileStatus(); rig.controller.applyStatus(state);
  rig.controller.selectTrack({title:'Song A',uri:'spotify:track:AAA'},'person_a');
  rig.controller.selectTrack({title:'Song B',uri:'spotify:track:BBB'},'person_b');
  rig.elements.spotifyProfile.value='person_b';
  state.profiles.reverse(); state.profiles[0].display_name='Mein neuer Spotify-Name'; state.profiles[1].display_name='Erlene neu';
  rig.controller.applyStatus(state);
  assert.deepEqual(rig.cards.map(card=>card.nodes['.spotify-session-profile-label'].textContent),['Erlene neu','Mein neuer Spotify-Name']);
  assert.deepEqual(rig.cards.map(card=>card.nodes['.spotify-session-track'].textContent),['Song A','Song B']);
  assert.deepEqual(rig.cards.map(card=>card.nodes['.spotify-session-target-select'].value),['living','office']);
  assert.equal(rig.elements.spotifyProfile.value,'person_b');
  assert.equal(rig.cards[1].nodes['.spotify-session-target-select'].attributes['aria-label'],'Spotify-Ziel für Mein neuer Spotify-Name');
  assert.equal(rig.elements.spotifyPlay.disabled,false);
});

test('Entferntes Profil wird gesperrt, nicht durch ein neu hinzugekommenes Konto ersetzt', async () => {
  const rig=createRig(); const state=actualProfileStatus(); rig.controller.applyStatus(state);
  rig.controller.selectTrack({title:'Privater Titel A',uri:'spotify:track:AAA'},'person_a');
  rig.elements.spotifyProfile.value='person_a';
  state.profiles[0]={...state.profiles[0],profile_id:'person_c',display_name:'Fremdes neues Profil'};
  rig.controller.applyStatus(state); rig.controller.applyStatus(state);
  assert.equal(rig.cards[0].nodes['.spotify-session-profile-label'].textContent,'Erlene Spotify');
  assert.equal(rig.cards[0].nodes['.spotify-session-track'].textContent,'Privater Titel A');
  assert.equal(rig.cards[0].nodes['.spotify-session-enabled'].checked,false);
  assert.equal(rig.cards[0].nodes['.spotify-session-enabled'].disabled,true);
  assert.match(rig.cards[0].nodes['.spotify-session-status'].textContent,/keinem anderen Konto/);
  assert.equal(rig.elements.spotifyProfile.value,'person_a','Suchprofil wechselt nicht stillschweigend');
  assert.equal(rig.controller.searchAllowed('person_a'),false);
  assert.equal(rig.controller.searchAllowed('person_c'),false);
  assert.equal(rig.controller.profileName('person_c'),'kein verfügbares Profil');
  assert.match(rig.elements.spotifyMessage.textContent,/Ein weiteres Profil wird nicht automatisch übernommen/);
  rig.controller.selectTrack({title:'Fremder Titel',uri:'spotify:track:CCC'},'person_c');
  await rig.controller.play(); assert.equal(rig.calls.length,0);
  rig.controller.applyStatus(actualProfileStatus());
  assert.equal(rig.cards[0].nodes['.spotify-session-enabled'].checked,false,'Wiederkehr schaltet nicht automatisch wieder mit');
  assert.equal(rig.cards[0].nodes['.spotify-session-target-select'].value,'','Ziel verlangt bewusste Neuauswahl nach Entfernung');
  assert.equal(rig.elements.spotifyPlay.disabled,true);
});

test('Mehrdeutige Profil-IDs sperren den Controller statt eine Namenszuordnung zu erraten', async () => {
  const rig=createRig(); rig.controller.applyStatus(actualProfileStatus());
  rig.controller.selectTrack({title:'Song A',uri:'spotify:track:AAA'},'person_a');
  const invalid=actualProfileStatus(); invalid.profiles[1].profile_id='person_a'; rig.setStatus(invalid);
  await rig.controller.refresh();
  assert.equal(rig.elements.spotifyPlay.disabled,true);
  assert.equal(rig.controller.searchAllowed('person_a'),false);
  assert.match(rig.elements.spotifyMessage.textContent,/Mehrdeutige Spotify-Profil-IDs/);
});

test('Initialer Statusausfall verhindert keine spätere Bindung tatsächlicher Profile', () => {
  const rig=createRig(); const unavailable=actualProfileStatus(); unavailable.available=false;
  rig.controller.applyStatus(unavailable);
  assert.equal(rig.elements.spotifyProfile.value,'');
  assert.equal(rig.controller.searchAllowed('person_a'),false);
  assert.equal(rig.elements.spotifyPlay.disabled,true);
  rig.controller.applyStatus(actualProfileStatus());
  assert.deepEqual(rig.cards.map(card=>card.nodes['.spotify-session-profile-label'].textContent),['Erlene Spotify','Mein Spotify']);
  assert.equal(rig.controller.searchAllowed('person_a'),true);
});

test('Später erstmalig verfügbares zweites Profil bindet nur den noch unbenutzten Slot', () => {
  const rig=createRig(); const initial=actualProfileStatus(); initial.profiles=[initial.profiles[1]];
  rig.controller.applyStatus(initial); rig.controller.selectTrack({title:'Titel B',uri:'spotify:track:BBB'},'person_b');
  rig.controller.applyStatus(actualProfileStatus());
  assert.deepEqual(rig.cards.map(card=>card.nodes['.spotify-session-profile-label'].textContent),['Mein Spotify','Erlene Spotify']);
  assert.equal(rig.cards[0].nodes['.spotify-session-track'].textContent,'Titel B');
  assert.equal(rig.elements.spotifyProfile.value,'person_b');
});

test('Nach POST erfolgt auch bei noch laufendem älteren GET eine neue Statusabfrage', async () => {
  const rig=createRig(); rig.controller.applyStatus(publicStatus());
  rig.controller.selectTrack({title:'Song A',uri:'spotify:track:AAA'},'andreas');
  let finishOld;
  rig.setStatus(new Promise(resolve=>{finishOld=resolve;}));
  const oldRefresh=rig.controller.refresh(); await tick();
  const play=rig.controller.play(); await tick(); rig.finishPost(); await tick();
  assert.equal(rig.elements.spotifyPlay.disabled,true,'Start bleibt bis zur frischen Nachprüfung gesperrt');
  assert.equal(rig.calls.filter(call=>call.path==='/api/spotify/status').length,1);
  const current=publicStatus(); current.profiles[0]={...current.profiles[0],status:'playing',is_playing:true,active_target_id:'living',active_device_name:"Andreas' Echo Show",track:{title:'Song A'}};
  rig.setStatus(current); finishOld(publicStatus()); await oldRefresh; await play;
  assert.equal(rig.calls.filter(call=>call.path==='/api/spotify/status').length,2,'alte Abfrage ersetzt niemals das GET nach POST');
  assert.equal(rig.rooms[0].nodes['.room-spotify-state'].textContent,'Spielt: Song A · Andreas');
});

test('Doppeltes Echo und nicht verfügbare Profile sperren Aktionen', async () => {
  const rig = createRig(); rig.controller.applyStatus(publicStatus());
  for (const profile of ['andreas','erlene']) rig.controller.selectTrack({title:'Track',uri:'spotify:track:AAA'},profile);
  rig.cards[1].nodes['.spotify-session-target-select'].value='living';
  rig.cards[1].nodes['.spotify-session-target-select'].fire('change');
  assert.equal(rig.elements.spotifyPlay.disabled,true); await rig.controller.play();
  assert.equal(rig.calls.length,0);
  const unavailable = publicStatus(); unavailable.profiles[1].status='unavailable';
  rig.controller.applyStatus(unavailable);
  assert.equal(rig.cards[1].nodes['.spotify-session-enabled'].disabled,true);
  assert.equal(rig.elements.spotifyPlay.disabled,true);
  const unknown = publicStatus(); unknown.profiles[0].status='unexpected';
  rig.controller.applyStatus(unknown);
  assert.equal(rig.cards[0].nodes['.spotify-session-target-select'].disabled,true);
});

test('Nur beobachtetes GET zeigt spielt; Statusausfall löscht veraltete Anzeigen', async () => {
  const rig = createRig(); const observed = publicStatus();
  observed.profiles[1] = {...observed.profiles[1],status:'playing',is_playing:true,active_target_id:'office',active_device_name:'Echo Büro',track:{title:'<b>Actual song</b>'}};
  rig.controller.applyStatus(observed);
  assert.equal(rig.rooms[1].nodes['.room-spotify-state'].textContent,'Spielt: <b>Actual song</b> · Erlene Andreia');
  observed.profiles[1].is_playing=false; rig.controller.applyStatus(observed);
  assert.doesNotMatch(rig.rooms[1].nodes['.room-spotify-state'].textContent,/Spielt:|Pausiert:/);
  observed.profiles[1].status='paused'; rig.controller.applyStatus(observed);
  assert.match(rig.rooms[1].nodes['.room-spotify-state'].textContent,/Pausiert:/);
  rig.setStatus(new Error('offline')); await rig.controller.refresh();
  assert.equal(rig.rooms[1].nodes['.room-spotify-state'].textContent,'Wiedergabestatus nicht verfügbar.');
  assert.doesNotMatch(rig.cards[1].nodes['.spotify-session-status'].textContent,/Actual song/);
  assert.equal(rig.elements.spotifyPlay.disabled,true);
  assert.ok(rig.timers.some(timer=>timer.delay===30000));
});

test('HTTP 200 mit failed Bridge-Antwort wird nie als Erfolg angezeigt', async () => {
  const rig=createRig(); rig.controller.applyStatus(publicStatus());
  rig.controller.selectTrack({title:'Song A',uri:'spotify:track:AAA'},'andreas');
  rig.setPost({ok:true,json:async()=>({status:'ok',bridge:{status:'failed',error:'Token expired'}})});
  await rig.controller.play();
  assert.match(rig.elements.spotifyMessage.textContent,/abgelehnt/);
  assert.doesNotMatch(rig.elements.spotifyMessage.textContent,/Spielt:|Befehl angenommen/);
});

test('Ungültige URI löst keine Auswahl und keinen POST aus', async () => {
  const rig=createRig(); rig.controller.applyStatus(publicStatus());
  rig.controller.selectTrack({title:'Bad',uri:'javascript:alert(1)'},'andreas');
  assert.equal(rig.cards[0].nodes['.spotify-session-enabled'].checked,false);
  assert.equal(rig.elements.spotifyPlay.disabled,true);
  await rig.controller.play(); assert.equal(rig.calls.length,0);
});

test('Konfigurierte, aber in Spotify unsichtbare Ziele und fehlende Availability bleiben gesperrt', async () => {
  const rig=createRig(); rig.controller.applyStatus(publicStatus());
  rig.controller.selectTrack({title:'Track',uri:'spotify:track:AAA'},'andreas');
  const partlyOffline=publicStatus(); partlyOffline.profiles[0].available_targets=['office'];
  rig.controller.applyStatus(partlyOffline);
  const select=rig.cards[0].nodes['.spotify-session-target-select'];
  assert.equal(select.value,'living','verschwundenes Ziel wird nicht heimlich auf einen anderen Echo umgeroutet');
  assert.equal(select.children.find(option=>option.value==='living').disabled,true);
  assert.equal(rig.elements.spotifyPlay.disabled,true);
  await rig.controller.play(); assert.equal(rig.calls.length,0);
  select.value='office'; select.fire('change');
  assert.equal(rig.elements.spotifyPlay.disabled,false);
  const legacy=publicStatus(); delete legacy.profiles[0].available_targets;
  rig.controller.applyStatus(legacy);
  assert.equal(rig.elements.spotifyPlay.disabled,true);
  assert.match(rig.cards[0].nodes['.spotify-session-status'].textContent,/Zielverfügbarkeit nicht bestätigt/);
});

test('Aus Konfiguration entferntes Ziel wird niemals heimlich auf ein anderes Echo umgelegt', async () => {
  const rig=createRig(); rig.controller.applyStatus(publicStatus());
  rig.controller.selectTrack({title:'Track',uri:'spotify:track:AAA'},'andreas');
  const changed=publicStatus(); changed.targets=changed.targets.filter(target=>target.target_id!=='living');
  changed.profiles.forEach(profile=>{profile.targets=['office'];profile.available_targets=['office'];});
  rig.controller.applyStatus(changed);
  assert.equal(rig.cards[0].nodes['.spotify-session-target-select'].value,'');
  assert.equal(rig.cards[0].nodes['.spotify-session-enabled'].checked,false);
  rig.controller.applyStatus(changed);
  assert.equal(rig.cards[0].nodes['.spotify-session-target-select'].value,'','auch der nächste Poll darf das Ziel nicht ersetzen');
  assert.equal(rig.elements.spotifyPlay.disabled,true);
  await rig.controller.play(); assert.equal(rig.calls.length,0);
});

test('Unklare POST-Antwort ist kein behaupteter Fehlschlag und verlangt bewusste Neuauswahl', async () => {
  const rig=createRig(); rig.controller.applyStatus(publicStatus());
  rig.controller.selectTrack({title:'Track',uri:'spotify:track:AAA'},'andreas');
  rig.setPost({ok:true,json:async()=>({status:'accepted',assignments:[]})});
  await rig.controller.play();
  assert.match(rig.elements.spotifyMessage.textContent,/Anfrageergebnis unklar/);
  assert.equal(rig.cards[0].nodes['.spotify-session-enabled'].checked,false);
  assert.equal(rig.elements.spotifyPlay.disabled,true);
  assert.equal(rig.calls.filter(call=>call.path==='/api/spotify/status').length,1);
});

test('Typografische Apostrophe ordnen Wohnzimmer korrekt zu; fremde undefinierte Räume nie', () => {
  const rig=createRig(); const state=publicStatus();
  state.targets[0].display_name='Andreas’ Echo Show'; state.targets[0].spotify_device_name='Andreas’ Echo Show';
  state.profiles[0]={...state.profiles[0],status:'playing',is_playing:true,active_target_id:'living',track:{title:'Song'}};
  rig.controller.applyStatus(state);
  assert.match(rig.rooms[0].nodes['.room-spotify-state'].textContent,/Spielt: Song · Andreas/);
  rig.rooms[0].dataset.roomTarget='missing';
  rig.controller.applyStatus(state);
  assert.equal(rig.rooms[0].nodes['.room-spotify-state'].textContent,'Kein Spotify-Ziel für diesen Raum zugeordnet.');
});

test('Raum-Aliasse ordnen echte Registry-IDs zu; verschiedene Profile bleiben getrennt', () => {
  const rig=createRig();
  for(const [id,label] of [['bath','Badezimmer'],['bed','Schlafzimmer'],['kitchen','Küche']]) {
    const room=new Element(); room.dataset.roomTarget=id;
    room.nodes['.room-name']=new Element(); room.nodes['.room-name'].textContent=label;
    room.nodes['.room-spotify-state']=new Element(); rig.rooms.push(room);
  }
  const state=publicStatus();
  state.targets=[
    {target_id:'buero',display_name:'Echo Dot Büro',spotify_device_name:'Echo Dot Büro',aliases:['Büro']},
    {target_id:'bad',display_name:'Echo Dot Badezimmer',spotify_device_name:'Echo Dot Badezimmer',aliases:['Badezimmer']},
    {target_id:'wohnzimmer',display_name:'Echo Show anderer Name',spotify_device_name:'Echo Show anderer Name',aliases:['Wohnzimmer']},
    {target_id:'schlafzimmer',display_name:'Echo Spot anderer Name',spotify_device_name:'Echo Spot anderer Name',aliases:['Schlafzimmer']},
    {target_id:'kueche',display_name:'Echo Dot Küche',spotify_device_name:'Echo Dot Küche',aliases:['Küche']},
  ];
  for(const profile of state.profiles) { profile.targets=state.targets.map(target=>target.target_id); profile.available_targets=profile.targets; }
  state.profiles[0]={...state.profiles[0],status:'playing',is_playing:true,active_target_id:'buero',track:{title:'Titel Büro'}};
  state.profiles[1]={...state.profiles[1],status:'paused',is_playing:false,active_target_id:'bad',track:{title:'Titel Bad'}};
  rig.controller.applyStatus(state);
  assert.equal(rig.rooms[1].nodes['.room-spotify-state'].textContent,'Spielt: Titel Büro · Andreas');
  assert.equal(rig.rooms[2].nodes['.room-spotify-state'].textContent,'Pausiert: Titel Bad · Erlene Andreia');
  for(const [roomIndex,target] of [[0,'wohnzimmer'],[3,'schlafzimmer'],[4,'kueche']]) {
    state.profiles[0].active_target_id=target;
    rig.controller.applyStatus(state);
    assert.equal(rig.rooms[roomIndex].nodes['.room-spotify-state'].textContent,'Spielt: Titel Büro · Andreas');
  }
  state.profiles[0].active_target_id='buero-falsche-id'; rig.controller.applyStatus(state);
  assert.equal(rig.rooms[1].nodes['.room-spotify-state'].textContent,'Keine bestätigte Wiedergabe.');
  assert.equal(rig.calls.length,0,'Zuordnung ist ausschließlich Anzeige, kein Gerätebefehl');
});

test('Ähnliche oder mehrdeutige Raum-Aliasse behaupten keine Wiedergabe', () => {
  const rig=createRig(); const state=publicStatus();
  state.targets=[{target_id:'buero',display_name:'Echo Dot Großraumbüro',spotify_device_name:'Echo Dot Großraumbüro',aliases:['Großraumbüro']}];
  state.profiles[0]={...state.profiles[0],status:'playing',is_playing:true,active_target_id:'buero',track:{title:'Fremder Titel'}};
  rig.controller.applyStatus(state);
  assert.equal(rig.rooms[1].nodes['.room-spotify-state'].textContent,'Wiedergabestatus nicht verfügbar.');
  state.targets[0].aliases=['Büro'];
  state.targets.push({target_id:'buero-anders',display_name:'Weiteres Echo',spotify_device_name:'Weiteres Echo',aliases:['Büro']});
  rig.controller.applyStatus(state);
  assert.equal(rig.rooms[1].nodes['.room-spotify-state'].textContent,'Spotify-Ziel dieses Raums ist nicht eindeutig zugeordnet.');
  assert.doesNotMatch(rig.rooms[1].nodes['.room-spotify-state'].textContent,/Spielt:|Pausiert:|Fremder Titel/);
});

test('Nur bekannte Zimmernamen sind Match-Schlüssel, und leere Titel behaupten keine Wiedergabe', () => {
  const rig=createRig(); const state=publicStatus();
  rig.rooms[1].nodes['.room-name'].textContent='Großraumbüro';
  state.targets=[{target_id:'fremd',display_name:'Echo Dot Großraumbüro',spotify_device_name:'Echo Dot Großraumbüro',aliases:['Großraumbüro']}];
  state.profiles[0]={...state.profiles[0],status:'playing',is_playing:true,active_target_id:'fremd',track:{title:'Fremder Titel'}};
  rig.controller.applyStatus(state);
  assert.equal(rig.rooms[1].nodes['.room-spotify-state'].textContent,'Wiedergabestatus nicht verfügbar.');
  rig.rooms[1].nodes['.room-name'].textContent='Büro'; state.targets[0].aliases=['Büro'];
  for(const title of ['', '   ']) {
    state.profiles[0].track.title=title; rig.controller.applyStatus(state);
    assert.equal(rig.rooms[1].nodes['.room-spotify-state'].textContent,'Keine bestätigte Wiedergabe.');
  }
  state.profiles[0].track.title='  Bekannter Titel  '; rig.controller.applyStatus(state);
  assert.equal(rig.rooms[1].nodes['.room-spotify-state'].textContent,'Spielt: Bekannter Titel · Andreas');
});

test('Album und Playlist bleiben auswählbar; Auswahl schreibt nie direkt einen Befehl', () => {
  const rig=createRig(); rig.controller.applyStatus(publicStatus());
  for (const type of ['album','playlist','artist']) {
    rig.controller.selectTrack({title:type,uri:'spotify:'+type+':ABC'},'andreas');
    assert.equal(rig.cards[0].nodes['.spotify-session-track'].textContent,type);
    assert.equal(rig.elements.spotifyPlay.disabled,false);
  }
  assert.equal(rig.calls.length,0);
});

// Minimal DOM implementation executes the complete production script, not only
// isolated functions. It intentionally has no device transport or browser APIs.
class DomElement extends Element {
  constructor(tag='div') { super(tag); this.attributes={}; this.parent=null; this.hidden=false; }
  get className() { return this.attributes.class || ''; }
  set className(value) { this.attributes.class=value; }
  get classList() { return {toggle:(name,on)=>{const list=new Set(this.className.split(/\s+/).filter(Boolean)); if(on)list.add(name);else list.delete(name);this.className=[...list].join(' ');}}; }
  matches(selector) {
    if(selector.startsWith('.'))return this.className.split(/\s+/).includes(selector.slice(1));
    if(selector.startsWith('[')) {
      const [,key,value]=selector.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/) || [];
      return key && key in this.attributes && (value===undefined || this.attributes[key]===value);
    }
    return this.tag===selector;
  }
  querySelectorAll(selector) { const choices=selector.split(',').map(x=>x.trim()); const nodes=[]; const walk=node=>{for(const child of node.children){if(choices.some(choice=>child.matches(choice)))nodes.push(child);walk(child);}};walk(this);return nodes; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { const choices=selector.split(',').map(x=>x.trim()); for(let node=this;node;node=node.parent)if(choices.some(choice=>node.matches(choice)))return node; return null; }
  append(...nodes) { for(const node of nodes){node.parent=this; super.append(node);} }
  prepend(node) { node.parent=this; this.children.unshift(node); }
  remove() { if(this.parent)this.parent.children=this.parent.children.filter(node=>node!==this); }
  setAttribute(key,value) { this.attributes[key]=String(value); }
  removeAttribute(key) { delete this.attributes[key]; }
  get options() { return this.children; }
  get selectedOptions() { return this.children.filter(node=>node.value===this.value); }
  set innerHTML(value) { this.children=[]; parseDom(value,this); }
  click() { if(!this.disabled)this.listeners.click?.({target:this,stopPropagation(){},preventDefault(){}}); }
}
function parseDom(markup, root=new DomElement('body')) {
  const stack=[root];
  for(const token of markup.match(/<[^>]+>|[^<]+/g) || []) {
    if(token.startsWith('</')) { if(stack.length>1)stack.pop(); continue; }
    if(token.startsWith('<')) {
      const tag=token.match(/^<([a-z0-9]+)/i)?.[1]; if(!tag)continue;
      const element=new DomElement(tag);
      for(const match of token.matchAll(/\s([a-zA-Z-]+)(?:="([^"]*)")?/g)) {
        const key=match[1]; const value=match[2] || ''; element.attributes[key]=value;
        if(key.startsWith('data-'))element.dataset[key.slice(5).replace(/-([a-z])/g,(_,x)=>x.toUpperCase())]=value;
        if(key==='value')element.value=value;
        if(key==='disabled')element.disabled=true; if(key==='hidden')element.hidden=true;
      }
      stack.at(-1).append(element);
      if(!['input','img','br','meta','link'].includes(tag))stack.push(element);
    } else stack.at(-1).textContent+=token.trim();
  }
  return root;
}

async function initializeDashboard(capabilities={ok:true,json:async()=>({mode:'live',home_assistant:true})}, inventory={ok:true,json:async()=>({entities:[]})}, actionResponse=null, alexaResponse=null, spotifyResponse=null) {
  const root=parseDom(html.slice(html.indexOf('<main '),html.indexOf('<script>')));
  const calls=[]; const document={hidden:true,querySelector:selector=>root.querySelector(selector),querySelectorAll:selector=>root.querySelectorAll(selector),
    getElementById:id=>{const walk=node=>node.attributes.id===id?node:node.children.map(walk).find(Boolean);return walk(root)||null;},createElement:tag=>new DomElement(tag)};
  const context=vm.createContext({document,AbortController,URL,console,
    navigator:{clipboard:{writeText:async()=>{}}},setTimeout:()=>1,clearTimeout:()=>{},
    fetch:async(path,options={})=>{
      calls.push({path,options});
      if(path==='/api/capabilities')return capabilities;
      if(path==='/api/ha/entities')return inventory;
      if(path==='/api/music/status')return {ok:true,json:async()=>({configured:false,available:false})};
      if(path==='/api/action' && actionResponse) { if(actionResponse instanceof Error)throw actionResponse;return actionResponse; }
      if(path==='/api/alexa/speak' && alexaResponse) { if(alexaResponse instanceof Error)throw alexaResponse;return alexaResponse; }
      if(path==='/api/spotify/search' && spotifyResponse) return spotifyResponse;
      throw new Error('Unexpected diagnostic fetch');
    },
  });
  assert.doesNotThrow(()=>vm.runInContext(script,context));
  await tick(); await tick();
  return {root,calls,document,controller:vm.runInContext('spotifyController',context)};
}

test('Realer Such-Handler sendet die Live-Profil-ID statt eines hartcodierten Namens', async () => {
  const rig=await initializeDashboard(undefined,undefined,null,null,{ok:true,json:async()=>({results:[]})});
  const state=actualProfileStatus(); state.profiles[0].available_targets=[]; state.profiles[1].available_targets=[];
  rig.controller.applyStatus(state);
  const profileSelect=rig.document.getElementById('spotifyProfile'); const query=rig.document.getElementById('spotifyQuery'); const search=rig.document.getElementById('spotifySearch');
  assert.equal(search.disabled,false,'Suche bleibt ohne sichtbares Echo möglich');
  query.value='Fields of Gold'; await search.listeners.click();
  profileSelect.value='person_b'; profileSelect.listeners.change();
  query.value='Anderer Titel'; await search.listeners.click();
  assert.deepEqual(rig.calls.filter(call=>call.path==='/api/spotify/search').map(call=>JSON.parse(call.options.body)),[
    {profile:'person_a',query:'Fields of Gold'}, {profile:'person_b',query:'Anderer Titel'},
  ]);
  state.profiles=state.profiles.filter(profile=>profile.profile_id!=='person_b'); rig.controller.applyStatus(state);
  assert.equal(profileSelect.value,'person_b'); assert.equal(search.disabled,true);
  await search.listeners.click();
  assert.equal(rig.calls.filter(call=>call.path==='/api/spotify/search').length,2,'entferntes Suchprofil erzeugt keinen weiteren Request');
});

test('Gesamtes Dashboard initialisiert Suche, Eigene Musik und Alexa ohne null Listener', async () => {
  const {root,calls,document}=await initializeDashboard();
  for(const [id,event] of [['spotifySearch','click'],['spotifyPlay','click'],['musicSearchForm','submit'],['musicPlay','click'],['alexaSpeak','click'],['refresh','click']])
    assert.equal(typeof document.getElementById(id).listeners[event],'function',id+' handler missing');
  assert.equal(calls.some(call=>call.options.method==='POST'),false,'initialization never writes devices');
  const room=root.querySelector('[data-room-target]');
  assert.equal(room.querySelector('.room-details').hidden,true);
  assert.equal(room.attributes['aria-expanded'],'false');
  const slider=room.querySelector('.room-temp');
  room.listeners.keydown({target:slider,key:' ',preventDefault(){throw new Error('nested slider key intercepted');}});
  assert.equal(room.querySelector('.room-details').hidden,true,'nested keyboard input does not collapse room');
  room.listeners.keydown({target:room,key:'Enter',preventDefault(){}});
  assert.equal(room.querySelector('.room-details').hidden,false);
  assert.equal(room.attributes['aria-expanded'],'true');
});

test('Header behauptet bei 404, Inventarausfall oder Vorschau nie Home Assistant verbunden', async () => {
  const connected=await initializeDashboard();
  assert.equal(connected.document.getElementById('systemStateLabel').textContent,'OK');
  for(const [capabilities,inventory] of [
    [{ok:false,json:async()=>({})},{ok:true,json:async()=>({entities:[]})}],
    [{ok:true,json:async()=>({mode:'simulation',home_assistant:false})},{ok:true,json:async()=>({entities:[]})}],
    [{ok:true,json:async()=>({mode:'live',home_assistant:true})},{ok:false,json:async()=>({error:'HA offline'})}],
    [{ok:true,json:async()=>({mode:'live',home_assistant:true})},{ok:true,json:async()=>({entities:'invalid'})}],
  ]) {
    const rig=await initializeDashboard(capabilities,inventory);
    assert.equal(rig.document.getElementById('systemStateLabel').textContent,'HA offline');
    assert.match(rig.document.getElementById('systemState').className,/offline/);
    assert.doesNotMatch(rig.root.querySelector('.pill').textContent,/verbunden/);
  }
});

test('Raumtemperaturen sind echte Sensordaten mit Wandthermostat-Vorrang, keine Demo-Werte', async () => {
  const rig=await initializeDashboard(undefined,{ok:true,json:async()=>({entities:[
    {entity_id:'sensor.wohnung_wandthermostat_temperature',state:'22.2',name:'Wohnung Wandthermostat Temperatur'},
    {entity_id:'sensor.living_radiator',state:'24',name:'Wohnzimmer Heizkörperthermostat Temperatur'},
    {entity_id:'sensor.wohnung_wandthermostat_temperature_3',state:'unavailable',name:'Wohnung Wandthermostat Temperatur 3'},
    {entity_id:'sensor.bed_radiator',state:'19',name:'Schlafzimmer Heizkörperthermostat Temperatur'},
    {entity_id:'sensor.wohnung_wandthermostat_temperature_2',state:'20.6',name:'Wohnung Wandthermostat Temperatur 2'},
    {entity_id:'sensor.bath_radiator',state:'21.8',name:'Badezimmer Heizkörperthermostat Temperatur'},
    {entity_id:'sensor.office_radiator',state:'21.1',name:'Büro Heizkörperthermostat Temperatur'},
  ]})});
  const nodes=rig.root.querySelectorAll('[data-room-temperature]');
  assert.deepEqual(nodes.map(node=>node.textContent),['22,2 °C','Temperatur: —','20,6 °C','21,8 °C','21,1 °C']);
  const offline=await initializeDashboard({ok:false,json:async()=>({})});
  assert.ok(offline.root.querySelectorAll('[data-room-temperature]').every(node=>node.textContent==='Temperatur: —'));
  assert.match(html,/Neue Solltemperatur/,'Slider ist eine neue Vorgabe, kein bestätigter Ist- oder Sollwert');
});

test('Lichtaktionen erfinden bei Transportfehler, Simulation oder noch unverändertem Istzustand keinen Erfolg', async () => {
  const capabilities={ok:true,json:async()=>({mode:'live',home_assistant:true,home_assistant_writes:true})};
  const inventory={ok:true,json:async()=>({entities:[{entity_id:'light.h618c',state:'off',name:'Living light'}]})};
  for(const actionResponse of [new Error('offline'),{ok:true,json:async()=>({status:'preview',mode:'simulation'})},{ok:true,json:async()=>({status:'ok',mode:'live'})}]) {
    const rig=await initializeDashboard(capabilities,inventory,actionResponse);
    const room=rig.root.querySelector('[data-room-target]'); const button=room.querySelector('.room-light');
    assert.equal(button.disabled,false);
    await button.listeners.click({currentTarget:button,stopPropagation(){}});
    assert.equal(room.querySelector('[data-room-light-state="living"]').textContent,'Licht: Aus');
    assert.doesNotMatch(room.querySelector('.room-action-status').textContent,/eingeschaltet|Licht: Ein/);
    assert.equal(button.disabled,false,'asynchrone Rückkehr verwendet gespeicherten Button, nicht event.currentTarget');
  }
  const offline=await initializeDashboard({ok:false,json:async()=>({})});
  const room=offline.root.querySelector('[data-room-target]');
  assert.equal(room.querySelector('.room-light').disabled,true);
  assert.equal(room.querySelector('[data-room-light-state="living"]').textContent,'Licht: —');
});

test('Kameraname bleibt Text und kann kein HTML oder Attribute einschleusen', async () => {
  const hostile='<img src=x onerror=alert(1)>" autofocus onfocus=alert(1)';
  const rig=await initializeDashboard(undefined,{ok:true,json:async()=>({entities:[{entity_id:'camera.test',state:'idle',name:hostile}]})});
  const grid=rig.document.getElementById('cameraGrid');
  assert.equal(grid.querySelector('.room-name').textContent,hostile);
  const image=grid.querySelector('img');
  assert.equal(image.alt,hostile);
  assert.equal(grid.querySelectorAll('img').length,1,'nur das echte Kamera-Image, keine eingeschleuste Grafik');
  assert.match(image.src,/^\/api\/ha\/camera_image\?entity_id=camera\.test&/);
});

test('Kamera-HTTP-Fehler oder ungültige Daten werden nicht als keine Kameras dargestellt', async () => {
  for(const inventory of [{ok:false,json:async()=>({entities:[]})},{ok:true,json:async()=>({entities:'invalid'})}]) {
    const rig=await initializeDashboard(undefined,inventory);
    assert.match(rig.document.getElementById('cameraGrid').children[0].textContent,/Kameras konnten nicht geladen/);
  }
});

test('Alexa-Ansage ist gegated, einmalig und meldet Simulation nie als gesendet', async () => {
  const readonly=await initializeDashboard();
  const disabled=readonly.document.getElementById('alexaSpeak');
  await disabled.listeners.click({currentTarget:disabled});
  assert.equal(readonly.calls.some(call=>call.path==='/api/alexa/speak'),false);
  const capabilities={ok:true,json:async()=>({mode:'live',home_assistant:true,home_assistant_writes:true})};
  for(const response of [{ok:true,json:async()=>({status:'failed',mode:'live'})},{ok:true,json:async()=>({status:'ok',mode:'simulation'})}]) {
    const rig=await initializeDashboard(capabilities,undefined,null,response);
    rig.document.getElementById('alexaDevice').value='Echo Büro';
    rig.document.getElementById('alexaCommand').value='Testtext';
    const button=rig.document.getElementById('alexaSpeak');
    const request=button.listeners.click({currentTarget:button});
    await button.listeners.click({currentTarget:button}); await request;
    assert.equal(rig.calls.filter(call=>call.path==='/api/alexa/speak').length,1);
    assert.doesNotMatch(rig.document.getElementById('alexaStatus').textContent,/gesendet/);
    assert.equal(button.disabled,false);
  }
});
