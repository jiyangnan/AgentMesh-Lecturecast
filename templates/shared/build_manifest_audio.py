#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import urllib.request
from pathlib import Path


async def synthesize(manifest: dict, output: Path) -> None:
    try:
        import edge_tts
    except ImportError as exc:
        raise SystemExit("edge-tts is required for local narration: pip install edge-tts") from exc
    voice = manifest["voice"]
    engine = voice["engine"]
    text = "\n".join(section["narration"] for section in manifest["script"])
    if engine == "minimax" and os.environ.get("MINIMAX_API_KEY", "").strip():
        request = urllib.request.Request(
            "https://api.minimaxi.com/v1/t2a_v2",
            data=json.dumps(
                {
                    "model": "speech-02-hd",
                    "text": text,
                    "stream": False,
                    "voice_setting": {
                        "voice_id": voice["voice_id"],
                        "speed": 1.0,
                        "vol": 1,
                        "pitch": 0,
                    },
                    "audio_setting": {
                        "sample_rate": 32000,
                        "bitrate": 128000,
                        "format": "mp3",
                        "channel": 1,
                    },
                }
            ).encode(),
            headers={
                "Authorization": f"Bearer {os.environ['MINIMAX_API_KEY'].strip()}",
                "Content-Type": "application/json",
            },
        )
        try:
            response = json.loads(urllib.request.urlopen(request, timeout=60).read())
            if response.get("base_resp", {}).get("status_code") == 0:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(bytes.fromhex(response["data"]["audio"]))
                return
        except Exception:
            pass
        print("MiniMax BYOK failed; falling back to local Edge workflow without exposing the key.")
    rate = int(voice.get("rate_percent", 0))
    communicator = edge_tts.Communicate(
        text,
        voice["voice_id"] if engine == "edge" else "zh-CN-YunjianNeural",
        rate=f"{rate:+d}%",
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    await communicator.save(str(output))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--reuse", action="store_true")
    args = parser.parse_args()
    if args.reuse and args.output.exists() and args.output.stat().st_size > 0:
        print(f"reusing local narration: {args.output}")
        return
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest["voice"]["engine"] not in {"edge", "minimax"}:
        raise SystemExit("unsupported local voice engine")
    asyncio.run(synthesize(manifest, args.output))


if __name__ == "__main__":
    main()
