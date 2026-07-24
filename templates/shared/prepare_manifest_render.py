#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from lecturecast.assets import materialize_manifest_assets
from lecturecast.host_agent import require_project_host_workflow
from lecturecast.timing import render_timing_from_audio_plan


def object_from(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--overrides", type=Path, required=True)
    parser.add_argument("--variant", choices=("vertical", "landscape"), required=True)
    parser.add_argument("--audio-src")
    parser.add_argument("--timing", type=Path)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--public-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    require_project_host_workflow(args.project_root)

    overrides_document = object_from(args.overrides)
    signed_manifest = object_from(args.manifest)
    manifest = materialize_manifest_assets(
        signed_manifest,
        project_root=args.project_root,
        public_root=args.public_root,
    )
    props = {
        "manifest": manifest,
        "overrides": overrides_document.get("overrides", overrides_document),
        "variant": args.variant,
    }
    if args.timing:
        props["renderTiming"] = render_timing_from_audio_plan(
            signed_manifest,
            object_from(args.timing),
        )
    if args.audio_src:
        props["audioSrc"] = args.audio_src
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(props, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
