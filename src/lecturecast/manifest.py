from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import LectureCastError
from .protocol import ProductionManifest, canonical_digest, manifest_signing_bytes


KEYRING_PATH = Path(__file__).with_name("signing-keyring.json")
KEY_ID_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*")
KEY_STATUSES = {"current", "previous", "revoked"}


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("signing key timestamps must include a UTC offset")
    return parsed


@dataclass(frozen=True)
class SigningKey:
    key_id: str
    algorithm: str
    public_key: str
    status: str
    not_before: str
    not_after: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class VerificationResult:
    valid: bool
    key_id: str
    key_status: str
    manifest_digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PublicKeyRing:
    def __init__(self, keys: list[SigningKey], *, keyring_version: str = "1.0") -> None:
        if keyring_version != "1.0":
            raise ValueError("unsupported signing keyring version")
        for key in keys:
            if not KEY_ID_PATTERN.fullmatch(key.key_id):
                raise ValueError("invalid signing key_id")
            if key.algorithm != "Ed25519":
                raise ValueError("unsupported signing key algorithm")
            if key.status not in KEY_STATUSES:
                raise ValueError("invalid signing key status")
            try:
                public_key = base64.b64decode(key.public_key, validate=True)
            except (binascii.Error, ValueError):
                raise ValueError("invalid Ed25519 public key") from None
            if len(public_key) != 32:
                raise ValueError("invalid Ed25519 public key")
            if _parse_time(key.not_before) >= _parse_time(key.not_after):
                raise ValueError("invalid signing key validity window")
        self._keys = {key.key_id: key for key in keys}
        if len(self._keys) != len(keys):
            raise ValueError("duplicate signing key_id")
        if sum(key.status == "current" for key in keys) > 1:
            raise ValueError("signing keyring has multiple current keys")
        self.keyring_version = keyring_version

    @classmethod
    def load(cls, path: Path | None = None) -> "PublicKeyRing":
        payload = json.loads((path or KEYRING_PATH).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"keyring_version", "keys"}:
            raise ValueError("invalid signing keyring document")
        if not isinstance(payload["keys"], list):
            raise ValueError("invalid signing keyring keys")
        try:
            keys = [SigningKey(**item) for item in payload["keys"]]
        except (TypeError, KeyError):
            raise ValueError("invalid signing keyring entry") from None
        return cls(keys, keyring_version=payload["keyring_version"])

    def get(self, key_id: str) -> SigningKey | None:
        return self._keys.get(key_id)

    @property
    def keys(self) -> tuple[SigningKey, ...]:
        return tuple(self._keys.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "keyring_version": self.keyring_version,
            "keys": [key.to_dict() for key in self._keys.values()],
        }

    def validate_for_release(self) -> None:
        keys = list(self._keys.values())
        if sum(key.status == "current" for key in keys) != 1:
            raise ValueError("release keyring must contain exactly one current key")
        if any(not key.key_id.startswith("lecturecast-prod-") for key in keys):
            raise ValueError("release keyring can trust only lecturecast-prod keys")

    @staticmethod
    def public_key_fingerprint(key: SigningKey) -> str:
        public_key = base64.b64decode(key.public_key, validate=True)
        return f"sha256:{hashlib.sha256(public_key).hexdigest()}"


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
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        raise LectureCastError(
            code="client_upgrade_required",
            message="当前 Community 安装未包含 Director 的签名验证依赖。",
            next_action=(
                "如需使用 Director，请运行："
                "~/.lecturecast/app/.venv/bin/pip install 'cryptography>=43'"
            ),
        ) from None
    document = (
        manifest if isinstance(manifest, ProductionManifest) else ProductionManifest.model_validate(manifest)
    )
    payload = document.model_dump()
    signature = payload["signature"]
    try:
        trusted_keyring = keyring or PublicKeyRing.load()
    except (OSError, ValueError, json.JSONDecodeError):
        raise LectureCastError(
            code="manifest_signature_invalid",
            message="客户端签名信任根无效或尚未发布。",
            next_action="请安装包含正式 LectureCast public keyring 的可信客户端版本。",
        ) from None
    key = trusted_keyring.get(signature["key_id"])
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
