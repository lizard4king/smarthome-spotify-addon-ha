# 0.2.75

Analyseansicht verarbeitet die bestehenden strukturierten Backend-Hinweise zu Kartenabbuchungen und einseitigen Umbuchungen. Ungültige Hinweisstrukturen werden als Ladefehler angezeigt. Finanzielle Berechnungen bleiben unverändert.

# 0.2.74

Neue Oberfläche unter /frontend mit bestehenden Konten, Buchungen, Kategorien, wiederkehrenden Zahlungen und Analysen. Fehlende Kontostände bleiben sichtbar; gemischte Buchungswährungen werden nicht summiert. Keine neue automatische Bankaktion.

# 0.2.73

Klickbarer Oberflächenentwurf unter Mehr → Oberflächenentwurf, mit ausschließlich synthetischen Beispieldaten, fünf Ansichten und Layouts für Handy und PC. Bestehende Finanzansichten bleiben erreichbar. Keine neue Bankaktion.

# Changelog

## 0.2.72

- Automatische Sicherungen auf zwei geprüfte Stände begrenzt und Änderungen in 15-Minuten-Abständen gebündelt. Manuelle und historische Pakete ohne Herkunftsnachweis bleiben geschützt.
- Endgültige Drive-Rotation nach frischer Größen-/Prüfsummenprüfung; unvollständige Löschvorgänge werden dauerhaft protokolliert und bei Zielwechsel angehalten.
- Einmalige lokale Altbestandsbereinigung ausschließlich über den exakten SHA-256-Wert eines privat geprüften Plans; zwei vollständige Sicherungspaare bleiben erhalten.
- Native Ausschlüsse, Startskript und Tresor unverändert. Tokenfreier Guard an 0.2.72 gebunden; reale Wiederherstellung bleibt gesondert zu prüfen.

## 0.2.70

- Keep future native Finance-only backups compact by excluding generated package ZIPs, their partial files and local SQLite snapshot copies from their fixed output paths. No local file is removed or backup generation changed.
- Preserve the active database, import originals, receipts, profile and manual CSV/JSON exports. Older excluded recovery points remain local; historical snapshots are not generally reproducible from current data.
- Bind the secret-free backup guard to approved add-on version 0.2.70; archive size and restore acceptance remain separate checks.

## 0.2.69

- Die lokale Hauptsicherung bleibt ohne app-internes Drive-Ziel sichtbar; eine separat eingerichtete Zusatzkopie wird nicht als fehlgeschlagene Hauptsicherung ausgegeben. Unbekannte und fehlende lokale Pakete bleiben erkennbar.
- Native Add-on-Dateisicherungen schließen zusätzlich die lokale Cockpit-Zugriffsschlüsseldatei aus. Banktresor-Ausschlüsse bleiben erhalten; Cloud-Kopie und Wiederherstellung benötigen eine gesonderte praktische Abnahme.

## 0.2.68

- Erfolgreiche Kontoabrufe erscheinen trotz weiterer noch nicht eingerichteter Bankregistrierung als erfolgreich. Echte Abruffehler und offene Einrichtungsaktionen bleiben getrennt erkennbar.

## 0.2.67

- Vertragsbeträge lassen sich aus mehreren ausdrücklich ausgewählten Zahlungen ableiten. Median, Mittelwert oder letzte Zahlung werden mit Quellen und Schätzkennzeichnung gespeichert; belegte Vertragsphasen und Laufzeiten bleiben maßgeblich.
- Kompakte Kontenansichten zeigen Beträge zuerst. Diagnosen und Herkunft sind aufklappbar; Bankprüfungen öffnen die passende Aktion unmittelbar.
- Einseitige eigene Umbuchungen können direkt aus einer Buchung begründet bestätigt und wieder aufgehoben werden. Quellenstand, Schreibsperre und unveränderlicher Verlauf schützen die Entscheidung. Originalbuchungen und Bankaufträge bleiben unverändert.

- PayPal-Stände ohne belegten Abrufzeitpunkt werden als unbekannt gekennzeichnet, ohne ein heutiges Datum zu erfinden.

## 0.2.66

