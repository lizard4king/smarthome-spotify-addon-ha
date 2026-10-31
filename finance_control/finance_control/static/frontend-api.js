'use strict';
// Same-origin transport only. No tokens or financial payloads are persisted.
(() => {
  const allowed = new Set(['/api/state', '/api/classification-list',
    '/api/classification-catalog', '/api/classification-transaction-get',
    '/api/classification-recipient-preview', '/api/classification-recipient-commit',
    '/api/classification-save', '/api/classification-reset', '/api/recurring-view', '/api/analytics-summary',
    '/api/wealth-load', '/api/contracts-inventory', '/api/contracts-load']);
  let csrf = null;
  let bootstrap = null;
  function failure(message, status, code) {
    const error = new Error(message);
    error.status = status;
    error.code = code;
    return error;
  }
  async function token() {
    if (csrf) return csrf;
    if (!bootstrap) bootstrap = request('/api/state').finally(() => { bootstrap = null; });
    await bootstrap;
    if (!csrf) throw failure('Die Sitzung ist nicht bereit. Bitte neu anmelden.', 401, 'session_required');
    return csrf;
  }
  async function request(route, data, retry = true) {
    if (typeof route !== 'string' || !allowed.has(route.split('?')[0]) || /[\r\n#]/.test(route))
      throw failure('Diese Anfrage ist in der Oberfläche nicht vorgesehen.', null, 'unsupported_route');
    const options = {credentials: 'same-origin', cache: 'no-store'};
    if (data !== undefined) {
      options.method = 'POST';
      options.headers = {'Content-Type': 'application/json', 'X-Finance-Token': await token()};
      options.body = JSON.stringify(data);
    }
    let response;
    try { response = await fetch(route, options); }
    catch { throw failure('Der Server ist nicht erreichbar. Bitte erneut versuchen.', null, 'network_error'); }
    const type = (response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
    if (response.redirected || !(type === 'application/json' || /^application\/[a-z0-9!#$&^_.+-]+\+json$/.test(type)))
      throw failure('Keine gültige Antwort. Bitte Anmeldung prüfen und die Seite neu laden.', response.status, 'invalid_response');
    let result;
    try { result = await response.json(); }
    catch { throw failure('Die Serverantwort konnte nicht gelesen werden.', response.status, 'invalid_response'); }
    if (!result || typeof result !== 'object' || Array.isArray(result))
      throw failure('Die Serverantwort ist unvollständig.', response.status, 'invalid_response');
    if (response.status === 403 && data !== undefined && retry &&
        result.error === 'Anfrage nicht freigegeben. Seite neu laden.') {
      csrf = null;
      await token();
      return request(route, data, false);
    }
    if (!response.ok) {
      const code = typeof result.code === 'string' ? result.code : typeof result.error === 'string' ? result.error : 'request_failed';
      const message = response.status === 409 ? 'Die Daten wurden inzwischen geändert. Bitte neu laden und erneut prüfen.'
        : response.status === 401 || response.status === 403 ? 'Anmeldung oder Berechtigung fehlt. Bitte neu anmelden.'
          : code === 'mixed_currency_analytics' ? 'Die Auswahl enthält mehrere Währungen. Bitte ein anderes Konto wählen.'
            : code === 'unknown_currency_analytics' ? 'Die Währung ist nicht eindeutig. Eine Geldanalyse ist nicht verfügbar.'
              : 'Die Anfrage konnte nicht abgeschlossen werden. Bitte erneut versuchen.';
      throw failure(message, response.status, code);
    }
    if (route.split('?')[0] === '/api/state' && typeof result.csrf === 'string' && result.csrf) csrf = result.csrf;
    return result;
  }
  window.financeFrontendApi = Object.freeze({request});
})();
