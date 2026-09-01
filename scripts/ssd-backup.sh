#!/usr/bin/env bash
set -uo pipefail
shopt -s nullglob

# defensive PATH -- don't rely on cron's (or any caller's) inherited PATH
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# runs under bjtn's own crontab, not root's -- nothing here needs root, and
# root's cron HOME is /root, not /home/bjtn (confirmed directly), which is
# exactly why these are hardcoded absolute paths rather than $HOME-relative
export RESTIC_PASSWORD_FILE="${RESTIC_PASSWORD_FILE:-/home/bjtn/.restic-password}"
B0_ROOT="${B0_ROOT:-/mnt/b0_1tb}"
REPLICA_A="${REPLICA_A:-/mnt/b3_1tb}"
REPLICA_B="${REPLICA_B:-/mnt/b4_1tb}"
LOG="${LOG:-/home/bjtn/logs/ssd-backup.log}"
LOCKFILE="${LOCKFILE:-/tmp/ssd-backup.lock}"
mkdir -p "$(dirname "$LOG")"

exec 9>"$LOCKFILE"
flock -n 9 || { echo "already running, exiting"; exit 1; }

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

# preflight: fail loudly and immediately if a required tool is missing,
# rather than let it manifest later as a confusing per-repo SKIP/failure
for bin in restic rsync jq mountpoint flock; do
  command -v "$bin" >/dev/null 2>&1 || { log "ABORT: required command '$bin' not found on PATH"; exit 1; }
done

# refuse to do ANYTHING if any of the three drives isn't actually mounted --
# an unmounted drive is just an empty local directory, and rsync --delete
# against an accidentally-empty "source" would silently wipe a real replica
for d in "$B0_ROOT" "$REPLICA_A" "$REPLICA_B"; do
  if ! mountpoint -q "$d"; then
    log "ABORT: $d is not a mounted filesystem -- refusing to run (would risk treating an unmounted drive as empty and wiping a good replica)"
    exit 1
  fi
done

FAILED=0
CHECK_FAILED=0

for repo_dir in "$B0_ROOT"/*/; do
  repo="$(basename "$repo_dir")"

  if [ ! -f "${repo_dir}config" ]; then
    log "SKIP $repo: not a restic repo (no config file)"
    continue
  fi

  export RESTIC_REPOSITORY="$repo_dir"

  restic unlock >>"$LOG" 2>&1

  if ! snap_json=$(restic snapshots --latest 1 --json 2>>"$LOG"); then
    log "FAILED $repo: could not query snapshots (see above -- likely wrong password, corrupt repo, or unmounted drive)"
    FAILED=1
    continue
  fi

  mapfile -t paths < <(echo "$snap_json" | jq -r '.[0].paths[]?' 2>/dev/null)
  if [ "${#paths[@]}" -eq 0 ]; then
    log "SKIP $repo: no prior snapshot yet -- back it up manually once first"
    continue
  fi

  log "backing up $repo <- ${paths[*]}"
  if restic backup "${paths[@]}" >>"$LOG" 2>&1; then
    if ! restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune >>"$LOG" 2>&1; then
      log "FAILED: $repo forget/prune (retention policy not enforced this run -- see log)"
      FAILED=1
    fi
  else
    log "FAILED: $repo"
    FAILED=1
    continue
  fi

  log "checking $repo (structural)"
  if ! restic check >>"$LOG" 2>&1; then
    log "CHECK FAILED: $repo -- will NOT replicate this run"
    CHECK_FAILED=1
  else
    # rotating deep check: verifies actual data content (catches same-size
    # bit-rot that structural check misses), 1/7th of data per night so the
    # full repo gets fully re-verified once a week instead of never, without
    # paying the cost of reading everything every single night
    day_of_week=$(date +%u)
    log "deep-checking $repo (data subset ${day_of_week}/7)"
    if ! restic check --read-data-subset="${day_of_week}/7" >>"$LOG" 2>&1; then
      log "DEEP CHECK FAILED: $repo -- will NOT replicate this run"
      CHECK_FAILED=1
    fi
  fi
  log "done: $repo"
done

if [ "$CHECK_FAILED" -eq 1 ]; then
  log "at least one repo failed integrity check -- skipping replication entirely this run to avoid propagating corruption. Fix the flagged repo, then rerun."
  exit 1
fi

log "replicating to $REPLICA_A"
rsync -a --delete-after "$B0_ROOT"/ "$REPLICA_A"/ >>"$LOG" 2>&1
rc=$?
if [ $rc -ne 0 ]; then
  log "FAILED: replication to $REPLICA_A (rsync exit code $rc, see log)"
  FAILED=1
fi

log "replicating to $REPLICA_B"
rsync -a --delete-after "$B0_ROOT"/ "$REPLICA_B"/ >>"$LOG" 2>&1
rc=$?
if [ $rc -ne 0 ]; then
  log "FAILED: replication to $REPLICA_B (rsync exit code $rc, see log)"
  FAILED=1
fi

[ "$FAILED" -eq 1 ] && { log "one or more jobs failed"; exit 1; }
log "all good"
