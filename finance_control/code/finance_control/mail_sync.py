"""Delegate differential mail acquisition to the pinned supplier package."""
import sys


def main(argv=None):
    if sys.version_info < (3, 12):
        print('Mail-Abgleich benoetigt die optionale Python-3.12-Mail-Umgebung.', file=sys.stderr)
        return 2
    try:
        from invoice_mail_archive.mail_sync_cli import main as supplier_main
    except ImportError:
        print('Bitte den festgeschriebenen Mail-Lieferstand installieren.', file=sys.stderr)
        return 2
    return supplier_main(argv)


if __name__ == '__main__':
    raise SystemExit(main())
