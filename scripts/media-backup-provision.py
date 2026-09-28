#!/usr/bin/env python3
"""Ensures every show/movie folder under /mnt/vault/tv and /mnt/vault/movies
has its own dedicated restic repo under B0_ROOT, replacing the old ad-hoc
"a few shows happen to have a backup repo" model. Never touches tv-english/
movies-english -- that content is replaceable, no dedicated backup needed.

Per folder:
  - Slug it (see slugify() -- two distinct steps, not one combined step:
    NFKD-decompose+strip-combining-marks for diacritics, THEN lowercase +
    non-alphanumeric-to-hyphen for everything else, including characters
    like the leading "!" in some titles that aren't diacritics at all).
  - A repo counts as provisioned only if <slug>-backup/config exists AND its
    latest snapshot tracks exactly this folder -- then skip (ssd-backup.sh
    handles the nightly backup/check/replicate cycle for every repo it
    finds generically). A repo with no snapshots, or whose latest snapshot
    tracks stale paths (renamed folder, old per-file paths), is reseeded.
  - If missing (or being reseeded): restic init if needed, then IMMEDIATELY
    run the first `restic backup <folder>` call itself. ssd-backup.sh's nightly loop doesn't take a
    fresh path each run -- it reads `paths` off the *latest existing
    snapshot* and reuses that. A repo with zero snapshots hits its
    explicit "no prior snapshot yet" branch and is silently skipped
    forever. Seeding the first snapshot here is what makes a newly
    provisioned repo actually get picked up starting the very next
    ssd-backup.sh run, not skipped indefinitely.

Defensive: if two different folder names produce the same slug, abort
loudly rather than silently overwriting one's repo with the other's
backups.

Reverse case: also scans existing *-backup repos under B0_ROOT whose slug
doesn't map back to any current /tv or /movies folder (the folder was
deleted after the repo was created) and moves them under a `retired/`
subdirectory rather than leaving them to fail silently in ssd-backup.sh's
output every night, or deleting them outright and losing backup history.

Usage:
    media-backup-provision.py [--dry-run]

Env:
    TV_ROOT               default /mnt/vault/tv
    MOVIES_ROOT           default /mnt/vault/movies
    B0_ROOT               default /mnt/b2_4tb
    RESTIC_PASSWORD_FILE  default /home/bjtn/.restic-password
"""
import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata

TV_ROOT = os.environ.get("TV_ROOT", "/mnt/vault/tv")
MOVIES_ROOT = os.environ.get("MOVIES_ROOT", "/mnt/vault/movies")
B0_ROOT = os.environ.get("B0_ROOT", "/mnt/b2_4tb")
PW_FILE_PATH = os.environ.get("RESTIC_PASSWORD_FILE", "/home/bjtn/.restic-password")


def log(msg):
    print(msg, flush=True)


def slugify(name):
    # Step 1: NFKD-decompose and strip combining marks -- drops diacritics
    # (e.g. "Código" -> "codigo"). Does NOT touch non-diacritic characters
    # like "¡", since it isn't a combining mark, it's its own character.
    decomposed = unicodedata.normalize("NFKD", name)
    no_diacritics = "".join(c for c in decomposed if not unicodedata.combining(c))
    # Step 2: lowercase, replace every remaining non-alphanumeric run with
    # a single hyphen. This is what actually handles "¡", "!", commas,
    # periods, etc.
    lowered = no_diacritics.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug


