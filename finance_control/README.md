# Finance Control Home Assistant add-on

Version `0.2.22` bundles 238 local bank and merchant marks, documented sources,
conservative counterparty recognition and category pictograms for unknown merchants.

Version `0.2.21` moves transaction setup and import to the bookings workspace.
Previously confirmed Postbank and ING checking-account selections can refresh
on opening or reloading that workspace, or with the manual refresh button.
Owner/revision checks, monthly controls and a persistent 60-second cooldown apply.
Missing month ends are imported individually before the current month. The server
uses Europe/Berlin. ING uses stored FinTS credentials; QR-only setup remains a
separate, unimplemented integration. No payments or scheduled fetches are added.

Version `0.2.20` adds a direct read-only Postbank checking-account import with
explicit bank/ledger-account selection, authentication-method/device choices,
full booking texts, a bounded preview and verified monthly control balances.
Existing categories and receipts are preserved. Ambiguous existing-row matches
block source adoption. No payments, scheduled fetches or Mastercard source change.

Version `0.2.19` adds owner-scoped, encrypted server credentials and an explicit,
read-only bank balance request. It requires signed Cloudflare user verification and
the registered FinTS product ID in the add-on options. Bank credentials are entered
directly in the cockpit; they are not copied from Windows. The encrypted vault and its
key are excluded from add-on backups. A Linux-only offline vault self-test runs during
the image build. Automatic booking imports, scheduled bank fetches and Mastercard
migration are not part of this version. See `docs/server-banking.md` in the source repo.
Version `0.2.18` adds merchant logos, a purchases and receipts workspace, and explicit
selection of individual Finanzguru source accounts. Version `0.2.17` introduced signed
Cloudflare user verification and bank registration.
Version `0.2.16` adds a responsive, clearly labelled cockpit sign-in page.
Its generic `/login.css` stylesheet is available before sign-in; it contains no financial data.
Version `0.2.15` adds the new app views and exposes `last_booking_date`.
Version `0.2.14` adds application-level authentication. By default financial data and
application assets require a shared access token (add-on option `access_token`;
if empty, a token is generated on first start, stored in
`/data/FinanceControl/data/app_access_token` with mode 0600 and printed to the
add-on log). The browser logs in once through a form and receives an `HttpOnly`,
`SameSite=Lax` cookie derived from the token; scripts may send `X-Finance-Access`
or `Authorization: Bearer`. The existing Host, Origin and CSRF checks are unchanged.
Deployments that are fully protected by Cloudflare Access can opt out explicitly with
`require_app_token: false`. See `CHANGELOG.md`.

Cloudflare Access user verification is optional. Configure `cloudflare_team_domain` with
the exact Cloudflare Access team domain and `cloudflare_audience` with the exact audience
tag of the Access application. Set `administrator_email` to the initial administrator's
email address. Keep these deployment values out of the repository. Empty values leave
Cloudflare identity verification disabled. The application-key protection remains
enabled by default (`require_app_token: true`); disable it only when the complete route is
protected by Cloudflare Access.

Version `0.2.13` records explicitly confirmed Bonsy voucher payments separately
from bank payments and cash withdrawals. Voucher redemption closes only the
receipt payment remainder; it does not create another bank transaction or expense.

Version `0.2.12` treats confirmed Bonsy receipts below EUR 50 without an exact
personal-account payment as cash immediately. Source duplicates and explicit
exclusions stay in the audit trail and are hidden from active document review.
Cashback withdrawals retain their separately documented purchase portion.

Version `0.2.11` allows categories and receipts to be edited directly on
original transactions in Plan & Ist. Version `0.2.10` shows actual monthly
positions by account and recipient, with
the original transactions, in the 2025 retrospective. 2026 budgets and
calculations remain unchanged. It also restores the selected monthly budget
reference for 2026, including September before the first calculation row in
October. The 2025
retrospective remains independently based on Plan=Ist. A compact Plan/Ist/
variance summary stays visible, with an explicit label for the comparison
basis. The earlier Android chart, as-of-date, and unavailable-payday-data fixes
remain included.
On mobile, all seven column sorts are available in the existing collapsed
filter section; sorting changes display order only, not calculations or data.
The interface has two main workspaces, Plan & Ist and bookings. Planning,
accounts, import and receipts open through tool buttons; mobile bookings use
compact cards. Year summaries use the current calendar year. Cash withdrawals
are shown separately from receipt spending, and explicitly excluded Bonsy rows
are reported instead of counted as purchases.
It runs the current `src/finance_control` package inside a Home Assistant
add-on. Build an installable add-on context from the repository with
`scripts/build_homeassistant_addon.py`; the resulting `.tar.gz` is written outside
the repository and includes a source manifest. The checked-in `code/` folder is
an older snapshot and is not used by this build process.

The add-on listens on port `8785` inside Home Assistant's add-on network. No host
port is published. Home Assistant automatically mounts the add-on's private,
persistent data directory at `/data` and includes it in cold backups. Finance
Control stores its active database under `/data/FinanceControl/data`. It does
not map the separate public `addon_config` directory.
On first start without a database, the add-on waits for one JSON line on
Home Assistant's add-on stdin and restores a verified backup package. This
one-time bootstrap is scoped to `/data/FinanceControl/data`; it rejects an
existing database and never stores the temporary download capability. Supply
the HTTPS URL, temporary capability, public CA PEM and expected SHA-256 only
through the Supervisor stdin action. An absent or empty database is never
silently initialized as a production database. A profile is needed only if the
source data directory used one. Run exactly one Finance Control instance
against an active data directory.

For Cloudflared, route `finance.pistelok.de` to
the installed app's internal hostname on port `8785` and set the origin request Host header to
`finance.pistelok.de`. Keep a Cloudflare Access application and an allow policy
in front of this hostname. Finance Control's Host, Origin, and CSRF checks are
request protections, not user authentication. Since 0.2.14 the application also
requires its own access token unless `require_app_token` is set to `false`.
When Cloudflare user verification is configured, pass the same team domain,
application audience, and initial administrator email described above.

The currently registered distribution repository is
`lizard4king/smarthome-spotify-addon-ha`; its expected app hostname is
`7b071411-finance-control`. Read the actual hostname from Supervisor after
installation. On Home Assistant 2026.10, an older SSH app's `/addons` mount may
not expose the active `/data/apps/local` store; use the registered repository
instead of broadening SSH permissions.

The manifest declares the Supervisor watchdog URL, but keep the per-add-on
watchdog option disabled during first initialization. Enable it only after the
database restore and a successful application start have been verified.

See [`docs/homeassistant-server.md`](../../docs/homeassistant-server.md) for
build, access, backup, and migration requirements.
