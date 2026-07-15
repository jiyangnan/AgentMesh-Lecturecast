#!/bin/bash
# Signed ProductionManifest -> local narration/subtitles -> two videos + two covers.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REMOTION_DIR="$REPO_ROOT/templates/remotion"
PROJECT_ROOT="${1:?usage: build_manifest_video.sh PROJECT_ROOT [CAPABILITIES_JSON]}"
CAPABILITIES="${2:-$PROJECT_ROOT/.lecturecast/client-capabilities.json}"
MANIFEST="$PROJECT_ROOT/.lecturecast/production-manifest.json"
OVERRIDES="$PROJECT_ROOT/.lecturecast/local-overrides.json"
BUILD_DIR="$PROJECT_ROOT/.lecturecast/build"
OUTPUT_DIR="$PROJECT_ROOT/output"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LECTURECAST_BIN="${LECTURECAST_BIN:-lecturecast}"

mkdir -p "$BUILD_DIR" "$OUTPUT_DIR" "$REMOTION_DIR/public/director"

echo "[1/7] 验证签名、能力和组件契约"
"$LECTURECAST_BIN" manifest preflight "$MANIFEST" --capabilities "$CAPABILITIES" --project-root "$PROJECT_ROOT" --json

echo "[2/7] 本地旁白（可复用）与字幕"
AUDIO_SRC=""
if [ "${LECTURECAST_SKIP_AUDIO:-0}" != "1" ]; then
  "$PYTHON_BIN" "$SCRIPT_DIR/build_manifest_audio.py" "$MANIFEST" "$BUILD_DIR/narration.mp3" --reuse
  cp "$BUILD_DIR/narration.mp3" "$REMOTION_DIR/public/director/narration.mp3"
  AUDIO_SRC="director/narration.mp3"
fi
"$PYTHON_BIN" "$SCRIPT_DIR/build_manifest_subtitles.py" "$MANIFEST" "$BUILD_DIR"

echo "[3/7] 生成纯声明式 Remotion props"
"$PYTHON_BIN" "$SCRIPT_DIR/prepare_manifest_render.py" --manifest "$MANIFEST" --overrides "$OVERRIDES" --variant vertical --audio-src "$AUDIO_SRC" --project-root "$PROJECT_ROOT" --public-root "$REMOTION_DIR/public" --output "$BUILD_DIR/props-vertical.json"
"$PYTHON_BIN" "$SCRIPT_DIR/prepare_manifest_render.py" --manifest "$MANIFEST" --overrides "$OVERRIDES" --variant landscape --audio-src "$AUDIO_SRC" --project-root "$PROJECT_ROOT" --public-root "$REMOTION_DIR/public" --output "$BUILD_DIR/props-landscape.json"

echo "[4/7] 渲染 Director 横竖双版"
(cd "$REMOTION_DIR" && npx remotion render DirectorVertical "$BUILD_DIR/video-vertical-raw.mp4" --props="$BUILD_DIR/props-vertical.json")
(cd "$REMOTION_DIR" && npx remotion render DirectorLandscape "$BUILD_DIR/video-landscape-raw.mp4" --props="$BUILD_DIR/props-landscape.json")

VIDEO_VERTICAL="$("$PYTHON_BIN" "$SCRIPT_DIR/manifest_output_name.py" "$MANIFEST" video 9:16)"
VIDEO_LANDSCAPE="$("$PYTHON_BIN" "$SCRIPT_DIR/manifest_output_name.py" "$MANIFEST" video 16:9)"
COVER_VERTICAL="$("$PYTHON_BIN" "$SCRIPT_DIR/manifest_output_name.py" "$MANIFEST" cover 3:4)"
COVER_LANDSCAPE="$("$PYTHON_BIN" "$SCRIPT_DIR/manifest_output_name.py" "$MANIFEST" cover 16:9)"

echo "[5/7] 本地烧录字幕"
if "$PYTHON_BIN" -c "import json,sys; raise SystemExit(0 if json.load(open(sys.argv[1]))['subtitles']['burn_in'] else 1)" "$MANIFEST"; then
  (cd "$BUILD_DIR" && ffmpeg -y -i video-vertical-raw.mp4 -vf "ass=subtitle_vertical.ass" -c:v libx264 -preset medium -crf 19 -pix_fmt yuv420p -c:a copy "$OUTPUT_DIR/$VIDEO_VERTICAL")
  (cd "$BUILD_DIR" && ffmpeg -y -i video-landscape-raw.mp4 -vf "ass=subtitle_landscape.ass" -c:v libx264 -preset medium -crf 19 -pix_fmt yuv420p -c:a copy "$OUTPUT_DIR/$VIDEO_LANDSCAPE")
else
  cp "$BUILD_DIR/video-vertical-raw.mp4" "$OUTPUT_DIR/$VIDEO_VERTICAL"
  cp "$BUILD_DIR/video-landscape-raw.mp4" "$OUTPUT_DIR/$VIDEO_LANDSCAPE"
fi

echo "[6/7] 渲染双封面"
(cd "$REMOTION_DIR" && npx remotion still DirectorCoverVertical "$OUTPUT_DIR/$COVER_VERTICAL" --props="$BUILD_DIR/props-vertical.json")
(cd "$REMOTION_DIR" && npx remotion still DirectorCoverLandscape "$OUTPUT_DIR/$COVER_LANDSCAPE" --props="$BUILD_DIR/props-landscape.json")

echo "[7/7] 校验尺寸、时长和文件完整性"
"$PYTHON_BIN" "$SCRIPT_DIR/validate_manifest_outputs.py" "$MANIFEST" "$OUTPUT_DIR"
echo "完成：$OUTPUT_DIR"
