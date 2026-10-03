import argparse
import json

from .core import Plan, Store, forecast, scenario


def main():
    parser = argparse.ArgumentParser(description='Finance Control synthetic demo')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--demo', action='store_true')
    mode.add_argument('--bank-readiness', action='store_true', help='Offline bank setup report')
    mode.add_argument('--preview-xlsx', metavar='PATH', help='Lesende XLSX-Importvorschau')
    mode.add_argument('--preview-eml', metavar='PATH', help='Lokale EML-Vorschau, optional Python >=3.12')
    mode.add_argument('--preview-invoice-json', metavar='PATH', help='Rechnungsdaten vom Lieferprojekt prüfen')
    mode.add_argument('--init', metavar='DIRECTORY', help='Neuen lokalen Datenordner initialisieren')
    init_source = parser.add_mutually_exclusive_group()
    init_source.add_argument('--template', choices=('individual', 'couple-shared', 'couple-separate', 'household-shared'))
    init_source.add_argument('--profile-file', metavar='PATH', help='Validiertes Profil schema_version 2 laden')
    parser.add_argument('--profile-name', default=None)
    args = parser.parse_args()
    if args.init is None and (args.template is not None or args.profile_file is not None or args.profile_name is not None):
        parser.error('--template, --profile-file und --profile-name benötigen --init')
    if args.init is not None:
        if args.template is None and args.profile_file is None:
            parser.error('--init benötigt --template oder --profile-file')
        if args.profile_file is not None and args.profile_name is not None:
            parser.error('--profile-name ist nur zusammen mit --template zulässig')
        from .onboarding import initialize, load_profile_file

        try:
            if args.profile_file is not None:
                report = initialize(args.init, profile=load_profile_file(args.profile_file))
            else:
                report = initialize(args.init, args.template,
                                    args.profile_name if args.profile_name is not None else 'Mein Haushalt')
        except (ValueError, OSError):
            parser.exit(2, 'Initialisierung fehlgeschlagen: Profil oder Zielordner ungültig.\n')
        print(json.dumps(report, ensure_ascii=False))
        return
    if args.preview_invoice_json is not None:
        from .invoice_import import read_invoice_candidate
        try:
            report = read_invoice_candidate(args.preview_invoice_json).summary()
        except Exception:
            parser.exit(2, 'Rechnungsdatei ungültig oder nicht lesbar. Keine Buchung übernommen.\n')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if args.preview_eml is not None:
        from .import_preview import RepositoryPathError
        from .mail_preview import MailPreviewError, inspect_eml
        try:
            report = inspect_eml(args.preview_eml)
        except (MailPreviewError, RepositoryPathError) as exc:
            parser.exit(2, str(exc) + '\n')
        except Exception:
            parser.exit(2, 'Maildatei nicht lesbar. Keine Daten übernommen.\n')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if args.preview_xlsx is not None:
        from .import_preview import RepositoryPathError, inspect_workbook
        try:
            report = inspect_workbook(args.preview_xlsx)
        except ImportError:
            parser.exit(2, 'Bitte das optionale Paket finance-control[imports] installieren.\n')
        except RepositoryPathError as exc:
            parser.exit(2, str(exc) + '\n')
        except Exception:
            # Parser exceptions can embed source XML or private file paths.
            parser.exit(2, 'Quelldatei nicht lesbar oder nicht unterstützt. Keine Daten übernommen.\n')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if args.bank_readiness:
        from .connectors.profiles import readiness
        print(json.dumps(readiness(), ensure_ascii=False, indent=2))
        return
    store = Store()
    try:
        store.add_account('personal-a', 'ANDREAS', {'ANDREAS': '1'}, '2000', '2026-08-31')
        store.add_account('personal-e', 'ERLENE', {'ERLENE': '1'}, '1500', '2026-08-31')
        store.add_account('joint', 'JOINT', {'ANDREAS': '.5', 'ERLENE': '.5'}, '3000', '2026-08-31')
        store.import_csv('external_id,account_id,date,amount,currency,category,transfer_id\n'
                         'salary,personal-a,2026-09-01,3000,EUR,Einkommen,\n'
                         'rent,joint,2026-09-02,-1200,EUR,Miete,\n'
                         'move-a,personal-a,2026-09-03,-500,EUR,Umbuchung,t1\n'
                         'move-j,joint,2026-09-03,500,EUR,Umbuchung,t1\n')
        status = store.status('2026-09-30')
        base = Plan('5000', '3500', '6000')
        adverse = scenario(base, income='3000', one_offs=((3, '-4000'),))
        print(json.dumps({'synthetic': True, 'status': status,
                          'base': forecast(status['liquidity'], base),
                          'scenario': forecast(status['liquidity'], adverse)}, default=str, indent=2))
    finally:
        store.close()


if __name__ == '__main__':
    main()
