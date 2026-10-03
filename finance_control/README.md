# Finance Control Home Assistant add-on

Version `0.2.0` runs the current `src/finance_control` package inside a Home
Assistant add-on. Build an installable add-on context from the repository with
`scripts/build_homeassistant_addon.py`; the resulting `.tar.gz` is written outside
the repository and includes a source manifest. The package keeps source provenance in `build-manifest.json`; no private data
is included.

For the current Home Assistant 2026.10 store, local apps are loaded from
`/data/apps/local`. A separate SSH add-on's `/addons` mount points at
`/supervisor/addons/local` and is not the active store path; do not broaden SSH
mounts to work around this. Install or update this repository through the
Home Assistant add-on store so Supervisor uses its supported app path.

The add-on listens on port `8785` inside Home Assistant's add-on network. No host
port is published. Its `/data` mount is persistent and included in cold Home
Assistant backups. The active database is under `/data/FinanceControl/data`.
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
`http://7b071411-finance-control:8785` and set the origin request Host header to
`finance.pistelok.de`. Keep a Cloudflare Access application and an allow policy
in front of this hostname. Finance Control's Host, Origin, and CSRF checks are
request protections, not user authentication.
Home Assistant's internal DNS name here follows its installed-repository
identifier plus add-on slug (`7b071411` + `finance_control`); use the name
shown by Supervisor if the repository identifier changes.

The manifest declares the Supervisor watchdog URL, but keep the per-add-on
watchdog option disabled during first initialization. Enable it only after the
database restore and a successful application start have been verified.

See the [Finance Control server documentation](https://github.com/lizard4king/finance-control/blob/main/docs/homeassistant-server.md)
for build, access, backup, and migration requirements.
