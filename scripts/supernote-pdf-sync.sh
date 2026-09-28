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

TARGET_DIR="${TARGET_DIR:-/mnt/vault/nextcloud/bjtn/files}"   # overridable for testing
LOG="${LOG:-/home/bjtn/logs/supernote-pdf-sync.log}"
mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
# tool output to the log file AND the console, indented under its file; pipefail keeps the tool's exit status
show() { tee -a "$LOG" | sed -u 's/^/      /'; }

if [ ! -d "$TARGET_DIR" ]; then
  log "ABORT: $TARGET_DIR does not exist (Nextcloud not mounted/up?)"
  exit 1
fi

converted=0
skipped=0
failed=0
log "looking for .note files under $TARGET_DIR (converter: $(command -v supernote_pdf || echo 'supernote_pdf NOT FOUND'))"

while IFS= read -r -d '' note; do
  pdf="${note%.note}.pdf"
  # Logged before the up-to-date check too (not just real conversions) --
  # this is also the per-file marker a progress bar counts against the
  # total files found, so it needs to cover every file touched, not just
  # ones that actually get (re)converted.
  log "processing: $note"
  note_info="note $(du -h "$note" | cut -f1), modified $(date -r "$note" '+%F %T')"
  if [ -f "$pdf" ] && [ "$pdf" -nt "$note" ]; then
    log "  up to date, skipped ($note_info; PDF from $(date -r "$pdf" '+%F %T'))"
    skipped=$((skipped+1))
    continue
  fi
  if [ -f "$pdf" ]; then
    log "  PDF is older than the note ($note_info; PDF from $(date -r "$pdf" '+%F %T')) -- rebuilding"
    rm -f "$pdf"   # stale (note edited since); supernote_pdf won't overwrite
  else
    log "  no PDF yet ($note_info) -- converting"
  fi
  t0=$SECONDS
  if supernote_pdf -i "$note" -o "$pdf" 2>&1 | show; then
    log "  converted -> $pdf ($(du -h "$pdf" 2>/dev/null | cut -f1), $((SECONDS - t0))s)"
    converted=$((converted+1))
  else
    log "FAILED converting: $note"
    failed=$((failed+1))
  fi
done < <(find "$TARGET_DIR" -type f -iname "*.note" -print0)

log "done: $converted converted, $skipped already up to date, $failed failed"

if [ "$converted" -eq 0 ]; then
  log "nothing converted, so no Nextcloud rescan needed"
fi
if [ "$converted" -gt 0 ]; then
  log "rescanning bjtn/files in Nextcloud so new PDFs show up"
  docker exec -u www-data nextcloud php occ files:scan --path="bjtn/files" 2>&1 | show
fi

if [ "$failed" -gt 0 ]; then
  exit 1
fi
exit 0
