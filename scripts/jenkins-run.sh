#!/usr/bin/env bash
# Wrapper every Jenkins job runs its script through, so every build log starts
# with the same context block and ends with the exit code + duration.
#
#   jenkins-run.sh <script> [args...]
#
# Secrets (API keys) reach the script as environment variables set in the job's
# command line; this wrapper never prints the environment, only the command.
set -uo pipefail
# Python block-buffers stdout into a pipe: lines would reach the console late (and with wrong timestamps)
export PYTHONUNBUFFERED=1

hr() { printf '%*s\n' 78 '' | tr ' ' '='; }
now() { date '+%Y-%m-%d %H:%M:%S %Z'; }

: "${1:?usage: jenkins-run.sh <script> [args...]}"
REPO=/home/bjtn/docker
# the script is the first argument that is a real file (skips an interpreter like python3)
SCRIPT="$1"
for a in "$@"; do [ -f "$a" ] && { SCRIPT="$a"; break; }; done

hr
echo "job:        ${JOB_NAME:-<not run by Jenkins>} #${BUILD_NUMBER:-?}  (${BUILD_URL:-no build url})"
echo "started:    $(now)"
echo "host:       $(hostname)   user: $(id -un)   pid: $$"
echo "command:    $*"
echo "script:     $(realpath "$SCRIPT" 2>/dev/null || echo "$SCRIPT")   (modified $(date -r "$SCRIPT" '+%F %T' 2>/dev/null || echo '?'))"
echo "git:        $(git -C "$REPO" log -1 --format='%h %s (%cr)' 2>/dev/null || echo 'unknown')"
dirty=$(git -C "$REPO" status --porcelain -- "$(realpath --relative-to="$REPO" "$SCRIPT" 2>/dev/null)" 2>/dev/null)
[ -n "$dirty" ] && echo "            WARNING: this script has uncommitted changes"
echo "uptime:     $(uptime -p)   load: $(cut -d' ' -f1-3 /proc/loadavg)"
echo "disks:"
for m in / /mnt/vault /mnt/b1_4tb /mnt/b2_4tb; do
  if mountpoint -q "$m" 2>/dev/null; then
    df -h --output=target,size,used,avail,pcent "$m" | tail -1 | awk '{printf "            %-12s %6s used of %6s (%s), %s free\n", $1, $3, $2, $5, $4}'
  else
    echo "            $m NOT MOUNTED"
  fi
done
hr

start=$(date +%s)
"$@"
rc=$?
dur=$(( $(date +%s) - start ))

hr
printf 'finished:   %s   duration: %dh %02dm %02ds   exit code: %d (%s)\n' \
  "$(now)" $((dur/3600)) $((dur%3600/60)) $((dur%60)) "$rc" "$([ $rc -eq 0 ] && echo OK || echo FAILED)"
hr
exit $rc
