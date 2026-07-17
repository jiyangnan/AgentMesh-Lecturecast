from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from lecturecast.keyring_update import revoke_key, update_keyring
from lecturecast.manifest import PublicKeyRing, SigningKey


ROOT = Path(__file__).parents[1]
PACKAGED_KEYRING = ROOT / "src" / "lecturecast" / "signing-keyring.json"


def _envelope(path: Path, key_id: str) -> dict[str, object]:
    public_key = Ed25519PrivateKey.generate().public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )
    key = {
        "key_id": key_id,
        "algorithm": "Ed25519",
        "public_key": base64.b64encode(public_key).decode(),
        "status": "current",
        "not_before": "2026-07-20T00:00:00Z",
        "not_after": "2027-01-20T00:00:00Z",
    }
    payload: dict[str, object] = {
        "entry_version": "1.0",
        "fingerprint": f"sha256:{hashlib.sha256(public_key).hexdigest()}",
        "key": key,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def test_packaged_prelaunch_keyring_trusts_no_fixture_key() -> None:
    ring = PublicKeyRing.load(PACKAGED_KEYRING)

    assert ring.keys == ()
    with pytest.raises(ValueError, match="exactly one current"):
        ring.validate_for_release()


def test_keyring_rejects_invalid_public_key_status_window_and_duplicates() -> None:
    valid = SigningKey(
        key_id="lecturecast-prod-202607-v1",
        algorithm="Ed25519",
        public_key=base64.b64encode(b"p" * 32).decode(),
        status="current",
        not_before="2026-07-20T00:00:00Z",
        not_after="2027-01-20T00:00:00Z",
    )

    with pytest.raises(ValueError, match="duplicate"):
        PublicKeyRing([valid, valid])
    with pytest.raises(ValueError, match="public key"):
        PublicKeyRing([SigningKey(**{**valid.to_dict(), "public_key": "bad"})])
    with pytest.raises(ValueError, match="status"):
        PublicKeyRing([SigningKey(**{**valid.to_dict(), "status": "disabled"})])
    with pytest.raises(ValueError, match="validity window"):
        PublicKeyRing(
            [
                SigningKey(
                    **{
                        **valid.to_dict(),
                        "not_before": "2027-01-20T00:00:00Z",
                        "not_after": "2026-07-20T00:00:00Z",
                    }
                )
            ]
        )


def test_publication_adds_current_key_and_demotes_old_key_without_deleting_it(
    tmp_path: Path,
) -> None:
    keyring = tmp_path / "signing-keyring.json"
    keyring.write_text(PACKAGED_KEYRING.read_text(encoding="utf-8"), encoding="utf-8")
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _envelope(first, "lecturecast-prod-202607-v1")
    _envelope(second, "lecturecast-prod-202701-v1")

    update_keyring(keyring_path=keyring, entry_path=first)
    first_payload = json.loads(keyring.read_text(encoding="utf-8"))
    update_keyring(keyring_path=keyring, entry_path=second)
    rotated = PublicKeyRing.load(keyring)

    assert first_payload["keys"][0]["status"] == "current"
    assert rotated.get("lecturecast-prod-202607-v1").status == "previous"  # type: ignore[union-attr]
    assert rotated.get("lecturecast-prod-202701-v1").status == "current"  # type: ignore[union-attr]
    assert len(rotated.keys) == 2
    rotated.validate_for_release()


def test_publication_rejects_fingerprint_mismatch_and_key_id_mutation(
    tmp_path: Path,
) -> None:
    keyring = tmp_path / "signing-keyring.json"
    keyring.write_text(PACKAGED_KEYRING.read_text(encoding="utf-8"), encoding="utf-8")
    entry = tmp_path / "entry.json"
    payload = _envelope(entry, "lecturecast-prod-202607-v1")

    broken = copy.deepcopy(payload)
    broken["fingerprint"] = "sha256:" + "0" * 64
    entry.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid signing public-entry"):
        update_keyring(keyring_path=keyring, entry_path=entry)

    entry.write_text(json.dumps(payload), encoding="utf-8")
    update_keyring(keyring_path=keyring, entry_path=entry)
    mutated = copy.deepcopy(payload)
    mutated["key"]["not_after"] = "2028-01-20T00:00:00Z"  # type: ignore[index]
    entry.write_text(json.dumps(mutated), encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be mutated"):
        update_keyring(keyring_path=keyring, entry_path=entry)


def test_revocation_requires_a_replacement_and_is_idempotent(tmp_path: Path) -> None:
    keyring = tmp_path / "signing-keyring.json"
    keyring.write_text(PACKAGED_KEYRING.read_text(encoding="utf-8"), encoding="utf-8")
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _envelope(first, "lecturecast-prod-202607-v1")
    _envelope(second, "lecturecast-prod-202701-v1")
    update_keyring(keyring_path=keyring, entry_path=first)

    with pytest.raises(ValueError, match="replacement"):
        revoke_key(keyring_path=keyring, key_id="lecturecast-prod-202607-v1")

    update_keyring(keyring_path=keyring, entry_path=second)
    revoked = revoke_key(keyring_path=keyring, key_id="lecturecast-prod-202607-v1")
    repeated = revoke_key(keyring_path=keyring, key_id="lecturecast-prod-202607-v1")

    assert revoked == repeated
    assert PublicKeyRing.load(keyring).get("lecturecast-prod-202607-v1").status == "revoked"  # type: ignore[union-attr]
