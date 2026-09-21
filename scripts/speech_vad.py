#!/usr/bin/env python3
"""Speech/no-speech detection with the Silero VAD ONNX model (CPU only, no GPU, no torch).

Runs under an isolated venv:  ~/venvs/vad/bin/python scripts/speech_vad.py FILE.wav   (needs onnxruntime + numpy)
Model file: ~/venvs/vad/silero_vad.onnx (from the `silero-vad` wheel, MIT licence).

speech_segments(wav, ...) -> [(start_s, end_s), ...] merged speech regions of a 16 kHz mono 16-bit WAV.
Used to (a) cut episodes into speech-only chunks so whisper never decodes across long stretches of silence/music
(where its timestamps drift) and (b) measure where subtitles really sit relative to speech.
"""
import os
import sys
import wave

import numpy as np

MODEL = os.environ.get("SILERO_VAD_MODEL", os.path.expanduser("~/venvs/vad/silero_vad.onnx"))
SR = 16000
WIN = 512          # samples per model step at 16 kHz (32 ms)
CTX = 64           # context samples the v5 model expects prepended to each window


def _session():
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.inter_op_num_threads = 1
    so.intra_op_num_threads = 1
    return ort.InferenceSession(MODEL, sess_options=so, providers=["CPUExecutionProvider"])


def read_wav(path):
    with wave.open(path) as w:
        if w.getframerate() != SR or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("expected 16 kHz mono 16-bit WAV")
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0


def speech_probs(x, sess=None):
    """Per-32 ms speech probability for the whole signal."""
    sess = sess or _session()
    n = len(x) // WIN
    state = np.zeros((2, 1, 128), np.float32)
    ctx = np.zeros((1, CTX), np.float32)
    sr = np.array(SR, np.int64)
    out = np.zeros(n, np.float32)
    for i in range(n):
        chunk = x[i * WIN:(i + 1) * WIN][None, :]
        inp = np.concatenate([ctx, chunk], axis=1)
        prob, state = sess.run(None, {"input": inp, "state": state, "sr": sr})
        ctx = inp[:, -CTX:]
        out[i] = prob[0, 0]
    return out


def segments_from_probs(p, threshold=0.5, min_speech_ms=250, min_silence_ms=500, pad_ms=120):
    """Hysteresis + merge: speech starts at p>=threshold, ends after >= min_silence_ms below threshold-0.15."""
    step = WIN / SR
    on = False
    segs = []
    start = 0
    silence = 0
    lo = max(threshold - 0.15, 0.05)
    for i, v in enumerate(p):
        t = i * step
        if not on and v >= threshold:
            on, start, silence = True, t, 0
        elif on:
            if v < lo:
                silence += step
                if silence * 1000 >= min_silence_ms:
                    segs.append((start, t - silence + step))
                    on = False
            else:
                silence = 0
    if on:
        segs.append((start, len(p) * step))
    segs = [(a, b) for a, b in segs if (b - a) * 1000 >= min_speech_ms]
    total = len(p) * step
    return [(max(0.0, a - pad_ms / 1000), min(total, b + pad_ms / 1000)) for a, b in segs]


def speech_segments(wav_path, **kw):
    x = read_wav(wav_path)
    return segments_from_probs(speech_probs(x), **kw)


if __name__ == "__main__":
    import json
    import time
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    t0 = time.time()
    segs = speech_segments(args[0])
    if "--json" in sys.argv:
        print(json.dumps([[round(a, 3), round(b, 3)] for a, b in segs]))
    else:
        dur = sum(b - a for a, b in segs)
        print(f"{len(segs)} speech segments, {dur:.0f}s of speech, computed in {time.time() - t0:.0f}s")
        for a, b in segs[:int(args[1]) if len(args) > 1 else 12]:
            print(f"  {a:8.2f} - {b:8.2f}  ({b - a:.1f}s)")
