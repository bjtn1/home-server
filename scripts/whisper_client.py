#!/usr/bin/env python3
"""Minimal stdlib client for the whisper.cpp server's POST /inference (no
`requests` dependency, matching the rest of this repo's scripts).

whisper.cpp's bundled server has NO health endpoint (only /inference and
/load), so server_up() is a raw TCP connect check.

CLI (for manual tests):
    whisper_client.py <audio.wav> [--url URL] [--timeout SEC] [--out FILE]
"""
import argparse
import os
import socket
import sys
import time
import urllib.request
import uuid
from urllib.parse import urlparse

DEFAULT_URL = os.environ.get("WHISPER_SERVER_URL", "https://whisper.bjtn.xyz")


def server_up(url=DEFAULT_URL, timeout=5):
    u = urlparse(url)
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        with socket.create_connection((u.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def _multipart(fields, file_field, file_path):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        )
    with open(file_path, "rb") as f:
        data = f.read()
    parts.append(
        (f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
         f'filename="{os.path.basename(file_path)}"\r\nContent-Type: audio/wav\r\n\r\n').encode()
        + data + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def transcribe(wav_path, url=DEFAULT_URL, timeout=600, language="es",
               response_format="srt", temperature="0", prompt=None, extra=None):
    """POST the wav to /inference, return (text, elapsed_seconds).
    Raises on HTTP error / timeout. `prompt` is whisper's initial_prompt
    (224-token budget, vocabulary mimicry -- see whisper-subtitle-plan.md)."""
    fields = {"response_format": response_format, "language": language,
              "temperature": temperature}
    if prompt:
        fields["prompt"] = prompt
    if extra:
        fields.update(extra)
    body, ctype = _multipart(fields, "file", wav_path)
    req = urllib.request.Request(url.rstrip("/") + "/inference", data=body,
                                 method="POST", headers={"Content-Type": ctype})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = r.read().decode("utf-8", "replace")
    return text, time.perf_counter() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--out")
    a = ap.parse_args()
    if not server_up(a.url):
        sys.exit(f"whisper server not reachable at {a.url}")
    text, secs = transcribe(a.wav, a.url, a.timeout)
    print(f"took {secs:.1f}s, {len(text)} chars", file=sys.stderr)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)


if __name__ == "__main__":
    main()
