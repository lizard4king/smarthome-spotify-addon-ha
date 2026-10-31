/* Independent library browser: all metadata is rendered as text, never HTML. */
(function (global) {
  'use strict';
  function initMusicLibrary(container, {onSelectTrack, onPlayTrack, compact = false, playerContainer} = {}) {
    if (!container) throw new Error('Container für Musikbibliothek fehlt.');
    const doc = container.ownerDocument || document;
    let destroyed = false, generation = 0, playGeneration = 0, timer;
    let tab = compact ? 'all' : 'tracks', query = '', path = '', collection = null, returnState = null, artistView = null;
    const folderHistory = [];
    let folderQueueRequest = 0;
    let items = [], nextOffset = null, busy = false, available = false, selectedTrackUri = null, expandedSuggestions = false;
    let queue = [], queueIndex = -1, preparedQueue = [];
    let selectionButtons = [], playbackTarget = {kind: 'browser', label: 'Dieses Gerät'};
    const handlers = [];
    const requests = new Set(), navigationRequests = new Set();
    const el = (tag, className, text) => {
      const node = doc.createElement(tag); node.className = className;
      if (text !== undefined) node.textContent = text;
      return node;
    };
    const on = (node, type, fn) => { node.addEventListener(type, fn); handlers.push([node, type, fn]); };
    const button = (text, fn) => { const node = el('button', 'ml-button', text); node.type = 'button'; node.addEventListener('click', fn); return node; };
    const root = el('section', 'ml-library' + (compact ? ' ml-compact' : ''));
    root.setAttribute('aria-label', 'Musikbibliothek und Wiedergabe auf diesem Gerät');
    const heading = el('h3', 'ml-heading', 'Musikbibliothek');
    const tabs = el('div', 'ml-tabs'); tabs.setAttribute('aria-label', 'Ansicht der Bibliothek');
    const tabButtons = {};
    for (const [key, label] of (compact ? [['all', 'Alle'], ['artists', 'Künstler'], ['albums', 'Alben'], ['tracks', 'Titel'], ['folders', 'Ordner']] : [['tracks', 'Titel'], ['albums', 'Alben'], ['folders', 'Ordner']])) {
      tabButtons[key] = button(label, () => chooseTab(key));
      tabs.append(tabButtons[key]);
    }
    const searchLabel = el('label', 'ml-search', 'Bibliothek durchsuchen');
    const search = el('input', 'ml-input'); search.type = 'search'; search.placeholder = 'Titel, Interpret oder Album';
    searchLabel.append(search);
    const back = button('← Zurück zur Übersicht', () => {
      if (returnState) { tab = returnState.tab; query = returnState.query; path = returnState.path; artistView = returnState.artistView || null; search.value = query; }
      else if (artistView) { const previous = artistView.returnState; tab = previous.tab; query = previous.query; path = previous.path; search.value = query; artistView = null; }
      else if (folderHistory.length) { const previous = folderHistory.pop(); path = previous.path; query = previous.query; search.value = query; }
      collection = null; returnState = null; reload();
    }); back.hidden = true;
    const location = el('h4', 'ml-location');
    const listStatus = el('p', 'ml-status'); listStatus.setAttribute('role', 'status');
    const list = el('div', 'ml-list');
    const more = button('Mehr laden', () => loadPage(true)); more.hidden = true;
    const playFolder = button('Ganzen Ordner auf diesem Gerät abspielen', playFolderQueue); playFolder.hidden = true;
    const playAll = button('Alle Bibliothekstitel auf diesem Gerät abspielen', () => startQueue(items)); playAll.hidden = true;
    const browser = el('section', 'ml-browser-player' + (compact ? ' ml-mini-player' : ''));
    browser.setAttribute('aria-label', 'Player für dieses Gerät');
    const browserTitle = el('h4', 'ml-heading', 'Auf diesem Gerät');
    const availability = el('p', 'ml-status', 'Browseraudio wird geprüft …'); availability.setAttribute('role', 'status');
    const current = el('p', 'ml-current', 'Noch kein Titel ausgewählt.');
    const audio = el('audio', 'ml-audio'); audio.controls = !compact; audio.hidden = compact; audio.preload = 'none';
    const controls = el('div', 'ml-controls');
    const previous = button('Vorheriger Titel', () => playAt(queueIndex - 1));
    const next = button('Nächster Titel', () => playAt(queueIndex + 1));
    const startPrepared = button('Wiedergabe starten', () => { startPrepared.hidden = true; startQueue(preparedQueue); }); startPrepared.hidden = true;
    controls.append(startPrepared, previous, next);
    const preparedStatus = el('p', 'ml-status'); preparedStatus.hidden = true; preparedStatus.setAttribute('role', 'status');
    const playbackStatus = el('p', 'ml-status'); playbackStatus.setAttribute('role', 'status');
    const cover = el('div', 'ml-cover', '♪'); cover.setAttribute('aria-hidden', 'true');
    const artwork = el('img', 'ml-artwork'); artwork.alt = ''; artwork.hidden = true; cover.append(artwork);
    const artist = el('p', 'ml-artist');
    const toggle = button(compact ? '▶' : 'Abspielen', togglePlayback); toggle.className += ' ml-play';
    toggle.setAttribute('aria-label', 'Wiedergabe fortsetzen');
    const progress = el('details', 'ml-progress');
    const progressTitle = el('summary', 'ml-progress-title', 'Fortschritt');
    const seek = el('input', 'ml-seek'); seek.type = 'range'; seek.min = '0'; seek.max = '0'; seek.step = '1'; seek.value = '0'; seek.disabled = true; seek.setAttribute('aria-label', 'Wiedergabeposition');
    const time = el('span', 'ml-time', '0:00 / 0:00'); progress.append(progressTitle, seek, time);
    if (compact) {
      browserTitle.textContent = 'Dieses Gerät';
      const details = el('div', 'ml-now-playing'); details.append(browserTitle, current, artist);
      const now = el('div', 'ml-player-track'); now.append(cover, details);
      controls.replaceChildren(previous, toggle, next);
      previous.textContent = '‹'; next.textContent = '›';
      previous.setAttribute('aria-label', 'Vorheriger Titel'); next.setAttribute('aria-label', 'Nächster Titel');
      startPrepared.className += ' ml-start-prepared';
      browser.append(now, audio, preparedStatus, startPrepared, controls, progress, playbackStatus);
      availability.hidden = true;
    } else browser.append(browserTitle, availability, current, audio, preparedStatus, controls, playbackStatus);
    root.append(heading, tabs, searchLabel, back, location, listStatus, playFolder, playAll, list, more);
    container.replaceChildren(root);
    if (compact && playerContainer && playerContainer !== container) playerContainer.replaceChildren(browser);
    else root.append(browser);
    if (compact) { heading.hidden = true; root.append(availability); }

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
    async function get(endpoint, params = {}, navigation = false) {
      const controller = new AbortController(); requests.add(controller); if (navigation) navigationRequests.add(controller);
      let timedOut = false;
      const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, ['collection', 'artist'].includes(endpoint) ? 35000 : 12000);
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
      } finally { clearTimeout(timeout); requests.delete(controller); navigationRequests.delete(controller); }
    }
    function chooseTab(key) {
      tab = key; path = ''; folderHistory.length = 0; collection = null; artistView = null; returnState = null; reload();
    }
    function updateControls() {
      if (compact) browser.hidden = queueIndex < 0 && !preparedQueue.length;
      previous.disabled = !available || queueIndex <= 0;
      next.disabled = !available || queueIndex < 0 || queueIndex >= queue.length - 1;
      playAll.disabled = !available || !items.length || busy;
      playFolder.disabled = playbackTarget.kind !== 'browser' || !available || busy;
      toggle.disabled = !available || queueIndex < 0;
      startPrepared.hidden = playbackTarget.kind !== 'browser' || !preparedQueue.length;
      if (!compact && !preparedQueue.length) startPrepared.hidden = true;

    }
    function folderLabel(uri) {
      let relative = String(uri || '').replace(/^[^:]+:\/\//, '').replace(/^\/+|\/+$/g, '');
      try { relative = decodeURIComponent(relative); } catch (_) { /* Keep readable raw path if malformed. */ }
      return relative.split('/').filter(Boolean).join(' / ') || 'Musikbibliothek';
    }
    function render() {
      for (const key of Object.keys(tabButtons)) tabButtons[key].setAttribute('aria-pressed', String(tab === key));
      back.hidden = !collection && !artistView && !folderHistory.length;
      location.textContent = collection ? collection.title : artistView ? artistView.title : (tab === 'folders' ? folderLabel(path) : {all:'Vorschläge', artists:'Künstler', tracks: 'Titel', albums: 'Alben'}[tab]);
      selectionButtons = [];
      list.replaceChildren();
      const grouped = compact && (tab === 'all' || artistView) && !collection;
      const mixed = compact && tab === 'all' && !artistView && !collection;
      const visibleItems = grouped ? ['artist', 'album', 'track'].flatMap(kind => { const matches = items.filter(item => item.kind === kind); return mixed && !expandedSuggestions ? matches.slice(0, 6) : matches; }) : items;
      let group = '';
      for (const item of visibleItems) {
        if (grouped && group !== item.kind) {
          group = item.kind;
          const name = {artist:'Künstler',album:'Alben',track:'Titel'}[group];
          const groupHeader = el('div', 'ml-group-header'); groupHeader.append(el('h5', 'ml-group-heading', name));
          if (mixed && (items.filter(value => value.kind === group).length > 6 || nextOffset !== null)) {
            const key = {artist:'artists',album:'albums',track:'tracks'}[group];
            groupHeader.append(button('Alle ' + name, () => chooseTab(key)));
          }
          list.append(groupHeader);
        }
        const isTrack = collection || (!artistView && tab === 'tracks') || item.kind === 'track';
        const row = el('div', 'ml-row');
        const meta = compact && isTrack ? button('', () => selectTrack(item)) : el('div', 'ml-meta');
        meta.className = 'ml-meta' + (compact && isTrack ? ' ml-select' : '');
        if (compact && isTrack) { meta.setAttribute('aria-label', 'Auswählen: ' + (item.title || 'Ohne Titel')); meta.setAttribute('aria-pressed', String(selectedTrackUri === item.uri)); selectionButtons.push({button: meta, uri: item.uri}); }
        meta.append(el('span', 'ml-title', item.title || 'Ohne Titel'));
        meta.append(el('span', 'ml-detail', [item.artist, item.album].filter(Boolean).join(' · ')));
        const action = button(isTrack ? (compact ? 'Abspielen' : 'Auf diesem Gerät abspielen') : 'Öffnen', () => isTrack ? (compact ? playTrack(item) : startQueue([item])) : item.kind === 'folder' ? openFolder(item) : item.kind === 'artist' ? openArtist(item) : openCollection(item));
        if (compact && isTrack) action.className += ' ml-play';
        action.setAttribute('aria-label', (isTrack ? 'Abspielen: ' : 'Öffnen: ') + (item.title || 'Ohne Titel'));
        if (isTrack) action.disabled = compact && playbackTarget.kind === 'speaker' ? typeof onPlayTrack !== 'function' : !available;
        const actions = el('div', 'ml-row-actions'); actions.append(action);
        if (!compact && isTrack && typeof onSelectTrack === 'function') {
          const select = button('Für Lautsprecher auswählen', () => {
            selectTrack(item);
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
      playAll.hidden = !collection || playbackTarget.kind !== 'browser';
      playFolder.hidden = tab !== 'folders' || !!collection || !!artistView || !path || playbackTarget.kind !== 'browser';
      updateControls();
    }
    function reload() {
      clearTimeout(timer); ++generation; for (const controller of navigationRequests) controller.abort(); busy = false;
      items = []; nextOffset = null; expandedSuggestions = false; render();
      return loadPage(false);
    }
    async function loadPage(append) {
      if (busy || destroyed || collection) return;
      const request = generation, requestTab = tab, requestQuery = query;
      busy = true; listStatus.textContent = 'Wird geladen …'; render();
      try {
        const params = {q: requestQuery, offset: append ? nextOffset : 0};
        if (requestTab === 'folders') params.path = path;
        let incoming, pageOffset = null, notices = [];
        if (artistView) {
          const data = await get('artist', {uri: artistView.uri, offset: params.offset}, true);
          incoming = validItems(data.items);
          if (data.source_limited === true) notices.push('Künstlerliste begrenzt: Die Quelle liefert höchstens 500 Alben und 500 Titel.');
          pageOffset = data.next_offset;
        } else if (requestTab === 'all') {
          const kinds = ['artists', 'albums', 'tracks'];
          const results = await Promise.allSettled(kinds.map(kind => get(kind, params, true)));
          incoming = [];
          for (let index = 0; index < results.length; index++) {
            const result = results[index], kind = kinds[index];
            try {
              if (result.status !== 'fulfilled') throw result.reason;
              const data = result.value;
              incoming.push(...validItems(kind === 'tracks' ? data.tracks : data.items).map(item => ({...item, kind: kind === 'tracks' ? 'track' : kind === 'artists' ? 'artist' : 'album'})));
              if (Number.isInteger(data.next_offset)) pageOffset = Math.max(pageOffset || 0, data.next_offset);
            } catch (_) { notices.push({artists:'Künstler',albums:'Alben',tracks:'Titel'}[kind] + ' konnten nicht geladen werden.'); }
          }
        } else {
          const data = await get(requestTab, params, true);
          incoming = validItems(requestTab === 'tracks' ? data.tracks : data.items);
          pageOffset = data.next_offset;
        }
        if (destroyed || request !== generation) return;
        if (append && requestTab === 'all' && !artistView) expandedSuggestions = true;
        const combined = append ? items.concat(incoming) : incoming;
        const seen = new Set();
        items = combined.filter(item => {
          const key = (item.kind || requestTab) + '\0' + item.uri;
          if (seen.has(key)) return false;
          seen.add(key); return true;
        });
        nextOffset = Number.isInteger(pageOffset) && pageOffset >= 0 && pageOffset > Number(params.offset) ? pageOffset : null;
        listStatus.textContent = (items.length ? (requestTab === 'all' && !artistView && !expandedSuggestions ? 'Bis zu 6 Vorschläge je Kategorie.' : items.length + ' Einträge angezeigt.') : nextOffset !== null ? 'Auf dieser Seite keine passenden Einträge. Weitere Ergebnisse können geladen werden.' : 'Keine Einträge gefunden.') + (notices.length ? ' ' + notices.join(' ') : '');
      } catch (error) {
        if (request === generation && !destroyed) listStatus.textContent = error.message;
      } finally {
        if (request === generation && !destroyed) { busy = false; render(); }
      }
    }
    function openFolder(item) {
      folderHistory.push({path, query}); path = item.uri; query = ''; search.value = '';
      collection = null; artistView = null; returnState = null; reload();
    }
    function openArtist(item) {
      artistView = {uri: item.uri, title: item.title || 'Künstler', returnState: {tab, query, path}};
      collection = null; returnState = null; reload();
    }
    async function playFolderQueue() {
      if (!available || !path || destroyed || playbackTarget.kind !== 'browser') return;
      const request = ++folderQueueRequest;
      const requestedPath = path;
      playFolder.disabled = true;
      playbackStatus.textContent = 'Vollständiger Ordner wird geladen …';
      if (compact) listStatus.textContent = playbackStatus.textContent;
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
          playbackStatus.textContent = ''; startPrepared.hidden = false; if (compact) listStatus.textContent = 'Ordner ist bereit. Starte ihn im Player.'; updateControls();
        }
      } catch (error) { if (!destroyed && request === folderQueueRequest) { playbackStatus.textContent = error.message; if (compact) listStatus.textContent = error.message; } }
      finally { if (!destroyed) updateControls(); }
    }
    async function openCollection(item) {
      clearTimeout(timer);
      const request = ++generation;
      for (const controller of navigationRequests) controller.abort();
      returnState = {tab, query, path, artistView}; collection = {title: item.title || 'Sammlung'};
      items = []; nextOffset = null; busy = true; listStatus.textContent = 'Vollständige Titelliste wird geladen …'; render();
      try {
        const data = await get('collection', {uri: item.uri}, true);
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
      current.textContent = (track.title || 'Ohne Titel') + (compact ? '' : ' · ' + (index + 1) + ' von ' + queue.length);
      artist.textContent = [track.artist, track.album].filter(Boolean).join(' · ');
      artwork.hidden = !(compact && typeof track.artwork_url === 'string' && track.artwork_url);
      if (!artwork.hidden) artwork.src = 'api/music/artwork?uri=' + encodeURIComponent(track.uri);
      else artwork.removeAttribute('src');
      playbackStatus.textContent = ''; metadata(track); updateControls();
      try { await audio.play(); }
      catch (_) { if (request === playGeneration && !destroyed) playbackStatus.textContent = 'Wiedergabe nicht möglich. Prüfe das Audioformat oder starte über die Audiosteuerung.'; }
    }
    function startQueue(tracks) {
      if (destroyed || playbackTarget.kind !== 'browser' || !available || !tracks.length) return;
      ++folderQueueRequest; preparedQueue = []; startPrepared.hidden = true; preparedStatus.hidden = true; queue = tracks.map(track => ({...track})); return playAt(0);
    }
    function selectTrack(track) {
      if (destroyed) return;
      selectedTrackUri = track?.uri || null;
      for (const entry of selectionButtons) entry.button.setAttribute('aria-pressed', String(entry.uri === selectedTrackUri));
      if (track && typeof onSelectTrack === 'function') onSelectTrack({...track});
    }
    function playTrack(track) {
      if (destroyed) return;
      selectTrack(track);
      if (playbackTarget.kind === 'browser') return startQueue([track]);
      if (typeof onPlayTrack === 'function') {
        try { return Promise.resolve(onPlayTrack({...track})).catch(() => { if (!destroyed) playbackStatus.textContent = 'Lautsprecherwiedergabe konnte nicht gestartet werden.'; }); }
        catch (_) { playbackStatus.textContent = 'Lautsprecherwiedergabe konnte nicht gestartet werden.'; }
      }
    }
    function togglePlayback() {
      if (destroyed || !available || queueIndex < 0) return;
      if (!audio.paused) audio.pause();
      else return audio.play().catch(() => { if (!destroyed) playbackStatus.textContent = 'Wiedergabe nicht möglich. Bitte erneut starten.'; });
    }
    function updateProgress() {
      const duration = Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : 0;
      const position = Number.isFinite(audio.currentTime) ? audio.currentTime : 0;
      seek.max = String(duration); seek.value = String(Math.min(position, duration)); seek.disabled = !duration;
      const stamp = seconds => Math.floor(seconds / 60) + ':' + String(Math.floor(seconds % 60)).padStart(2, '0');
      time.textContent = stamp(position) + ' / ' + stamp(duration);
      toggle.textContent = compact ? (audio.paused ? '▶' : '❚❚') : (audio.paused ? 'Abspielen' : 'Pause');
      toggle.setAttribute('aria-label', audio.paused ? 'Wiedergabe fortsetzen' : 'Wiedergabe pausieren');
    }
    for (const event of ['play', 'pause', 'timeupdate', 'durationchange', 'loadedmetadata', 'ended']) on(audio, event, updateProgress);
    on(artwork, 'error', () => { artwork.hidden = true; artwork.removeAttribute('src'); });
    on(seek, 'input', () => {
      const position = Number(seek.value);
      if (!destroyed && Number.isFinite(audio.duration) && audio.duration > 0 && Number.isFinite(position)) audio.currentTime = Math.max(0, Math.min(position, audio.duration));
    });
    on(search, 'input', () => {
      clearTimeout(timer); query = search.value.trim(); collection = null; artistView = null; returnState = null;
      ++generation; for (const controller of navigationRequests) controller.abort(); busy = false; items = []; nextOffset = null; expandedSuggestions = false; render();
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
      for (const [action, handler] of Object.entries({play: () => { if (!destroyed && available && queueIndex >= 0) audio.play().catch(() => { playbackStatus.textContent = 'Wiedergabe nicht möglich.'; }); }, pause: () => audio.pause(), previoustrack: () => playAt(queueIndex - 1), nexttrack: () => playAt(queueIndex + 1)})) {
        try { mediaSession.setActionHandler(action, handler); } catch (_) { /* Unsupported action. */ }
      }
    }
    updateControls();
    const ready = Promise.all([reload(), get('browser-status').then(data => {
      if (destroyed) return;
      available = data.available === true;
      if (compact) availability.hidden = available;
      availability.textContent = available ? 'Browseraudio verfügbar. Wiedergabe startet erst nach Deinem Klick.' : 'Browseraudio nicht verfügbar. Du kannst die Bibliothek weiterhin lesen.' + (data.reason ? ' ' + data.reason : '');
      render();
    }).catch(() => {
      if (!destroyed) { availability.hidden = false; availability.textContent = 'Browseraudio derzeit nicht verfügbar. Die Bibliothek bleibt lesbar.'; }
    })]);
    return {ready, setSelectedTrack(track) {
      if (destroyed) return;
      selectedTrackUri = typeof track === 'string' ? track : track?.uri || null;
      for (const entry of selectionButtons) entry.button.setAttribute('aria-pressed', String(entry.uri === selectedTrackUri));
    }, setPlaybackTarget(target) {
      if (destroyed || !target || !['browser', 'speaker'].includes(target.kind)) return;
      const label = typeof target.label === 'string' && target.label ? target.label : (target.kind === 'browser' ? 'Dieses Gerät' : 'Lautsprecher');
      if (playbackTarget.kind === target.kind && playbackTarget.label === label) return;
      playbackTarget = {kind: target.kind, label};
      browserTitle.textContent = compact ? 'Dieses Gerät' : 'Auf diesem Gerät';
      browser.setAttribute('aria-label', 'Player: ' + browserTitle.textContent);
      render();
    }, destroy() {
      destroyed = true; for (const controller of requests) controller.abort();
      ++folderQueueRequest; ++generation; ++playGeneration; clearTimeout(timer); audio.pause(); audio.removeAttribute('src'); audio.load();
      for (const [node, type, fn] of handlers) node.removeEventListener(type, fn);
      if (mediaSession) for (const action of ['play', 'pause', 'previoustrack', 'nexttrack']) { try { mediaSession.setActionHandler(action, null); } catch (_) {} }
      container.replaceChildren();
      if (compact && playerContainer && playerContainer !== container) playerContainer.replaceChildren();
    }};
  }
  global.initMusicLibrary = initMusicLibrary;
})(typeof window !== 'undefined' ? window : globalThis);
