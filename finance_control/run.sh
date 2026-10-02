#!/bin/sh
set -eu
umask 077
mkdir -p /data
exec python -m finance_control.web \
  --host 0.0.0.0 \
  --port 8785 \
  --allowed-host finance.pistelok.de
