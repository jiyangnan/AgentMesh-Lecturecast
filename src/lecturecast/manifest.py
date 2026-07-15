from __future__ import annotations

import base64
import binascii
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .errors import LectureCastError
from .protocol import ProductionManifest, canonical_digest, manifest_signing_bytes


KEYRING_PATH = Path(__file__).with_name("signing-keyring.json")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class SigningKey:
    key_id: str
    algorithm: str
    public_key: str
    status: str
    not_before: str
    not_after: str


@dataclass(frozen=True)
class VerificationResult:
    valid: bool
    key_id: str
    key_status: str
    manifest_digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PublicKeyRing:
    def __init__(self, keys: list[SigningKey]) -> None:
        self._keys = {key.key_id: key for key in keys}
        if len(self._keys) != len(keys):
            raise ValueError("duplicate signing key_id")

    @classmethod
    def load(cls, path: Path = KEYRING_PATH) -> "PublicKeyRing":
        payload = json.loads(path.read_text(encoding="utf-8"))
        keys = [SigningKey(**item) for item in payload["keys"]]
        return cls(keys)

    def get(self, key_id: str) -> SigningKey | None:
        return self._keys.get(key_id)


def load_manifest(path: Path | str) -> ProductionManifest:
    try:
        content = Path(path).read_text(encoding="utf-8")
        return ProductionManifest.model_validate_json(content)
    except LectureCastError:
        raise
    except Exception as exc:
        raise LectureCastError(
            code="manifest_incompatible",
            message="ProductionManifest 无法读取或不符合 v1 协议。",
            next_action="请重新下载云端签发的 Manifest，或升级 LectureCast 客户端。",
            cause=type(exc).__name__,
        ) from None


def verify_manifest(
    manifest: ProductionManifest | dict[str, Any],
    *,
    keyring: PublicKeyRing | None = None,
) -> VerificationResult:
    document = (
        manifest if isinstance(manifest, ProductionManifest) else ProductionManifest.model_validate(manifest)
    )
    payload = document.model_dump()
    signature = payload["signature"]
    key = (keyring or PublicKeyRing.load()).get(signature["key_id"])
    if key is None or key.status not in {"current", "previous"}:
        raise LectureCastError(
            code="manifest_signature_invalid",
            message="Manifest 使用了未知或已撤销的签名 Key。",
            next_action="升级客户端并重新获取 Manifest；不要绕过签名验证。",
        )
    if signature["algorithm"] != key.algorithm or key.algorithm != "Ed25519":
        raise LectureCastError(
            code="manifest_signature_invalid",
            message="Manifest 签名算法与 Key 不匹配。",
            next_action="重新获取 Manifest；不要手工修改 signature 元数据。",
        )
    created_at = _parse_time(payload["created_at"])
    if not (_parse_time(key.not_before) <= created_at <= _parse_time(key.not_after)):
        raise LectureCastError(
            code="manifest_signature_invalid",
            message="Manifest 的签发时间不在该 Key 的有效窗口内。",
            next_action="升级客户端并重新请求生成。",
        )
    try:
        public_bytes = base64.b64decode(key.public_key, validate=True)
        signature_bytes = base64.b64decode(signature["value"], validate=True)
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            signature_bytes,
            manifest_signing_bytes(document),
        )
    except (InvalidSignature, ValueError, binascii.Error):
        raise LectureCastError(
            code="manifest_signature_invalid",
            message="Manifest 签名验证失败，内容可能已被修改。",
            next_action="恢复云端签发的原件；本地调整请写入 local-overrides.json。",
        ) from None
    return VerificationResult(
        valid=True,
        key_id=key.key_id,
        key_status=key.status,
        manifest_digest=canonical_digest(document),
    )


def inspect_manifest(manifest: ProductionManifest | dict[str, Any]) -> dict[str, Any]:
    document = (
        manifest if isinstance(manifest, ProductionManifest) else ProductionManifest.model_validate(manifest)
    )
    payload = document.model_dump()
    return {
        "schema_version": payload["schema_version"],
        "manifest_id": payload["manifest_id"],
        "generation_id": payload["generation_id"],
        "brief_digest": payload["brief_digest"],
        "capability_digest": payload["capability_digest"],
        "component_catalog_digest": payload["component_catalog_digest"],
        "scene_count": len(payload["scenes"]),
        "output_count": len(payload["outputs"]),
        "duration_seconds": payload["total_frames"] / payload["fps"],
        "voice_engine": payload["voice"]["engine"],
        "signature_key_id": payload["signature"]["key_id"],
        "manifest_digest": canonical_digest(document),
    }
