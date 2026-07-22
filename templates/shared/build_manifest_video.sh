#!/bin/bash
# Signed ProductionManifest -> local narration/subtitles -> two videos + two covers.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PROJECT_ROOT_INPUT="${1:?usage: build_manifest_video.sh PROJECT_ROOT [CAPABILITIES_JSON]}"
PROJECT_ROOT="$(cd "$PROJECT_ROOT_INPUT" && pwd)"
REMOTION_DIR="${LECTURECAST_REMOTION_DIR:-$PROJECT_ROOT/remotion}"
CAPABILITIES="${2:-$PROJECT_ROOT/.lecturecast/client-capabilities.json}"
MANIFEST="$PROJECT_ROOT/.lecturecast/production-manifest.json"
OVERRIDES="$PROJECT_ROOT/.lecturecast/local-overrides.json"
BUILD_DIR="$PROJECT_ROOT/.lecturecast/build"
TIMING="$BUILD_DIR/audio-timing.json"
OUTPUT_DIR="$PROJECT_ROOT/output"
if [ -z "${PYTHON_BIN:-}" ]; then
  if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi
if [ -z "${LECTURECAST_BIN:-}" ]; then
  if [ -x "$REPO_ROOT/.venv/bin/lecturecast" ]; then
    LECTURECAST_BIN="$REPO_ROOT/.venv/bin/lecturecast"
  else
    LECTURECAST_BIN="lecturecast"
  fi
fi

if [ ! -f "$REMOTION_DIR/package.json" ]; then
  echo "episode Remotion runtime missing: $REMOTION_DIR" >&2
  echo "copy templates/remotion into PROJECT_ROOT/remotion and run npm install first" >&2
  exit 1
fi
REMOTION_DIR="$(cd "$REMOTION_DIR" && pwd)"

mkdir -p "$BUILD_DIR" "$OUTPUT_DIR" "$REMOTION_DIR/public/director"

echo "[1/8] 验证完整脚本批准记录"
"$LECTURECAST_BIN" manifest approval "$PROJECT_ROOT" --json

echo "[2/8] 验证签名、能力、旁白时间和组件契约"
"$LECTURECAST_BIN" manifest preflight "$MANIFEST" --capabilities "$CAPABILITIES" --project-root "$PROJECT_ROOT" --json

echo "[3/8] 分节生成本地旁白并测量真实时间线"
"$PYTHON_BIN" "$SCRIPT_DIR/build_manifest_audio.py" "$MANIFEST" "$BUILD_DIR/narration.mp3" --timing-out "$TIMING" --reuse
cp "$BUILD_DIR/narration.mp3" "$REMOTION_DIR/public/director/narration.mp3"
AUDIO_SRC="director/narration.mp3"
TIMING_ARGS=(--timing "$TIMING")
"$PYTHON_BIN" "$SCRIPT_DIR/build_manifest_subtitles.py" "$MANIFEST" "$BUILD_DIR" "${TIMING_ARGS[@]}"

echo "[4/8] 从签名计划和实测音频生成本地执行 props"
"$PYTHON_BIN" "$SCRIPT_DIR/prepare_manifest_render.py" --manifest "$MANIFEST" --overrides "$OVERRIDES" --variant vertical --audio-src "$AUDIO_SRC" --project-root "$PROJECT_ROOT" --public-root "$REMOTION_DIR/public" --output "$BUILD_DIR/props-vertical.json" "${TIMING_ARGS[@]}"
"$PYTHON_BIN" "$SCRIPT_DIR/prepare_manifest_render.py" --manifest "$MANIFEST" --overrides "$OVERRIDES" --variant landscape --audio-src "$AUDIO_SRC" --project-root "$PROJECT_ROOT" --public-root "$REMOTION_DIR/public" --output "$BUILD_DIR/props-landscape.json" "${TIMING_ARGS[@]}"

echo "[5/8] 渲染 Director 横竖双版"
(cd "$REMOTION_DIR" && npx remotion render DirectorVertical "$BUILD_DIR/video-vertical-raw.mp4" --props="$BUILD_DIR/props-vertical.json")
(cd "$REMOTION_DIR" && npx remotion render DirectorLandscape "$BUILD_DIR/video-landscape-raw.mp4" --props="$BUILD_DIR/props-landscape.json")

VIDEO_VERTICAL="$("$PYTHON_BIN" "$SCRIPT_DIR/manifest_output_name.py" "$MANIFEST" video 9:16)"
VIDEO_LANDSCAPE="$("$PYTHON_BIN" "$SCRIPT_DIR/manifest_output_name.py" "$MANIFEST" video 16:9)"
COVER_VERTICAL="$("$PYTHON_BIN" "$SCRIPT_DIR/manifest_output_name.py" "$MANIFEST" cover 3:4)"
COVER_LANDSCAPE="$("$PYTHON_BIN" "$SCRIPT_DIR/manifest_output_name.py" "$MANIFEST" cover 16:9)"

echo "[6/8] 本地烧录字幕"
if "$PYTHON_BIN" -c "import json,sys; raise SystemExit(0 if json.load(open(sys.argv[1]))['subtitles']['burn_in'] else 1)" "$MANIFEST"; then
  (cd "$BUILD_DIR" && ffmpeg -y -i video-vertical-raw.mp4 -vf "ass=subtitle_vertical.ass" -c:v libx264 -preset medium -crf 19 -pix_fmt yuv420p -c:a copy "$OUTPUT_DIR/$VIDEO_VERTICAL")
  (cd "$BUILD_DIR" && ffmpeg -y -i video-landscape-raw.mp4 -vf "ass=subtitle_landscape.ass" -c:v libx264 -preset medium -crf 19 -pix_fmt yuv420p -c:a copy "$OUTPUT_DIR/$VIDEO_LANDSCAPE")
else
  cp "$BUILD_DIR/video-vertical-raw.mp4" "$OUTPUT_DIR/$VIDEO_VERTICAL"
  cp "$BUILD_DIR/video-landscape-raw.mp4" "$OUTPUT_DIR/$VIDEO_LANDSCAPE"
fi

echo "[7/8] 渲染双封面"
(cd "$REMOTION_DIR" && npx remotion still DirectorCoverVertical "$OUTPUT_DIR/$COVER_VERTICAL" --props="$BUILD_DIR/props-vertical.json")
(cd "$REMOTION_DIR" && npx remotion still DirectorCoverLandscape "$OUTPUT_DIR/$COVER_LANDSCAPE" --props="$BUILD_DIR/props-landscape.json")

echo "[8/8] 校验尺寸、实测时长、音频覆盖和文件完整性"
"$PYTHON_BIN" "$SCRIPT_DIR/validate_manifest_outputs.py" "$MANIFEST" "$OUTPUT_DIR" --timing "$TIMING" --narration "$BUILD_DIR/narration.mp3"
echo "完成：$OUTPUT_DIR"
