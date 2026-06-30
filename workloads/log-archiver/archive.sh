#!/bin/sh
# log-archiver: compress everything on the shared PVC - big sequential read+write (S2).
set -x
SRC="${DATA_DIR:-/shared}"
DST="$SRC/archive"
mkdir -p "$DST"
tar czf "$DST/logs-$(date +%s).tar.gz" -C "$SRC" --exclude=archive . 2>/dev/null
# keep last 3 archives
ls -t "$DST"/logs-*.tar.gz 2>/dev/null | tail -n +4 | xargs -r rm -f
