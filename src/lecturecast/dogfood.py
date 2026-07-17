from __future__ import annotations

import fcntl
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import zipfile
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .director import DIRECTOR_ADAPTER_KINDS, DirectorStateStore
from .errors import LectureCastError
from .manifest import PublicKeyRing, load_manifest
from .preflight import run_preflight
from .project import ProjectStore, atomic_write_json
from .protocol import ClientCapabilities


DOGFOOD_SESSION_SCHEMA = "dogfood-session.v1"
DOGFOOD_RECEIPT_SCHEMA = "dogfood-receipt.v1"
DOGFOOD_GATE_SCHEMA = "dogfood-gate.v1"
DOGFOOD_RUN_KINDS = frozenset({"native_full", "handoff", "text_fallback"})
DOGFOOD_INTERACTION_MODES = frozenset({"native_choice", "text_fallback"})
NATIVE_ADAPTERS = frozenset({"codex", "claude-code", "openclaw"})
HANDOFF_ADAPTER_ORDER = ("codex", "claude-code", "openclaw")
FULL_RUN_EVENTS = (
    "director_start",
    "decision_answer",
    "brief_confirm",
    "generation_request",
    "generation_status",
    "render_captured",
)
REQUIRED_OUTPUTS = frozenset(
    {
        ("video", "16:9", 1920, 1080, "mp4"),
        ("video", "9:16", 1080, 1920, "mp4"),
        ("cover", "16:9", 1920, 1080, "png"),
        ("cover", "3:4", 1242, 1660, "png"),
    }
)
DOGFOOD_EVENTS = frozenset(
    {
        "director_start",
        "decision_next",
        "director_resume",
        "decision_answer",
        "brief_show",
        "brief_confirm",
        "generation_request",
        "generation_status",
        "cloud_delete",
        "director_handoff",
        "render_captured",
    }
)
_SESSION_FIELDS = {
    "schema_version",
    "run_id",
    "run_kind",
    "project_id",
    "expected_adapter",
    "client_version",
    "release",
    "started_at",
    "journal_revision",
    "events",
}
_EVENT_FIELDS = {
    "sequence",
    "event",
    "recorded_at",
    "adapter",
    "session_id",
    "generation_id",
    "status",
    "interaction_mode",
    "adapter_changed",
    "fresh_task",
    "capability_digest",
    "manifest_digest",
    "signature_key_id",
    "signature_key_status",
    "outputs",
}
_RECEIPT_FIELDS = {
    "schema_version",
    "run_id",
    "run_kind",
    "project_id",
    "expected_adapter",
    "client_version",
    "release",
    "started_at",
    "completed_at",
    "adapters_seen",
    "interaction_modes_seen",
    "event_counts",
    "session_id",
    "generation_id",
    "capability_digest",
    "manifest_digest",
    "signature_key_id",
    "signature_key_status",
    "outputs",
    "journal_digest",
}
_OUTPUT_FIELDS = {
    "output_id",
    "kind",
    "aspect_ratio",
    "width",
    "height",
    "format",
    "filename",
    "bytes",
    "sha256",
    "duration_seconds",
}
_RELEASE_FIELDS = {
    "version",
    "commit",
    "wheel_sha256",
    "package_digest",
    "attestation_digest",
    "key_id",
}
_STABLE_ID = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][0-9A-Za-z.-]+)?")
_MAX_OUTPUT_BYTES = 16 * 1024 * 1024 * 1024
_MAX_PROBE_BYTES = 1024 * 1024
_MAX_ATTESTATION_BYTES = 64 * 1024
_MAX_PUBLIC_WHEEL_BYTES = 128 * 1024 * 1024
_MAX_PACKAGE_MEMBER_BYTES = 16 * 1024 * 1024
_MAX_PACKAGE_BYTES = 64 * 1024 * 1024
_MINIMUM_PUBLICATION_LEAD = timedelta(days=7)
_MAXIMUM_ATTESTATION_CLOCK_SKEW = timedelta(minutes=15)
_PACKAGE_SUFFIXES = frozenset({".py", ".json"})


