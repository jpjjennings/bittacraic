#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Creates one compressed app/database backup per day, retaining the newest three.
# Run as root so the app files and SQLite database can be read.

if [[ ${EUID} -ne 0 ]]; then
    printf 'Run this script as root (for example: sudo %s)\n' "$0" >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
APP_DIR="$(realpath -- "${APP_DIR:-${SCRIPT_DIR}/..}")"
DATA_DIR="${DATA_DIR:-/var/lib/bittacraic/data}"
DB_PATH="${DB_PATH:-${DATA_DIR}/bittacraic.sqlite3}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/bittacraic}"

if [[ ! -d ${APP_DIR} ]]; then
    printf 'App directory not found: %s\n' "$APP_DIR" >&2
    exit 1
fi
if [[ ! -d ${DATA_DIR} || ! -f ${DB_PATH} ]]; then
    printf 'App data directory or SQLite database not found: %s\n' "$DB_PATH" >&2
    exit 1
fi

DATA_DIR="$(realpath -- "$DATA_DIR")"
DB_PATH="$(realpath -- "$DB_PATH")"
DB_REL_PATH="$(realpath --relative-to="$DATA_DIR" "$DB_PATH")"
if [[ ${DB_REL_PATH} == .. || ${DB_REL_PATH} == ../* ]]; then
    printf 'Database must be inside DATA_DIR (%s)\n' "$DATA_DIR" >&2
    exit 1
fi

mkdir -p -- "$BACKUP_DIR"
BACKUP_DIR="$(realpath -- "$BACKUP_DIR")"
exec 9>"${BACKUP_DIR}/.backup.lock"
if ! flock -n 9; then
    printf 'Another Bitta Craic backup is already running.\n' >&2
    exit 1
fi

TODAY="$(date +%F)"
ARCHIVE="${BACKUP_DIR}/${TODAY}-bittacraic.tar.gz"
STAGE="$(mktemp -d --tmpdir="$BACKUP_DIR" '.bittacraic-stage.XXXXXX')"
ARCHIVE_TMP="${BACKUP_DIR}/.${TODAY}-bittacraic.tar.gz.tmp.$$"

cleanup() {
    rm -rf -- "$STAGE"
    rm -f -- "$ARCHIVE_TMP"
}
trap cleanup EXIT

mkdir -p -- "${STAGE}/app" "${STAGE}/data"

# Package the app source, omitting uploads, Git metadata, virtualenvs and caches.
tar -C "$APP_DIR" \
    --exclude='./.git' \
    --exclude='./.venv' \
    --exclude='./__pycache__' \
    --exclude='**/__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='uploads' \
    --exclude='**/uploads' \
    -cf - . | tar -C "${STAGE}/app" -xpf -

# Preserve other data files, but take the live SQLite database using SQLite's
# backup API so concurrent app writes do not produce an inconsistent copy.
tar -C "$DATA_DIR" \
    --exclude="./${DB_REL_PATH}" \
    --exclude="./${DB_REL_PATH}-wal" \
    --exclude="./${DB_REL_PATH}-shm" \
    --exclude='uploads' \
    --exclude='**/uploads' \
    -cf - . | tar -C "${STAGE}/data" -xpf -

STAGED_DB="${STAGE}/data/${DB_REL_PATH}"
mkdir -p -- "$(dirname -- "$STAGED_DB")"
python3 - "$DB_PATH" "$STAGED_DB" <<'PY'
import os
import shutil
import sqlite3
import sys

source_path, destination_path = sys.argv[1:]
source = sqlite3.connect(source_path, timeout=60)
destination = sqlite3.connect(destination_path)
try:
    source.backup(destination, pages=256, sleep=0.1)
finally:
    destination.close()
    source.close()

shutil.copystat(source_path, destination_path)
source_stat = os.stat(source_path)
os.chown(destination_path, source_stat.st_uid, source_stat.st_gid)
PY

tar -czf "$ARCHIVE_TMP" -C "$STAGE" app data
gzip -t "$ARCHIVE_TMP"
mv -f -- "$ARCHIVE_TMP" "$ARCHIVE"

# ISO-formatted names sort chronologically; keep only the newest three.
mapfile -t BACKUPS < <(
    find "$BACKUP_DIR" -maxdepth 1 -type f \
        -name '????-??-??-bittacraic.tar.gz' -printf '%f\n' |
        LC_ALL=C sort -r
)
RETAINED_COUNT=${#BACKUPS[@]}
for ((i = 3; i < ${#BACKUPS[@]}; i++)); do
    rm -f -- "${BACKUP_DIR}/${BACKUPS[i]}"
done
if ((RETAINED_COUNT > 3)); then
    RETAINED_COUNT=3
fi

printf 'Backup created: %s\n' "$ARCHIVE"
printf 'Retained backups: %s\n' "$RETAINED_COUNT"
