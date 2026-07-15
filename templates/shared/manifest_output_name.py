#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("kind", choices=("video", "cover"))
    parser.add_argument("aspect_ratio", choices=("16:9", "9:16", "3:4"))
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    output = next(
        item
        for item in manifest["outputs"]
        if item["kind"] == args.kind and item["aspect_ratio"] == args.aspect_ratio
    )
    print(output["filename"])


if __name__ == "__main__":
    main()

