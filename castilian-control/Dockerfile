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
WORKDIR /app
COPY app.py .
EXPOSE 8768
CMD ["python", "app.py"]
