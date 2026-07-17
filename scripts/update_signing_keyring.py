from __future__ import annotations

import argparse
import json
from pathlib import Path

from lecturecast.keyring_update import revoke_key, update_keyring
from lecturecast.manifest import PublicKeyRing


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KEYRING = REPO_ROOT / "src" / "lecturecast" / "signing-keyring.json"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate or atomically add a production public key to LectureCast"
    )
    parser.add_argument("--keyring", type=Path, default=DEFAULT_KEYRING)
    parser.add_argument("--entry", type=Path)
    parser.add_argument("--revoke")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    selected = sum((args.entry is not None, args.revoke is not None, args.check))
    if selected != 1:
        parser.error("choose exactly one of --entry, --revoke, or --check")
    if args.check:
        ring = PublicKeyRing.load(args.keyring)
        ring.validate_for_release()
        result = ring.to_dict()
    elif args.revoke is not None:
        try:
            result = revoke_key(keyring_path=args.keyring, key_id=args.revoke)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        try:
            assert args.entry is not None
            result = update_keyring(keyring_path=args.keyring, entry_path=args.entry)
        except ValueError as exc:
            parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
