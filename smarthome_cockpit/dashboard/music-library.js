/* Independent library browser: all metadata is rendered as text, never HTML. */
(function (global) {
  'use strict';
  function initMusicLibrary(container, {onSelectTrack} = {}) {
    if (!container) throw new Error('Container für Musikbibliothek fehlt.');
    const doc = container.ownerDocument || document;
    let destroyed = false, generation = 0, playGeneration = 0, timer;
    let tab = 'tracks', query = '', path = '', collection = null, returnState = null;
    const folderHistory = [];
    let folderQueueRequest = 0;
    let items = [], nextOffset = null, busy = false, available = false, selectedTrackUri = null;
    let queue = [], queueIndex = -1, preparedQueue = [];
    let selectionButtons = [];
    const handlers = [];
    const requests = new Set();
    const el = (tag, className, text) => {
      const node = doc.createElement(tag); node.className = className;
      if (text !== undefined) node.textContent = text;
      return node;
    };
    const on = (node, type, fn) => { node.addEventListener(type, fn); handlers.push([node, type, fn]); };
    const button = (text, fn) => { const node = el('button', 'ml-button', text); node.type = 'button'; node.addEventListener('click', fn); return node; };
    const root = el('section', 'ml-library');
    root.setAttribute('aria-label', 'Musikbibliothek und Wiedergabe auf diesem Gerät');
    const heading = el('h3', 'ml-heading', 'Musikbibliothek');
    const tabs = el('div', 'ml-tabs'); tabs.setAttribute('aria-label', 'Ansicht der Bibliothek');
    const tabButtons = {};
    for (const [key, label] of [['tracks', 'Titel'], ['albums', 'Alben'], ['folders', 'Ordner']]) {
      tabButtons[key] = button(label, () => { tab = key; path = ''; folderHistory.length = 0; collection = null; returnState = null; reload(); });
      tabs.append(tabButtons[key]);
    }
    const searchLabel = el('label', 'ml-search', 'Bibliothek durchsuchen');
    const search = el('input', 'ml-input'); search.type = 'search'; search.placeholder = 'Titel, Interpret oder Album';
    searchLabel.append(search);
    const back = button('← Zurück zur Übersicht', () => {
      if (returnState) { tab = returnState.tab; query = returnState.query; path = returnState.path; search.value = query; }
      else if (folderHistory.length) { const previous = folderHistory.pop(); path = previous.path; query = previous.query; search.value = query; }
      collection = null; returnState = null; reload();
    }); back.hidden = true;
    const location = el('h4', 'ml-location');
    const listStatus = el('p', 'ml-status'); listStatus.setAttribute('role', 'status');
    const list = el('div', 'ml-list');
    const more = button('Mehr laden', () => loadPage(true)); more.hidden = true;
    const playFolder = button('Ganzen Ordner auf diesem Gerät abspielen', playFolderQueue); playFolder.hidden = true;
    const playAll = button('Alle Bibliothekstitel auf diesem Gerät abspielen', () => startQueue(items)); playAll.hidden = true;
    const browser = el('section', 'ml-browser-player');
    const browserTitle = el('h4', 'ml-heading', 'Auf diesem Gerät');
    const availability = el('p', 'ml-status', 'Browseraudio wird geprüft …'); availability.setAttribute('role', 'status');
    const current = el('p', 'ml-current', 'Noch kein Titel ausgewählt.');
    const audio = el('audio', 'ml-audio'); audio.controls = true; audio.preload = 'none';
    const controls = el('div', 'ml-controls');
    const previous = button('Vorheriger Titel', () => playAt(queueIndex - 1));
    const next = button('Nächster Titel', () => playAt(queueIndex + 1));
    const startPrepared = button('Wiedergabe starten', () => { startPrepared.hidden = true; startQueue(preparedQueue); }); startPrepared.hidden = true;
    controls.append(startPrepared, previous, next);
    const preparedStatus = el('p', 'ml-status'); preparedStatus.hidden = true; preparedStatus.setAttribute('role', 'status');
    const playbackStatus = el('p', 'ml-status'); playbackStatus.setAttribute('role', 'status');
    browser.append(browserTitle, availability, current, audio, preparedStatus, controls, playbackStatus);
    root.append(heading, tabs, searchLabel, back, location, listStatus, playFolder, playAll, list, more, browser);
    container.replaceChildren(root);

    function isRecord(value) {
      if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
      const prototype = Object.getPrototypeOf(value);
      return prototype === null || Object.getPrototypeOf(prototype) === null;
    }
    function validItems(value) {
      if (!Array.isArray(value) || !value.every(item => isRecord(item)
          && typeof item.uri === 'string' && item.uri.length > 0 && typeof item.title === 'string'
          && (item.artist === undefined || typeof item.artist === 'string')
          && (item.album === undefined || typeof item.album === 'string'))) {
        throw new Error('Die Bibliothek hat eine ungültige Antwort geliefert.');
      }
      return value;
    }
    // Deliberately self-contained: this widget also works without index.html's
    // private player/queue closure, and collections need a longer timeout.
    async function get(endpoint, params = {}) {
      const controller = new AbortController(); requests.add(controller);
      let timedOut = false;
      const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, endpoint === 'collection' ? 35000 : 12000);
      try {
        const suffix = new URLSearchParams(params).toString();
        const response = await global.fetch('api/music/' + endpoint + (suffix ? '?' + suffix : ''), {credentials: 'same-origin', signal: controller.signal});
        if (!response.ok) throw new Error('Die Bibliothek ist derzeit nicht erreichbar.');
        const data = await response.json();
        if (!isRecord(data)) throw new Error('Die Bibliothek hat eine ungültige Antwort geliefert.');
        return data;
      } catch (error) {
        if (timedOut) throw new Error('Die Bibliothek antwortet zu langsam. Bitte versuche es erneut.');
        if (error?.name === 'AbortError') throw new Error('Die Anfrage wurde abgebrochen.');
        if (error instanceof SyntaxError) throw new Error('Die Bibliothek hat eine ungültige Antwort geliefert.');
        throw error;
      } finally { clearTimeout(timeout); requests.delete(controller); }
    }
    function updateControls() {
      previous.disabled = !available || queueIndex <= 0;
      next.disabled = !available || queueIndex < 0 || queueIndex >= queue.length - 1;
      playAll.disabled = !available || !items.length || busy;
      playFolder.disabled = !available || busy;
    }
    function folderLabel(uri) {
      let relative = String(uri || '').replace(/^[^:]+:\/\//, '').replace(/^\/+|\/+$/g, '');
      try { relative = decodeURIComponent(relative); } catch (_) { /* Keep readable raw path if malformed. */ }
      return relative.split('/').filter(Boolean).join(' / ') || 'Musikbibliothek';
    }
    function render() {
      for (const key of Object.keys(tabButtons)) tabButtons[key].setAttribute('aria-pressed', String(tab === key));
      back.hidden = !collection && !folderHistory.length;
      location.textContent = collection ? collection.title : (tab === 'folders' ? folderLabel(path) : {tracks: 'Titel', albums: 'Alben'}[tab]);
      selectionButtons = [];
      list.replaceChildren();
      for (const item of items) {
        const isTrack = collection || tab === 'tracks' || item.kind === 'track';
        const row = el('div', 'ml-row');
        const meta = el('div', 'ml-meta');
        meta.append(el('span', 'ml-title', item.title || 'Ohne Titel'));
        meta.append(el('span', 'ml-detail', [item.artist, item.album].filter(Boolean).join(' · ')));
        const action = button(isTrack ? 'Auf diesem Gerät abspielen' : 'Öffnen', () => isTrack ? startQueue([item]) : item.kind === 'folder' ? openFolder(item) : openCollection(item));
        action.setAttribute('aria-label', (isTrack ? 'Abspielen: ' : 'Öffnen: ') + (item.title || 'Ohne Titel'));
        if (isTrack) action.disabled = !available;
        const actions = el('div', 'ml-row-actions'); actions.append(action);
        if (isTrack && typeof onSelectTrack === 'function') {
          const select = button('Für Lautsprecher auswählen', () => {
            selectedTrackUri = item.uri;
            for (const entry of selectionButtons) entry.button.setAttribute('aria-pressed', String(entry.uri === selectedTrackUri));
            onSelectTrack({...item});
          });
          select.setAttribute('aria-label', 'Für Lautsprecher auswählen: ' + (item.title || 'Ohne Titel'));
          select.setAttribute('aria-pressed', String(selectedTrackUri === item.uri));
          selectionButtons.push({button: select, uri: item.uri});
          actions.append(select);
        }
        row.append(meta, actions); list.append(row);
      }
      more.hidden = collection !== null || nextOffset === null;
      more.disabled = busy;
      playAll.hidden = !collection;
      playFolder.hidden = tab !== 'folders' || !!collection || !path;
      updateControls();
    }
    function reload() {
      clearTimeout(timer); ++generation; busy = false;
      items = []; nextOffset = null; render();
      return loadPage(false);
    }
    async function loadPage(append) {
      if (busy || destroyed || collection) return;
      const request = generation, requestTab = tab, requestQuery = query;
      busy = true; listStatus.textContent = 'Wird geladen …'; render();
      try {
        const params = {q: requestQuery, offset: append ? nextOffset : 0};
        if (requestTab === 'folders') params.path = path;
        const data = await get(requestTab, params);
        if (destroyed || request !== generation) return;
        const incoming = requestTab === 'tracks' ? data.tracks : data.items;
        validItems(incoming);
        items = append ? items.concat(incoming) : incoming;
        nextOffset = Number.isInteger(data.next_offset) && data.next_offset >= 0 && data.next_offset > Number(params.offset) ? data.next_offset : null;
        listStatus.textContent = items.length ? items.length + ' Einträge angezeigt.' : 'Keine Einträge gefunden.';
      } catch (error) {
        if (request === generation && !destroyed) listStatus.textContent = error.message;
      } finally {
        if (request === generation && !destroyed) { busy = false; render(); }
      }
    }
    function openFolder(item) {
      folderHistory.push({path, query}); path = item.uri; query = ''; search.value = '';
      collection = null; returnState = null; reload();
    }
    async function playFolderQueue() {
      if (!available || !path || destroyed) return;
      const request = ++folderQueueRequest;
      const requestedPath = path;
      playFolder.disabled = true;
      playbackStatus.textContent = 'Vollständiger Ordner wird geladen …';
      try {
        const data = await get('collection', {uri: requestedPath});
        if (destroyed || request !== folderQueueRequest) return;
        if (data.complete !== true) throw new Error('Der Ordner ist nicht vollständig verfügbar.');
        validItems(data.tracks);
        if (!data.tracks.length) playbackStatus.textContent = 'Dieser Ordner enthält keine Titel.';
        else {
          preparedQueue = data.tracks.map(track => ({...track}));
          const name = typeof data.title === 'string' && data.title ? (data.title.includes('://') ? folderLabel(data.title) : data.title) : folderLabel(requestedPath);
          const relativePath = folderLabel(requestedPath);
          preparedStatus.textContent = 'Ordner bereit: ' + name + (name === relativePath ? '' : ' · ' + relativePath) + ' · ' + preparedQueue.length + ' Titel.'; preparedStatus.hidden = false;
          startPrepared.textContent = 'Ordner „' + name + '“ starten';
          playbackStatus.textContent = ''; startPrepared.hidden = false; updateControls();
        }
      } catch (error) { if (!destroyed && request === folderQueueRequest) playbackStatus.textContent = error.message; }
      finally { if (!destroyed) updateControls(); }
    }
    async function openCollection(item) {
      clearTimeout(timer);
      const request = ++generation;
      returnState = {tab, query, path}; collection = {title: item.title || 'Sammlung'};
      items = []; nextOffset = null; busy = true; listStatus.textContent = 'Vollständige Titelliste wird geladen …'; render();
      try {
        const data = await get('collection', {uri: item.uri});
        if (destroyed || request !== generation) return;
        if (data.complete !== true) throw new Error('Die Sammlung ist nicht vollständig verfügbar.');
        validItems(data.tracks);
        collection.title = typeof data.title === 'string' && data.title ? data.title : collection.title; items = data.tracks;
        listStatus.textContent = items.length ? items.length + ' Bibliothekstitel.' : 'Diese Sammlung enthält keine Titel.';
      } catch (error) {
        if (request === generation && !destroyed) listStatus.textContent = error.message;
      } finally {
        if (request === generation && !destroyed) { busy = false; render(); }
      }
    }
    function metadata(track) {
      if (!global.navigator?.mediaSession) return;
      try {
        if (global.MediaMetadata) global.navigator.mediaSession.metadata = new global.MediaMetadata({title: track.title || 'Ohne Titel', artist: track.artist || '', album: track.album || ''});
      } catch (_) { /* Browser support varies; audio controls remain available. */ }
    }
    async function playAt(index) {
      if (!available || destroyed || index < 0 || index >= queue.length) return;
      const track = queue[index];
      if (typeof track.uri !== 'string' || !track.uri) { playbackStatus.textContent = 'Dieser Titel hat keine gültige Audioadresse.'; return; }
      const request = ++playGeneration;
      queueIndex = index; audio.pause();
      audio.src = 'api/music/audio?uri=' + encodeURIComponent(track.uri);
      current.textContent = (track.title || 'Ohne Titel') + ' · ' + (index + 1) + ' von ' + queue.length;
      playbackStatus.textContent = ''; metadata(track); updateControls();
      try { await audio.play(); }
      catch (_) { if (request === playGeneration && !destroyed) playbackStatus.textContent = 'Wiedergabe nicht möglich. Prüfe das Audioformat oder starte über die Audiosteuerung.'; }
    }
    function startQueue(tracks) {
      if (!available || !tracks.length) return;
      ++folderQueueRequest; startPrepared.hidden = true; preparedStatus.hidden = true; queue = tracks.map(track => ({...track})); return playAt(0);
    }
    on(search, 'input', () => {
      clearTimeout(timer); query = search.value.trim(); collection = null; returnState = null;
      ++generation; busy = false; items = []; nextOffset = null; render();
      listStatus.textContent = 'Suche …';
      timer = setTimeout(() => loadPage(false), 300);
    });
    on(audio, 'ended', () => {
      if (queueIndex < queue.length - 1) playAt(queueIndex + 1);
      else playbackStatus.textContent = 'Warteschlange beendet.';
    });
    on(audio, 'error', () => {
      audio.pause(); playbackStatus.textContent = 'Audio konnte nicht wiedergegeben werden. Das Format wird möglicherweise nicht unterstützt. Der Titel wurde nicht übersprungen.';
    });
    const mediaSession = global.navigator?.mediaSession;
    if (mediaSession) {
      for (const [action, handler] of Object.entries({play: () => { if (available && queueIndex >= 0) audio.play().catch(() => { playbackStatus.textContent = 'Wiedergabe nicht möglich.'; }); }, pause: () => audio.pause(), previoustrack: () => playAt(queueIndex - 1), nexttrack: () => playAt(queueIndex + 1)})) {
        try { mediaSession.setActionHandler(action, handler); } catch (_) { /* Unsupported action. */ }
      }
    }
    updateControls();
    const ready = Promise.all([reload(), get('browser-status').then(data => {
      if (destroyed) return;
      available = data.available === true;
      availability.textContent = available ? 'Browseraudio verfügbar. Wiedergabe startet erst nach Deinem Klick.' : 'Browseraudio nicht verfügbar. Du kannst die Bibliothek weiterhin lesen.' + (data.reason ? ' ' + data.reason : '');
      render();
    }).catch(() => {
      if (!destroyed) availability.textContent = 'Browseraudio derzeit nicht verfügbar. Die Bibliothek bleibt lesbar.';
    })]);
    return {ready, destroy() {
      destroyed = true; for (const controller of requests) controller.abort();
      ++folderQueueRequest; ++generation; ++playGeneration; clearTimeout(timer); audio.pause(); audio.removeAttribute('src'); audio.load();
      for (const [node, type, fn] of handlers) node.removeEventListener(type, fn);
      if (mediaSession) for (const action of ['play', 'pause', 'previoustrack', 'nexttrack']) { try { mediaSession.setActionHandler(action, null); } catch (_) {} }
      container.replaceChildren();
    }};
  }
  global.initMusicLibrary = initMusicLibrary;
})(typeof window !== 'undefined' ? window : globalThis);
