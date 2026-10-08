# Finance Control Home Assistant add-on

Version `0.2.15` adds the new app views and exposes `last_booking_date`.
Version `0.2.14` adds application-level authentication. By default every route
except `/health` now requires a shared access token (add-on option `access_token`;
if empty, a token is generated on first start, stored in
`/data/FinanceControl/data/app_access_token` with mode 0600 and printed to the
add-on log). The browser logs in once through a form and receives an `HttpOnly`,
`SameSite=Lax` cookie derived from the token; scripts may send `X-Finance-Access`
or `Authorization: Bearer`. The existing Host, Origin and CSRF checks are unchanged.
Deployments that are fully protected by Cloudflare Access can opt out explicitly with
`require_app_token: false`. See `CHANGELOG.md`.

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
request protections, not user authentication; since 0.2.14 the application
additionally requires its own access token unless `require_app_token` is set to
`false`.

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
