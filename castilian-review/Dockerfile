FROM python:3.13-slim
# Same tools installed on the host for this pipeline (mkvtoolnix, ffmpeg
# for ffprobe, jq, curl) -- this container runs castilian-queue.sh /
# mux-castilian-audio.sh / castilian-drop-scan.sh itself rather than
# shelling out to the host, so it needs its own copies. curl added
# 2026-09-04 for castilian-drop-scan.sh (Sonarr/Radarr lookups + ntfy) --
# caught by an actual button click against the deployed container failing
# with "missing required command: curl", not by inspection.
#
# megatools/unrar (needed MEGA link downloads + "Importante leer" archive
# indirection) dropped 2026-09-04 along with castilian-queue.sh's MEGA
# support -- with them gone, everything needed lives in Debian's main
# component, so the non-free sources.list override they required is gone
# too; the base image's default sources are enough now.
RUN apt-get update \
    && apt-get install -y --no-install-recommends mkvtoolnix ffmpeg jq curl \
    && rm -rf /var/lib/apt/lists/*
# 2026-09-06: this image never set a locale, so it defaulted to
# POSIX/C -- glibc's C locale treats multi-byte UTF-8 as a stream of
# unrelated single bytes, and mkvmerge truncated any filename with an
# accented character (á/é/í/ó/ú/ñ, all over real Spanish-dub releases)
# mid-string, then failed to open the truncated path and reported it as
# "unreadable/corrupt" -- a real file misdiagnosed as damaged. Found live:
# "4x07 La máscara.mkv" truncated to "4x07 La m" inside this container
# (mkvmerge -J on the host, no locale issue there, read the exact same
# file fine). glibc's C.UTF-8 locale is always available with no extra
# package (unlike en_US.UTF-8, which needs locales+locale-gen) -- good
# enough here since nothing in this pipeline needs locale-specific
# collation/formatting, just correct byte handling for non-ASCII paths.
ENV LANG=C.UTF-8
WORKDIR /app
COPY app.py .
EXPOSE 8768
CMD ["python", "app.py"]
