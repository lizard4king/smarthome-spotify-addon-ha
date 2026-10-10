# Changelog

## 0.2.35

- Add an owner-bound, read-only historical ING month check for explicitly selected savings targets, without import authority or balance changes.
- Show bounded transaction details and exact totals per currency; empty results never prove completeness.
- Preserve normal import and automatic refresh limits, authentication, revision checks and financial reconciliation rules.

## 0.2.34

- Correct the static return type of fully validated reconciliation evidence without changing runtime values or validation rules.

## 0.2.33

- Show a strictly validated, owner-bound comparison of bank and ledger control balances inside a failed transaction preview.
- Retain only bounded dates, amounts and booking counts in RAM; exclude raw bank text and prevent stale comparisons after selection changes.
- Preserve all reconciliation, revision, rollback and explicit import rules; do not adjust opening balances automatically.

## 0.2.32

- Respect explicit account-level balance restrictions from conclusive FinTS user parameters before issuing a balance request; missing or inconclusive permissions keep the existing read behavior.
- Retain bounded, validated failure context for standalone balance reads without exposing raw bank messages.
- Report initial savings-account ledger control conflicts explicitly instead of hiding them behind a generic import error; keep all financial validation and rollback rules.

## 0.2.31

- Preserve fixed, non-sensitive error stages and categories in standalone balance reads.
- Keep successful balances when an individual account does not support balance reads or returns invalid balance data; authentication and transport failures still stop the request.
- Show an explicit recovery message when the bookings renderer or its data is unavailable, instead of silently leaving the workspace blank.
- Run the standalone server-banking UI regression contract in the standard CI suite.

## 0.2.30

- Add direct period refresh for Postbank and ING checking and savings accounts; NASPA remains checking-only. Multiple explicit account bindings can share one login, with status and cooldown shown per account.
- Add owner-bound Mastercard C.12 setup and asynchronous read-only transaction retrieval with encrypted card credentials and masked card numbers.
- Keep card retrieval separate from ledger imports and persistent balance overview until direct delivery is reconciled against existing card transactions and billing controls. Practical acceptance for additional accounts remains pending.

## 0.2.29

- Add a controlled one-time source switch for uniquely matched legacy bookings; display text differences side by side and require an additional explicit confirmation.
- Keep ambiguous tuples blocked, bank checkpoints strict, and legacy text, categories and links unchanged. Practical server acceptance remains pending.

## 0.2.28

- Accept absent optional FinTS account fields without changing existing string-based account fingerprints; still reject invalid or duplicate identities.
- Preserve the bank's actual period closing date and reject dates outside the requested month or beyond the request.
- Keep full page, amount and previous-month controls; stop refresh batches after a partial close and retry the unfinished month.

## 0.2.27

- Give direct bank sources priority: reject a generic CSV upload completely if it targets any bank-bound account; keep unbound file imports and internal bank imports available.
- Carry fixed, validated error stages through manual bank reads and automatic refresh without exposing bank messages or personal values.
- Show the safe diagnostic beside failed bank reads and clear it for new attempts and successful updates.

## 0.2.26

- Show the latest recorded account balance and its bank/statement date in both account views, independently of the selected booking month.
- Keep unknown balances unavailable instead of substituting monthly movements or opening balances.
- Persist the balance already returned by successful server transaction reads without a second bank request.
- Combine refresh status and collapsed connection settings into one row per registration; retain and distinguish separate registrations with the same name.

## 0.2.25

- Add NASPA Giro to owner-bound server transaction previews, controlled imports and refresh.
- Preserve NASPA MT940 text and local period identities, with synthetic replay and balance checks.
- Poll NASPA app authorizations only after explicit authentication selection and a bank-reported decoupled challenge; ordinary or graphical TAN entry remains unsupported.
- Keep practical NASPA acceptance separate from code validation; existing imports remain until a controlled source switch succeeds.

## 0.2.24

- Reject ambiguous plan amounts and malformed cash/fee declarations.
- Reverify backup packages and copied checksums before accepting sync receipts.
- Commit new document registrations only after their source cache is written.
- Bound HTTP clients, serialize business actions and revalidate queued user access.
- Add synthetic non-bank browser checks to the local Windows CI runner.

## 0.2.23

- Limits failed access-token attempts per client (10 in 15 minutes, then HTTP 429 with Retry-After); applies to the login form and to `X-Finance-Access`/Bearer requests.
- Replaces the token-derived session cookie with random server-side sessions (30 days, at most 32). Sessions end on restart; rotating the access token no longer needs to be paired with cookie invalidation.
- Adds "Abmelden" (`POST /logout`) to the header when token login is active.
- Prints a newly generated access token once at creation; later starts only state that the stored token is in use. Note the token or set the add-on option `access_token`.

## 0.2.22

- Expands locally served bank and merchant marks from 55 to 238, with documented official sources, preserved raster bytes and LF-normalized SVGs.
- Matches verified leading IBANs, explicit merchant aliases and payment intermediaries without changing original booking text. Ambiguous or unknown merchants use the category pictogram.
- Includes the complete logo library in authenticated static routes and deterministic add-on packages; the browser makes no external logo requests.
- Distinguishes safe bank failure codes and uses bank-neutral error messages for Postbank and ING.

## 0.2.21

- Neue Umsätze auf der Buchungsseite aktualisieren; die Verwaltung zeigt nur Bankzugänge und Benutzer.
- Bestätigte Girokontenzuordnungen für serverseitige Aktualisierung beim Öffnen oder Neuladen wiederverwenden, mit Abrufstatus und 60 Sekunden Sperrfrist.
- ING-Serverabruf mit vorhandenen Zugangsdaten; ein ausschließlich per QR eingerichteter Zugang bleibt separat offen.
- Fehlende Monatsenden vor dem laufenden Monat nachholen und geänderte Kontrollmonate sperren.
- Serverkalender für deutsche Bankkonten auf Europe/Berlin festlegen.

## 0.2.20

- Separates explicitly stated supermarket cash payouts from the purchase for category reports and virtual cash, retaining the original bank debit and identity. Unclear amounts stay visible for review.
- Hides stored-credential replacement fields behind an explicit change button and preserves separate balance reads for other Postbank accounts.
- Adds explicit Postbank account, authentication-method and device selection for read-only server transaction requests.
- Shows the booked current-month transactions with merchant, purpose and original booking text before import.
- Verifies the previous closed month, current-month opening/closing controls and the existing ledger before switching to the direct source.
- Preserves existing categories, receipts and transfers; ambiguous legacy matches block the import. Repeated unchanged requests add no duplicate transactions.
- Uses encrypted server credentials and owner/revision-bound, expiring previews. No payments, scheduled fetch or Mastercard source switch is included.

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
