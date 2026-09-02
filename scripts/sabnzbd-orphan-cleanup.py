#!/usr/bin/env python3
"""
Cleans up SABnzbd's own leftover incomplete/ folders for jobs its OWN
history already marked terminally Failed (repair-block shortfall,
"aborted, cannot be completed", etc). SABnzbd doesn't always clean
these up itself, and they otherwise sit dead forever.

This is NOT covered by Cleanuparr -- confirmed 2026-08-24 by reading
Cleanuparr's own database (download_clients has qBittorrent only, no
SABnzbd row) and its docs: the orphaned-files feature is torrent-only
(qBittorrent/Transmission/Deluge/rTorrent/uTorrent) and works by
matching files against active torrents' save paths -- a mechanism
that has no SABnzbd/Usenet equivalent at all. See the questarr-romarr
session's disk-cleanup investigation / the sabnzbd-orphan-cleanup
memory entry for the full story.

Safety model (mirrors the manual 2026-08-24 cleanup this automates):
a folder is deleted ONLY if BOTH, checked live immediately before
deletion:
  1. Its name is NOT present in SABnzbd's current active queue
  2. Its name IS present in SABnzbd's history with status "Failed"
Nothing else is ever touched. No guessing, no age-based heuristics --
only SABnzbd's own authoritative status is trusted.
"""
import json
import os
import shutil
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone

SABNZBD_URL = "https://nzb.bjtn.xyz"
SABNZBD_API_KEY = os.environ["SABNZBD_API_KEY"]
INCOMPLETE_DIR = "/mnt/vault/downloads/incomplete"
LOG_FILE = "/home/bjtn/logs/sabnzbd-orphan-cleanup.log"
KUMA_PUSH_URL = "https://kuma.bjtn.xyz/api/push/Zx5WlZatCe"


def log(msg):
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.load(resp)


def push_kuma(status, msg):
    url = f"{KUMA_PUSH_URL}?status={status}&msg={urllib.parse.quote(msg)}"
    try:
        with urllib.request.urlopen(url, timeout=10):
            pass
    except Exception as e:
        log(f"WARNING: failed to push Kuma status ({status}): {e}")


def dir_size(path):
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def main():
    log("=== run start ===")

    queue = fetch_json(
        f"{SABNZBD_URL}/api?mode=queue&apikey={SABNZBD_API_KEY}&output=json"
    )
    history = fetch_json(
        f"{SABNZBD_URL}/api?mode=history&apikey={SABNZBD_API_KEY}"
        f"&output=json&limit=5000"
    )

    active_names = {
        s.get("filename", "") for s in queue["queue"].get("slots", [])
    }
    failed_names = {
        s.get("name", "")
        for s in history["history"].get("slots", [])
        if s.get("status") == "Failed"
    }

    if not os.path.isdir(INCOMPLETE_DIR):
        log(f"ERROR: {INCOMPLETE_DIR} does not exist, aborting")
        push_kuma("down", f"{INCOMPLETE_DIR} does not exist")
        sys.exit(1)

    deleted_count = 0
    deleted_bytes = 0

    for name in sorted(os.listdir(INCOMPLETE_DIR)):
        folder = os.path.join(INCOMPLETE_DIR, name)
        if not os.path.isdir(folder):
            continue
        if name in active_names:
            continue
        if name not in failed_names:
            continue

        size = dir_size(folder)
        try:
            shutil.rmtree(folder)
            deleted_count += 1
            deleted_bytes += size
            log(f"DELETED (confirmed Failed, not active): {name} ({size} bytes)")
        except OSError as e:
            log(f"SKIP (delete failed: {e}): {name}")

    log(
        f"=== run end: deleted {deleted_count} folders, "
        f"{deleted_bytes / 1e9:.2f} GB ==="
    )
    push_kuma("up", f"OK, deleted {deleted_count} folders")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        log(f"ERROR: unhandled exception: {e}")
        push_kuma("down", f"unhandled exception: {e}")
        sys.exit(1)
