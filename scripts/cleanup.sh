#!/usr/bin/env bash
# Prune old local backups. Default: keep 30 days.
# Usage: ./scripts/cleanup.sh [days]
set -euo pipefail

DAYS="${1:-30}"
BACKUP_DIR="${MEMORIA_BACKUP_DIR:-$HOME/memoria/data/backups}"

echo "Pruning backups older than $DAYS days from $BACKUP_DIR"
if [[ ! -d "$BACKUP_DIR" ]]; then
    echo "  (directory missing -- nothing to prune)"
    exit 0
fi

count=$(find "$BACKUP_DIR" -name 'memoria-*.tar.gz' -mtime "+$DAYS" -print | wc -l)
if [[ "$count" -eq 0 ]]; then
    echo "  (no backups older than $DAYS days)"
    exit 0
fi

find "$BACKUP_DIR" -name 'memoria-*.tar.gz' -mtime "+$DAYS" -print -delete
echo "  deleted $count backup(s)"