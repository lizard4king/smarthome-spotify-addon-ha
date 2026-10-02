"""Run differential mail acquisition and structured payment-status import."""
import argparse
import json
from pathlib import Path

from .core import Store
from .import_preview import outside_repository
from .mail_sync import main as supplier_sync
from .payment_status import import_supplier_statuses


def run(account, folder, supplier_database, database, since='2026-01-01'):
    supplier_database = outside_repository(Path(supplier_database))
    database = outside_repository(Path(database))
    try:
        result = supplier_sync([
            '--account', account, '--folder', folder,
            '--database', str(supplier_database), '--execute',
        ])
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else 2
        return {'status': 'sync_failed', 'supplier_exit_code': code}
    if result != 0:
        return {'status': 'sync_failed', 'supplier_exit_code': result}
    store = Store(database)
    try:
        report = import_supplier_statuses(store, supplier_database, since)
    finally:
        store.close()
    return {'status': 'complete', 'supplier_exit_code': 0, 'import': report}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Differenziellen Mailabruf und Zahlungsstatusimport ausführen')
    parser.add_argument('--account', required=True)
    parser.add_argument('--folder', default='INBOX')
    parser.add_argument('--supplier-database', required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--since', default='2026-01-01')
    args = parser.parse_args(argv)
    try:
        report = run(args.account, args.folder, args.supplier_database,
                     args.database, args.since)
    except Exception as error:
        parser.exit(2, 'Mailabgleich oder Zahlungsstatusimport fehlgeschlagen; '
                       f'Details bleiben lokal. ({type(error).__name__})\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
