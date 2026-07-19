#!/usr/bin/env python3
"""Convert SRT to ASS with Bilibili-friendly Chinese styling embedded."""
import re
from pathlib import Path

from subtitle_font import subtitle_font_name

ROOT = Path(__file__).parent
SRT = (ROOT / "assets" / "subtitle.srt").read_text()
ASS = ROOT / "assets" / "subtitle.ass"

HEADER = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{subtitle_font_name()},52,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,4,0,2,120,120,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

def srt_t_to_ass(t):
    # 00:00:00,123 -> 0:00:00.12
    h, m, sms = t.split(":")
    s, ms = sms.split(",")
    cs = int(ms) // 10
    return f"{int(h)}:{int(m):02d}:{int(s):02d}.{cs:02d}"

events = []
for block in re.split(r"\n\s*\n", SRT.strip()):
    lines = [l for l in block.splitlines() if l.strip()]
    if len(lines) < 3:
        continue
    times = lines[1]
    text = " ".join(lines[2:]).replace("{", "(").replace("}", ")")
    m = re.match(r"(\S+)\s*-->\s*(\S+)", times)
    if not m: continue
    start = srt_t_to_ass(m.group(1))
    end = srt_t_to_ass(m.group(2))
    events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

ASS.write_text(HEADER + "\n".join(events) + "\n")
print(f"wrote {len(events)} events to {ASS}")
