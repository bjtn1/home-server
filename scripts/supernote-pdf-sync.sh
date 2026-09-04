#!/usr/bin/env bash
# Converts Supernote .note files to PDFs, in place, so they're readable
# without manual export. Scans the whole of bjtn's Nextcloud "files" tree
# recursively (not just one subfolder, since 2026-09-04 -- notes can land
# in any synced folder, not only "lifelong-learning"). Runs daily via
# bjtn's crontab.
#
# supernote_pdf itself refuses to overwrite an existing output file/dir
# (non-destructive by design) -- so incremental behavior (skip unchanged,
# regenerate edited notes) is handled here: a .pdf is (re)built only if it's
# missing or older than its source .note.
#
# Nextcloud doesn't notice files written directly to disk (bypasses its own
# app layer) -- occ files:scan at the end makes new/updated PDFs show up in
# the web UI/apps without waiting for Nextcloud's own periodic scan.
#
# Flocked (2026-09-04, added alongside a manual-trigger web button) so a
# button click can't overlap the nightly cron run -- a second invocation
# just exits quietly rather than racing the first over the same PDFs.

set -uo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

LOCKFILE="/tmp/supernote-pdf-sync.lock"
exec 9>"$LOCKFILE"
flock -n 9 || { echo "supernote-pdf-sync: already running (lock held), exiting"; exit 0; }

TARGET_DIR="/mnt/vault/nextcloud/bjtn/files"
LOG="/home/bjtn/logs/supernote-pdf-sync.log"
KUMA_PUSH_URL="https://kuma.bjtn.xyz/api/push/MIIQdecBCa"
mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

if [ ! -d "$TARGET_DIR" ]; then
  log "ABORT: $TARGET_DIR does not exist (Nextcloud not mounted/up?)"
  curl -fsS -m 10 "$KUMA_PUSH_URL?status=down&msg=target+dir+missing" >/dev/null 2>&1
  exit 1
fi

converted=0
skipped=0
failed=0

while IFS= read -r -d '' note; do
  pdf="${note%.note}.pdf"
  if [ -f "$pdf" ] && [ "$pdf" -nt "$note" ]; then
    skipped=$((skipped+1))
    continue
  fi
  [ -f "$pdf" ] && rm -f "$pdf"   # stale (note edited since); supernote_pdf won't overwrite
  if supernote_pdf -i "$note" -o "$pdf" >>"$LOG" 2>&1; then
    converted=$((converted+1))
  else
    log "FAILED converting: $note"
    failed=$((failed+1))
  fi
done < <(find "$TARGET_DIR" -type f -iname "*.note" -print0)

log "done: $converted converted, $skipped already up to date, $failed failed"

if [ "$converted" -gt 0 ]; then
  log "rescanning bjtn/files in Nextcloud so new PDFs show up"
  docker exec -u www-data nextcloud php occ files:scan --path="bjtn/files" >>"$LOG" 2>&1
fi

if [ "$failed" -gt 0 ]; then
  curl -fsS -m 10 "$KUMA_PUSH_URL?status=down&msg=$failed+conversions+failed" >/dev/null 2>&1
  exit 1
fi
curl -fsS -m 10 "$KUMA_PUSH_URL?status=up&msg=OK" >/dev/null 2>&1
exit 0
