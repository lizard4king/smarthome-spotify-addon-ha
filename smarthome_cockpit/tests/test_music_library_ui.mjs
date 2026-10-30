import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import vm from 'node:vm';
import test from 'node:test';
const source = readFileSync(fileURLToPath(new URL('../dashboard/music-library.js', import.meta.url)), 'utf8');
const css = readFileSync(fileURLToPath(new URL('../dashboard/music-library.css', import.meta.url)), 'utf8');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.attributes = {}; this.textContent = ''; this.value = ''; this.hidden = false; this.disabled = false; this.playCount = 0; this.paused = true; this.currentTime = 0; this.duration = NaN; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; if (key === 'src') this.src = ''; }
  addEventListener(key, fn) { (this.listeners[key] ||= []).push(fn); }
  removeEventListener(key, fn) { this.listeners[key] = (this.listeners[key] || []).filter(value => value !== fn); }
  fire(key) { if (key === 'click' && this.disabled) return; return Promise.all((this.listeners[key] || []).map(fn => fn({target: this}))); }
  pause() { this.paused = true; this.fire('pause'); } load() {} play() { this.playCount++; this.paused = false; this.fire('play'); return Promise.resolve(); }
}
function descendants(node) { return [node, ...node.children.flatMap(descendants)]; }
const tick = async () => { for(let n=0;n<8;n++) await Promise.resolve(); };
function rig(responder, options = {}) {
  const calls = [], signals = [], timers = new Map(); let timerId = 0;
  const doc = {createElement: tag => new Element(tag)}; const container = new Element('div'); container.ownerDocument = doc;
  const context = {URLSearchParams, AbortController, console, setTimeout(fn, ms) { assert.ok([300,12000,35000].includes(ms)); timers.set(++timerId, {fn, ms}); return timerId; }, clearTimeout(id) {timers.delete(id);}, fetch: async (url, options) => {
    calls.push(url); signals.push(options.signal);
    const result = responder(url);
    if (result instanceof Promise) return new Promise((resolve,reject) => {
      options.signal.addEventListener('abort',()=>reject(Object.assign(new Error('aborted'),{name:'AbortError'})),{once:true});
      result.then(data=>resolve({ok:true,json:async()=>data}),reject);
    });
    return {ok:true,json:async()=>result};
  }, navigator: {mediaSession:{setActionHandler(){}}}};
  vm.runInNewContext(source, context);
  const api = context.initMusicLibrary(container, options);
  const all = () => [...descendants(container), ...(options.playerContainer ? descendants(options.playerContainer) : [])];
  return {api, container, calls, signals, all, audio: () => all().find(n => n.tag === 'audio'), search: () => all().find(n => n.tag === 'input'), button: text => all().find(n => n.tag === 'button' && n.textContent === text), flush: async () => { for(const [id, value] of [...timers.entries()]) if(value.ms===300) {timers.delete(id);value.fn();} await tick(); }, expire: async ms => {for(const [id,value] of [...timers.entries()]) if(value.ms===ms) {timers.delete(id);value.fn();} await tick();}, timers};
}
const track = (n) => ({kind:'track', uri:'local://track/'+n, title:'Titel '+n, artist:'Synthetisch'});
const standard = url => url.includes('browser-status') ? {available:true} : url.includes('/tracks?') ? {tracks:[track(1)],next_offset:null} : {items:[],next_offset:null};
test('Keine Wiedergabe beim Initialisieren; URI wird ausschließlich an relativen Audioendpoint übergeben', async () => {
  const r = rig(standard); await r.api.ready;
  assert.equal(r.audio().playCount,0);
  await r.button('Auf diesem Gerät abspielen').fire('click');
  assert.equal(r.audio().src,'api/music/audio?uri=local%3A%2F%2Ftrack%2F1');
  assert.equal(r.audio().playCount,1);
  assert.ok(r.calls.every(url=>url.startsWith('api/music/')));
  r.api.destroy();
});
test('Debounce invalidiert alte Antworten bereits während des Tippens', async () => {
  let release;
  const r = rig(url => url.includes('browser-status') ? {available:true} : new Promise(resolve => {release=resolve;}));
  await tick();
  r.search().value = 'neu'; await r.search().fire('input');
  release({tracks:[{...track(0), title:'Veraltet'}],next_offset:null}); await tick();
  assert.ok(!r.all().some(node=>node.textContent==='Veraltet'));
  assert.equal(r.calls.length,2,'Noch keine Suche vor Ablauf der 300 ms');
  await r.flush(); release({tracks:[{...track(2),title:'Aktuell'}],next_offset:null}); await tick();
  assert.ok(r.all().some(node=>node.textContent==='Aktuell'));
  r.api.destroy();
});
test('Album lädt vollständige Queue; Suche und Navigation verändern laufende Queue nicht', async () => {
  const r = rig(url => url.includes('albums?') ? {items:[{kind:'album',uri:'library://album/1',title:'Album'}],next_offset:null} : url.includes('collection?') ? {title:'Album',tracks:[track(1),track(2)],complete:true} : standard(url));
  await r.api.ready; await r.button('Alben').fire('click'); await tick(); await r.button('Öffnen').fire('click'); await tick();
  assert.equal(r.audio().playCount,0);
  await r.button('Alle Bibliothekstitel auf diesem Gerät abspielen').fire('click');
  r.search().value='andere'; await r.search().fire('input'); await r.flush();
  await r.audio().fire('ended');
  assert.match(r.audio().src,/track%2F2$/);
  await r.audio().fire('error'); assert.match(r.all().find(n=>n.className==='ml-browser-player').children.at(-1).textContent,/nicht übersprungen/);
  assert.equal(r.audio().playCount,2);
  r.api.destroy();
});
test('Ordner navigieren, zurückkehren und volle Sammlung explizit vorbereiten', async () => {
  const r = rig(url => url.includes('/folders?') ? {items:[{kind:'folder',uri:'local://Musik/Lang',title:'Unterordner'}],next_offset:null} : url.includes('collection?') ? {tracks:[track(1),track(2)],complete:true} : standard(url));
  await r.api.ready; await r.button('Ordner').fire('click'); await tick(); await r.button('Öffnen').fire('click'); await tick();
  assert.ok(r.calls.some(url=>url.includes('path=local%3A%2F%2FMusik%2FLang')));
  await r.button('Ganzen Ordner auf diesem Gerät abspielen').fire('click'); await tick();
  assert.equal(r.audio().playCount,0);
  await r.button('Ordner „Musik / Lang“ starten').fire('click');
  await r.button('← Zurück zur Übersicht').fire('click'); await tick();
  await r.audio().fire('ended'); assert.match(r.audio().src,/track%2F2$/);
  r.api.destroy();
});
test('Unvollständige Sammlung ist nicht abspielbar; unavailable lässt Browsen zu', async () => {
  const r = rig(url => url.includes('browser-status') ? {available:false,reason:'Nicht konfiguriert'} : url.includes('albums?') ? {items:[{kind:'album',uri:'a',title:'A'}],next_offset:null} : url.includes('collection?') ? {tracks:[track(1)],complete:false} : standard(url));
  await r.api.ready; assert.equal(r.button('Auf diesem Gerät abspielen').disabled,true);
  await r.button('Alben').fire('click'); await tick(); await r.button('Öffnen').fire('click'); await tick();
  assert.ok(r.all().some(n=>n.textContent==='Die Sammlung ist nicht vollständig verfügbar.'));
  assert.equal(r.button('Alle Bibliothekstitel auf diesem Gerät abspielen').disabled,true);
  r.api.destroy();
});
test('Metadaten bleiben Text, Artwork wird nicht geladen; mobile Layoutregeln begrenzen Breite', async () => {
  const malicious = '<img src=x onerror=alert(1)>';
  const r = rig(url => url.includes('browser-status') ? {available:true} : {tracks:[{...track(1),title:malicious,artwork_url:'https://private.invalid/token'}],next_offset:null});
  await r.api.ready;
  assert.ok(r.all().some(n=>n.textContent===malicious)); assert.ok(!r.all().some(n=>n.tag==='img'));
  assert.doesNotMatch(source,/innerHTML/);
  assert.match(css,/min-height:44px/); assert.match(css,/@media \(max-width:480px\)/); assert.match(css,/grid-template-columns:minmax\(0,1fr\)/); assert.match(css,/overflow-wrap:anywhere/);
  r.api.destroy();
});
test('Mehr laden bewahrt Suchbegriff, fügt Seiten hinzu und beendet Pagination', async () => {
  const r = rig(url => {
    if (url.includes('browser-status')) return {available:true};
    const params = new URL(url,'https://synthetic.invalid/').searchParams;
    return {tracks:[track(params.get('offset') === '2' ? 2 : 1)],next_offset:params.get('offset') === '2' ? null : 2};
  });
  await r.api.ready;
  r.search().value='Test'; await r.search().fire('input'); await r.flush();
  await r.button('Mehr laden').fire('click'); await tick();
  assert.ok(r.calls.some(url=>url.includes('q=Test&offset=2')));
  assert.equal(r.all().filter(n=>n.className==='ml-row').length,2);
  assert.equal(r.button('Mehr laden').hidden,true);
  r.api.destroy();
});
test('Veraltete Sammlung kann Reiterwechsel nicht überschreiben; Formatfehler stoppen Queue', async () => {
  let release;
  const r = rig(url => url.includes('albums?') ? {items:[{kind:'album',uri:'album',title:'Alt'}],next_offset:null} : url.includes('collection?') ? new Promise(resolve=>{release=resolve;}) : standard(url));
  await r.api.ready; await r.button('Alben').fire('click'); await tick();
  const opening = r.button('Öffnen').fire('click'); await tick();
  await r.button('Titel').fire('click'); await tick();
  release({title:'Veraltetes Album',tracks:[track(9)],complete:true}); await opening; await tick();
  assert.ok(!r.all().some(n=>n.textContent==='Veraltetes Album'));
  await r.button('Auf diesem Gerät abspielen').fire('click');
  const before = r.audio().playCount;
  await r.audio().fire('error'); await tick();
  assert.equal(r.audio().playCount,before);
  r.api.destroy();
});
test('Lautsprecherauswahl ruft ausschließlich Callback auf und benötigt kein Browseraudio', async () => {
  const selections = [];
  const r = rig(url => url.includes('browser-status') ? {available:false} : {tracks:[track(1)],next_offset:null}, {onSelectTrack: value => selections.push(value)});
  await r.api.ready;
  assert.equal(r.button('Auf diesem Gerät abspielen').disabled,true);
  await r.button('Für Lautsprecher auswählen').fire('click');
  assert.equal(selections.length,1); assert.equal(selections[0].uri,track(1).uri);
  assert.equal(r.audio().playCount,0); assert.equal(r.audio().src,undefined);
  assert.equal(r.calls.length,2,'Auswahl löst keine weitere HTTP-Anfrage aus');
  r.api.destroy();
});
test('Lautsprecherauswahl markiert genau einen Track und bleibt nach Such-Rerender erhalten', async () => {
  const selected = [];
  const r = rig(url => url.includes('browser-status') ? {available:false} : {tracks:[track(1),track(2)],next_offset:null}, {onSelectTrack: value => selected.push(value.uri)});
  const selectionButtons = () => r.all().filter(node => node.tag === 'button' && node.textContent === 'Für Lautsprecher auswählen');
  await r.api.ready;
  let buttons = selectionButtons();
  assert.deepEqual(buttons.map(node => node.attributes['aria-pressed']), ['false','false']);
  await buttons[0].fire('click');
  assert.strictEqual(selectionButtons()[0], buttons[0]);
  assert.deepEqual(buttons.map(node => node.attributes['aria-pressed']), ['true','false']);
  await buttons[1].fire('click');
  assert.strictEqual(selectionButtons()[1], buttons[1]);
  assert.deepEqual(buttons.map(node => node.attributes['aria-pressed']), ['false','true']);
  r.search().value = 'erneut'; await r.search().fire('input');
  assert.equal(selectionButtons().length, 0);
  await r.flush();
  buttons = selectionButtons();
  assert.deepEqual(buttons.map(node => node.attributes['aria-pressed']), ['false','true']);
  assert.deepEqual(selected, [track(1).uri,track(2).uri]);
  assert.equal(r.audio().playCount,0); assert.equal(r.audio().src,undefined);
  assert.equal(r.calls.length,3,'Nur Initialladen und Suche erzeugen Bibliotheksanfragen');
  r.api.destroy();
});
test('Ungültige Metadaten werden kontrolliert abgewiesen, bevor sie gerendert werden', async () => {
  for(const payload of [null, false, [], {tracks:[null]}, {tracks:[false]}, {tracks:[[]]}, {tracks:[{title:'Kein URI'}]}, {tracks:[{uri:'x',title:42}]}, {tracks:[{...track(1),artist:{name:'x'}}]}]) {
    const r = rig(url=>url.includes('browser-status') ? {available:true} : payload);
    await r.api.ready;
    assert.ok(r.all().some(n=>n.textContent==='Die Bibliothek hat eine ungültige Antwort geliefert.'));
    assert.equal(r.all().filter(n=>n.className==='ml-row').length,0);
    assert.equal(r.timers.size,0);
    r.api.destroy();
  }
});
test('12-s-Timeout gibt kontrollierte Meldung aus, destroy bricht ausstehende Anfragen ab', async () => {
  const r = rig(url=>url.includes('browser-status') ? {available:true} : new Promise(()=>{}));
  await tick(); assert.equal(r.timers.size,1);
  await r.expire(12000); await r.api.ready;
  assert.ok(r.all().some(n=>n.textContent==='Die Bibliothek antwortet zu langsam. Bitte versuche es erneut.'));
  assert.ok(r.signals.some(signal=>signal.aborted)); assert.equal(r.timers.size,0);
  r.api.destroy();
  const pending = rig(()=>new Promise(()=>{})); await tick();
  pending.api.destroy(); await pending.api.ready;
  assert.ok(pending.signals.every(signal=>signal.aborted)); assert.equal(pending.timers.size,0);
});
test('Collection hat 35-s-Timeout und fehlerhafte Tracks können nicht vorbereitet werden', async () => {
  const r = rig(url=>url.includes('albums?') ? {items:[{kind:'album',uri:'album',title:'A'}],next_offset:null} : url.includes('collection?') ? new Promise(()=>{}) : standard(url));
  await r.api.ready; await r.button('Alben').fire('click'); await tick();
  const opening = r.button('Öffnen').fire('click'); await tick();
  assert.ok([...r.timers.values()].some(value=>value.ms===35000));
  await r.expire(12000); assert.ok([...r.timers.values()].some(value=>value.ms===35000));
  await r.expire(35000); await opening;
  assert.ok(r.all().some(n=>n.textContent==='Die Bibliothek antwortet zu langsam. Bitte versuche es erneut.'));
  r.api.destroy();
  const malformed = rig(url=>url.includes('albums?') ? {items:[{kind:'album',uri:'album',title:'A'}],next_offset:null} : url.includes('collection?') ? {complete:true,tracks:[null]} : standard(url));
  await malformed.api.ready; await malformed.button('Alben').fire('click'); await tick(); await malformed.button('Öffnen').fire('click'); await tick();
  assert.ok(malformed.all().some(n=>n.textContent==='Die Bibliothek hat eine ungültige Antwort geliefert.'));
  assert.equal(malformed.button('Alle Bibliothekstitel auf diesem Gerät abspielen').disabled,true);
  malformed.api.destroy();
});
test('Vorbereitete Ordnerqueue nennt nach Navigation weiterhin ihre ursprüngliche Identität', async () => {
  const r = rig(url=>url.includes('folders?') ? {items:[{kind:'folder',uri:'local://Ordner-A',title:'Ordner A'}],next_offset:null} : url.includes('collection?') ? {complete:true,title:'Ordner A',tracks:[track(1),track(2)]} : standard(url));
  await r.api.ready; await r.button('Ordner').fire('click'); await tick(); await r.button('Öffnen').fire('click'); await tick();
  await r.button('Ganzen Ordner auf diesem Gerät abspielen').fire('click'); await tick();
  await r.button('Titel').fire('click'); await tick();
  assert.ok(r.all().some(n=>!n.hidden && n.textContent==='Ordner bereit: Ordner A · Ordner-A · 2 Titel.'));
  assert.equal(r.button('Ordner „Ordner A“ starten').hidden,false);
  assert.equal(r.audio().playCount,0);
  await r.button('Ordner „Ordner A“ starten').fire('click'); await r.audio().fire('ended');
  assert.match(r.audio().src,/track%2F2$/);
  r.api.destroy();
});
test('Ordnerüberschriften und Vorbereitung zeigen relative Pfade ohne Providerkennung', async () => {
  const uri = 'filesystem_local--demo://Demo/Album%20Name';
  const r = rig(url=>url.includes('folders?') ? {items:[{kind:'folder',uri,title:'Album Name'}],next_offset:null} : url.includes('collection?') ? {complete:true,tracks:[track(1)]} : standard(url));
  await r.api.ready; await r.button('Ordner').fire('click'); await tick();
  assert.equal(r.all().find(n=>n.className==='ml-location').textContent,'Musikbibliothek');
  await r.button('Öffnen').fire('click'); await tick();
  assert.equal(r.all().find(n=>n.className==='ml-location').textContent,'Demo / Album Name');
  assert.ok(r.calls.some(url=>url.includes('path='+encodeURIComponent(uri))));
  await r.button('Ganzen Ordner auf diesem Gerät abspielen').fire('click'); await tick();
  assert.equal(r.button('Ordner „Demo / Album Name“ starten').hidden,false);
  assert.ok(!r.all().some(n=>n.textContent.includes('filesystem_local--demo')));
  assert.ok(r.all().some(n=>n.textContent==='Ordner bereit: Demo / Album Name · 1 Titel.'));
  r.api.destroy();
});

