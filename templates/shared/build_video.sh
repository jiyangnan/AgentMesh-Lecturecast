#!/bin/bash
# Lecturecast — render BOTH platforms + burn subtitles + covers, one command.
# Pipeline (pure Remotion + local ffmpeg, no Playwright/Docker):
#   merge narration → rewrite theme timing → SRT+ASS → render V/H → burn subs → covers
#
# Prereqs (do these first):
#   1) python3 build_audio_mm.py        # audio/<id>.mp3 + .json  (MiniMax→Edge fallback)
#   2) cd remotion && npm install        # deps (NOT bun — see SKILL.md)
#   3) write scenes/<Id>.tsx + scenesH/<Id>H.tsx for every section
#
# Usage:  ./build_video.sh [slug]      slug defaults to project dir name
set -eu
cd "$(dirname "$0")"; ROOT="$PWD"
SLUG="${1:-$(basename "$ROOT")}"

echo "[1/6] 合并 narration.mp3"
ls audio/*.mp3 | sort | sed "s|^|file '$ROOT/|; s|$|'|" > audio/_concat.txt
ffmpeg -y -f concat -safe 0 -i audio/_concat.txt -c copy remotion/public/narration.mp3 2>&1 | tail -1
cp remotion/public/narration.mp3 assets/narration.mp3 2>/dev/null || true

echo "[2/6] 回写场景时长 → theme.ts"
python3 update_theme.py

echo "[3/6] 生成字幕 SRT + 双版 ASS"
python3 build_srt.py
python3 srt_to_ass_vertical.py   # 竖版小红书（关键词高亮，编辑其 HIGHLIGHTS dict）
python3 srt_to_ass.py            # 横版 B站

echo "[4/6] 渲染双平台（纯 Remotion）"
( cd remotion && npx remotion render VideoVertical  out/video.mp4 )
( cd remotion && npx remotion render VideoLandscape out/videoH.mp4 )

echo "[5/6] 烧字幕（本地 ffmpeg + libass）"
mkdir -p output
ffmpeg -y -i remotion/out/video.mp4  -vf "ass=assets/subtitle_vertical.ass" \
  -c:v libx264 -preset medium -crf 19 -pix_fmt yuv420p -c:a copy "output/${SLUG}-xiaohongshu.mp4" 2>&1 | tail -1
ffmpeg -y -i remotion/out/videoH.mp4 -vf "ass=assets/subtitle.ass" \
  -c:v libx264 -preset medium -crf 19 -pix_fmt yuv420p -c:a copy "output/${SLUG}-bilibili.mp4" 2>&1 | tail -1

echo "[6/6] 渲染封面"
( cd remotion && npx remotion still CoverVertical  "$ROOT/output/${SLUG}-cover-xiaohongshu.png" )
( cd remotion && npx remotion still CoverLandscape "$ROOT/output/${SLUG}-cover-bilibili.png" )

echo "=== 完成 ==="
for f in "output/${SLUG}-xiaohongshu.mp4" "output/${SLUG}-bilibili.mp4"; do
  echo "  $f → $(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f")s $(ffprobe -v error -select_streams v -show_entries stream=width,height -of csv=p=0:s=x "$f")"
done
echo "下一步：跑雷词终检 + 出 publish-meta.md（见 SKILL.md Step 7-8）"
