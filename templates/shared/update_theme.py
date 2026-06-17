#!/usr/bin/env python3
"""Recompute theme.ts SECTIONS (start/duration) + TOTAL_SEC from audio/*.json.
Run AFTER build_audio_mm.py so on-screen scene timing matches the new narration.
"""
import json, re
from pathlib import Path

ROOT = Path(__file__).parent
SCRIPT = json.loads((ROOT / "scripts" / "bilibili.json").read_text())
AUDIO = ROOT / "audio"

rows, acc = [], 0.0
for s in SCRIPT["sections"]:
    sid = s["id"]
    short = sid[3:]  # "01_hook" -> "hook"
    dur = json.loads((AUDIO / f"{sid}.json").read_text())["duration"]
    rows.append((short, round(acc, 2), round(dur, 2)))
    acc += dur
total = round(acc, 2)

body = "\n".join(f"  {{ id: '{sh}', start: {st}, duration: {du} }}," for sh, st, du in rows)
block = ("export const SECTIONS: { id: string; start: number; duration: number }[] = [\n"
         + body + "\n];")

theme = (ROOT / "remotion" / "src" / "theme.ts").read_text()
theme = re.sub(r"export const SECTIONS[^\]]*\];", block, theme, flags=re.S)
theme = re.sub(r"export const NARRATION_SEC = [\d.]+;", f"export const NARRATION_SEC = {total};", theme)
theme = re.sub(r"export const TOTAL_SEC = [\d.]+;", f"export const TOTAL_SEC = {total};", theme)
(ROOT / "remotion" / "src" / "theme.ts").write_text(theme)

print(f"✓ theme.ts updated · TOTAL_SEC = {total}s ({total/60:.2f} min)")
for sh, st, du in rows:
    print(f"  {sh:9s} start={st:7.2f} dur={du:6.2f}")