- Die fortlaufende Buchungsliste erhält die geprüfte PayPal-Datumsherkunft auch aus dem tatsächlichen Dashboard-Datensatz. Die Kennzeichnung bleibt in beiden Ansichten und in den Details sichtbar.
- Synthetische Tests prüfen die gespeicherte Herkunft nach erneutem Öffnen des Bestands und über mehrere begrenzte Abfrageblöcke. Buchungen und finanzielle Berechnungen bleiben unverändert.

## 0.2.65

- Fehlende PayPal-Buchungsdaten können durch ein gültiges, dauerhaft als „PayPal-Transaktionsdatum“ gekennzeichnetes Datum ersetzt werden. Ungültige Buchungsdaten bleiben gesperrt.
- Diese Datumsbasis erzeugt keine abgeleiteten Tagesend-, Bewegungs- oder Prognosesalden. Alte Finanzguru-Saldometadaten werden dafür nicht wiederverwendet; unabhängig gespeicherte Kontrollen bleiben sichtbar.

- Vorhandene PayPal-Konten werden bei der direkten Übernahme auch mit der Institutsbezeichnung „PayPal“ statt „PAYPAL“ angeboten.
- Zielauswahl und abschließende Übernahme verwenden denselben exakten, von Groß-/Kleinschreibung unabhängigen Institutsvergleich. Andere Institute und Kontoarten bleiben gesperrt.

## 0.2.64

- PayPal-Umsätze können auf der Buchungsseite nach ausdrücklicher Kontozuordnung und Vorschau übernommen werden.
- Stabile Quellenkennung, erneuter Abruf und Prüfung des Buchungsbestands verhindern unbemerkte Änderungen und doppelte Übernahmen.
- Finanzguru-Altbuchungen lassen sich ausdrücklich zuordnen; Kategorien, Belege, Verträge, Umbuchungen und frühere Revisionen bleiben erhalten.
- Nach bestätigter Direktübernahme können spätere Excel-/CSV-Importe dieses Konto nicht zurücküberschreiben. Andere Konten bleiben importierbar.
- Mobile Zuordnungsansicht; erneutes Laden nach der Übernahme startet keinen zusätzlichen Bankabruf. EUR-only, keine Zahlungen.

## 0.2.63

- Validate manually read PayPal credit dates against the Europe/Berlin calendar in both the form and backend, including summer and winter midnight boundaries.
- Keep audit timestamps in UTC and preserve existing revision and repayment checks.

## 0.2.62

- Reuse bounded immutable day-end proof calculations while checking current source, metadata and ledger evidence on every request.
- Publish an existing private PayPal installment plan into the household forecast only after its owner confirms the account and current source revision.
- Preserve exact due dates and final rates; keep booked payments, explicit assignments and conflicting schedules from creating duplicate projected debits.
- Add readable mobile account-link controls and keep private credit principal and interest details outside the shared forecast.

## 0.2.61

- Preserve booking originals and decision history; reconcile the ledger against dated bank evidence.
- Add dated, reversible classification rules and a single document review cutoff.
- Plan accounting reserve pots, savings and safety thresholds without initiating payments.
- Use Planstand labels, readable responsive charts and central light/dark contrast roles with larger touch targets.

## 0.2.60

- Prove a file-based day-end balance from every posting and recorded following balance in one intact, accepted full snapshot.
- Require complete current-day coverage and one connected balance path with an unambiguous end, independently of row order.
- Keep incomplete, conflicting and mixed evidence unknown; retain direct-bank priority and avoid any ledger mutation.

## 0.2.59

- Allow an explicit per-booking review of display changes on an existing transfer when automatic IBAN evidence is unavailable.
- Bind each decision to verified originals, unchanged payment data and the existing pair; reject stale or ineligible decisions before writing.
- Keep automatic matching strict and skipped bank accounts unchanged.

## 0.2.58

- Preserve an existing cross-account transfer through a counterparty display-name change only when the same valid counterparty IBAN is proven from both hash-verified accepted and incoming originals.
- Keep missing identity evidence and changed payment data blocked; retain skipped bank accounts and revalidate the evidence at commit.

## 0.2.57

- Refresh the shared account snapshot after explicitly saving additional bank balances, without another bank request.
- Keep the successful save visible and distinguish display-refresh errors from failed persistence.

## 0.2.56

