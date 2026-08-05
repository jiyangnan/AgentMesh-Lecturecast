#!/usr/bin/env python3
"""gen_bgm.py — creatorcut_local_synth: 本地程序化合成 BGM（无下载/无 AI/无 API）。

参考 CreatorCut 配乐菜单（light_tech / bright_launch），用纯 stdlib 现配乐：
  light_tech    BPM 122  明亮大调 F-G-Am-C  marimba 旋律 + 亮 arp + 轻正弦 bass + 轻鼓
  bright_launch BPM 128  大调 C-G-Am-F      明亮 pad + 分解和弦 + 能量 bass + 强鼓

混音参数照抄 lecturecast-server/docs/manifest-schema-v1.1.md L83-92：
  fade up 0.35s / down 0.8s；峰值归一化到 -10 dBFS 留 headroom。
默认 noduck（本机验证听感）；duck 走执行引擎 sidechaincompress。

零依赖：wave + struct + math + random。确定性（fixed seed 42）。

Usage:
  python gen_bgm.py --genre light_tech --dur 215.5 --out bgm_light_tech.wav
"""
from __future__ import annotations

import argparse
import math
import random
import struct
import wave

SR = 44100

NOTE_FREQ = {
    "C2": 65.41, "C3": 130.81, "C4": 261.63, "C5": 523.25,
    "D3": 146.83, "D4": 293.66, "E3": 164.81, "E4": 329.63,
    "F3": 174.61, "F4": 349.23, "G2": 98.0, "G3": 196.0, "G4": 392.0,
    "A2": 110.0, "A3": 220.0, "A4": 440.0, "B3": 246.94, "B4": 493.88,
}

GENRES = {
    "light_tech": {
        "bpm": 122,
        "prog": [("F3", "C4"), ("G3", "D4"), ("A3", "E4"), ("C4", "G4")],
        "style": "light",
    },
    "bright_launch": {
        "bpm": 128,
        "prog": [("C4", "G4"), ("G3", "D4"), ("A3", "E4"), ("F3", "C4")],
        "style": "bright",
    },
}

_DECAY_CACHE: dict[tuple, list[float]] = {}


def _exp(t: list[float], k: float) -> list[float]:
    """Element-wise exp(-k*t) without numpy."""
    return [math.exp(-k * x) for x in t]


def _pluck(freq: float, dur: float, decay: float, bright: bool = False) -> list[float]:
    n = int(dur * SR)
    key = (round(freq, 3), n, round(decay, 3), bright)
    cached = _DECAY_CACHE.get(key)
    if cached is not None:
        return list(cached)
    omega = 2.0 * math.pi * freq / SR
    env = _exp([i / SR for i in range(n)], decay)
    out = []
    for i in range(n):
        w = math.sin(omega * i)
        if bright:
            w += 0.35 * math.sin(2.0 * omega * i)  # +octave for sparkle
        out.append(w * env[i])
    if len(_DECAY_CACHE) > 4000:
        _DECAY_CACHE.clear()
    _DECAY_CACHE[key] = out
    return out


def _sine_pluck(freq: float, dur: float, decay: float) -> list[float]:
    return _pluck(freq, dur, decay, bright=False)


def _pad(freq: float, dur: float, detune: float = 0.6) -> list[float]:
    n = int(dur * SR)
    omega = 2.0 * math.pi * freq / SR
    omega2 = 2.0 * math.pi * freq * (1.0 + detune / 100.0) / SR
    out = []
    f = int(0.06 * SR)
    for i in range(n):
        w = (math.sin(omega * i) + math.sin(omega2 * i)) / 2.0
        w = math.tanh(1.5 * w)
        a = 1.0
        if i < f:
            a = i / f
        elif i > n - f - 1:
            a = max(0.0, (n - 1 - i) / f)
        out.append(w * a)
    return out


def _kick(dur: float = 0.3) -> list[float]:
    n = int(dur * SR)
    out = []
    phase = 0.0
    for i in range(n):
        t = i / SR
        f = 120.0 * math.exp(-t * 18.0) + 50.0
        phase += 2.0 * math.pi * f / SR
        out.append(math.sin(phase) * math.exp(-t * 14.0))
    return out


def _snare(dur: float = 0.2) -> list[float]:
    n = int(dur * SR)
    rng = random.Random(7)
    out = []
    for i in range(n):
        t = i / SR
        noise = rng.uniform(-1, 1)
        tone = math.sin(2.0 * math.pi * 190.0 * t)
        out.append(0.6 * noise * math.exp(-t * 40.0) + 0.4 * tone * math.exp(-t * 28.0))
    return out


def _hat(dur: float = 0.07) -> list[float]:
    n = int(dur * SR)
    rng = random.Random(9)
    prev = 0.0
    out = []
    for i in range(n):
        t = i / SR
        noise = rng.uniform(-1, 1)
        hp = noise - prev  # crude high-pass
        prev = noise
        out.append(hp * math.exp(-t * 70.0))
    return out


def _arp_note(freq: float, dur: float, decay: float) -> list[float]:
    return _pluck(freq, dur, decay, bright=True)