test('Compact dock bleibt außerhalb der Bibliothek und startet nur durch eine explizite Geste', async () => {
  const dock = new Element('aside'), selected = [];
  const r = rig(standard, {compact:true,playerContainer:dock,onSelectTrack:value=>selected.push(value.uri)});
  await r.api.ready;
  assert.equal(descendants(r.container).some(n=>n.tag==='audio'),false);
  const audio = r.audio(); assert.ok(audio); assert.equal(dock.children[0].hidden,true); assert.equal(audio.controls,false); assert.equal(audio.hidden,true);
  assert.equal(audio.playCount,0); assert.ok(r.all().some(n=>n.textContent==='Dieses Gerät'));
  assert.equal(r.all().filter(n=>n.className?.includes('ml-row-actions')).flatMap(n=>n.children).length,1);
  const meta = r.all().find(n=>n.className==='ml-meta ml-select');
  await meta.fire('click'); assert.deepEqual(selected,[track(1).uri]); assert.equal(audio.playCount,0); assert.equal(dock.children[0].hidden,true);
  r.api.setSelectedTrack(track(1)); assert.equal(meta.attributes['aria-pressed'],'true');
  await r.button('Abspielen').fire('click'); assert.equal(audio.playCount,1); assert.equal(dock.children[0].hidden,false);
  r.search().value='neu'; await r.search().fire('input'); await r.flush(); assert.strictEqual(r.audio(),audio);
  r.api.destroy(); assert.equal(dock.children.length,0); assert.equal(Object.values(audio.listeners).flat().length,0);
});