- Show confirmed contract sources, payment phases, existing rules and missing fields in a bounded, mobile-readable inventory.
- Interpret Finanzguru ja/nein contract flags without using them as names or granting negative flags marker confidence.
- Explicitly map successful additional bank balance reads to existing accounts, independently of transaction imports.
- Retain immutable owner-bound balance evidence and recheck active users, connection revisions, currencies and source conflicts before display.
- Prefer the newest qualified bank observation on the same bank date; preserve missing and genuinely zero balances separately.

## 0.2.55

- Start the chronological booking timeline at Today and extend future grayscale virtual contract payments while scrolling, with an accessible manual fallback.
- Select multiple accounts without combining currencies or account balances; display each movement with its own calculated or projected balance.
- Confirm recurring-contract membership and a signed planning amount directly from the booking, preserving original transactions and authoritative contract phases.
- Open linked receipts, invoices and contracts from the timeline; preserve confirmed membership through proven import/source adoption.
- Keep mobile controls readable and finger-accessible, including enlarged text, and explicitly identify assumed same-day sequence and unknown balances.

## 0.2.54

- Display PayPal XPCD instant provider balances separately from booked closing balances and Finance Control forecasts.
- Preserve explicit balance currencies for provider accounts marked XXX; never convert or combine currencies.
- Keep invalid or missing wallet values unknown and indicate additional balances beyond the overview limit.

## 0.2.53

- Show expected recurring payments beside booked transactions in the continuous booking feed, with distinct forecast labels and source details.
- Add a desktop account-balance column with calculated daily balances and separately labeled future projections; preserve unknown values and coverage warnings.
- Display own PayPal wallet balances and dated manual installment liabilities separately in the overview.
- Allow confirmed account closure dates and hide closed accounts from later overviews while retaining history.

## 0.2.52

- Place account balances below account names on narrow screens, in both presentation styles.
- Preserve complete navigation words and monthly analysis values with enlarged text.
- Stack auxiliary overview task controls instead of widening the page; enlarge the Klarna disclosure touch target.
- Check all 20 routes, expanded details and dialogs with synthetic browser data at mobile and desktop widths.

## 0.2.51

- Keep recurring payment histories together when direct bank records add a valid German IBAN before the same counterparty name; original bank texts remain unchanged.
- Allow explicit metadata enrichment from an exactly proven, already accepted Finanzguru file without changing transactions or snapshot baselines. Direct bank accounts remain excluded.
- Verify source integrity, unchanged ledger state and review-token freshness before adding file balances or contract markers.

## 0.2.50

- Start in the current calendar month; show compact multi-account balances and a direct mobile Finanzguru/Finanzblick switch.
- Refresh configured own bank connections on cockpit opening, with one shared status across navigation.
- Recognize recurring net payments from existing decisions and booking history, show provenance, and project continuous dated balances while respecting known contract ends.
- Preserve explicit Finanzguru balances and contract markers; direct bank sources remain authoritative.

## 0.2.49

- Keep confirmed contract documents without payment terms visible in Contracts.
- Explain missing setup and link to the document; never infer a recurring payment from its document amount.
- Preserve configured contract matching, forecasts and existing portfolio behavior.

## 0.2.48

- Add a primary Contracts workspace alongside Budgets, with confirmed recurring payments, booked/expected/unresolved dates and conditional per-account balance curves.
- Reuse confirmed contract phases and explicit booking evidence; keep forecasts separate from bank originals and budget amounts.
- Allow manual recurring income/expenses and account/day configuration with confirmation and revision checks; preserve unknown balances, separate currencies and visible estimation limits.
- Verify cutoff isolation, transfer treatment, payment matching and mobile editing with synthetic tests.

## 0.2.47

- Introduce a shared mobile cockpit with Overview, Accounts, Budgets, Analysis and More; retain all existing tools.
- Show recorded current balances with explicit coverage and separate currencies; distinguish account movements, household analysis and budget remainder.
- Add readable date-grouped booking cards, combined filters in a mobile sheet, category drilldown and preserved editing/navigation state.
- Add budget progress cards from existing calculations and a read-only portfolio of confirmed contracts and their linked payments.
- Validate complete workflows on synthetic 360, 390, 430 and desktop viewports; retain bank originals, classifications and existing financial calculations.

## 0.2.46