def restic(args, env, show=False):
    """Run restic; with show=True print its full output (indented) and how long it took."""
    import time
    t0 = time.time()
    r = subprocess.run(["restic"] + args, env=env, capture_output=True, text=True)
    if show:
        log(f"  $ restic {' '.join(args)}   (exit {r.returncode}, {time.time() - t0:.1f}s)")
        for line in (r.stdout + r.stderr).splitlines():
            if line.strip():
                log(f"      {line}")
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-retire", action="store_true",
                     help="skip the reverse-case pass entirely (e.g. while a library rebuild/transfer is still in flight)")
    args = ap.parse_args()

    env = os.environ.copy()
    env["RESTIC_PASSWORD_FILE"] = PW_FILE_PATH

    log(f"mode: {'DRY RUN' if args.dry_run else 'APPLY'} | tv: {TV_ROOT} | movies: {MOVIES_ROOT} | repos: {B0_ROOT}"
        f"{' | retire pass skipped' if args.skip_retire else ''}")
    folders = []
    for root in (TV_ROOT, MOVIES_ROOT):
        if not os.path.isdir(root):
            log(f"WARN: root {root} does not exist, skipping")
            continue
        for entry in sorted(os.listdir(root)):
            full = os.path.join(root, entry)
            if os.path.isdir(full):
                folders.append((entry, full))

    log(f"{len(folders)} show/movie folders found")
    slug_map = {}
    for name, full in folders:
        slug = slugify(name)
        if slug in slug_map and slug_map[slug] != full:
            log(f"ABORT: slug collision -- '{name}' and folder at {slug_map[slug]} "
                f"both produce slug '{slug}'. Refusing to continue.")
            sys.exit(1)
        slug_map[slug] = full

    created = 0
    skipped = 0
    for slug, full in sorted(slug_map.items()):
        repo_dir = os.path.join(B0_ROOT, f"{slug}-backup")
        config_path = os.path.join(repo_dir, "config")
        needs_init = not os.path.isfile(config_path)
        env["RESTIC_REPOSITORY"] = repo_dir

        if not needs_init:
            # An initialized repo with zero snapshots (e.g. a prior run killed
            # mid-seed) is NOT provisioned: ssd-backup.sh would skip it forever.
            r = restic(["snapshots", "--latest", "1", "--json"], env)
            try:
                snaps = json.loads(r.stdout) if r.returncode == 0 else []
            except ValueError:
                snaps = []
            # --latest 1 returns one snapshot per (host, paths) group, oldest group first
            latest_paths = max(snaps, key=lambda x: x["time"]).get("paths", []) if snaps else []
            if latest_paths == [full]:
                newest = max(snaps, key=lambda x: x["time"])
                log(f"SKIP {slug}-backup: already provisioned ({full}); latest snapshot {newest.get('short_id', '?')} "
                    f"from {newest.get('time', '?')[:19]}")
                skipped += 1
                continue
            # ssd-backup.sh reuses the latest snapshot's paths verbatim, so a
            # snapshot tracking anything other than exactly this folder (none
            # at all, or stale per-file/renamed paths) breaks every nightly run.
            why = "no snapshots" if not latest_paths else f"latest snapshot tracks stale path(s), e.g. {latest_paths[0]}"
            log(f"RESEED {slug}-backup <- {full} ({why})")
        else:
            log(f"CREATE {slug}-backup <- {full}")
        created += 1
        if args.dry_run:
            continue

        if needs_init:
            os.makedirs(repo_dir, exist_ok=True)
            restic(["unlock"], env)
            r = restic(["init"], env, show=True)
            if r.returncode != 0:
                log(f"FAILED init {slug}-backup: {r.stderr.strip()}")
                continue
        else:
            restic(["unlock"], env)

        r = restic(["backup", "--verbose", full], env, show=True)
        if r.returncode != 0:
            log(f"FAILED seed backup {slug}-backup: {r.stderr.strip()}")
            continue
        log(f"OK: seeded first snapshot for {slug}-backup")

    # Reverse case: repos that track a /tv or /movies folder that no longer
    # exists. Scope check is by the repo's OWN latest-snapshot path, not by
    # name pattern -- this repo dir holds backups for plenty of unrelated
    # things (books, immich, nextcloud, a whole-vault repo) that also
    # happen to end in "-backup" and must never be touched here.
    if args.skip_retire:
        log("\n(retire/reverse-case pass skipped: --skip-retire)")
    else:
        retired_dir = os.path.join(B0_ROOT, "retired")
        tv_prefix = TV_ROOT.rstrip("/") + "/"
        movies_prefix = MOVIES_ROOT.rstrip("/") + "/"
        log("\nretire check: repos whose /tv or /movies folder no longer exists")
        if os.path.isdir(B0_ROOT):
            for entry in sorted(os.listdir(B0_ROOT)):
                if entry == "retired" or not entry.endswith("-backup"):
                    continue
                repo_dir = os.path.join(B0_ROOT, entry)
                if not os.path.isdir(repo_dir) or not os.path.isfile(os.path.join(repo_dir, "config")):
                    continue

                env["RESTIC_REPOSITORY"] = repo_dir
                r = restic(["snapshots", "--latest", "1", "--json"], env)
                if r.returncode != 0:
                    log(f"WARN: could not read snapshots for {entry}, leaving alone: {r.stderr.strip()}")
                    continue
                try:
                    snaps = json.loads(r.stdout)
                except ValueError:
                    log(f"WARN: could not parse snapshot JSON for {entry}, leaving alone")
                    continue
                if not snaps:
                    log(f"  retire check {entry}: no snapshots, leaving alone")
                    continue
                newest = max(snaps, key=lambda x: x["time"])
                if not newest.get("paths"):
                    log(f"  retire check {entry}: latest snapshot has no paths, leaving alone")
                    continue
                path = newest["paths"][0]
                if not (path.startswith(tv_prefix) or path.startswith(movies_prefix)):
                    log(f"  retire check {entry}: backs up {path} (not /tv or /movies) -- not this script's repo")
                    continue  # out of scope for this script entirely -- a different backup system's repo

                slug = entry[: -len("-backup")]
                if slug in slug_map:
                    log(f"  retire check {entry}: folder still exists, keep")
                    continue
                log(f"RETIRE {entry}: tracked path {path} no longer exists under /tv or /movies")
                if not args.dry_run:
                    os.makedirs(retired_dir, exist_ok=True)
                    dest = os.path.join(retired_dir, entry)
                    if os.path.exists(dest):
                        log(f"  SKIP move: {dest} already exists, leaving {entry} in place")
                    else:
                        os.rename(repo_dir, dest)
                        log(f"  moved to {dest}")

    log(f"\ndone: {created} to create, {skipped} already provisioned")


if __name__ == "__main__":
    main()
