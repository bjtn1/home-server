#!/usr/bin/env python3
"""
Keeps the Immich "OBS Recordings" album in sync with the "OBS Recordings"
External Library (source: /mnt/vault/obs). Immich albums have no
rule-based/dynamic membership -- confirmed 2026-09-02 by reading the
actual server source (dtos/album.dto.d.ts): AlbumsAddAssetsDto is just a
flat list of asset IDs, nothing query-based. So a new file dropped into
/mnt/vault/obs gets picked up by the External Library's own scan
automatically, but never lands in the album on its own -- this script is
the missing piece: find every asset already in the library that isn't yet
in the album, and add it.

Does NOT trigger a library scan itself -- Immich's own library-refresh
schedule (or a manual scan) is what discovers new files on disk in the
first place; this only reconciles album membership against whatever the
library already knows about.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

IMMICH_URL = "https://immich.bjtn.xyz"
IMMICH_API_KEY = os.environ["IMMICH_API_KEY"]
LIBRARY_ID = "1655f2bc-93cc-418b-b22b-d98eaeded5c4"
ALBUM_ID = "65699072-c99c-4251-8b6a-8c9f927f2eb4"
LOG_FILE = "/home/bjtn/logs/obs-album-sync.log"


def log(msg):
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}"
    print(line)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def api(method, path, body=None):
    url = f"{IMMICH_URL}/api{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("x-api-key", IMMICH_API_KEY)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def get_library_asset_ids():
    ids = set()
    page = 1
    while True:
        resp = api("POST", "/search/metadata", {
            "libraryId": LIBRARY_ID,
            "size": 1000,
            "page": page,
        })
        items = resp.get("assets", {}).get("items", [])
        if not items:
            break
        ids.update(i["id"] for i in items)
        if not resp.get("assets", {}).get("nextPage"):
            break
        page += 1
    return ids


def get_album_asset_ids():
    ids = set()
    page = 1
    while True:
        resp = api("POST", "/search/metadata", {
            "albumIds": [ALBUM_ID],
            "size": 1000,
            "page": page,
        })
        items = resp.get("assets", {}).get("items", [])
        if not items:
            break
        ids.update(i["id"] for i in items)
        if not resp.get("assets", {}).get("nextPage"):
            break
        page += 1
    return ids


def main():
    log("=== run start ===")
    library_ids = get_library_asset_ids()
    album_ids = get_album_asset_ids()
    missing = list(library_ids - album_ids)
    log(f"library has {len(library_ids)} asset(s), album has {len(album_ids)}, {len(missing)} to add")

    if not missing:
        log("=== run end: nothing to add ===")
        return

    added = 0
    failed = 0
    # batch in chunks of 500 to keep requests reasonable
    for i in range(0, len(missing), 500):
        batch = missing[i:i + 500]
        results = api("PUT", f"/albums/{ALBUM_ID}/assets", {"ids": batch})
        for r in results:
            if r.get("success"):
                added += 1
            else:
                failed += 1

    log(f"=== run end: added {added}, failed {failed} ===")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        log(f"ERROR: unhandled exception: {e}")
        sys.exit(1)