- Show factual booking categories separately from technical bank-source labels; mark proposals and unresolved categories explicitly without changing ledger data.
- Add confirmed contract terms, optional amount phases and individually confirmed recurring-payment suggestions with reasons and uncertainty.
- Keep contract evidence separate from invoice payment capacity, including older contract links; preserve payment amounts, balances, categories and plans.
- Verify contract workflows and category status on synthetic mobile and desktop browser fixtures; no private documents or bank network are used in tests.

## 0.2.45

- Remove a late compact toolbar override that squeezed primary tools into narrow buttons with smaller text.
- Retain the two-column mobile toolbar and scalable primary text; keep complete words and at least 44 CSS-pixel touch targets.
- Extend the synthetic mobile browser test to check the effective toolbar layout, text growth and the complete "Belege bearbeiten" label. Physical Samsung verification remains separate.

## 0.2.44

- Keep the five main workspaces readable on narrow screens, with scalable text and primary touch targets of at least 44 CSS pixels.
- Present account comparisons as mobile label/value rows, retain complete amounts and favorable green/unfavorable red deviations, and contain detailed table scrolling.
- Verify 30 synthetic browser combinations: five workspaces at 360, 384 and 412 CSS pixels with 100% and 125% root text size. Physical Samsung behavior remains unverified.

## 0.2.43

- Treat recognized explicitly negated or partial Klarna/PayPal payment statements as non-final for `paid`.
- Preserve independently recognized events and do not invent an `unpaid` state from unknown or negated wording.
- Keep historical `paid` events; this release does not delete them automatically and does not add a full invoice or installment balance view.

## 0.2.42

- Add a separate personal Klarna connection through Enable Banking with its own encrypted application keys, owner-bound consent and single-use return state.
- Show bounded balance and transaction previews on the bookings page; keep account setup in administration and leave the ledger unchanged.
- State explicitly that account AIS does not provide a verified consumer invoice or installment-plan overview; include isolated Linux vault checks in the image build.

## 0.2.41

- Queue finite startup request bursts instead of immediately dropping essential frontend assets when all eight HTTP workers are occupied.
- Retain eight active workers and existing socket timeouts; limit the listen backlog to 64 and the accepted request's wait to five seconds.
- Keep authentication, financial calculations, bank operations and business-operation serialization unchanged.

## 0.2.40

- Record owner-bound, dated manual PayPal credit plans with immutable revisions and cent-exact checks for complete unchanged regular plans.
- Separate remaining scheduled payments including future interest from unknown outstanding principal; show verified repayment components without creating ledger entries.
- Explain the current AIS credit-data limitation and display supplied transaction codes and unconfirmed repayment text hints without automatic classification.

## 0.2.39

- Restore favorable green and unfavorable red plan deviations in the account overview and its expandable planned positions.
- Judge account outflow deviations in the opposite direction to inflows while preserving the displayed actual-minus-plan values.
- Keep missing plan values and zero deviations neutral; leave the virtual cash account without plan coloring.

## 0.2.38

- Accept both documented Enable Banking consent hosts, `auth.enablebanking.com` and `tilisy.enablebanking.com`, using exact HTTPS host validation in the client and UI.
- Report non-JSON API responses with bounded HTTP metadata instead of exposing an HTML parser error.
- Preserve existing JSON errors and the single CSRF refresh; never show response HTML, headers or return URLs.

## 0.2.37

- Add owner-bound Enable Banking setup for a personal PayPal read-only connection, with a server-generated public certificate and official consent return.
- Encrypt application keys and consent metadata in the excluded server vault; require verified cockpit identity and one-use, short-lived consent state.
- Provide bounded balances and transaction previews on the bookings page. No ledger import or source cutover until real account coverage, references and reconciliation have been verified.
- Extend the offline Linux build self-test to cover the PayPal vault and its authenticated key check.

## 0.2.36

- Add a separate, manual historical backfill for explicitly selected ING EUR savings targets, one fully closed month at a time within 366 days.
- Reuse full prior-month and target-month controls, owner/revision checks, reviewed import confirmation, server backup and atomic import; reject partial bank coverage.
- Keep the historical read-only check separate and preserve normal 90-day import and refresh limits. No opening balance adjustments or automatic historical series.

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
