#!/usr/bin/env python3
"""Convert SRT to ASS for 1080x1920 vertical (Xiaohongshu) output.
Keywords are highlighted with color override tags inside ASS.
"""
import re
from pathlib import Path

from subtitle_font import subtitle_font_name

ROOT = Path(__file__).parent
SRT = (ROOT / "assets" / "subtitle.srt").read_text()
ASS = ROOT / "assets" / "subtitle_vertical.ass"

# Keywords to highlight (substring match) → ASS color (BGR hex)
HIGHLIGHTS = {
    # AI 实战教程 EP1 关键词 — ASS BGR (&HAABBGGRR&)
    # 赤焰橙 FF5C00: &H00005CFF&   绿 27D796: &H0096D727&   蓝 2E5BFF: &H00FF5B2E&
    "拆解":            "&H00005CFF&",
    "爆款博主":        "&H00005CFF&",
    "方法论":          "&H00005CFF&",
    "合集":            "&H00005CFF&",
    "采集":            "&H00005CFF&",
    "往里拆":          "&H00005CFF&",
    "翻往期":          "&H00005CFF&",
    "看板":            "&H0096D727&",
    "关键词":          "&H0096D727&",
    "写作组件":        "&H0096D727&",
    "金句":            "&H0096D727&",
    "开源":            "&H00FF5B2E&",
    "冒烟":            "&H00FF5B2E&",
    "自检":            "&H00FF5B2E&",
    "规律":            "&H00005CFF&",
    "个例":            "&H0096D727&",
    "Claude":          "&H00FF5B2E&",
    "Codex":           "&H00FF5B2E&",
    "Agent":           "&H00FF5B2E&",
    "Markdown":        "&H00FF5B2E&",
    "Python":          "&H00FF5B2E&",
}

HEADER = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{subtitle_font_name()},64,&H00FFFFFF,&H000000FF,&H001A1A1A,&H00000000,1,0,0,0,100,100,0,0,1,6,2,2,80,80,180,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

def srt_t_to_ass(t):
    h, m, sms = t.split(":")
    s, ms = sms.split(",")
    cs = int(ms) // 10
    return f"{int(h)}:{int(m):02d}:{int(s):02d}.{cs:02d}"

# regex of all keywords (longest first to avoid partial overlap)
keys = sorted(HIGHLIGHTS.keys(), key=len, reverse=True)
pattern = re.compile("(" + "|".join(re.escape(k) for k in keys) + ")")

def colorize(text):
    out = []
    last = 0
    for m in pattern.finditer(text):
        out.append(text[last:m.start()])
        word = m.group(1)
        color = HIGHLIGHTS[word]
        out.append("{\\c" + color + "\\b1}" + word + "{\\c&H00FFFFFF&\\b1}")
        last = m.end()
    out.append(text[last:])
    return "".join(out)

events = []
for block in re.split(r"\n\s*\n", SRT.strip()):
    lines = [l for l in block.splitlines() if l.strip()]
    if len(lines) < 3: continue
    m = re.match(r"(\S+)\s*-->\s*(\S+)", lines[1])
    if not m: continue
    start = srt_t_to_ass(m.group(1))
    end = srt_t_to_ass(m.group(2))
    text = " ".join(lines[2:]).replace("{", "(").replace("}", ")")
    text = colorize(text)
    events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

ASS.write_text(HEADER + "\n".join(events) + "\n")
print(f"wrote {len(events)} vertical-style events to {ASS}")
