(function (root) {
  'use strict';

  const HEX32 = /^[0-9a-f]{32}$/i;
  const HEX64 = /^[0-9a-f]{64}$/i;
  const POLL_MS = 700;
  const MAX_MESSAGES = 24;
  const REQUEST_TIMEOUT_MS = 12000;

  function localGermanVoices(synthesis) {
    return synthesis && typeof synthesis.getVoices === 'function'
      ? synthesis.getVoices().filter(v => v.localService === true && /^de(?:-|$)/i.test(v.lang || ''))
      : [];
  }

  function createLumiController(options) {
    const { fetch: request, view, recognitionFactory, synthesis, utteranceFactory,
      setTimer = setTimeout, clearTimer = clearTimeout,
      setRequestTimer = setTimeout, clearRequestTimer = clearTimeout,
      randomHex = defaultRandomHex } = options;
    let generation = 0;
    let active = false;
    let paused = false;
    let fatalPaused = false;
    let micPaused = false;
    let starting = false;
    let ending = false;
    let openingRequest = null;
    let enabled = false;
    let csrf = '';
    let controls = false;
    let current = null;
    let uncertainPending = false;
    let queue = [];
    let pollTimer = null;
    let recognition = null;
    let micWanted = false;
    let micRunning = false;
    let speaking = false;
    let speechSerial = 0;
    let speechQueue = [];
    let headphoneMode = false;
    let recognizerAvailable = false;
    let restartTimes = [];
    let seenFinal = new Set();
    let transcript = [];

    function emit() {
      view.update({ active, paused, fatalPaused, micPaused, starting, ending, enabled, controls, current: !!current,
        queued: queue.length, speaking, micRunning, headphoneMode,
        transcript: transcript.slice() });
    }
    function status(label, hint = '') { view.status(label, hint); emit(); }
    function add(role, text) {
      transcript.push({ role, text });
      if (transcript.length > MAX_MESSAGES) transcript = transcript.slice(-MAX_MESSAGES);
      emit();
    }
    function clearPending() {
      queue = [];
      current = null;
      if (pollTimer !== null) clearTimer(pollTimer);
      pollTimer = null;
      emit();
    }
    function stopMic() {
      micWanted = false;
      if (recognition) {
        try { recognition.abort(); } catch (_) { /* already stopped */ }
      }
      micRunning = false;
      emit();
    }
    function stopSpeech() {
      ++speechSerial;
      speechQueue = [];
      speaking = false;
      if (synthesis && typeof synthesis.cancel === 'function') synthesis.cancel();
      emit();
    }
    function networkFailure() {
      uncertainPending = uncertainPending || !!current;
      clearPending();
      stopMic();
      stopSpeech();
      paused = true;
      fatalPaused = true;
      status('Verbindung unterbrochen', 'Unklare Übermittlung wird nicht automatisch wiederholt. Gespräch beenden und neu starten.');
    }
    async function timedRequest(path, options) {
      const aborter = new AbortController();
      let timer;
      const timeout = new Promise((_, reject) => {
        timer = setRequestTimer(() => {
          aborter.abort();
          reject(new Error('request timeout'));
        }, REQUEST_TIMEOUT_MS);
      });
      try {
        return await Promise.race([
          (async () => {
            const response = await request(path, { ...options, signal: aborter.signal });
            if (!response.ok) throw new Error('HTTP ' + response.status);
            return response.json();
          })(), timeout
        ]);
      } finally {
        clearRequestTimer(timer);
      }
    }
    function post(path, body, token = '') {
      const headers = { 'Content-Type': 'application/json', 'X-Lumi-Client': '1' };
      if (token) headers['X-Lumi-CSRF'] = token;
      return timedRequest(path, {
        method: 'POST', credentials: 'same-origin', redirect: 'error',
        headers, body: JSON.stringify(body)
      });
    }
    async function json(path, body, token = '', gen = generation) {
      const data = await post(path, body, token);
      if (gen !== generation) return null;
      return data;
    }
    async function checkEnabled() {
      const gen = generation;
      try {
        const data = await timedRequest('/api/lumi/status', { credentials: 'same-origin', redirect: 'error', cache: 'no-store' });
        if (gen !== generation) return;
        enabled = data.enabled === true;
        status(enabled ? 'Bereit' : 'Noch nicht verbunden', enabled ? 'Starte das Gespräch ausdrücklich.' : 'Lumi ist hier noch nicht verbunden.');
      } catch (_) {
        if (gen === generation) status('Verbindung unterbrochen', 'Status konnte nicht geladen werden.');
      }
    }
    async function start() {
      if (!enabled || starting || ending || fatalPaused) return;
      if (active) {
        if (paused || micPaused) {
          paused = false;
          micPaused = false;
          restartTimes = [];
          status('Bereit');
          if (recognizerAvailable && recognition) startRecognition();
          else await enableMicrophone(generation);
          drain();
        }
        return;
      }
      starting = true;
      const gen = ++generation;
      status('Bereit', 'Verbindung wird aufgebaut.');
      try {
        openingRequest = post('/api/lumi/session', {});
        const data = await openingRequest;
        openingRequest = null;
        if (gen !== generation || !data) return;
        if (!HEX64.test(data.csrf || '') || data.state !== 'ready') throw new Error('session');
        csrf = data.csrf;
        controls = data.controls === true;
        transcript = [];
        active = true;
        paused = false;
        status('Bereit', 'Texteingabe ist jederzeit möglich.');
        await enableMicrophone(gen);
      } catch (_) {
        if (gen === generation) networkFailure();
      } finally {
        starting = false;
        emit();
      }
    }
    async function enableMicrophone(gen) {
      if (gen !== generation || !active || paused) return;
      if (!recognitionFactory) {
        status('Bereit', 'Lokale Spracherkennung wird von diesem Browser nicht unterstützt. Texteingabe ist verfügbar.');
        return;
      }
      try {
        const candidate = recognitionFactory();
        const ctor = candidate && candidate.constructor;
        if (!candidate || !('processLocally' in candidate) || typeof ctor.available !== 'function') throw new Error('local unavailable');
        const availability = await ctor.available({ langs: ['de-DE'], processLocally: true });
        if (gen !== generation || !active || paused) return;
        if (availability !== 'available') throw new Error('local unavailable');
        candidate.lang = 'de-DE';
        candidate.continuous = true;
        candidate.interimResults = true;
        candidate.processLocally = true;
        recognition = candidate;
        recognizerAvailable = true;
        candidate.onresult = event => {
          if (gen !== generation || !active || paused || !micWanted || speaking && !headphoneMode) return;
          let interim = '';
          for (let i = event.resultIndex || 0; i < event.results.length; i++) {
            const result = event.results[i];
            const text = (result[0] && result[0].transcript || '').trim();
            if (result.isFinal) {
              if (!seenFinal.has(i) && text) {
                seenFinal.add(i);
                view.interim('');
                if (speaking && headphoneMode) interrupt();
                submit(text);
              }
            } else interim += text;
          }
          view.interim(interim);
          if (speaking && headphoneMode && interim) interrupt();
        };
        candidate.onerror = event => {
          if (gen !== generation || !active) return;
          const error = event && event.error;
          if (['not-allowed', 'service-not-allowed', 'language-not-supported', 'network', 'audio-capture'].includes(error)) {
            stopMic();
            recognizerAvailable = false;
            micPaused = true;
            const explanation = {
              'not-allowed': 'Mikrofonzugriff wurde nicht erlaubt.',
              'service-not-allowed': 'Lokale Spracherkennung wurde nicht erlaubt.',
              'language-not-supported': 'Deutsch ist lokal nicht verfügbar.',
              network: 'Spracherkennung ist unterbrochen.',
              'audio-capture': 'Das Mikrofon ist nicht verfügbar.'
            };
            status('Bereit', explanation[error] + ' Texteingabe bleibt verfügbar.');
          }
        };
        candidate.onend = () => {
          micRunning = false;
          emit();
          if (gen !== generation || !active || paused || !micWanted || speaking && !headphoneMode || !recognizerAvailable) return;
          const now = Date.now();
          restartTimes = restartTimes.filter(t => now - t < 10000);
          if (restartTimes.length >= 3) {
            stopMic();
            micPaused = true;
            status('Bereit', 'Spracherkennung pausiert. Mit „Fortsetzen“ erneut starten.');
            return;
          }
          restartTimes.push(now);
          setTimer(() => {
            if (gen === generation && active && !paused && !micPaused && micWanted && !micRunning && (!speaking || headphoneMode)) startRecognition();
          }, 200);
        };
        startRecognition();
      } catch (_) {
        if (gen === generation) status('Bereit', 'Lokale Spracherkennung wird von diesem Browser nicht unterstützt. Texteingabe ist verfügbar.');
      }
    }
    function startRecognition() {
      if (!recognition || !active || paused || speaking && !headphoneMode) return;
      try {
        seenFinal = new Set();
        micWanted = true;
        recognition.start();
        micRunning = true;
        status('Zuhören');
      } catch (_) {
        micRunning = false;
        status('Bereit', 'Mikrofon konnte nicht gestartet werden. Texteingabe bleibt verfügbar.');
      }
    }
    function submit(raw) {
      if (!active || paused) return false;
      const text = String(raw || '').trim();
      if (!text || text.length > 600) {
        view.hint('Bitte 1 bis 600 Zeichen eingeben.');
        return false;
      }
      const phrase = text.toLocaleLowerCase('de-DE').replace(/[.!?]+$/, '').trim();
      if (phrase === 'lumi stopp' || phrase === 'gespräch beenden') { end(); return true; }
      if (phrase === 'unterbrechen') { interrupt(); return true; }
      if (current && queue.length >= 3) {
        view.hint('Warteschlange voll: höchstens drei weitere Nachrichten.');
        return false;
      }
      add('user', text);
      queue.push({ message_id: randomHex(16), text });
      drain();
      return true;
    }
    async function drain() {
      if (!active || paused || current || !queue.length) return;
      const gen = generation;
      const item = queue.shift();
      current = item;
      status('Lumi denkt');
      try {
        const accepted = await json('/api/lumi/turn', item, csrf, gen);
        if (gen !== generation || !active || !accepted) return;
        if (accepted.state !== 'pending' || !HEX32.test(accepted.turn_id || '')) throw new Error('turn');
        current.turn_id = accepted.turn_id;
        poll(gen, accepted.turn_id);
      } catch (_) {
        if (gen === generation && active) networkFailure();
      }
    }
    async function poll(gen, id) {
      if (gen !== generation || !active || !current || current.turn_id !== id) return;
      try {
        const result = await json('/api/lumi/result', { turn_id: id }, csrf, gen);
        if (gen !== generation || !active || !current || !result) return;
        if (result.state === 'pending') {
          pollTimer = setTimer(() => poll(gen, id), POLL_MS);
          return;
        }
        current = null;
        if (result.state === 'done' && typeof result.answer === 'string') {
          const answer = result.answer.slice(0, 6000);
          add('lumi', answer);
          if (!paused) speak(answer, gen);
        } else if (result.state === 'failed') {
          queue = [];
          uncertainPending = true;
          paused = true;
          fatalPaused = true;
          stopMic();
          stopSpeech();
          status('Verbindung unterbrochen', 'Lumi konnte diese Nachricht nicht beantworten. Wartende Nachrichten wurden verworfen; Gespräch beenden und neu starten.');
          return;
        } else throw new Error('result');
        drain();
        if (!speaking && !current) status(micRunning ? 'Zuhören' : 'Bereit');
      } catch (_) {
        if (gen === generation && active) networkFailure();
      }
    }
    function speak(answer, gen) {
      if (speaking) {
        speechQueue.push(answer);
        return;
      }
      const voice = localGermanVoices(synthesis)[0];
      if (!voice || !utteranceFactory || !answer) {
        if (answer && (!voice || !utteranceFactory)) view.hint('Keine lokale deutsche Sprachausgabe verfügbar. Die Antwort steht als Text bereit.');
        return;
      }
      if (!headphoneMode) stopMic();
      const utterance = utteranceFactory(answer);
      const serial = ++speechSerial;
      utterance.voice = voice;
      utterance.lang = 'de-DE';
      utterance.onend = utterance.onerror = () => {
        if (gen !== generation || !active || serial !== speechSerial) return;
        speaking = false;
        if (speechQueue.length && !paused) {
          speak(speechQueue.shift(), gen);
          return;
        }
        status(current ? 'Lumi denkt' : 'Bereit');
        if (!paused && !micRunning && recognizerAvailable) startRecognition();
      };
      speaking = true;
      status('Lumi spricht');
      synthesis.speak(utterance);
    }
    function interrupt() {
      if (speaking) {
        stopSpeech();
        if (active && !paused && !micRunning && recognizerAvailable) startRecognition();
      }
      if (active) status(current ? 'Lumi denkt' : micRunning ? 'Zuhören' : 'Bereit', 'Nur die lokale Sprachausgabe wurde unterbrochen.');
    }
    async function end() {
      if (ending) return;
      const wasActive = active;
      const pending = !!current || uncertainPending;
      const token = csrf;
      const opening = openingRequest;
      ending = true;
      ++generation;
      active = false;
      paused = false;
      csrf = '';
      controls = false;
      uncertainPending = false;
      fatalPaused = false;
      micPaused = false;
      clearPending();
      stopMic();
      stopSpeech();
      view.interim('');
      status('Beendet', pending ? 'Bereits übermittelte Aufträge können noch ausgeführt werden.' : 'Gespräch beendet.');
      try {
        let endToken = token;
        if (opening) {
          try {
            const opened = await opening;
            if (HEX64.test(opened && opened.csrf || '')) endToken = opened.csrf;
          } catch (_) { /* opening was aborted or timed out */ }
        }
        if (wasActive || endToken) await json('/api/lumi/end', {}, endToken, generation);
      } catch (_) {
        /* local end remains final; timed request prevents permanent lock */
      } finally {
        ending = false;
        emit();
      }
    }
    function hide() {
      if (starting && !active) {
        end();
        return;
      }
      if (!active) return;
      paused = true;
      stopMic();
      stopSpeech();
      view.interim('');
      status('Bereit', 'Seite verborgen. Mikrofon bleibt aus; „Fortsetzen“ startet es ausdrücklich.');
    }
    function setHeadphones(value) {
      headphoneMode = !!value;
      if (!headphoneMode && speaking) stopMic();
      emit();
    }
    emit();
    return { checkEnabled, start, submit, end, hide, interrupt, setHeadphones,
      snapshot: () => ({ active, paused, fatalPaused, micPaused, starting, ending, current: !!current, queued: queue.length, micRunning, speaking, transcript: transcript.slice() }) };
  }

  function defaultRandomHex(bytes) {
    const data = new Uint8Array(bytes);
    root.crypto.getRandomValues(data);
    return Array.from(data, b => b.toString(16).padStart(2, '0')).join('');
  }

  function mount(document, environment = root) {
    const el = id => document.getElementById(id);
    const transcript = el('lumi-transcript');
    const status = el('lumi-status');
    const hint = el('lumi-hint');
    const queueLabel = el('lumi-queue');
    const interim = el('lumi-interim');
    const start = el('lumi-start');
    const stop = el('lumi-end');
    const interrupt = el('lumi-interrupt');
    const input = el('lumi-input');
    const send = el('lumi-send');
    const headphones = el('lumi-headphones');
    let renderedTranscript = [];
    const view = {
      status(label, detail) { status.textContent = label; if (detail) hint.textContent = detail; },
      hint(message) { hint.textContent = message; },
      interim(message) { interim.textContent = message; },
      update(state) {
        if (status.parentElement) status.parentElement.dataset.mode = state.micRunning ? 'listening' :
          state.speaking ? 'speaking' : state.current ? 'thinking' : state.active ? 'ready' : 'ended';
        start.disabled = !state.enabled || state.starting || state.ending || state.fatalPaused || state.active && !state.paused && !state.micPaused;
        start.textContent = state.active && (state.paused || state.micPaused) ? 'Fortsetzen' : 'Gespräch starten';
        stop.disabled = state.ending || !state.active && !state.starting && !state.fatalPaused;
        send.disabled = !state.active || state.paused || state.ending;
        input.disabled = !state.active || state.paused || state.ending;
        interrupt.disabled = !state.active;
        queueLabel.textContent = state.queued ? state.queued + (state.queued === 1 ? ' Nachricht wartet.' : ' Nachrichten warten.') : 'Keine Nachrichten in der Warteschlange.';
        if (renderedTranscript.length === state.transcript.length &&
            renderedTranscript.every((item, i) => item === state.transcript[i])) return;
        const previousScroll = transcript.scrollTop || 0;
        const atBottom = (transcript.scrollHeight || 0) - previousScroll - (transcript.clientHeight || 0) < 64;
        transcript.replaceChildren();
        for (const item of state.transcript) {
          const row = document.createElement('li');
          row.className = 'lumi-message lumi-' + item.role;
          const who = document.createElement('strong');
          who.textContent = item.role === 'user' ? 'Du' : 'Lumi';
          const body = document.createElement('span');
          body.textContent = item.text;
          row.append(who, body);
          transcript.append(row);
        }
        transcript.scrollTop = atBottom ? transcript.scrollHeight : previousScroll;
        renderedTranscript = state.transcript.slice();
      }
    };
    const Recognition = environment.SpeechRecognition || environment.webkitSpeechRecognition;
    const controller = createLumiController({
      fetch: environment.fetch.bind(environment), view,
      recognitionFactory: Recognition ? () => new Recognition() : null,
      synthesis: environment.speechSynthesis,
      utteranceFactory: environment.SpeechSynthesisUtterance ? text => new environment.SpeechSynthesisUtterance(text) : null
    });
    start.addEventListener('click', () => controller.start());
    stop.addEventListener('click', () => controller.end());
    interrupt.addEventListener('click', () => controller.interrupt());
    headphones.addEventListener('change', () => controller.setHeadphones(headphones.checked));
    el('lumi-form').addEventListener('submit', event => {
      event.preventDefault();
      if (controller.submit(input.value)) input.value = '';
    });
    document.addEventListener('visibilitychange', () => { if (document.hidden) controller.hide(); });
    environment.addEventListener('pagehide', () => controller.hide());
    controller.checkEnabled();
    return controller;
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = { createLumiController, localGermanVoices, mount };
  if (root.document && root.document.getElementById('lumi-app')) mount(root.document, root);
})(typeof globalThis === 'object' ? globalThis : this);
