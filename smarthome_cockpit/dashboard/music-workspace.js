/* Source navigation only. Playback stays behind the existing controllers. */
(function (global) {
  'use strict';
  const TITLES = {erlene:'Spotify Erlene', mine:'Mein Spotify', own:'Eigene Musik', home:'Zuhause'};
  const normalize = value => String(value || '').normalize('NFKC').trim().toLocaleLowerCase('de-DE');

  function resolveSpotifySource(profiles, source, pinnedId = null) {
    const names = source === 'erlene' ? ['erlene', 'erlene andreia', 'erlene spotify', 'spotify erlene']
      : source === 'mine' ? ['andreas', 'mein spotify'] : [];
    const valid = Array.isArray(profiles) ? profiles.filter(profile => profile &&
      typeof profile.id === 'string' && profile.id.trim() && typeof profile.name === 'string' && profile.name.trim()) : [];
    if (new Set(valid.map(profile => profile.id)).size !== valid.length) return null;
    // Display names identify the intended account on first use. Opaque IDs are
    // never interpreted by position; the first verified ID stays pinned.
    if (pinnedId) return valid.find(profile => profile.id === pinnedId) || null;
    const matches = valid.filter(profile => names.includes(normalize(profile.name)));
    return matches.length === 1 ? matches[0] : null;
  }

  function initMusicWorkspace(doc, {spotify, ownMusic}) {
    const byId = id => doc.getElementById(id);
    const sources = byId('musicSources');
    const spotifyPanel = byId('spotifyWorkspace');
    const ownPanel = byId('ownMusicWorkspace');
    const homePanel = byId('homeWorkspace');
    const notice = byId('workspaceNotice');
    const back = byId('workspaceBack');
    const title = byId('workspaceTitle');
    const subtitle = byId('workspaceSubtitle');
    const profileSelect = byId('spotifyProfile');
    const playerSelect = byId('musicPlayer');
    const targetSelect = byId('workspaceMusicTarget');
    const pins = {erlene:null, mine:null};
    const searches = {erlene:'', mine:''};
    let source = null;
    let scopedProfile;
    let syncing = false;
    let playbackTarget = '';
    let playerSignature = '';
    let dockMode = null;

    function details(parent, label) {
      const node = doc.createElement('details'); node.className = 'workspace-more';
      const summary = doc.createElement('summary'); summary.textContent = label;
      node.append(summary); parent.append(node); return node;
    }
    const spotifyDock = doc.createElement('div'); spotifyDock.className = 'workspace-player-dock spotify-dock';
    spotifyDock.setAttribute('aria-label', 'Spotify-Player');
    const sessions = spotifyPanel.querySelector('.spotify-sessions');
    const spotifyActions = byId('spotifyPlay').parentElement;
    spotifyDock.append(sessions, spotifyActions);
    spotifyPanel.append(spotifyDock);
    const spotifyMore = details(spotifyPanel, 'Mehr');
    spotifyMore.append(byId('spotifyMessage'), byId('spotifyEmbedWrap'), byId('nowPlaying'));
    profileSelect.parentElement.hidden = true;
    const spotifyExtras = new Map();
    for (const card of doc.querySelectorAll('[data-spotify-slot]')) {
      card.querySelector('.spotify-session-enabled-label').hidden = true;
      const device = details(card, 'Gerät & Fortschritt');
      device.className = 'spotify-device-details';
      device.append(card.querySelector('.spotify-session-target'), card.querySelector('.spotify-seek'));
      const extra = doc.createElement('div');
      extra.append(card.querySelector('.spotify-session-status'));
      spotifyMore.append(extra); spotifyExtras.set(card, extra);
    }
    const ownMore = details(ownPanel, 'Mehr · Warteschlange & Auswahl');
    ownMore.append(byId('musicSelected'), byId('musicAdd'), byId('musicStop'),
      ownPanel.querySelector('.music-queue'));
    byId('musicPlay').hidden = true;
    byId('musicMore').parentElement.hidden = Boolean(ownMusic.library);
    // Legacy fallback actions remain available if the browser library is absent.
    if (!ownMusic.library) byId('musicPlay').hidden = false;
    const browserMore = details(ownPanel, 'Dieses Gerät · Browserplayer');
    browserMore.append(byId('musicBrowserDock'));

    function setScope(id) {
      if (scopedProfile === id) return;
      scopedProfile = id;
      spotify.setWorkspaceProfile(id);
    }
    function syncSpotify() {
      if (syncing) return;
      syncing = true;
      try {
        const profiles = spotify.getWorkspaceProfiles();
        const isSpotify = source === 'erlene' || source === 'mine';
        const matched = isSpotify ? resolveSpotifySource(profiles, source, pins[source]) : null;
        const chosen = matched?.card ? matched : null;
        if (chosen && !pins[source]) pins[source] = chosen.id;
        setScope(chosen?.id || null);
        for (const card of doc.querySelectorAll('[data-spotify-slot]')) {
          card.hidden = card !== chosen?.card;
          spotifyExtras.get(card).hidden = card !== chosen?.card;
        }
        if (!isSpotify) return;
        notice.hidden = Boolean(chosen);
        notice.textContent = chosen ? '' : 'Dieses Spotify-Profil ist nicht eindeutig verfügbar. Bitte prüfe die Verbindung unter Zuhause.';
        // Even during refresh/outage, search may never fall back to another account.
        const next = chosen?.id || '';
        if (profileSelect.value !== next) {
          profileSelect.value = next;
          profileSelect.dispatchEvent(new Event('change', {bubbles:true}));
        }
        spotifyDock.hidden = !chosen;
        const selected = chosen?.selected;
        spotifyActions.hidden = !selected;
        spotifyDock.classList.toggle('has-selection', Boolean(selected));
        if (chosen) {
          chosen.card.querySelector('.spotify-selection').hidden = !selected;
          if (selected) chosen.card.querySelector('.spotify-session-track').textContent = 'Bereit: ' + selected.title;
          chosen.card.querySelector('.spotify-previous').textContent = '⏮';
          chosen.card.querySelector('.spotify-next').textContent = '⏭';
          const toggle = chosen.card.querySelector('.spotify-toggle');
          toggle.textContent = toggle.getAttribute?.('aria-label')?.startsWith('Fortsetzen') ? '▶' : 'Ⅱ';
        }
        byId('spotifyPlay').textContent = '▶ Auswahl starten';
      } finally { syncing = false; }
    }

    function syncMusic() {
      const validated = Array.from(playerSelect.options).filter(option => option.value);
      const signature = JSON.stringify(validated.map(option => [option.value, option.textContent, option.disabled]));
      const stillKnown = validated.some(option => option.value === playbackTarget);
      // Keep a removed chosen speaker explicit and disabled; never redirect it.
      if (signature !== playerSignature) {
        playerSignature = signature;
        targetSelect.replaceChildren();
        const browser = doc.createElement('option'); browser.value = ''; browser.textContent = 'Dieses Gerät';
        targetSelect.append(browser);
        for (const old of validated) {
          const option = doc.createElement('option'); option.value = old.value;
          option.textContent = old.textContent; option.disabled = old.disabled;
          targetSelect.append(option);
        }
        if (playbackTarget && !stillKnown) {
          const missing = doc.createElement('option'); missing.value = playbackTarget;
          missing.textContent = 'Gewählter Lautsprecher nicht mehr verfügbar'; missing.disabled = true;
          targetSelect.append(missing);
        }
        targetSelect.value = playbackTarget;
      }
      playerSelect.value = stillKnown ? playbackTarget : '';
      // The hidden MA select keeps its original write lock. Browser stays usable.
      targetSelect.disabled = Boolean(playbackTarget) && playerSelect.disabled &&
        validated.some(option => !option.disabled);
      if (dockMode !== Boolean(playbackTarget)) {
        dockMode = Boolean(playbackTarget);
        browserMore.open = !dockMode;
        browserMore.classList.toggle('workspace-browser-primary', !dockMode);
        byId('musicBrowserDock').classList.toggle('workspace-browser-secondary', dockMode);
      }
      byId('musicBrowserDock').hidden = false;
      byId('musicSpeakerDock').hidden = !playbackTarget;
      const selected = Array.from(targetSelect.options).find(option => option.value === playbackTarget);
      ownMusic.library?.setPlaybackTarget?.({kind:playbackTarget ? 'speaker' : 'browser',
        label:playbackTarget ? selected?.textContent || 'Lautsprecher nicht verfügbar' : 'Dieses Gerät'});
      byId('musicSpeakerDock').setAttribute('aria-label', 'Player · ' + (selected?.textContent || 'Dieses Gerät'));
    }
    targetSelect.addEventListener('change', () => {
      const selected = targetSelect.selectedOptions[0];
      if (!selected || selected.disabled) { targetSelect.value = playbackTarget; return; }
      const next = selected.value;
      if (next && !Array.from(playerSelect.options).some(option => option.value === next && !option.disabled)) return;
      // Merely selecting a target never transfers, pauses or starts anything.
      playbackTarget = next;
      playerSelect.value = next;
      playerSelect.dispatchEvent(new Event('change', {bubbles:true}));
      syncMusic();
    });

    function open(next, {initial = false} = {}) {
      if (next !== null && !Object.hasOwn(TITLES, next)) return;
      if (source === 'erlene' || source === 'mine') searches[source] = byId('spotifyQuery').value;
      source = next;
      sources.hidden = source !== null;
      spotifyPanel.hidden = source !== 'erlene' && source !== 'mine';
      ownPanel.hidden = source !== 'own';
      homePanel.hidden = source !== 'home';
      back.hidden = source === null;
      byId('workspaceHome').hidden = source === 'home';
      byId('refresh').hidden = source !== 'home';
      title.textContent = TITLES[source] || 'Deine Musik';
      subtitle.textContent = source === null ? 'Was möchtest Du hören?' : source === 'home'
        ? 'Räume, Alexa, Lumi und Kameras.' : source === 'own'
          ? 'Deine Bibliothek. Hören auf diesem Gerät oder einem Lautsprecher.' : 'Deine Auswahl. Dein Wiedergabeziel.';
      notice.hidden = true;
      if (source === 'erlene' || source === 'mine') byId('spotifyQuery').value = searches[source];
      syncSpotify(); syncMusic();
      if (!initial) {
        global.scrollTo?.({top:0, left:0, behavior:'instant'});
        title.focus?.({preventScroll:true});
      }
      // Navigation only changes visibility; audio and speaker sessions persist.
    }
    for (const button of doc.querySelectorAll('[data-music-source]')) {
      button.addEventListener('click', () => open(button.dataset.musicSource));
    }
    back.addEventListener('click', () => open(null));
    byId('workspaceHome').addEventListener('click', () => open('home'));
    spotify.observeWorkspace(syncSpotify);
    open(null, {initial:true});
    return {open, syncSpotify, syncMusic, getSource:() => source};
  }
  global.resolveSpotifySource = resolveSpotifySource;
  global.initMusicWorkspace = initMusicWorkspace;
})(globalThis);