test('Compact Zielwechsel und Auswahl erzeugen keine Geräteaktion; Speaker-Play ist separat', async () => {
  const played = [], selected = [];
  const r = rig(url=>url.includes('browser-status') ? {available:false} : {tracks:[track(1)],next_offset:null}, {compact:true,onSelectTrack:value=>selected.push(value.uri),onPlayTrack:value=>played.push(value.uri)});
  await r.api.ready;
  r.api.setPlaybackTarget({kind:'speaker',label:'Wohnzimmer'});
  assert.deepEqual(played,[]); assert.equal(r.audio().playCount,0);
  assert.ok(r.all().some(n=>n.textContent==='Dieses Gerät'));
  const action= r.all().find(n=>n.className==='ml-button ml-play'); assert.equal(action.disabled,false);
  await r.all().find(n=>n.className==='ml-meta ml-select').fire('click'); assert.deepEqual(played,[]);
  await action.fire('click'); assert.deepEqual(played,[track(1).uri]); assert.equal(r.audio().src,undefined);
  r.api.setPlaybackTarget({kind:'browser',label:'Browser'}); assert.deepEqual(played,[track(1).uri]);
  assert.equal(r.all().find(n=>n.className==='ml-button ml-play').disabled,true);
  r.api.destroy();
});

test('Compact Mini-Player steuert nur Browseraudio, begrenzt Seek und nutzt nur Artworkproxy', async () => {
  const r = rig(url=>url.includes('browser-status') ? {available:true} : {tracks:[{...track(1),artwork_url:'https://untrusted.invalid/private'}],next_offset:null},{compact:true});
  await r.api.ready;
  await r.button('Abspielen').fire('click');
  const audio=r.audio(), art=r.all().find(n=>n.tag==='img');
  assert.equal(art.src,'api/music/artwork?uri=local%3A%2F%2Ftrack%2F1');
  const toggle=r.all().find(n=>n.attributes['aria-label']==='Wiedergabe pausieren');
  await toggle.fire('click'); assert.equal(audio.paused,true);
  await toggle.fire('click'); assert.equal(audio.playCount,2);
  audio.duration=123; audio.currentTime=30; await audio.fire('timeupdate');
  const seek=r.all().find(n=>n.className==='ml-seek'); assert.equal(seek.disabled,false); assert.equal(seek.max,'123');
  seek.value='999'; await seek.fire('input'); assert.equal(audio.currentTime,123);
  r.api.setPlaybackTarget({kind:'speaker',label:'Küche'}); assert.equal(audio.paused,false);
  const before=audio.playCount; await toggle.fire('click'); assert.equal(audio.paused,true); assert.equal(audio.playCount,before);
  seek.value='5'; await seek.fire('input'); assert.equal(audio.currentTime,5);
  await art.fire('error'); assert.equal(art.hidden,true); assert.equal(art.src,'');
  r.api.destroy();
});

