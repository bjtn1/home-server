#!/usr/bin/env bash
# Converts Supernote .note files (synced via WebDAV into Nextcloud's
# lifelong-learning folder) to PDFs, in place, so they're readable without
# manual export. Runs daily via bjtn's crontab.
#
# supernote_pdf itself refuses to overwrite an existing output file/dir
# (non-destructive by design) -- so incremental behavior (skip unchanged,
# regenerate edited notes) is handled here: a .pdf is (re)built only if it's
# missing or older than its source .note.
#
# Nextcloud doesn't notice files written directly to disk (bypasses its own
# app layer) -- occ files:scan at the end makes new/updated PDFs show up in
# the web UI/apps without waiting for Nextcloud's own periodic scan.

set -uo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

TARGET_DIR="/mnt/vault/nextcloud/bjtn/files/lifelong-learning"
LOG="/home/bjtn/logs/supernote-pdf-sync.log"
mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

if [ ! -d "$TARGET_DIR" ]; then
  log "ABORT: $TARGET_DIR does not exist (Nextcloud not mounted/up?)"
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
  log "rescanning lifelong-learning in Nextcloud so new PDFs show up"
  docker exec -u www-data nextcloud php occ files:scan --path="bjtn/files/lifelong-learning" >>"$LOG" 2>&1
fi

[ "$failed" -gt 0 ] && exit 1
exit 0
