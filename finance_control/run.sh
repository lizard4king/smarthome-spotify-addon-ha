#!/bin/sh
set -eu
umask 077
mkdir -p /data
data_dir=/data/FinanceControl/data
database="$data_dir/finance.sqlite"
if [ ! -e "$database" ] && [ ! -L "$database" ]; then
  python /app/bootstrap.py
fi
if [ -L "$database" ] || [ ! -f "$database" ] || [ ! -s "$database" ]; then
  echo "Finance Control nicht gestartet: keine gültige Datenbank vorhanden." >&2
  exit 1
fi
exec python -m finance_control.web \
  --host 0.0.0.0 \
  --port 8785 \
  --options-file /data/options.json \
  --allowed-host finance.pistelok.de \
  --allowed-origin https://finance.pistelok.de