test('Compact Sammlungen bleiben bei Speakerziel verborgen und können dort nicht starten', async () => {
  const r = rig(url=>url.includes('albums?') ? {items:[{kind:'album',uri:'a',title:'A'}],next_offset:null} : url.includes('collection?') ? {complete:true,tracks:[track(1),track(2)]} : standard(url),{compact:true,onPlayTrack:()=>assert.fail('Keine implizite Speakeraktion')});
  await r.api.ready; await r.button('Alben').fire('click'); await tick(); await r.button('Öffnen').fire('click'); await tick();
  const all=r.button('Alle Bibliothekstitel auf diesem Gerät abspielen'); assert.equal(all.hidden,false);
  r.api.setPlaybackTarget({kind:'speaker',label:'Büro'}); assert.equal(all.hidden,true);
  await all.fire('click'); assert.equal(r.audio().playCount,0);
  r.api.setPlaybackTarget({kind:'browser'}); await all.fire('click'); assert.equal(r.audio().playCount,1);
  r.api.destroy();
});

test('Zielwechsel und Speaker-Play lassen laufende Browserqueue unangetastet', async () => {
  const played=[];
  const r = rig(url=>url.includes('albums?') ? {items:[{kind:'album',uri:'a',title:'A'}],next_offset:null} : url.includes('collection?') ? {complete:true,tracks:[track(1),track(2)]} : standard(url),{compact:true,onPlayTrack:value=>played.push(value.uri)});
  await r.api.ready; await r.button('Alben').fire('click'); await tick(); await r.button('Öffnen').fire('click'); await tick();
  await r.button('Alle Bibliothekstitel auf diesem Gerät abspielen').fire('click');
  const audio=r.audio(), src=audio.src; assert.equal(audio.paused,false);
  r.api.setPlaybackTarget({kind:'speaker',label:'Wohnzimmer'});
  const row=r.all().find(n=>n.className==='ml-row');
  r.api.setPlaybackTarget({kind:'speaker',label:'Wohnzimmer'}); assert.strictEqual(r.all().find(n=>n.className==='ml-row'),row);
  assert.equal(audio.paused,false); assert.equal(audio.src,src); assert.equal(audio.playCount,1);
  await r.all().find(n=>n.className==='ml-button ml-play').fire('click');
  assert.deepEqual(played,[track(1).uri]); assert.equal(audio.paused,false); assert.equal(audio.src,src);
  await audio.fire('ended'); assert.match(audio.src,/track%2F2$/); assert.equal(audio.playCount,2);
  r.api.setPlaybackTarget({kind:'browser'}); assert.equal(audio.playCount,2); assert.equal(audio.paused,false);
  r.api.destroy();
});

