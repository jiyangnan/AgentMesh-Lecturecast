from __future__ import annotations

import argparse
import json
from pathlib import Path

from lecturecast.protocol.update import update_protocol


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_DIR = REPO_ROOT / "src" / "lecturecast" / "protocol" / "schemas"
DEFAULT_LOCK_PATH = REPO_ROOT / "src" / "lecturecast" / "protocol" / "protocol.lock"


def main() -> None:
    parser = argparse.ArgumentParser(description="Import the signed-off LectureCast protocol bundle")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--schema-dir", type=Path, default=DEFAULT_SCHEMA_DIR)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK_PATH)
    args = parser.parse_args()
    lock = update_protocol(args.source, args.schema_dir, args.lock)
    print(json.dumps(lock, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
