#!/bin/bash
# Rescans Nextcloud's file-cache index against the real filesystem for the
# bjtn user (`occ files:scan`). Needed whenever files under
# /mnt/vault/nextcloud/bjtn/files/... get created/renamed/deleted from
# outside Nextcloud's own API -- e.g. Syncthing, or any direct filesystem
# edit (this is exactly what went stale after the 2026-09-10 books-library
# renaming run -- Nextcloud's web UI kept showing old filenames next to
# new ones, looking like duplicates, until this scan ran).
set -euo pipefail

docker exec -u www-data nextcloud php occ files:scan bjtn