const artistItem = {kind:'artist',uri:'library://artist/7',title:'Echter Künstler'};
const albumItem = {kind:'album',uri:'library://album/8',title:'Echtes Album',artist:'Echter Künstler'};
test('Gemischte Vorschläge suchen automatisch getrennt nach echten Künstlern, Alben und Titeln', async () => {
  const r = rig(url=>url.includes('browser-status') ? {available:true} : url.includes('/artists?') ? {items:[artistItem]} : url.includes('/albums?') ? {items:[albumItem]} : {tracks:[track(1)]},{compact:true});
  await r.api.ready;
  assert.deepEqual(r.all().filter(n=>n.className==='ml-group-heading').map(n=>n.textContent),['Künstler','Alben','Titel']);
  r.search().value='Künstler'; await r.search().fire('input');
  const before=r.calls.length; await r.flush();
  assert.equal(r.calls.length,before+3);
  assert.ok(['artists','albums','tracks'].every(kind=>r.calls.some(url=>url.includes('/'+kind+'?q=K%C3%BCnstler'))));
  assert.equal(r.audio().playCount,0);
  r.api.destroy();
});

test('Künstler öffnet lokale Alben/Titel ohne autoplay und kehrt einschließlich Suchfilter zurück', async () => {
  const r = rig(url=>url.includes('browser-status') ? {available:true} : url.includes('/artists?') ? {items:[artistItem]} : url.includes('/artist?') ? {items:[albumItem,{kind:'track',...track(1)}],source_limited:true} : url.includes('/albums?') ? {items:[]} : url.includes('/collection?') ? {complete:true,tracks:[track(1)]} : {tracks:[]},{compact:true});
  await r.api.ready; await r.button('Künstler').fire('click'); await tick();
  r.search().value='Echt'; await r.search().fire('input'); await r.flush();
  await r.button('Öffnen').fire('click'); await tick();
  assert.ok(r.calls.some(url=>url.includes('/artist?uri=library%3A%2F%2Fartist%2F7')));
  assert.ok(r.all().some(n=>n.textContent.includes('Künstlerliste begrenzt')));
  assert.equal(r.audio().playCount,0);
  assert.equal(r.button('Alle Bibliothekstitel auf diesem Gerät abspielen').hidden,true);
  await r.button('Öffnen').fire('click'); await tick();
  await r.button('← Zurück zur Übersicht').fire('click'); await tick();
  assert.equal(r.all().find(n=>n.className==='ml-location').textContent,'Echter Künstler');
  await r.button('← Zurück zur Übersicht').fire('click'); await tick();
  assert.equal(r.search().value,'Echt'); assert.equal(r.button('Künstler').attributes['aria-pressed'],'true');
  assert.ok(r.calls.at(-1).includes('/artists?q=Echt'));
  assert.equal(r.audio().playCount,0); r.api.destroy();
});

