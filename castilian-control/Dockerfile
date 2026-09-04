FROM python:3.13-slim
# Same tools installed on the host for this pipeline (megatools, mkvtoolnix,
# ffmpeg for ffprobe, jq, unrar) -- this container runs castilian-queue.sh /
# mux-castilian-audio.sh itself rather than shelling out to the host, so it
# needs its own copies. megatools/unrar live in Debian's non-free
# component, matching what the host's /etc/apt/sources.list already has.
# curl added 2026-09-04 for castilian-drop-scan.sh (Sonarr/Radarr lookups +
# ntfy) -- caught by an actual button click against the deployed container
# failing with "missing required command: curl", not by inspection.
RUN echo "deb http://deb.debian.org/debian trixie main non-free non-free-firmware" > /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends megatools mkvtoolnix ffmpeg jq unrar curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY app.py .
EXPOSE 8768
CMD ["python", "app.py"]
