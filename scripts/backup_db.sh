#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly ENV_FILE="/opt/fullbox/.env"
readonly BACKUP_ROOT="/opt/fullbox/backups/scheduled"
readonly WEEKLY_ROOT="${BACKUP_ROOT}/weekly"
readonly LOCK_FILE="${BACKUP_ROOT}/.backup.lock"
readonly DAILY_KEEP=4
readonly WEEKLY_KEEP=2
readonly MIN_FREE_KB=$((1024 * 1024))

log() {
    printf '%s %s\n' "$(date --iso-8601=seconds)" "$*"
}

fail() {
    log "ERROR: $*" >&2
    exit 1
}

for command_name in pg_dump pg_restore sha256sum flock find sort sed; do
    command -v "${command_name}" >/dev/null 2>&1 \
        || fail "required command is missing: ${command_name}"
done

[[ -r "${ENV_FILE}" ]] || fail "environment file is not readable: ${ENV_FILE}"

mkdir -p "${BACKUP_ROOT}" "${WEEKLY_ROOT}"
chmod 700 "${BACKUP_ROOT}" "${WEEKLY_ROOT}"

exec 9>"${LOCK_FILE}"
flock -n 9 || fail "another database backup is already running"

set -a
# shellcheck disable=SC1090
# Production .env currently uses CRLF line endings. Normalize only the stream
# consumed by this process; do not modify the live environment file.
. <(sed 's/\r$//' "${ENV_FILE}")
set +a

: "${DB_NAME:?DB_NAME is not set}"
: "${DB_USER:?DB_USER is not set}"
: "${DB_PASSWORD:?DB_PASSWORD is not set}"
DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-5432}"

available_kb="$(df -Pk "${BACKUP_ROOT}" | awk 'NR == 2 {print $4}')"
[[ "${available_kb}" =~ ^[0-9]+$ ]] || fail "cannot determine free disk space"
(( available_kb >= MIN_FREE_KB )) \
    || fail "less than 1 GiB is free on the backup filesystem"

timestamp="$(date '+%Y%m%d_%H%M')"
filename="fullbox_${timestamp}.dump"
final_path="${BACKUP_ROOT}/${filename}"
temporary_path="${final_path}.tmp.$$"
checksum_path="${final_path}.sha256"
temporary_checksum="${checksum_path}.tmp.$$"

cleanup() {
    rm -f -- "${temporary_path}" "${temporary_checksum}"
}
trap cleanup EXIT

[[ ! -e "${final_path}" ]] || fail "backup already exists: ${final_path}"

log "starting PostgreSQL custom-format backup: ${filename}"
PGPASSWORD="${DB_PASSWORD}" pg_dump \
    --format=custom \
    --compress=6 \
    --host="${DB_HOST}" \
    --port="${DB_PORT}" \
    --username="${DB_USER}" \
    --file="${temporary_path}" \
    "${DB_NAME}"

[[ -s "${temporary_path}" ]] || fail "pg_dump produced an empty file"
pg_restore --list "${temporary_path}" >/dev/null
chmod 600 "${temporary_path}"
mv -- "${temporary_path}" "${final_path}"

(
    cd "${BACKUP_ROOT}"
    sha256sum "${filename}" >"$(basename "${temporary_checksum}")"
)
chmod 600 "${temporary_checksum}"
mv -- "${temporary_checksum}" "${checksum_path}"

if [[ "$(date '+%u')" == "7" ]]; then
    weekly_path="${WEEKLY_ROOT}/${filename}"
    ln "${final_path}" "${weekly_path}"
    cp --preserve=mode,timestamps "${checksum_path}" "${weekly_path}.sha256"
    chmod 600 "${weekly_path}" "${weekly_path}.sha256"
    log "created weekly retention link: weekly/${filename}"
fi

mapfile -t daily_files < <(
    find "${BACKUP_ROOT}" -maxdepth 1 -type f -name 'fullbox_*.dump' \
        -printf '%T@ %p\n' | sort -rn | cut -d' ' -f2-
)
for old_file in "${daily_files[@]:${DAILY_KEEP}}"; do
    rm -f -- "${old_file}" "${old_file}.sha256"
    log "removed expired daily backup: $(basename "${old_file}")"
done

mapfile -t weekly_files < <(
    find "${WEEKLY_ROOT}" -maxdepth 1 -type f -name 'fullbox_*.dump' \
        -printf '%T@ %p\n' | sort -rn | cut -d' ' -f2-
)
for old_file in "${weekly_files[@]:${WEEKLY_KEEP}}"; do
    rm -f -- "${old_file}" "${old_file}.sha256"
    log "removed expired weekly backup: $(basename "${old_file}")"
done

size_bytes="$(stat -c '%s' "${final_path}")"
log "backup completed and verified: ${final_path} (${size_bytes} bytes)"