test('Fehler einer Vorschlagskategorie bleiben sichtbar, leere Gruppen entfallen, Titel bleiben nutzbar', async () => {
  const r = rig(url=> {
    if (url.includes('browser-status')) return {available:true};
    if (url.includes('/artists?')) throw Error('upstream private error');
    if (url.includes('/albums?')) return {items:[]};
    return {tracks:[track(1)]};
  },{compact:true});
  await r.api.ready;
  assert.deepEqual(r.all().filter(n=>n.className==='ml-group-heading').map(n=>n.textContent),['Titel']);
  assert.ok(r.all().some(n=>n.textContent.includes('Künstler konnten nicht geladen werden.')));
  assert.ok(!r.all().some(n=>n.textContent.includes('private error')));
  await r.button('Abspielen').fire('click'); assert.equal(r.audio().playCount,1);
  r.api.destroy();
});

test('Veraltete gemischte Vorschläge werden abgebrochen und überschreiben keine neuere Suche', async () => {
  const pending=[];
  const r=rig(url=>url.includes('browser-status') ? {available:true} : new Promise(resolve=>pending.push({url,resolve})),{compact:true});
  await tick(); assert.equal(pending.length,3);
  r.search().value='Neu'; await r.search().fire('input');
  assert.equal(r.signals.filter(signal=>signal.aborted).length,3);
  for(const request of pending) request.resolve(request.url.includes('/tracks?') ? {tracks:[{...track(9),title:'Veraltet'}]} : {items:[]});
  await r.flush();
  for(const request of pending.slice(3)) request.resolve(request.url.includes('/artists?') ? {items:[artistItem]} : request.url.includes('/tracks?') ? {tracks:[track(1)]} : {items:[]});
  await tick(); await r.api.ready;
  assert.ok(!r.all().some(n=>n.textContent==='Veraltet'));
  assert.ok(r.all().some(n=>n.textContent==='Echter Künstler'));
  assert.equal(r.audio().playCount,0); r.api.destroy();
});

