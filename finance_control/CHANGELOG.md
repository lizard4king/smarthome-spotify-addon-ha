# Changelog

## 0.2.19

- Extends booking logos to 55 locally served bank and merchant identities and adds pet, bus and rail category pictograms.
- Shows the latest recorded booking on collapsed account cards and identifies imported bookings as the calculation basis.

- Adds encrypted, owner-scoped Linux server credentials with explicit save and removal confirmation.
- Adds a single, read-only balance request in the cockpit; pending requests do not block other cockpit pages.
- Excludes the credential vault and its encryption key from add-on backups and checks the vault offline during image build.
- Preserves unchanged existing transfers to skipped accounts during selected Finanzguru reimports and explains remaining transfer conflicts by row and changed fields.
- Requires signed Cloudflare user verification and an add-on FinTS product ID. No automatic booking import, scheduled fetch or Mastercard migration is included.

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
