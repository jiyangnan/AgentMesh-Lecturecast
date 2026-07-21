from __future__ import annotations

import base64
import builtins
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from lecturecast.errors import LectureCastError
from lecturecast.manifest import PublicKeyRing, SigningKey, verify_manifest
from lecturecast.protocol import manifest_signing_bytes


FIXTURE = Path(__file__).parent / "fixtures" / "production-manifest-v1.json"


def _manifest() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_manifest_import_reports_missing_signature_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("cryptography"):
            raise ImportError("optional dependency intentionally unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)

    with pytest.raises(LectureCastError) as captured:
        verify_manifest(_manifest())

    assert captured.value.code == "client_upgrade_required"
    assert "官方安装器" in captured.value.next_action


def test_fixture_manifest_has_valid_default_signature() -> None:
    result = verify_manifest(_manifest())

    assert result.valid
    assert result.key_id == "fixture_key_v1"
    assert result.key_status == "current"


@pytest.mark.parametrize("field", ["title", "props", "digest", "key_id"])
def test_manifest_tampering_is_rejected(field: str) -> None:
    payload = copy.deepcopy(_manifest())
    if field == "title":
        payload["script"][0]["title"] = "篡改标题"
    elif field == "props":
        payload["scenes"][0]["props"]["headline"] = "篡改 Props"
    elif field == "digest":
        payload["brief_digest"] = "sha256:" + "0" * 64
    else:
        payload["signature"]["key_id"] = "unknown_key_v1"

    with pytest.raises(LectureCastError) as captured:
        verify_manifest(payload)

    assert captured.value.code == "manifest_signature_invalid"


def test_previous_rotation_key_remains_valid_for_its_signing_window() -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = base64.b64encode(
        private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    payload = _manifest()
    payload["signature"]["key_id"] = "rotation_previous_v1"
    payload["signature"]["value"] = ""
    payload["signature"]["value"] = base64.b64encode(
        private_key.sign(manifest_signing_bytes(payload))
    ).decode()
    created = datetime.fromisoformat(payload["created_at"].replace("Z", "+00:00"))
    keyring = PublicKeyRing(
        [
            SigningKey(
                key_id="rotation_previous_v1",
                algorithm="Ed25519",
                public_key=public_key,
                status="previous",
                not_before=(created - timedelta(days=1)).astimezone(UTC).isoformat(),
                not_after=(created + timedelta(days=1)).astimezone(UTC).isoformat(),
            )
        ]
    )

    assert verify_manifest(payload, keyring=keyring).key_status == "previous"


def test_invalid_packaged_trust_root_fails_as_a_signature_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        PublicKeyRing,
        "load",
        classmethod(lambda _cls, _path=None: (_ for _ in ()).throw(ValueError("bad"))),
    )

    with pytest.raises(LectureCastError) as captured:
        verify_manifest(_manifest())

    assert captured.value.code == "manifest_signature_invalid"
    assert "信任根" in captured.value.message