test('Leerer Compactplayer bleibt verborgen, Verfügbarkeitsfehler bleiben außerhalb sichtbar', async () => {
  const dock=new Element('aside');
  const r=rig(url=>url.includes('browser-status') ? {available:false,reason:'Keine Freigabe'} : url.includes('/tracks?') ? {tracks:[]} : {items:[]},{compact:true,playerContainer:dock});
  await r.api.ready; assert.equal(dock.children[0].hidden,true);
  assert.ok(descendants(r.container).some(n=>!n.hidden && n.textContent.includes('Keine Freigabe')));
  r.api.destroy();
});

test('Compactplayer erscheint für vorbereiteten Ordner vor Start, Vorbereitung bleibt ohne Audio', async () => {
  const r=rig(url=>url.includes('/folders?') ? {items:[{kind:'folder',uri:'local://A',title:'A'}]} : url.includes('/collection?') ? {complete:true,tracks:[track(1)]} : standard(url),{compact:true});
  await r.api.ready; const player=r.all().find(n=>n.className==='ml-browser-player ml-mini-player'); assert.equal(player.hidden,true);
  await r.button('Ordner').fire('click'); await tick(); await r.button('Öffnen').fire('click'); await tick();
  await r.button('Ganzen Ordner auf diesem Gerät abspielen').fire('click'); await tick();
  assert.equal(player.hidden,false); assert.equal(r.audio().playCount,0);
  await r.button('Ordner „A“ starten').fire('click'); assert.equal(r.audio().playCount,1);
  r.api.destroy();
});

test('Gemischte Vorschläge bleiben kurz und verweisen für weitere echte Treffer auf den Filter', async () => {
  const r=rig(url=>url.includes('browser-status') ? {available:true} : url.includes('/artists?') ? {items:Array.from({length:8},(_,n)=>({...artistItem,uri:'library://artist/'+n,title:'Künstler '+n}))} : url.includes('/tracks?') ? {tracks:[]} : {items:[]},{compact:true});
  await r.api.ready; assert.equal(r.all().filter(n=>n.className==='ml-row').length,6);
  assert.equal(r.button('Mehr laden').hidden,true);
  await r.button('Alle Künstler').fire('click'); await tick();
  assert.equal(r.button('Künstler').attributes['aria-pressed'],'true'); assert.equal(r.all().filter(n=>n.className==='ml-row').length,8);
  r.api.destroy();
});

test('Leere gefilterte Vorschlagsseite bleibt nachladbar und behauptet keinen vollständigen Nulltreffer', async () => {
  const r = rig(url => url.includes('browser-status') ? {available:true} : url.includes('offset=0') ? (url.includes('/tracks?') ? {tracks:[],next_offset:50} : {items:[]}) : url.includes('/tracks?') ? {tracks:[track(77)]} : {items:[]}, {compact:true});
  await r.api.ready;
  assert.equal(r.button('Mehr laden').hidden,false);
  assert.ok(r.all().some(n=>n.textContent.includes('Weitere Ergebnisse können geladen werden.')));
  assert.ok(!r.all().some(n=>n.textContent==='Keine Einträge gefunden.'));
  await r.button('Mehr laden').fire('click'); await tick();
  assert.ok(r.all().some(n=>n.textContent==='Titel 77'));
  assert.equal(r.button('Mehr laden').hidden,true);
  assert.equal(r.audio().playCount,0); r.api.destroy();
});

