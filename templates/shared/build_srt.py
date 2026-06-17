#!/usr/bin/env python3
"""Concatenate per-section TTS cues into one SRT, offset by section start time.
Long sentence cues are split at internal punctuation so on-screen lines stay readable.
"""
import json, re
from pathlib import Path
from datetime import timedelta

ROOT = Path(__file__).parent
SCRIPT = json.loads((ROOT / "scripts" / "bilibili.json").read_text())
AUDIO_DIR = ROOT / "audio"

# section start times (cumulative)
starts = []
acc = 0.0
for s in SCRIPT["sections"]:
    starts.append(acc)
    d = json.loads((AUDIO_DIR / f"{s['id']}.json").read_text())
    acc += d["duration"]

# split a cue into shorter visible lines based on punctuation
SPLITTERS = re.compile(r'([，。！？、；])')
MAX_LEN = 22  # chars per subtitle line

def split_cue(text, start, end):
    """Split a long sentence cue into shorter aligned sub-cues."""
    parts = SPLITTERS.split(text)
    # rejoin punctuation with previous segment
    chunks = []
    cur = ''
    for p in parts:
        if not p: continue
        if SPLITTERS.match(p):
            cur += p
            chunks.append(cur)
            cur = ''
        else:
            cur += p
    if cur:
        chunks.append(cur)
    # merge tiny chunks until <= MAX_LEN
    merged = []
    buf = ''
    for c in chunks:
        if len(buf) + len(c) <= MAX_LEN:
            buf += c
        else:
            if buf: merged.append(buf)
            buf = c
    if buf: merged.append(buf)
    if not merged:
        return [(text, start, end)]
    # apportion time by char length
    total = sum(len(m) for m in merged)
    out = []
    t = start
    for m in merged:
        dt = (end - start) * len(m) / total
        out.append((m.strip(), t, t + dt))
        t += dt
    return out

def fmt(sec):
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

lines = []
idx = 1
for sec, base in zip(SCRIPT["sections"], starts):
    d = json.loads((AUDIO_DIR / f"{sec['id']}.json").read_text())
    for cue in d["cues"]:
        text = cue["text"].strip()
        start = base + cue["start"]
        end = base + cue["end"]
        for line, ls, le in split_cue(text, start, end):
            if not line: continue
            lines.append(f"{idx}\n{fmt(ls)} --> {fmt(le)}\n{line}\n")
            idx += 1

out = ROOT / "assets" / "subtitle.srt"
out.write_text("\n".join(lines))
print(f"wrote {idx-1} subtitle lines to {out}")
print(f"total duration covered: {fmt(starts[-1] + d['duration'])}")
