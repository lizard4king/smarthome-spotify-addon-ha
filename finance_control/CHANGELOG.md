# Changelog

## 0.2.18

- Adds local merchant logos to booking rows and a dedicated purchases and receipts workspace with available article details.
- Allows explicit account selection for Finanzguru imports; skipped accounts retain their existing source and ledger state.
- Bank access remains Windows-bound; server banking and credential migration are not enabled by this UI release.

## 0.2.17

- Adds official bank marks and category icons to the add-on package.
- Adds optional signed Cloudflare Access user verification, initial administrator bootstrap, and app user/bank registration.
- Bank registrations remain `LOCAL_SETUP_REQUIRED`; bank credentials stay on local Windows and no remote bank connection or live bank setup is included.
- Adds the allowlisted PayPal and Sparkasse PNGs plus the Lucide license text to the deterministic package while preserving binary bytes.

## 0.2.16

- Understandable, responsive cockpit sign-in with a labelled cockpit key and accessible error state.
- Authentication, session cookies and the protection of financial data remain unchanged.

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