ProbeRunner = Callable[[Path], dict[str, Any]]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(payload: Mapping[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(_canonical(payload)).hexdigest()}"


def _client_version() -> str:
    try:
        return package_version("lecturecast")
    except PackageNotFoundError:
        return "source"


def _safe_input_file(path: Path, *, maximum_bytes: int, label: str) -> Path:
    raw = path.expanduser()
    try:
        file_stat = os.lstat(raw)
    except OSError:
        raise _fail(f"{label} 不存在。", "提供正式发布流程保存的可信原件。") from None
    if (
        stat.S_ISLNK(file_stat.st_mode)
        or not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_size <= 0
        or file_stat.st_size > maximum_bytes
    ):
        raise _fail(f"{label} 不是安全的常规文件。", "提供正式发布流程保存的可信原件。")
    return raw.resolve(strict=True)


def _tree_digest(entries: list[tuple[str, bytes]]) -> str:
    files = [
        {
            "path": name,
            "bytes": len(content),
            "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
        }
        for name, content in sorted(entries)
    ]
    return _digest({"files": files})


def _installed_package_digest() -> str:
    root = Path(__file__).resolve().parent
    entries: list[tuple[str, bytes]] = []
    total = 0
    for candidate in sorted(root.rglob("*")):
        if candidate.suffix not in _PACKAGE_SUFFIXES:
            continue
        try:
            file_stat = os.lstat(candidate)
        except OSError:
            raise _fail("当前 LectureCast 安装无法验证。", "重新安装正式 Public wheel。") from None
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise _fail("当前 LectureCast 包含不安全文件。", "重新安装正式 Public wheel。")
        if file_stat.st_size > _MAX_PACKAGE_MEMBER_BYTES:
            raise _fail("当前 LectureCast 包文件过大。", "重新安装正式 Public wheel。")
        content = candidate.read_bytes()
        total += len(content)
        if total > _MAX_PACKAGE_BYTES:
            raise _fail("当前 LectureCast 包超过验证上限。", "重新安装正式 Public wheel。")
        name = PurePosixPath("lecturecast", *candidate.relative_to(root).parts).as_posix()
        entries.append((name, content))
    if not entries:
        raise _fail("当前 LectureCast 安装缺少可验证源码。", "重新安装正式 Public wheel。")
    return _tree_digest(entries)


def _wheel_package_identity(path: Path) -> tuple[str, str, str]:
    wheel = _safe_input_file(path, maximum_bytes=_MAX_PUBLIC_WHEEL_BYTES, label="Public wheel")
    wheel_digest = _file_digest(wheel)
    entries: list[tuple[str, bytes]] = []
    metadata_documents: list[bytes] = []
    total = 0
    try:
        with zipfile.ZipFile(wheel) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ValueError
            for info in infos:
                member = PurePosixPath(info.filename)
                if (
                    member.is_absolute()
                    or ".." in member.parts
                    or "\\" in info.filename
                    or info.file_size < 0
                    or info.file_size > _MAX_PACKAGE_MEMBER_BYTES
                ):
                    raise ValueError
                mode = info.external_attr >> 16
                if mode and stat.S_ISLNK(mode):
                    raise ValueError
                if len(member.parts) == 2 and member.parts[0].endswith(".dist-info") and member.name == "METADATA":
                    metadata_documents.append(archive.read(info))
                if (
                    len(member.parts) >= 2
                    and member.parts[0] == "lecturecast"
                    and member.suffix in _PACKAGE_SUFFIXES
                    and not info.is_dir()
                ):
                    content = archive.read(info)
                    total += len(content)
                    if total > _MAX_PACKAGE_BYTES:
                        raise ValueError
                    entries.append((member.as_posix(), content))
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        raise _fail("Public wheel 无法安全验证。", "重新下载正式发布的 exact wheel。") from None
    if len(metadata_documents) != 1 or not entries:
        raise _fail("Public wheel 缺少唯一包元数据或 LectureCast 内容。", "使用正式发布的 wheel。")
    try:
        metadata = metadata_documents[0].decode("utf-8")
    except UnicodeDecodeError:
        raise _fail("Public wheel 元数据无效。", "使用正式发布的 wheel。") from None
    fields: dict[str, str] = {}
    for line in metadata.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            if key in {"Name", "Version"} and key not in fields:
                fields[key] = value.strip()
    version = fields.get("Version")
    if fields.get("Name", "").lower() != "lecturecast" or not isinstance(version, str):
        raise _fail("Public wheel 不是 LectureCast 正式包。", "使用正式发布的 LectureCast wheel。")
    if _VERSION.fullmatch(version) is None:
        raise _fail("Public wheel 版本无效。", "使用正式发布的 LectureCast wheel。")
    return version, wheel_digest, _tree_digest(entries)


def _parse_utc(value: Any, *, label: str) -> datetime:
    normalized = _timestamp(value, label=label)
    return datetime.fromisoformat(normalized.replace("Z", "+00:00")).astimezone(UTC)


def _attestation_signing_bytes(evidence: Mapping[str, Any]) -> bytes:
    return _canonical(
        {
            "schema_version": "signing-public-first-attestation.v1",
            "evidence": evidence,
        }
    )


def _validate_release(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _RELEASE_FIELDS:
        raise _fail("Dogfood release binding 契约不匹配。", "用正式 release 证据重新开始 run。")
    if not isinstance(value["version"], str) or _VERSION.fullmatch(value["version"]) is None:
        raise _fail("Dogfood release version 无效。", "用正式 release 证据重新开始 run。")
    if not isinstance(value["commit"], str) or _COMMIT.fullmatch(value["commit"]) is None:
        raise _fail("Dogfood release commit 无效。", "用正式 release 证据重新开始 run。")
    for field in ("wheel_sha256", "package_digest", "attestation_digest"):
        if not isinstance(value[field], str) or _DIGEST.fullmatch(value[field]) is None:
            raise _fail("Dogfood release digest 无效。", "用正式 release 证据重新开始 run。")
    _stable_id(value["key_id"], label="release key_id")
    if not value["key_id"].startswith("lecturecast-prod-"):
        raise _fail("Dogfood release 未使用 production key。", "等待正式 Public release 后再运行。")
    return dict(value)


def verify_release_binding(
    attestation_path: Path,
    public_wheel_path: Path,
    *,
    keyring: PublicKeyRing | None = None,
    checked_at: datetime | None = None,
) -> dict[str, str]:
    attestation_file = _safe_input_file(
        attestation_path,
        maximum_bytes=_MAX_ATTESTATION_BYTES,
        label="Public-first attestation",
    )
    try:
        attestation = json.loads(attestation_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise _fail("Public-first attestation 无法读取。", "提供正式发布流程生成的签名原件。") from None
    if not isinstance(attestation, dict) or set(attestation) != {
        "schema_version",
        "evidence",
        "signature",
    } or attestation.get("schema_version") != "signing-public-first-attestation.v1":
        raise _fail("Public-first attestation 契约无效。", "提供正式发布流程生成的签名原件。")
    evidence = attestation["evidence"]
    signature = attestation["signature"]
    evidence_fields = {
        "schema_version",
        "ready",
        "key_id",
        "fingerprint",
        "checked_at",
        "minimum_publication_lead_days",
        "publication_lead_seconds",
        "key_window",
        "public_release",
    }
    if not isinstance(evidence, dict) or set(evidence) != evidence_fields:
        raise _fail("Public-first evidence 字段无效。", "提供正式发布流程生成的签名原件。")
    if not isinstance(signature, dict) or set(signature) != {"algorithm", "key_id", "value"}:
        raise _fail("Public-first signature 字段无效。", "提供正式发布流程生成的签名原件。")
    key_window = evidence["key_window"]
    release = evidence["public_release"]
    if not isinstance(key_window, dict) or set(key_window) != {"not_before", "not_after"}:
        raise _fail("Public-first key window 无效。", "提供正式发布流程生成的签名原件。")
    if not isinstance(release, dict) or set(release) != {
        "package",
        "version",
        "commit",
        "published_at",
        "wheel_sha256",
    }:
        raise _fail("Public-first release 字段无效。", "提供正式发布流程生成的签名原件。")
    if (
        evidence.get("schema_version") != "signing-public-first-check.v1"
        or evidence.get("ready") is not True
        or evidence.get("minimum_publication_lead_days") != _MINIMUM_PUBLICATION_LEAD.days
        or type(evidence.get("publication_lead_seconds")) is not int
        or evidence["publication_lead_seconds"] < int(_MINIMUM_PUBLICATION_LEAD.total_seconds())
        or not isinstance(evidence.get("fingerprint"), str)
        or _DIGEST.fullmatch(evidence["fingerprint"]) is None
        or release.get("package") != "lecturecast"
        or not isinstance(release.get("version"), str)
        or _VERSION.fullmatch(release["version"]) is None
        or not isinstance(release.get("commit"), str)
        or _COMMIT.fullmatch(release["commit"]) is None
        or not isinstance(release.get("wheel_sha256"), str)
        or _DIGEST.fullmatch(release["wheel_sha256"]) is None
        or signature.get("algorithm") != "Ed25519"
        or signature.get("key_id") != evidence.get("key_id")
    ):
        raise _fail("Public-first evidence 内容无效。", "提供正式发布流程生成的签名原件。")
    try:
        trusted = keyring or PublicKeyRing.load()
        trusted.validate_for_release()
    except (OSError, ValueError, json.JSONDecodeError):
        raise _fail("正式 Public keyring 无效或尚未发布。", "安装包含正式 keyring 的 Public wheel。") from None
    key_id = evidence.get("key_id")
    key = trusted.get(key_id) if isinstance(key_id, str) else None
    if key is None or key.status != "current" or not key.key_id.startswith("lecturecast-prod-"):
        raise _fail("Public-first key 不是当前 production key。", "升级到当前正式 Public release。")
    if (
        key.algorithm != "Ed25519"
        or key.not_before != key_window["not_before"]
        or key.not_after != key_window["not_after"]
        or PublicKeyRing.public_key_fingerprint(key) != evidence["fingerprint"]
    ):
        raise _fail("Public-first evidence 与 Public keyring 不一致。", "恢复同一 release 的证据与 wheel。")
    try:
        raw_signature = base64.b64decode(signature["value"], validate=True)
        public_bytes = base64.b64decode(key.public_key, validate=True)
        if len(raw_signature) != 64:
            raise ValueError
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            raw_signature,
            _attestation_signing_bytes(evidence),
        )
    except (InvalidSignature, ValueError, binascii.Error, TypeError):
        raise _fail("Public-first attestation 签名无效。", "恢复正式发布流程生成的原件。") from None
    current = checked_at or datetime.now(UTC)
    if current.tzinfo is None:
        raise _fail("Release 检查时间缺少 UTC offset。", "使用带时区的检查时间。")
    current = current.astimezone(UTC)
    attested = _parse_utc(evidence["checked_at"], label="attestation checked_at")
    published = _parse_utc(release["published_at"], label="release published_at")
    starts = _parse_utc(key_window["not_before"], label="key not_before")
    ends = _parse_utc(key_window["not_after"], label="key not_after")
    if (
        attested > current + _MAXIMUM_ATTESTATION_CLOCK_SKEW
        or published > attested
        or int((attested - published).total_seconds()) != evidence["publication_lead_seconds"]
        or current - published < _MINIMUM_PUBLICATION_LEAD
        or not starts <= attested <= ends
        or not starts <= current <= ends
    ):
        raise _fail("Public-first attestation 尚未生效或已经过期。", "等待七天窗口或更新正式 release 证据。")
    wheel_version, wheel_digest, wheel_package_digest = _wheel_package_identity(public_wheel_path)
    installed_digest = _installed_package_digest()
    installed_version = _client_version()
    if (
        wheel_version != release["version"]
        or wheel_digest != release["wheel_sha256"]
        or installed_version != wheel_version
        or installed_digest != wheel_package_digest
    ):
        raise _fail("当前 LectureCast 安装与 attested exact wheel 不一致。", "从该正式 wheel 重装后再开始 dogfood。")
    return _validate_release(
        {
            "version": wheel_version,
            "commit": release["commit"],
            "wheel_sha256": wheel_digest,
            "package_digest": wheel_package_digest,
            "attestation_digest": _file_digest(attestation_file),
            "key_id": key.key_id,
        }
    )


def _fail(message: str, next_action: str) -> LectureCastError:
    return LectureCastError(
        code="dogfood_evidence_invalid",
        message=message,
        next_action=next_action,
    )


def _stable_id(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not 3 <= len(value) <= 96
        or _STABLE_ID.fullmatch(value) is None
    ):
        raise _fail(
            f"{label} 不符合稳定 ID 协议。",
            "使用 3～96 字符的小写稳定 ID，不要写入路径、凭证或用户内容。",
        )
    return value


def _timestamp(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise _fail(f"{label} 缺少 UTC 时间。", "重新开始该 dogfood run。")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _fail(f"{label} 时间无效。", "重新开始该 dogfood run。") from None
    if parsed.tzinfo is None:
        raise _fail(f"{label} 时间缺少 UTC offset。", "重新开始该 dogfood run。")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _session_path(root: Path | str) -> Path:
    return ProjectStore(root).directory / "dogfood-session.json"


@contextmanager
def _locked(root: Path | str) -> Iterator[None]:
    directory = ProjectStore(root).directory
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".dogfood.lock"
    with lock_path.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ValueError
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise _fail(
            "Dogfood 证据文件无法安全读取。",
            "恢复可信原件，或在新项目中重新运行 dogfood；不要手工修补证据。",
        ) from None
    if not isinstance(payload, dict):
        raise _fail("Dogfood 证据必须是 JSON 对象。", "重新运行 dogfood 收集流程。")
    return payload


def _validate_output(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict) or set(item) != _OUTPUT_FIELDS:
        raise _fail("Dogfood 输出证据字段不完整。", "重新运行 capture-render。")
    _stable_id(item["output_id"], label="output_id")
    if item["kind"] not in {"video", "cover"}:
        raise _fail("Dogfood 输出类型无效。", "重新运行 capture-render。")
    if item["aspect_ratio"] not in {"16:9", "9:16", "3:4"}:
        raise _fail("Dogfood 输出比例无效。", "重新运行 capture-render。")
    if item["format"] not in {"mp4", "png"}:
        raise _fail("Dogfood 输出格式无效。", "重新运行 capture-render。")
    if not isinstance(item["filename"], str) or Path(item["filename"]).name != item["filename"]:
        raise _fail("Dogfood 输出文件名不安全。", "重新运行 capture-render。")
    if Path(item["filename"]).suffix.lower() != f".{item['format']}":
        raise _fail("Dogfood 输出扩展名与格式不一致。", "重新运行 capture-render。")
    for field in ("width", "height", "bytes"):
        if not isinstance(item[field], int) or item[field] <= 0:
            raise _fail("Dogfood 输出尺寸或大小无效。", "重新运行 capture-render。")
    if not isinstance(item["sha256"], str) or _DIGEST.fullmatch(item["sha256"]) is None:
        raise _fail("Dogfood 输出 digest 无效。", "重新运行 capture-render。")
    duration = item["duration_seconds"]
    if item["kind"] == "video":
        if not isinstance(duration, (int, float)) or duration <= 0:
            raise _fail("Dogfood 视频时长无效。", "重新运行 capture-render。")
    elif duration is not None:
        raise _fail("Dogfood 封面不应包含视频时长。", "重新运行 capture-render。")
    return dict(item)


def _validate_event(item: Any, expected_sequence: int) -> dict[str, Any]:
    if not isinstance(item, dict) or not set(item).issubset(_EVENT_FIELDS):
        raise _fail("Dogfood journal 含未知事件字段。", "重新开始该 dogfood run。")
    required = {"sequence", "event", "recorded_at", "adapter"}
    if not required.issubset(item) or item["sequence"] != expected_sequence:
        raise _fail("Dogfood journal 事件序号不连续。", "重新开始该 dogfood run。")
    if item["adapter"] not in DIRECTOR_ADAPTER_KINDS:
        raise _fail("Dogfood journal adapter 无效。", "重新开始该 dogfood run。")
    if item["event"] not in DOGFOOD_EVENTS:
        raise _fail("Dogfood journal event 无效。", "重新开始该 dogfood run。")
    _timestamp(item["recorded_at"], label="recorded_at")
    for field in ("session_id", "generation_id"):
        if field in item and item[field] is not None:
            _stable_id(item[field], label=field)
    for field in ("capability_digest", "manifest_digest"):
        if field in item and item[field] is not None:
            if not isinstance(item[field], str) or _DIGEST.fullmatch(item[field]) is None:
                raise _fail("Dogfood journal digest 无效。", "重新开始该 dogfood run。")
    if "interaction_mode" in item and item["interaction_mode"] not in DOGFOOD_INTERACTION_MODES:
        raise _fail("Dogfood interaction mode 无效。", "重新运行对应 answer。")
    if "status" in item and (
        not isinstance(item["status"], str)
        or len(item["status"]) > 64
        or re.fullmatch(r"[a-z][a-z0-9_-]*", item["status"]) is None
    ):
        raise _fail("Dogfood status 无效。", "重新运行对应 Director 命令。")
    if "signature_key_id" in item:
        _stable_id(item["signature_key_id"], label="signature_key_id")
    if "signature_key_status" in item and item["signature_key_status"] not in {
        "current",
        "previous",
        "revoked",
    }:
        raise _fail("Dogfood signature key status 无效。", "重新运行 capture-render。")
    for field in ("adapter_changed", "fresh_task"):
        if field in item and not isinstance(item[field], bool):
            raise _fail("Dogfood handoff 标记无效。", "重新运行 handoff/resume。")
    if "outputs" in item:
        if item["event"] != "render_captured" or not isinstance(item["outputs"], list):
            raise _fail("Dogfood outputs 必须是数组。", "重新运行 capture-render。")
        return {**item, "outputs": [_validate_output(output) for output in item["outputs"]]}
    return dict(item)


def _validate_session(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != _SESSION_FIELDS or payload.get("schema_version") != DOGFOOD_SESSION_SCHEMA:
        raise _fail("Dogfood session 契约不匹配。", "升级客户端并重新开始 dogfood。")
    _stable_id(payload["run_id"], label="run_id")
    _stable_id(payload["project_id"], label="project_id")
    if payload["run_kind"] not in DOGFOOD_RUN_KINDS:
        raise _fail("Dogfood run_kind 无效。", "重新开始该 dogfood run。")
    if payload["expected_adapter"] not in DIRECTOR_ADAPTER_KINDS:
        raise _fail("Dogfood expected_adapter 无效。", "重新开始该 dogfood run。")
    if payload["run_kind"] == "native_full" and payload["expected_adapter"] not in NATIVE_ADAPTERS:
        raise _fail("native_full 只能使用原生 Agent Adapter。", "选择 codex、claude-code 或 openclaw。")
    if payload["run_kind"] == "handoff" and payload["expected_adapter"] != "codex":
        raise _fail("handoff 必须从 Codex 开始。", "以 codex adapter 重新开始 handoff run。")
    if payload["run_kind"] == "text_fallback" and payload["expected_adapter"] != "text":
        raise _fail("text_fallback 必须使用 text adapter。", "以 text adapter 重新开始。")
    if not isinstance(payload["client_version"], str) or not payload["client_version"]:
        raise _fail("Dogfood client version 缺失。", "重新开始该 dogfood run。")
    release = _validate_release(payload["release"])
    if release["version"] != payload["client_version"]:
        raise _fail("Dogfood client version 与 release binding 不一致。", "重新安装正式 wheel 并开始新 run。")
    _timestamp(payload["started_at"], label="started_at")
    if not isinstance(payload["journal_revision"], int) or payload["journal_revision"] < 1:
        raise _fail("Dogfood journal revision 无效。", "重新开始该 dogfood run。")
    if not isinstance(payload["events"], list):
        raise _fail("Dogfood events 必须是数组。", "重新开始该 dogfood run。")
    events = [_validate_event(item, index) for index, item in enumerate(payload["events"], 1)]
    if payload["journal_revision"] != len(events) + 1:
        raise _fail("Dogfood journal revision 与事件数不一致。", "重新开始该 dogfood run。")
    return {**payload, "release": release, "events": events}


def begin_dogfood(
    root: Path | str,
    *,
    run_id: str,
    run_kind: str,
    expected_adapter: str,
    public_first_attestation_path: Path,
    public_wheel_path: Path,
    keyring: PublicKeyRing | None = None,
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    project = ProjectStore(root).load()
    _stable_id(run_id, label="run_id")
    release = verify_release_binding(
        public_first_attestation_path,
        public_wheel_path,
        keyring=keyring,
        checked_at=checked_at,
    )
    payload = {
        "schema_version": DOGFOOD_SESSION_SCHEMA,
        "run_id": run_id,
        "run_kind": run_kind,
        "project_id": project.payload["project_id"],
        "expected_adapter": expected_adapter,
        "client_version": _client_version(),
        "release": release,
        "started_at": _utc_now(),
        "journal_revision": 1,
        "events": [],
    }
    document = _validate_session(payload)
    path = _session_path(root)
    with _locked(root):
        if path.exists() or path.is_symlink():
            raise _fail(
                "当前项目已经有 dogfood session。",
                "完成并保留现有 receipt，或为下一次 run 使用新项目。",
            )
        atomic_write_json(path, document)
    return document


def load_dogfood_session(root: Path | str) -> dict[str, Any]:
    return _validate_session(_read_json(_session_path(root)))


def handoff_requires_fresh_task(root: Path | str) -> bool:
    path = _session_path(root)
    if not path.exists() and not path.is_symlink():
        return False
    return load_dogfood_session(root)["run_kind"] == "handoff"


def record_event_if_active(
    root: Path | str,
    event: str,
    *,
    adapter: str,
    session_id: str | None = None,
    generation_id: str | None = None,
    status: str | None = None,
    interaction_mode: str | None = None,
    adapter_changed: bool | None = None,
    fresh_task: bool | None = None,
    capability_digest: str | None = None,
    manifest_digest: str | None = None,
    signature_key_id: str | None = None,
    signature_key_status: str | None = None,
    outputs: list[dict[str, Any]] | None = None,
) -> bool:
    path = _session_path(root)
    if not path.exists() and not path.is_symlink():
        return False
    with _locked(root):
        document = _validate_session(_read_json(path))
        item: dict[str, Any] = {
            "sequence": len(document["events"]) + 1,
            "event": event,
            "recorded_at": _utc_now(),
            "adapter": adapter,
        }
        optional = {
            "session_id": session_id,
            "generation_id": generation_id,
            "status": status,
            "interaction_mode": interaction_mode,
            "adapter_changed": adapter_changed,
            "fresh_task": fresh_task,
            "capability_digest": capability_digest,
            "manifest_digest": manifest_digest,
            "signature_key_id": signature_key_id,
            "signature_key_status": signature_key_status,
            "outputs": outputs,
        }
        item.update({key: value for key, value in optional.items() if value is not None})
        document["events"].append(_validate_event(item, item["sequence"]))
        document["journal_revision"] += 1
        atomic_write_json(path, _validate_session(document))
    return True


def require_interaction_mode_if_active(
    root: Path | str,
    interaction_mode: str | None,
    *,
    adapter: str,
) -> str | None:
    if interaction_mode is not None and interaction_mode not in DOGFOOD_INTERACTION_MODES:
        raise _fail(
            "interaction mode 只能是 native_choice 或 text_fallback。",
            "用宿主原生选项卡时传 native_choice；纯文本编号回退时传 text_fallback。",
        )
    path = _session_path(root)
    if not path.exists() and not path.is_symlink():
        return interaction_mode
    session = load_dogfood_session(root)
    expected = "text_fallback" if session["run_kind"] == "text_fallback" else "native_choice"
    if interaction_mode != expected:
        raise _fail(
            "当前 dogfood answer 缺少正确的交互来源声明。",
            f"本 run 的每个 answer 都必须传 --interaction-mode {expected}。",
        )
    if session["run_kind"] in {"native_full", "text_fallback"} and adapter != session["expected_adapter"]:
        raise _fail(
            "当前 adapter 与 dogfood run 契约不一致。",
            "在原 adapter 完成本 run，跨宿主流程请使用 handoff run_kind。",
        )
    if session["run_kind"] == "handoff" and adapter not in NATIVE_ADAPTERS:
        raise _fail("handoff run 不能使用 text adapter。", "切回 Codex、Claude Code 或 OpenClaw。")
    return interaction_mode


def require_fresh_task_if_active(
    root: Path | str,
    *,
    adapter_changed: bool,
    fresh_task: bool,
) -> None:
    path = _session_path(root)
    if not path.exists() and not path.is_symlink():
        return
    session = load_dogfood_session(root)
    if adapter_changed and session["run_kind"] == "handoff" and not fresh_task:
        raise _fail(
            "Handoff dogfood 必须在新 Agent task 中恢复。",
            "从 handoff payload 新建任务，并在 director resume 时传 --fresh-task。",
        )
    if adapter_changed and session["run_kind"] != "handoff":
        raise _fail(
            "当前 dogfood run 不允许切换 adapter。",
            "在原宿主完成 native/text run，或新建 handoff run。",
        )


def _run_ffprobe(path: Path) -> dict[str, Any]:
    executable = shutil.which("ffprobe")
    if executable is None:
        raise _fail("本机缺少 ffprobe。", "安装带 ffprobe 的 ffmpeg 后重试。")
    try:
        result = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,width,height",
                "-of",
                "json",
                "--",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        raise _fail("ffprobe 无法执行。", "检查本机 ffmpeg 安装后重试。") from None
    if result.returncode != 0 or result.stderr or len(result.stdout.encode("utf-8")) > _MAX_PROBE_BYTES:
        raise _fail("ffprobe 拒绝或无法读取本地输出。", "重新渲染该输出后重试。")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise _fail("ffprobe 输出无效。", "重新渲染该输出后重试。") from None
    if not isinstance(payload, dict):
        raise _fail("ffprobe 输出契约无效。", "重新渲染该输出后重试。")
    return payload


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def capture_render_evidence(
    root: Path | str,
    output_directory: Path | str,
    *,
    keyring: PublicKeyRing | None = None,
    probe_runner: ProbeRunner | None = None,
) -> dict[str, Any]:
    store = ProjectStore(root)
    project = store.load()
    session = load_dogfood_session(root)
    if session["run_kind"] == "text_fallback":
        raise _fail("text_fallback run 不应产生付 credit 输出。", "在 Brief 阶段完成该 run。")
    for path in (store.manifest_path, store.capabilities_path):
        if path.is_symlink() or not path.is_file():
            raise _fail("Manifest 或 ClientCapabilities 缺失或不安全。", "恢复可信项目文件后重试。")
    manifest = load_manifest(store.manifest_path)
    try:
        capabilities = ClientCapabilities.model_validate_json(
            store.capabilities_path.read_text(encoding="utf-8")
        )
    except Exception:
        raise _fail("ClientCapabilities 无法读取。", "重新采集当前宿主能力后重试。") from None
    preflight = run_preflight(manifest, capabilities, keyring=keyring, project_root=store.root)
    payload = manifest.model_dump()
    output_contract = {
        (item["kind"], item["aspect_ratio"], item["width"], item["height"], item["format"])
        for item in payload["outputs"]
    }
    if len(payload["outputs"]) != 4 or output_contract != REQUIRED_OUTPUTS:
        raise _fail("Manifest 不是精确的双视频双封面契约。", "不要继续 dogfood，重新生成 Manifest。")
    raw_output_root = Path(output_directory).expanduser()
    if raw_output_root.is_symlink() or not raw_output_root.is_dir():
        raise _fail("本地输出目录不存在或不安全。", "完成本地渲染后提供真实 output 目录。")
    output_root = raw_output_root.resolve(strict=True)
    expected_duration = payload["total_frames"] / payload["fps"]
    probe = probe_runner or _run_ffprobe
    outputs: list[dict[str, Any]] = []
    for item in payload["outputs"]:
        candidate = output_root / item["filename"]
        try:
            file_stat = os.lstat(candidate)
            resolved = candidate.resolve(strict=True)
        except OSError:
            raise _fail("Dogfood 本地输出缺失。", "重新渲染四个输出后重试。") from None
        if (
            stat.S_ISLNK(file_stat.st_mode)
            or not stat.S_ISREG(file_stat.st_mode)
            or resolved.parent != output_root
            or file_stat.st_size <= 0
            or file_stat.st_size > _MAX_OUTPUT_BYTES
        ):
            raise _fail("Dogfood 本地输出不是安全的常规文件。", "移除链接/越界文件并重新渲染。")
        metadata = probe(resolved)
        streams = metadata.get("streams")
        if not isinstance(streams, list):
            raise _fail("Dogfood 输出没有可验证的视频流。", "重新渲染该输出。")
        stream = next(
            (entry for entry in streams if isinstance(entry, dict) and entry.get("codec_type") == "video"),
            None,
        )
        if stream is None or (stream.get("width"), stream.get("height")) != (
            item["width"],
            item["height"],
        ):
            raise _fail("Dogfood 输出尺寸与 Manifest 不一致。", "重新渲染该输出。")
        duration: float | None = None
        if item["kind"] == "video":
            try:
                duration = float(metadata["format"]["duration"])
            except (KeyError, TypeError, ValueError):
                raise _fail("Dogfood 视频时长无法验证。", "重新渲染该视频。") from None
            if abs(duration - expected_duration) > 1.0:
                raise _fail("Dogfood 视频时长与 Manifest 不一致。", "重新渲染该视频。")
        outputs.append(
            {
                "output_id": item["output_id"],
                "kind": item["kind"],
                "aspect_ratio": item["aspect_ratio"],
                "width": item["width"],
                "height": item["height"],
                "format": item["format"],
                "filename": item["filename"],
                "bytes": file_stat.st_size,
                "sha256": _file_digest(resolved),
                "duration_seconds": duration,
            }
        )
    state = DirectorStateStore(root).load()
    verification = preflight.verification
    record_event_if_active(
        root,
        "render_captured",
        adapter=str(state.payload["adapter_kind"]),
        session_id=state.session_id,
        generation_id=state.generation_id,
        status=str(project.payload["status"]),
        capability_digest=str(project.payload["capability_digest"]),
        manifest_digest=str(project.payload["production_manifest_digest"]),
        signature_key_id=verification.key_id,
        signature_key_status=verification.key_status,
        outputs=outputs,
    )
    return {
        "captured": True,
        "generation_id": state.generation_id,
        "manifest_digest": project.payload["production_manifest_digest"],
        "output_count": len(outputs),
        "signature_key_id": verification.key_id,
        "signature_key_status": verification.key_status,
    }


def _ordered_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _event_index(events: list[dict[str, Any]], name: str, *, ready: bool = False) -> int:
    for index, item in enumerate(events):
        if item["event"] == name and (not ready or item.get("status") == "ready"):
            return index
    return -1


def _full_run_render(events: list[dict[str, Any]]) -> dict[str, Any]:
    positions = [
        _event_index(events, "director_start"),
        _event_index(events, "decision_answer"),
        _event_index(events, "brief_confirm"),
        _event_index(events, "generation_request"),
        _event_index(events, "generation_status", ready=True),
        _event_index(events, "render_captured"),
    ]
    if any(index < 0 for index in positions) or positions != sorted(positions):
        raise _fail(
            "Dogfood 完整流程事件缺失或顺序错误。",
            "按 start→answer→confirm→generate→ready→本地渲染重新执行。",
        )
    render = events[positions[-1]]
    outputs = render.get("outputs", [])
    output_contract = {
        (item["kind"], item["aspect_ratio"], item["width"], item["height"], item["format"])
        for item in outputs
    }
    if len(outputs) != 4 or output_contract != REQUIRED_OUTPUTS:
        raise _fail("Dogfood receipt 缺少精确四输出。", "重新运行 capture-render。")
    if (
        render.get("signature_key_status") != "current"
        or not str(render.get("signature_key_id", "")).startswith("lecturecast-prod-")
    ):
        raise _fail(
            "Dogfood 未使用 current production signing key。",
            "安装正式 Public keyring 并使用 production Manifest 重新运行。",
        )
    return render


def build_dogfood_receipt(root: Path | str, *, completed_at: str | None = None) -> dict[str, Any]:
    session = load_dogfood_session(root)
    events = session["events"]
    if not events:
        raise _fail("Dogfood session 尚无事件。", "先执行对应 Director 流程。")
    adapters = _ordered_unique([str(item["adapter"]) for item in events])
    interactions = _ordered_unique(
        [str(item["interaction_mode"]) for item in events if "interaction_mode" in item]
    )
    answers = [item for item in events if item["event"] == "decision_answer"]
    run_kind = session["run_kind"]
    render: dict[str, Any] | None = None
    if run_kind == "native_full":
        if adapters != [session["expected_adapter"]] or not answers or any(
            item.get("interaction_mode") != "native_choice" for item in answers
        ):
            raise _fail(
                "原生宿主 dogfood 没有全程使用同一 native choice adapter。",
                "在该宿主用原生选项卡重新完成所有 answer。",
            )
        render = _full_run_render(events)
    elif run_kind == "handoff":
        if adapters != list(HANDOFF_ADAPTER_ORDER) or not answers or any(
            item.get("interaction_mode") != "native_choice" for item in answers
        ):
            raise _fail(
                "Handoff dogfood 没有按 Codex→Claude Code→OpenClaw 使用原生交互。",
                "从 Codex 开始，并按固定顺序在新任务中接力。",
            )
        changed_resumes = [
            item
            for item in events
            if item["event"] == "director_resume" and item.get("adapter_changed") is True
        ]
        if [item["adapter"] for item in changed_resumes] != ["claude-code", "openclaw"] or any(
            item.get("fresh_task") is not True for item in changed_resumes
        ):
            raise _fail(
                "Handoff dogfood 缺少两个新任务 resume 证据。",
                "每次 handoff 后在新任务中运行 director resume --fresh-task。",
            )
        adapter_positions = [HANDOFF_ADAPTER_ORDER.index(str(item["adapter"])) for item in events]
        if adapter_positions != sorted(adapter_positions):
            raise _fail(
                "Handoff 事件没有按固定宿主顺序推进。",
                "按 Codex→Claude Code→OpenClaw 单向接力，不要回到旧宿主。",
            )
        if sum(item["event"] == "director_handoff" for item in events) != 2:
            raise _fail("Handoff payload 证据不是精确两次。", "每次切换宿主前运行一次 director handoff。")
        render = _full_run_render(events)
        brief_confirm = next(item for item in events if item["event"] == "brief_confirm")
        generation_request = next(item for item in events if item["event"] == "generation_request")
        generation_ready = next(
            item
            for item in events
            if item["event"] == "generation_status" and item.get("status") == "ready"
        )
        if brief_confirm.get("adapter") != "claude-code" or any(
            item.get("adapter") != "openclaw"
            for item in (generation_request, generation_ready, render)
        ):
            raise _fail(
                "Handoff 各阶段未绑定约定宿主。",
                "在 Claude Code 确认 Brief，并在 OpenClaw 生成、等待 ready 和渲染。",
            )
    else:
        if adapters != ["text"] or not answers or any(
            item.get("interaction_mode") != "text_fallback" for item in answers
        ):
            raise _fail("文本回退 dogfood 没有使用 text_fallback。", "用编号文本选项重新完成 answer。")
        if any(
            item["event"]
            in {"brief_confirm", "generation_request", "generation_status", "render_captured"}
            for item in events
        ):
            raise _fail(
                "文本回退 run 不应确认 Brief、扣 credit 或渲染。",
                "新建项目，仅验证 Brief 确认前的文本回退。",
            )
        start = _event_index(events, "director_start")
        answer = _event_index(events, "decision_answer")
        if start < 0 or answer <= start:
            raise _fail("文本回退流程不完整。", "按 start→文本 answer 重新执行。")

    start_event = next(item for item in events if item["event"] == "director_start")
    event_counts = dict(sorted(Counter(str(item["event"]) for item in events).items()))
    receipt = {
        "schema_version": DOGFOOD_RECEIPT_SCHEMA,
        "run_id": session["run_id"],
        "run_kind": run_kind,
        "project_id": session["project_id"],
        "expected_adapter": session["expected_adapter"],
        "client_version": session["client_version"],
        "release": session["release"],
        "started_at": session["started_at"],
        "completed_at": _timestamp(completed_at or _utc_now(), label="completed_at"),
        "adapters_seen": adapters,
        "interaction_modes_seen": interactions,
        "event_counts": event_counts,
        "session_id": start_event.get("session_id"),
        "generation_id": render.get("generation_id") if render else None,
        "capability_digest": render.get("capability_digest") if render else None,
        "manifest_digest": render.get("manifest_digest") if render else None,
        "signature_key_id": render.get("signature_key_id") if render else None,
        "signature_key_status": render.get("signature_key_status") if render else None,
        "outputs": render.get("outputs", []) if render else [],
        "journal_digest": _digest(session),
    }
    return _validate_receipt(receipt)


def _validate_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != _RECEIPT_FIELDS or payload.get("schema_version") != DOGFOOD_RECEIPT_SCHEMA:
        raise _fail("Dogfood receipt 契约不匹配。", "重新运行 dogfood finish。")
    _stable_id(payload["run_id"], label="run_id")
    _stable_id(payload["project_id"], label="project_id")
    if payload["run_kind"] not in DOGFOOD_RUN_KINDS:
        raise _fail("Dogfood receipt run_kind 无效。", "重新运行 dogfood finish。")
    if payload["expected_adapter"] not in DIRECTOR_ADAPTER_KINDS:
        raise _fail("Dogfood receipt adapter 无效。", "重新运行 dogfood finish。")
    if not isinstance(payload["client_version"], str) or not payload["client_version"]:
        raise _fail("Dogfood receipt client version 缺失。", "重新运行 dogfood finish。")
    release = _validate_release(payload["release"])
    if release["version"] != payload["client_version"]:
        raise _fail("Dogfood receipt release binding 无效。", "重新运行 dogfood finish。")
    _timestamp(payload["started_at"], label="started_at")
    _timestamp(payload["completed_at"], label="completed_at")
    if not isinstance(payload["adapters_seen"], list) or any(
        adapter not in DIRECTOR_ADAPTER_KINDS for adapter in payload["adapters_seen"]
    ):
        raise _fail("Dogfood receipt adapters_seen 无效。", "重新运行 dogfood finish。")
    if not isinstance(payload["interaction_modes_seen"], list) or any(
        mode not in DOGFOOD_INTERACTION_MODES for mode in payload["interaction_modes_seen"]
    ):
        raise _fail("Dogfood receipt interaction modes 无效。", "重新运行 dogfood finish。")
    if not isinstance(payload["event_counts"], dict) or any(
        key not in DOGFOOD_EVENTS or not isinstance(value, int) or value < 1
        for key, value in payload["event_counts"].items()
    ):
        raise _fail("Dogfood receipt event_counts 无效。", "重新运行 dogfood finish。")
    for field in ("session_id", "generation_id"):
        if payload[field] is not None:
            _stable_id(payload[field], label=field)
    for field in ("capability_digest", "manifest_digest", "journal_digest"):
        value = payload[field]
        if value is not None and (not isinstance(value, str) or _DIGEST.fullmatch(value) is None):
            raise _fail("Dogfood receipt digest 无效。", "重新运行 dogfood finish。")
    if not isinstance(payload["outputs"], list):
        raise _fail("Dogfood receipt outputs 无效。", "重新运行 dogfood finish。")
    outputs = [_validate_output(item) for item in payload["outputs"]]
    if len({item["output_id"] for item in outputs}) != len(outputs) or len(
        {item["filename"] for item in outputs}
    ) != len(outputs):
        raise _fail("Dogfood receipt 输出不唯一。", "重新运行 dogfood finish。")
    run_kind = payload["run_kind"]
    if run_kind == "native_full" and (
        payload["expected_adapter"] not in NATIVE_ADAPTERS
        or payload["adapters_seen"] != [payload["expected_adapter"]]
        or payload["interaction_modes_seen"] != ["native_choice"]
        or any(payload["event_counts"].get(event, 0) < 1 for event in FULL_RUN_EVENTS)
    ):
        raise _fail("Dogfood native receipt 摘要无效。", "从原始 session 重新运行 finish。")
    if run_kind == "handoff" and (
        payload["expected_adapter"] != "codex"
        or payload["adapters_seen"] != list(HANDOFF_ADAPTER_ORDER)
        or payload["interaction_modes_seen"] != ["native_choice"]
        or payload["event_counts"].get("director_handoff") != 2
        or payload["event_counts"].get("director_resume") != 2
        or any(payload["event_counts"].get(event, 0) < 1 for event in FULL_RUN_EVENTS)
    ):
        raise _fail("Dogfood handoff receipt 摘要无效。", "从原始 session 重新运行 finish。")
    if run_kind == "text_fallback" and (
        payload["expected_adapter"] != "text"
        or payload["adapters_seen"] != ["text"]
        or payload["interaction_modes_seen"] != ["text_fallback"]
        or any(
            payload[field] is not None
            for field in (
                "generation_id",
                "capability_digest",
                "manifest_digest",
                "signature_key_id",
                "signature_key_status",
            )
        )
        or outputs
        or payload["event_counts"].get("director_start", 0) < 1
        or payload["event_counts"].get("decision_answer", 0) < 1
        or any(
            payload["event_counts"].get(event, 0) > 0
            for event in (
                "brief_confirm",
                "generation_request",
                "generation_status",
                "render_captured",
            )
        )
    ):
        raise _fail("Dogfood text receipt 摘要无效。", "从原始 session 重新运行 finish。")
    if payload["signature_key_id"] is not None:
        _stable_id(payload["signature_key_id"], label="signature_key_id")
    if payload["signature_key_status"] not in {None, "current", "previous", "revoked"}:
        raise _fail("Dogfood receipt signature 状态无效。", "重新运行 dogfood finish。")
    return {**payload, "release": release, "outputs": outputs}


def _exclusive_json(path: Path, payload: Mapping[str, Any]) -> None:
    raw_path = path.expanduser()
    if not raw_path.parent.is_dir() or raw_path.parent.is_symlink():
        raise _fail("证据输出目录不存在或不安全。", "选择受保护的现有目录。")
    path = raw_path.resolve()
    raw = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        raise _fail("证据输出文件已存在或无法创建。", "使用新的输出文件名；不要覆盖旧证据。") from None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        path.unlink(missing_ok=True)
        raise _fail("证据输出写入失败。", "检查受保护目录后使用新文件名重试。") from None


def write_dogfood_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    _exclusive_json(path, _validate_receipt(dict(receipt)))


def read_dogfood_receipt(path: Path) -> dict[str, Any]:
    return _validate_receipt(_read_json(path))


def _check(check_id: str, passed: bool, message: str, next_action: str) -> dict[str, Any]:
    return {
        "id": check_id,
        "status": "passed" if passed else "failed",
        "message": message,
        "next_action": None if passed else next_action,
    }


def evaluate_dogfood_gate(
    receipts: list[dict[str, Any]],
    *,
    checked_at: str | None = None,
) -> dict[str, Any]:
    documents = [_validate_receipt(dict(receipt)) for receipt in receipts]
    native = [item for item in documents if item["run_kind"] == "native_full"]
    handoff = [item for item in documents if item["run_kind"] == "handoff"]
    text = [item for item in documents if item["run_kind"] == "text_fallback"]
    native_adapters = {item["expected_adapter"] for item in native}
    paid = native + handoff
    identifiers = [(item["run_id"], item["project_id"]) for item in documents]
    generation_ids = [item["generation_id"] for item in paid]
    manifest_digests = [item["manifest_digest"] for item in paid]
    client_versions = {item["client_version"] for item in documents}
    releases = {_canonical(item["release"]) for item in documents}
    signing_keys = {item["signature_key_id"] for item in paid}
    output_contracts = [
        {
            (entry["kind"], entry["aspect_ratio"], entry["width"], entry["height"], entry["format"])
            for entry in item["outputs"]
        }
        for item in paid
    ]
    checks = [
        _check(
            "matrix.native_hosts",
            len(native) == 3 and native_adapters == NATIVE_ADAPTERS,
            "Codex、Claude Code、OpenClaw 各有一份完整 native receipt。",
            "分别在三个真实宿主用 native_choice 完成完整流程。",
        ),
        _check(
            "matrix.handoff",
            len(handoff) == 1 and handoff[0]["adapters_seen"] == list(HANDOFF_ADAPTER_ORDER),
            "同一项目完成 Codex→Claude Code→OpenClaw 新任务接力。",
            "按固定顺序重新执行 handoff，并在每个新任务 director resume --fresh-task。",
        ),
        _check(
            "matrix.text_fallback",
            len(text) == 1
            and text[0]["adapters_seen"] == ["text"]
            and text[0]["interaction_modes_seen"] == ["text_fallback"]
            and text[0]["generation_id"] is None,
            "无原生 UI 的纯文本回退在扣 credit 前通过。",
            "使用 text adapter 与 text_fallback 完成一个不生成 Manifest 的流程。",
        ),
        _check(
            "matrix.unique_runs",
            len(documents) == 5
            and len({run_id for run_id, _ in identifiers}) == 5
            and len({project_id for _, project_id in identifiers}) == 5
            and None not in generation_ids
            and len(set(generation_ids)) == 4
            and None not in manifest_digests
            and len(set(manifest_digests)) == 4,
            "五个 run/project 独立，四个付 credit generation/Manifest 均唯一。",
            "不要复用项目、generation 或 receipt；按矩阵重新运行。",
        ),
        _check(
            "release.client_binding",
            len(client_versions) == 1
            and len(releases) == 1
            and len(signing_keys) == 1
            and all(item["release"]["key_id"] == item["signature_key_id"] for item in paid)
            and all(item["signature_key_status"] == "current" for item in paid)
            and all(str(item["signature_key_id"]).startswith("lecturecast-prod-") for item in paid),
            "所有 receipt 使用同一 attested exact Public wheel 和 current production signing key。",
            "从同一正式 Public wheel 重装，并用其 current production Manifest 重跑。",
        ),
        _check(
            "outputs.local_four",
            len(paid) == 4
            and all(len(item["outputs"]) == 4 for item in paid)
            and all(contract == REQUIRED_OUTPUTS for contract in output_contracts),
            "四个付 credit run 都在本机产出双视频与双封面。",
            "在每个付 credit 项目完成本地渲染并重新 capture-render。",
        ),
    ]
    safe_receipts = [
        {
            "run_id": item["run_id"],
            "run_kind": item["run_kind"],
            "project_id": item["project_id"],
            "expected_adapter": item["expected_adapter"],
            "release": item["release"],
            "journal_digest": item["journal_digest"],
            "manifest_digest": item["manifest_digest"],
        }
        for item in sorted(documents, key=lambda value: value["run_id"])
    ]
    return {
        "schema_version": DOGFOOD_GATE_SCHEMA,
        "ready": all(item["status"] == "passed" for item in checks),
        "checked_at": _timestamp(checked_at or _utc_now(), label="checked_at"),
        "client_version": next(iter(client_versions)) if len(client_versions) == 1 else None,
        "release": documents[0]["release"] if len(releases) == 1 and documents else None,
        "signing_key_id": next(iter(signing_keys)) if len(signing_keys) == 1 else None,
        "receipt_count": len(documents),
        "evidence_digest": _digest({"receipts": safe_receipts}),
        "checks": checks,
    }


def write_dogfood_gate(path: Path, report: Mapping[str, Any]) -> None:
    _exclusive_json(path, report)
