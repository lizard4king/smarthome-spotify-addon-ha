#!/bin/sh
set -eu
umask 077
exec python /app/addon_entrypoint.py