def render(genre: str, dur: float, seed: int = 42) -> list[list[float]]:
    cfg = GENRES[genre]
    bpm = cfg["bpm"]
    beat = 60.0 / bpm
    rng = random.Random(seed)
    n_total = int(dur * SR)
    pad_buf = [0.0] * n_total
    arp_buf = [0.0] * n_total
    bass_buf = [0.0] * n_total
    drum_buf = [0.0] * n_total

    bar = 4 * beat
    n_bars = int(math.ceil(dur / bar))

    for bi in range(n_bars):
        root, fifth = cfg["prog"][bi % len(cfg["prog"])]
        t0 = bi * bar
        if t0 > dur:
            break
        span = min(bar, dur - t0)
        t0_s = int(t0 * SR)

        # pad: root+fifth, sustained across the bar
        for nn in (root, fifth):
            _place(pad_buf, _scale(_pad(NOTE_FREQ[nn], span), 0.09), t0_s)
        if cfg["style"] == "bright":
            _place(pad_buf, _scale(_pad(NOTE_FREQ[fifth] * 2.0, span), 0.03), t0_s)

        # bass: sine root, light 8th notes (not heavy saw)
        for k in range(8):
            tt = t0 + k * beat / 2.0
            if tt >= dur:
                break
            _place(bass_buf, _scale(_sine_pluck(NOTE_FREQ[root] / 2.0, beat * 0.8, 7.0), 0.14), int(tt * SR))

        # arp: 16th-note sparkly arpeggio, root→fifth→octave
        base = NOTE_FREQ[root]
        oct5 = NOTE_FREQ[fifth]
        oct_root = base * 2.0
        steps = [base, oct5, oct_root, oct5, base]
        for k in range(16):
            tt = t0 + k * beat / 4.0
            if tt >= dur:
                break
            nn = steps[k % len(steps)]
            v = 0.05 if cfg["style"] == "light" else 0.07
            _place(arp_buf, _scale(_arp_note(nn, beat * 0.42, 7.0), v), int(tt * SR))

        # melody: marimba-style pluck on bar downbeat (root + fifth → next chord)
        if cfg["style"] == "light":
            nxt = cfg["prog"][(bi + 1) % len(cfg["prog"])][0]
            melody_notes = [NOTE_FREQ[fifth], NOTE_FREQ[root], NOTE_FREQ[nxt]]
            for k, nn in enumerate(melody_notes):
                tt = t0 + k * beat
                if tt < dur:
                    _place(arp_buf, _scale(_arp_note(nn, beat * 0.9, 5.5), 0.09), int(tt * SR))

        # sparkle: random high notes (light style only, sparse)
        if cfg["style"] == "light":
            for _ in range(3):
                tt = t0 + rng.uniform(0, span)
                if tt >= dur:
                    break
                high = rng.choice([NOTE_FREQ["A4"], NOTE_FREQ["C5"], NOTE_FREQ["E4"]])
                _place(pad_buf, _scale(_arp_note(high * 2.0, 0.5, 9.0), 0.03), int(tt * SR))

        # drums
        kick_steps = [0, 2.5] if cfg["style"] == "light" else [0, 2, 3.5]
        for k in kick_steps:
            tt = t0 + k * beat
            if tt < dur:
                _place(drum_buf, _scale(_kick(), 0.5), int(tt * SR))
        for k in (1, 3):
            tt = t0 + k * beat
            if tt < dur:
                _place(drum_buf, _scale(_snare(), 0.16), int(tt * SR))
        hat_every = 0.25 if cfg["style"] == "bright" else 0.5
        for k in _frange(0, 4, hat_every):
            tt = t0 + k * beat
            if tt < dur:
                _place(drum_buf, _scale(_hat(), 0.09), int(tt * SR))

    mix = [pad_buf[i] + arp_buf[i] + bass_buf[i] + drum_buf[i] for i in range(n_total)]
    if cfg["style"] == "bright":
        mix = [x * 0.95 for x in mix]

    # fades (manifest: up 0.35s / down 0.8s)
    n_up, n_down = int(0.35 * SR), int(0.8 * SR)
    if n_total > n_up + n_down:
        for i in range(n_up):
            mix[i] *= i / n_up
        for i in range(n_down):
            mix[n_total - n_down + i] *= (n_down - i) / n_down

    # peak normalize to -10 dBFS (headroom for duck)
    peak = max(abs(x) for x in mix) + 1e-9
    target = 10 ** (-10 / 20)
    mix = [x * target / peak for x in mix]

    # stereo widen: slight delay on right channel
    d = int(12.0 / 1000 * SR)
    left = mix
    right = [0.0] * d + mix[: n_total - d]
    return [left, right]


def _scale(xs: list[float], k: float) -> list[float]:
    return [x * k for x in xs]


def _place(buf: list[float], x: list[float], t_s: int) -> None:
    n = len(buf)
    e = t_s + len(x)
    if e > n:
        x = x[: n - t_s]
    for i, v in enumerate(x):
        buf[t_s + i] += v


def _frange(start: float, stop: float, step: float) -> list[float]:
    out = []
    v = start
    while v < stop - 1e-9:
        out.append(v)
        v += step
    return out


def _write_wav(stereo: list[list[float]], out: str, sr: int) -> None:
    n = len(stereo[0])
    with wave.open(out, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = bytearray()
        for i in range(n):
            l = int(max(-32767, min(32767, stereo[0][i] * 32767)))
            r = int(max(-32767, min(32767, stereo[1][i] * 32767)))
            frames += struct.pack("<hh", l, r)
        w.writeframes(bytes(frames))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--genre", choices=sorted(GENRES), default="light_tech")
    ap.add_argument("--dur", type=float, required=True, help="final video duration (seconds)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--sr", type=int, default=SR)
    args = ap.parse_args()
    stereo = render(args.genre, args.dur, seed=42)
    _write_wav(stereo, args.out, int(args.sr))
    print(f"✓ {args.genre} BGM → {args.out}  ({args.dur:.1f}s, {args.sr}Hz stereo, peak -10dBFS)")


if __name__ == "__main__":
    main()
