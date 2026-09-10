#!/bin/bash
# Nightly backup of all docker service config/data + database dumps to the
# restic repo on /mnt/vault/restic. Replaces the old /home/bjtn/pi-configs/backup.sh
# cron job, which silently failed every night because that path no longer
# existed on disk. (Repo path updated 2026-08-26 after the 6-drive-to-vault
# migration moved /mnt/other's content to /mnt/vault/other; moved again
# 2026-08-29 from /mnt/vault/other/restic-backups to its own top-level
# /mnt/vault/restic -- same filesystem, plain `mv`, no data touched. Restore
# instructions live alongside the repo at /mnt/vault/restic/RESTORE.md.)
#
# Scope: /home/bjtn/docker (every service's bind-mounted config/data --
# this includes /home/bjtn/docker/scripts itself, moved here 2026-09-01 for
# git tracking, so it's covered without a separate path entry),
# /home/bjtn/api-keys.txt, plus SQL dumps of the 5
# databases (dumped logically instead of raw-copying live DB files, to
# avoid backing up a DB mid-write). Does NOT include the media libraries
# (/mnt/media, /mnt/emu, /mnt/games, /mnt/obs, /mnt/beige) -- that's a much
# bigger, separate concern from "redo the configuration."
#
# Also excludes caddy's internal state (caddy_config/caddy, caddy_data/caddy
# -- TLS certs, autosave.json, instance.uuid) -- fully auto-regenerated via
# the porkbun DNS-01 wildcard on next start, not worth backing up. The
# authored Caddyfile itself is a separate path and still gets backed up.
#
# On success, pings a Kuma push monitor so a missed/failed run shows up as
# a monitoring alert instead of a silent failure.

set -uo pipefail

# Runs as bjtn via bjtn's own crontab (moved off root's crontab 2026-09-01 --
# nothing here actually needs root: docker exec works via bjtn's docker-group
# membership, and /mnt/vault/restic is bjtn-owned). Previously ran as root,
# which needed `umask 000` to avoid root-created repo objects coming out
# owner-only (0600) and unreadable to bjtn (found + fixed 2026-08-26, see
# [[jellyfin-metadata-delete-danger]]-adjacent incident in
# [[media-drive-consolidation]] memory for the full story). That problem
# doesn't exist running as bjtn -- bjtn's own default umask (0002) is used
# instead, which is also tighter (no longer world-writable DB dumps/config).

export RESTIC_REPOSITORY=/mnt/vault/restic
export RESTIC_PASSWORD_FILE=/home/bjtn/.restic-password

STAGING=/home/bjtn/.backup-staging
LOG_TAG="[$(date '+%Y-%m-%d %H:%M:%S')]"
KUMA_PUSH_URL="https://kuma.bjtn.xyz/api/push/ad383c5157752f22313f"

mkdir -p "$STAGING"
fail=0

echo "$LOG_TAG Starting backup"

dump() {
  # dump <label> <command...>
  local label="$1"; shift
  if "$@" > "$STAGING/$label.sql.tmp" 2>"$STAGING/$label.err"; then
    mv "$STAGING/$label.sql.tmp" "$STAGING/$label.sql"
    rm -f "$STAGING/$label.err"
    echo "$LOG_TAG  dumped $label OK ($(du -h "$STAGING/$label.sql" | cut -f1))"
  else
    echo "$LOG_TAG  FAILED dumping $label -- see $STAGING/$label.err"
    fail=1
  fi
}