test('Nachladen gemischter Treffer zeigt auch Titel nach den ersten sechs und entfernt Duplikate', async () => {
  const r=rig(url=>url.includes('browser-status') ? {available:true} : url.includes('/tracks?') ? (url.includes('offset=0') ? {tracks:Array.from({length:8},(_,n)=>track(n)),next_offset:50} : {tracks:[track(7),track(8)]}) : {items:[]},{compact:true});
  await r.api.ready;
  assert.equal(r.all().filter(n=>n.className==='ml-row').length,6);
  assert.equal(r.button('Mehr laden').hidden,false);
  await r.button('Mehr laden').fire('click'); await tick();
  assert.equal(r.all().filter(n=>n.className==='ml-row').length,9);
  assert.ok(r.all().some(n=>n.textContent==='Titel 8'));
  r.api.destroy();
});

test('Fehlgeschlagene Kategorie verhindert weder Nachladen noch den sichtbaren Fehlerhinweis', async () => {
  const r=rig(url=> {
    if(url.includes('browser-status')) return {available:true};
    if(url.includes('/artists?')) throw Error('private upstream detail');
    if(url.includes('/tracks?')) return url.includes('offset=0') ? {tracks:[],next_offset:50} : {tracks:[track(88)]};
    return {items:[]};
  },{compact:true});
  await r.api.ready; assert.equal(r.button('Mehr laden').hidden,false);
  await r.button('Mehr laden').fire('click'); await tick();
  assert.ok(r.all().some(n=>n.textContent==='Titel 88'));
  assert.ok(r.all().some(n=>n.textContent.includes('Künstler konnten nicht geladen werden.')));
  assert.ok(!r.all().some(n=>n.textContent.includes('private upstream detail')));
  r.api.destroy();
});

test('Neue Suche verwirft verspätete Nachladeseite und setzt Vorschlagsbegrenzung zurück', async () => {
  let release;
  const r=rig(url=> {
    if(url.includes('browser-status')) return {available:true};
    if(!url.includes('/tracks?')) return {items:[]};
    if(url.includes('q=Neu')) return {tracks:Array.from({length:8},(_,n)=>({...track(100+n),title:'Neu '+n}))};
    if(url.includes('offset=50')) return new Promise(resolve=>release=resolve);
    return {tracks:[track(1)],next_offset:50};
  },{compact:true});
  await r.api.ready;
  const loading=r.button('Mehr laden').fire('click'); await tick();
  r.search().value='Neu'; await r.search().fire('input'); await r.flush();
  release({tracks:[{...track(2),title:'Veralteter Titel'}]}); await loading; await tick();
  assert.ok(!r.all().some(n=>n.textContent==='Veralteter Titel'));
  assert.equal(r.all().filter(n=>n.className==='ml-row').length,6);
  assert.ok(r.all().some(n=>n.textContent==='Neu 0'));
  assert.equal(r.audio().playCount,0); r.api.destroy();
});

test('Compacttransport verwendet beschriftete Icons und einen weiterhin ausklappbaren Fortschritt', async () => {
  const r=rig(standard,{compact:true}); await r.api.ready;
  const previous=r.all().find(n=>n.attributes['aria-label']==='Vorheriger Titel');
  const next=r.all().find(n=>n.attributes['aria-label']==='Nächster Titel');
  const toggle=r.all().find(n=>n.attributes['aria-label']==='Wiedergabe fortsetzen');
  assert.equal(previous.textContent,'‹'); assert.equal(next.textContent,'›'); assert.equal(toggle.textContent,'▶');
  assert.ok(r.all().some(n=>n.tag==='details' && n.className==='ml-progress'));
  await r.button('Abspielen').fire('click'); assert.equal(toggle.textContent,'❚❚'); assert.equal(toggle.attributes['aria-label'],'Wiedergabe pausieren');
  await toggle.fire('click'); assert.equal(toggle.textContent,'▶'); assert.equal(r.audio().paused,true);
  assert.match(css,/#musicBrowserDock\.workspace-player-dock \{ padding:0; border:0/);
  r.api.destroy();
});

test('Künstlerdetails haben für zwei lesende MA-Aufrufe denselben 35-s-Timeout wie Collections', async () => {
  const r=rig(url=>url.includes('browser-status') ? {available:true} : url.includes('/artists?') ? {items:[artistItem]} : url.includes('/artist?') ? new Promise(()=>{}) : url.includes('/tracks?') ? {tracks:[]} : {items:[]},{compact:true});
  await r.api.ready;
  const opening=r.button('Öffnen').fire('click'); await tick();
  assert.ok([...r.timers.values()].some(value=>value.ms===35000));
  await r.expire(12000); assert.ok([...r.timers.values()].some(value=>value.ms===35000));
  await r.expire(35000); await opening;
  assert.ok(r.all().some(n=>n.textContent==='Die Bibliothek antwortet zu langsam. Bitte versuche es erneut.'));
  assert.equal(r.audio().playCount,0); r.api.destroy();
});
