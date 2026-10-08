# Changelog

## 0.2.15

- Adds the new app views and exposes `last_booking_date`.

## 0.2.14

- **Application-level authentication (secure by default).** Before this version
  anyone who reached the port with a trusted `Host` header could read `GET /api/state`,
  including the CSRF token. Now all routes except `/health` need the access token.
- New add-on options: `access_token` (password, at least 16 characters; if empty one
  is generated, stored under `/data/FinanceControl/data/app_access_token` with mode 0600
  and printed to the add-on log at every start) and `require_app_token` (default `true`).
- **Owner steps:** (1) after the update read the token from the add-on log or set
  `access_token`; (2) open the cockpit once and sign in with the form (cookie lasts 30
  days); (3) scripts and cockpit integrations must send `X-Finance-Access: <token>`
  or `Authorization: Bearer <token>`; (4) Cloudflare Access stays in front. If the app must
  stay token-free behind Cloudflare Access, set `require_app_token: false` explicitly.
- The Host/Origin/CSRF logic, `/health` (watchdog) and all POST contracts are unchanged.
- Local runs bound to `127.0.0.1` keep working without a token; `--options-file` and
  `--no-app-auth` give explicit control.