# Auto-discovers database containers by image rather than a fixed list,
# so adding or removing a service's DB doesn't require remembering to
# edit this script. Found live 2026-09-09, in both directions at once:
# a long-decommissioned grimmory-db was still hardcoded here (silently
# "failing" every single night), while a real, currently-running
# youtarr-db was never added at all -- never backed up, zero signal
# either way. Detected purely by each running container's IMAGE name
# (mariadb/mysql vs postgres family); credentials are read from THAT
# container's own environment (docker exec inherits it), trying each
# family's common root-credential variable name in order -- confirmed
# live that this genuinely varies even among containers already in use
# here (romm-db/yourls-db use MARIADB_ROOT_PASSWORD, youtarr-db uses the
# older MYSQL_ROOT_PASSWORD instead) -- and, found the same night, the
# dump BINARY name varies just as much: youtarr-db's older mariadb:10.3
# image only ships the legacy `mysqldump`, not the newer `mariadb-dump`
# every other mariadb-family container here happens to have. Tries
# mariadb-dump first, falls back to mysqldump if that binary doesn't
# exist in the container at all. This covers the standard case (a
# service using its image's normal root-auth setup, which is everything
# currently running) -- a container with genuinely nonstandard auth
# (e.g. a non-root-only user with no root password at all, which is what
# made the old grimmory-db dump command look different from the others)
# can't be discovered automatically, but it fails LOUDLY here (a clear
# "FAILED dumping" line + non-zero exit, same as any other dump failure)
# rather than silently never being attempted.
while IFS=$'\t' read -r db_name db_image; do
  case "$db_image" in
    *mariadb*|*mysql*)
      dump "$db_name" docker exec "$db_name" sh -c \
        'DUMP_BIN=mariadb-dump; command -v "$DUMP_BIN" >/dev/null 2>&1 || DUMP_BIN=mysqldump
         "$DUMP_BIN" -uroot -p"${MARIADB_ROOT_PASSWORD:-$MYSQL_ROOT_PASSWORD}" --all-databases'
      ;;
    *postgres*)
      dump "$db_name" docker exec "$db_name" sh -c \
        'PGPASSWORD="$POSTGRES_PASSWORD" pg_dumpall -U "${POSTGRES_USER:-postgres}"'
      ;;
  esac
done < <(docker ps --format '{{.Names}}\t{{.Image}}')

# Stale dumps from a since-removed database would otherwise sit in
# STAGING forever (never deleted, just never updated) -- same "forget to
# clean up" problem the fixed list had, just for output instead of input.
# Only ever removes .sql files for containers NOT seen this run; a
# transient blip that stops a container appearing in `docker ps` for one
# run doesn't delete real data, it just means that container's dump.err
# from the loop above (recorded as a normal dump failure) explains why
# its .sql wasn't refreshed this time.
running_dbs=$(docker ps --format '{{.Names}}' | sort)
for f in "$STAGING"/*.sql; do
  [ -e "$f" ] || continue
  label=$(basename "$f" .sql)
  if ! grep -qx "$label" <<<"$running_dbs"; then
    echo "$LOG_TAG  removing stale dump: $label.sql (container no longer running)"
    rm -f "$f"
  fi
done

# Back up the crontab too, since that's config that lives nowhere else on disk.
crontab -l > "$STAGING/bjtn-crontab.txt" 2>/dev/null

echo "$LOG_TAG Running restic backup"
restic backup \
  /home/bjtn/docker \
  /home/bjtn/api-keys.txt \
  "$STAGING" \
  --exclude /home/bjtn/docker/arr/romm/db \
  --exclude /home/bjtn/docker/yourls/db \
  --exclude /home/bjtn/docker/immich/postgres \
  --exclude /home/bjtn/docker/nextcloud/postgres \
  --exclude /home/bjtn/docker/nextcloud/redis/dump.rdb \
  --exclude /home/bjtn/docker/caddy/caddy_config/caddy \
  --exclude /home/bjtn/docker/caddy/caddy_data/caddy \
  --exclude-caches \
  2>&1 | sed "s/^/$LOG_TAG  /"

if [ "${PIPESTATUS[0]}" -ne 0 ]; then
  echo "$LOG_TAG restic backup FAILED"
  fail=1
fi

echo "$LOG_TAG Pruning old snapshots (keep 7 daily / 4 weekly / 6 monthly)"
restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune 2>&1 | sed "s/^/$LOG_TAG  /"

if [ "$fail" -eq 0 ]; then
  echo "$LOG_TAG Backup completed successfully"
  [ "$KUMA_PUSH_URL" != "__KUMA_PUSH_URL__" ] && curl -fsS -m 10 "$KUMA_PUSH_URL?status=up&msg=OK" >/dev/null 2>&1
  exit 0
else
  echo "$LOG_TAG Backup completed WITH ERRORS -- see above"
  [ "$KUMA_PUSH_URL" != "__KUMA_PUSH_URL__" ] && curl -fsS -m 10 "$KUMA_PUSH_URL?status=down&msg=backup+script+reported+errors" >/dev/null 2>&1
  exit 1
fi
