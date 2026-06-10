#!/bin/bash
# Daily backup of the LIVE coloom db (~/coloom-data/coloom.sqlite, service
# coloom-live on :5555) into ~/coloom-backups/, keeping the newest 14.
# Uses sqlite3 .backup (safe against a live WAL db — consistent snapshot,
# no copy-while-writing torn pages). Cron'd daily; run by hand any time.
set -euo pipefail

DB="$HOME/coloom-data/coloom.sqlite"
OUT_DIR="$HOME/coloom-backups"
KEEP=14

mkdir -p "$OUT_DIR"
[ -f "$DB" ] || { echo "no live db at $DB" >&2; exit 1; }

stamp="$(date +%Y-%m-%d_%H%M%S)"
out="$OUT_DIR/coloom-$stamp.sqlite"
sqlite3 "$DB" ".backup '$out'"
echo "backed up $DB -> $out ($(du -h "$out" | cut -f1))"

# prune: keep the newest $KEEP backups
ls -1t "$OUT_DIR"/coloom-*.sqlite 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r old; do
    rm -- "$old"
    echo "pruned $old"
done
