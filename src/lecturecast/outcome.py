from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any, Iterator, Mapping

from .errors import LectureCastError
from .file_lock import exclusive_file_lock
from .manifest import load_manifest
from .project import ProjectStore, atomic_write_json


LOCAL_OUTCOME_RECEIPT_SCHEMA = "local-outcome-receipt.v1"
ANONYMOUS_OUTCOME_REPORT_SCHEMA = "anonymous-outcome-report.v1"
OUTCOME_AGGREGATE_SCHEMA = "anonymous-outcome-aggregate.v1"
SHARE_CONSENT = "share-anonymous-outcome"
MINIMUM_AGGREGATE_REPORTS = 3

RENDER_STATUSES = ("completed", "partial", "failed", "not_attempted")
ADOPTION_STATUSES = ("published", "exported", "discarded", "undecided")
FAILURE_REASONS = (
    "asset_missing",
    "voice_failed",
    "subtitle_failed",
    "render_failed",
    "quality_rejected",
    "local_runtime",
    "source_not_ready",
    "workflow_abandoned",
)

_RECEIPT_FIELDS = {
    "schema_version",
    "receipt_id",
    "receipt_revision",
    "project_id",
    "manifest_digest",
    "manifest_key_id",
    "client_version",
    "render_status",
    "adoption_status",
    "failure_reason",
    "created_at",
    "updated_at",
    "shareable",
}
_REPORT_FIELDS = {
    "schema_version",
    "report_id",
    "evidence_level",
    "render_status",
    "adoption_status",
    "failure_reason",
    "client_version",
    "consent",
    "privacy",
}
_REPORT_PRIVACY = {
    "contains_identity": False,
    "contains_cloud_identifiers": False,
    "contains_digests": False,
    "contains_media_or_source": False,
    "contains_paths": False,
    "network_sent": False,
}
_AGGREGATE_FIELDS = {
    "schema_version",
    "ready",
    "report_count",
    "render_status_counts",
    "adoption_status_counts",
    "failure_reason_counts",
    "render_completed_rate",
    "adoption_published_rate",
    "privacy",
}
_AGGREGATE_PRIVACY = {
    "contains_individual_report_ids": False,
    "minimum_cohort_size": MINIMUM_AGGREGATE_REPORTS,
    "network_sent": False,
}
_RECEIPT_ID = re.compile(r"outcome_[0-9a-f]{32}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_KEY_ID = re.compile(r"(?=.{1,128}\Z)[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*")
_CLIENT_VERSION = re.compile(
    r"(?=.{1,64}\Z)[0-9]+(?:\.[0-9A-Za-z]+)+(?:[._+\-][0-9A-Za-z]+)*"
)
_MAX_DOCUMENT_BYTES = 32 * 1024


def _fail(message: str, next_action: str) -> LectureCastError:
    return LectureCastError(
        code="outcome_evidence_invalid",
        message=message,
        next_action=next_action,
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _validate_utc(value: Any, *, label: str) -> None:
    if not isinstance(value, str):
        raise _fail(f"{label} 必须是 UTC 时间。", "重新生成本地 outcome receipt。")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _fail(f"{label} 不是有效时间。", "重新生成本地 outcome receipt。") from None
    if parsed.tzinfo is None:
        raise _fail(f"{label} 缺少 UTC offset。", "重新生成本地 outcome receipt。")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _installed_version() -> str:
    try:
        value = package_version("lecturecast")
    except PackageNotFoundError:
        value = "source"
    if not _CLIENT_VERSION.fullmatch(value):
        raise _fail("LectureCast 客户端版本无法安全写入回执。", "从可信 wheel 重新安装后重试。")
    return value


def _validate_choices(
    render_status: Any,
    adoption_status: Any,
    failure_reason: Any,
) -> None:
    if render_status not in RENDER_STATUSES:
        raise _fail("render_status 不在允许枚举中。", "从 CLI help 选择一个稳定状态值。")
    if adoption_status not in ADOPTION_STATUSES:
        raise _fail("adoption_status 不在允许枚举中。", "从 CLI help 选择一个稳定状态值。")
    if failure_reason is not None and failure_reason not in FAILURE_REASONS:
        raise _fail("failure_reason 不在允许枚举中。", "从 CLI help 选择一个稳定原因值。")
    if render_status == "completed" and failure_reason is not None:
        raise _fail("已完成渲染不能同时记录失败原因。", "清空 failure_reason 后重试。")
    if render_status in {"partial", "failed"} and failure_reason is None:
        raise _fail("部分完成或失败必须选择失败原因。", "补充一个 bounded failure_reason。")
    if adoption_status in {"published", "exported"} and render_status != "completed":
        raise _fail("发布或导出要求渲染状态为 completed。", "按实际结果修正两个状态。")


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise _fail(f"{label} 必须是普通文件。", "移除符号链接或特殊文件后重试。")
        if metadata.st_size > _MAX_DOCUMENT_BYTES:
            raise _fail(f"{label} 超过 32 KiB。", "不要在 outcome 证据中加入自由文本或媒体。")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o022:
            raise _fail(f"{label} 权限过宽。", "移除 group/world 写权限后重试。")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except LectureCastError:
        raise
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise _fail(f"{label} 无法安全读取。", "恢复可信 JSON 文件后重试。") from None
    if not isinstance(payload, dict):
        raise _fail(f"{label} 必须是 JSON 对象。", "重新生成该文件。")
    return payload


def _write_exclusive(path: Path | str, payload: Mapping[str, Any], *, label: str) -> None:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(destination, flags, 0o600)
    except FileExistsError:
        raise _fail(f"{label} 已存在，拒绝覆盖。", "选择一个新的输出文件名。") from None
    except OSError:
        raise _fail(f"{label} 无法创建。", "选择本机可写且非符号链接的输出位置。") from None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(
                (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
            )
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            destination.unlink()
        except OSError:
            pass
        raise


def _validate_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    if set(payload) != _RECEIPT_FIELDS:
        raise _fail("本地 outcome receipt 字段不匹配。", "不要手工编辑；重新运行 outcome record。")
    if payload.get("schema_version") != LOCAL_OUTCOME_RECEIPT_SCHEMA:
        raise _fail("本地 outcome receipt 版本不受支持。", "升级客户端并重新生成。")
    if not isinstance(payload.get("receipt_id"), str) or not _RECEIPT_ID.fullmatch(
        payload["receipt_id"]
    ):
        raise _fail("本地 outcome receipt ID 无效。", "重新生成本地 receipt。")
    if not isinstance(payload.get("receipt_revision"), int) or payload["receipt_revision"] < 1:
        raise _fail("本地 outcome receipt revision 无效。", "重新生成本地 receipt。")
    if not isinstance(payload.get("project_id"), str) or not payload["project_id"]:
        raise _fail("本地 outcome receipt 缺少 project binding。", "从有效项目重新记录。")
    if not isinstance(payload.get("manifest_digest"), str) or not _DIGEST.fullmatch(
        payload["manifest_digest"]
    ):
        raise _fail("本地 outcome receipt 缺少 Manifest binding。", "从有效项目重新记录。")
    if not isinstance(payload.get("manifest_key_id"), str) or not _KEY_ID.fullmatch(
        payload["manifest_key_id"]
    ):
        raise _fail("本地 outcome receipt signing key 无效。", "重新验证 Manifest 后记录。")
    if not isinstance(payload.get("client_version"), str) or not _CLIENT_VERSION.fullmatch(
        payload["client_version"]
    ):
        raise _fail("本地 outcome receipt client version 无效。", "从可信客户端重新记录。")
    _validate_choices(
        payload.get("render_status"),
        payload.get("adoption_status"),
        payload.get("failure_reason"),
    )
    _validate_utc(payload.get("created_at"), label="created_at")
    _validate_utc(payload.get("updated_at"), label="updated_at")
    if _parse_utc(payload["created_at"]) > _parse_utc(payload["updated_at"]):
        raise _fail("outcome receipt 时间顺序无效。", "重新生成本地 receipt。")
    if payload.get("shareable") is not False:
        raise _fail("本地 outcome receipt 必须标记为不可分享。", "重新生成本地 receipt。")
    return dict(payload)


class OutcomeStore:
    def __init__(self, root: Path | str) -> None:
        self.project = ProjectStore(root)
        self.path = self.project.directory / "outcome-receipt.json"
        self.lock_path = self.project.directory / ".outcome.lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with exclusive_file_lock(self.lock_path):
            yield

    def _project_binding(self) -> tuple[str, str, str]:
        state = self.project.load().payload
        manifest_digest = state.get("production_manifest_digest")
        if not isinstance(manifest_digest, str):
            raise _fail(
                "当前项目还没有经过验证的 ProductionManifest。",
                "先完成 Director generation、签名验证和本地保存。",
            )
        manifest = load_manifest(self.project.manifest_path)
        return state["project_id"], manifest_digest, manifest.payload["signature"]["key_id"]

    def _load_unlocked(self) -> dict[str, Any]:
        try:
            return _validate_receipt(_read_object(self.path, label="本地 outcome receipt"))
        except FileNotFoundError:
            raise _fail(
                "当前项目没有本地 outcome receipt。",
                "用户明确选择结果后运行 lecturecast outcome record。",
            ) from None

    def load(self) -> dict[str, Any]:
        with self._locked():
            project_id, manifest_digest, manifest_key_id = self._project_binding()
            receipt = self._load_unlocked()
            if (
                receipt["project_id"] != project_id
                or receipt["manifest_digest"] != manifest_digest
                or receipt["manifest_key_id"] != manifest_key_id
            ):
                raise _fail(
                    "本地 outcome receipt 与当前项目或 Manifest 不匹配。",
                    "不要复制 receipt；请为当前项目重新记录。",
                )
            return receipt

    def record(
        self,
        *,
        render_status: str,
        adoption_status: str,
        failure_reason: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        _validate_choices(render_status, adoption_status, failure_reason)
        with self._locked():
            project_id, manifest_digest, manifest_key_id = self._project_binding()
            if self.path.exists():
                current = self._load_unlocked()
                if expected_revision is None or current["receipt_revision"] != expected_revision:
                    raise LectureCastError(
                        code="project_revision_conflict",
                        message="本地 outcome receipt 已存在或已由另一个 Agent 更新。",
                        next_action="运行 outcome status，并用最新 --expected-revision 重试。",
                        retryable=True,
                    )
                if (
                    current["project_id"] != project_id
                    or current["manifest_digest"] != manifest_digest
                    or current["manifest_key_id"] != manifest_key_id
                ):
                    raise _fail(
                        "现有 outcome receipt 与当前项目或 Manifest 不匹配。",
                        "保留旧项目证据，并为当前项目重新建立独立目录。",
                    )
                if (
                    current["render_status"],
                    current["adoption_status"],
                    current["failure_reason"],
                ) == (render_status, adoption_status, failure_reason):
                    return current
                now = _utc_now()
                receipt = {
                    **current,
                    "receipt_revision": current["receipt_revision"] + 1,
                    "render_status": render_status,
                    "adoption_status": adoption_status,
                    "failure_reason": failure_reason,
                    "updated_at": now,
                }
            else:
                if expected_revision is not None:
                    raise LectureCastError(
                        code="project_revision_conflict",
                        message="当前项目还没有可更新的 outcome receipt。",
                        next_action="首次记录时移除 --expected-revision。",
                        retryable=True,
                    )
                now = _utc_now()
                receipt = {
                    "schema_version": LOCAL_OUTCOME_RECEIPT_SCHEMA,
                    "receipt_id": f"outcome_{uuid.uuid4().hex}",
                    "receipt_revision": 1,
                    "project_id": project_id,
                    "manifest_digest": manifest_digest,
                    "manifest_key_id": manifest_key_id,
                    "client_version": _installed_version(),
                    "render_status": render_status,
                    "adoption_status": adoption_status,
                    "failure_reason": failure_reason,
                    "created_at": now,
                    "updated_at": now,
                    "shareable": False,
                }
            validated = _validate_receipt(receipt)
            atomic_write_json(self.path, validated, mode=0o600)
            return validated


def build_anonymous_report(
    receipt: Mapping[str, Any],
    *,
    consent: str,
) -> dict[str, Any]:
    if consent != SHARE_CONSENT:
        raise _fail(
            "匿名 outcome report 需要用户明确同意。",
            f"用户确认后传入 --consent {SHARE_CONSENT}；不要代替用户同意。",
        )
    validated = _validate_receipt(receipt)
    report = {
        "schema_version": ANONYMOUS_OUTCOME_REPORT_SCHEMA,
        "report_id": validated["receipt_id"],
        "evidence_level": "user_confirmed",
        "render_status": validated["render_status"],
        "adoption_status": validated["adoption_status"],
        "failure_reason": validated["failure_reason"],
        "client_version": validated["client_version"],
        "consent": "manual_anonymous_export",
        "privacy": dict(_REPORT_PRIVACY),
    }
    return _validate_report(report)


def _validate_report(payload: Mapping[str, Any]) -> dict[str, Any]:
    if set(payload) != _REPORT_FIELDS:
        raise _fail("匿名 outcome report 字段不匹配。", "拒绝该文件并从可信客户端重新导出。")
    if payload.get("schema_version") != ANONYMOUS_OUTCOME_REPORT_SCHEMA:
        raise _fail("匿名 outcome report 版本不受支持。", "升级客户端后重新导出。")
    if not isinstance(payload.get("report_id"), str) or not _RECEIPT_ID.fullmatch(
        payload["report_id"]
    ):
        raise _fail("匿名 outcome report ID 无效。", "从可信本地 receipt 重新导出。")
    if payload.get("evidence_level") != "user_confirmed":
        raise _fail("匿名 outcome report evidence level 无效。", "不要推断或自动生成用户结果。")
    _validate_choices(
        payload.get("render_status"),
        payload.get("adoption_status"),
        payload.get("failure_reason"),
    )
    if not isinstance(payload.get("client_version"), str) or not _CLIENT_VERSION.fullmatch(
        payload["client_version"]
    ):
        raise _fail("匿名 outcome report client version 无效。", "从可信客户端重新导出。")
    if payload.get("consent") != "manual_anonymous_export":
        raise _fail("匿名 outcome report 缺少 consent 声明。", "由用户明确同意后重新导出。")
    if payload.get("privacy") != _REPORT_PRIVACY:
        raise _fail("匿名 outcome report 隐私声明不匹配。", "拒绝额外字段并重新导出。")
    return dict(payload)


def write_anonymous_report(path: Path | str, report: Mapping[str, Any]) -> None:
    _write_exclusive(path, _validate_report(report), label="匿名 outcome report")


def read_anonymous_report(path: Path | str) -> dict[str, Any]:
    try:
        payload = _read_object(Path(path).expanduser(), label="匿名 outcome report")
    except FileNotFoundError:
        raise _fail("匿名 outcome report 不存在。", "提供一个已导出的 report 文件。") from None
    return _validate_report(payload)


def aggregate_outcome_reports(reports: list[Mapping[str, Any]]) -> dict[str, Any]:
    validated = [_validate_report(report) for report in reports]
    if len(validated) < MINIMUM_AGGREGATE_REPORTS:
        raise _fail(
            "匿名 outcome 聚合至少三份独立 report。",
            "继续收集显式同意的 report；不要为凑数量复制文件。",
        )
    report_ids = [report["report_id"] for report in validated]
    if len(set(report_ids)) != len(report_ids):
        raise _fail("匿名 outcome report ID 重复。", "移除重复文件后重新聚合。")
    render_counts = Counter(report["render_status"] for report in validated)
    adoption_counts = Counter(report["adoption_status"] for report in validated)
    failure_counts = Counter(
        report["failure_reason"]
        for report in validated
        if report["failure_reason"] is not None
    )
    total = len(validated)
    aggregate = {
        "schema_version": OUTCOME_AGGREGATE_SCHEMA,
        "ready": True,
        "report_count": total,
        "render_status_counts": {status: render_counts[status] for status in RENDER_STATUSES},
        "adoption_status_counts": {
            status: adoption_counts[status] for status in ADOPTION_STATUSES
        },
        "failure_reason_counts": {
            reason: failure_counts[reason] for reason in FAILURE_REASONS
        },
        "render_completed_rate": round(render_counts["completed"] / total, 6),
        "adoption_published_rate": round(adoption_counts["published"] / total, 6),
        "privacy": dict(_AGGREGATE_PRIVACY),
    }
    return _validate_aggregate(aggregate)


def _validate_count_map(value: Any, *, keys: tuple[str, ...], label: str) -> None:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise _fail(f"{label} 字段不匹配。", "重新运行 outcome aggregate。")
    if any(not isinstance(count, int) or count < 0 for count in value.values()):
        raise _fail(f"{label} 包含无效计数。", "重新运行 outcome aggregate。")


def _validate_aggregate(payload: Mapping[str, Any]) -> dict[str, Any]:
    if set(payload) != _AGGREGATE_FIELDS:
        raise _fail("outcome aggregate 字段不匹配。", "重新运行 outcome aggregate。")
    if payload.get("schema_version") != OUTCOME_AGGREGATE_SCHEMA or payload.get("ready") is not True:
        raise _fail("outcome aggregate 版本或状态无效。", "重新运行 outcome aggregate。")
    count = payload.get("report_count")
    if not isinstance(count, int) or count < MINIMUM_AGGREGATE_REPORTS:
        raise _fail("outcome aggregate cohort 太小。", "至少聚合三份独立 report。")
    _validate_count_map(payload.get("render_status_counts"), keys=RENDER_STATUSES, label="render counts")
    _validate_count_map(
        payload.get("adoption_status_counts"),
        keys=ADOPTION_STATUSES,
        label="adoption counts",
    )
    _validate_count_map(
        payload.get("failure_reason_counts"),
        keys=FAILURE_REASONS,
        label="failure reason counts",
    )
    if sum(payload["render_status_counts"].values()) != count:
        raise _fail("render counts 与 report_count 不一致。", "重新运行 outcome aggregate。")
    if sum(payload["adoption_status_counts"].values()) != count:
        raise _fail("adoption counts 与 report_count 不一致。", "重新运行 outcome aggregate。")
    if sum(payload["failure_reason_counts"].values()) > count:
        raise _fail("failure reason counts 超过 report_count。", "重新运行 outcome aggregate。")
    for rate_name in ("render_completed_rate", "adoption_published_rate"):
        rate = payload.get(rate_name)
        if not isinstance(rate, (float, int)) or isinstance(rate, bool) or not 0 <= rate <= 1:
            raise _fail(f"{rate_name} 无效。", "重新运行 outcome aggregate。")
    expected_render_rate = round(payload["render_status_counts"]["completed"] / count, 6)
    expected_publish_rate = round(payload["adoption_status_counts"]["published"] / count, 6)
    if payload["render_completed_rate"] != expected_render_rate:
        raise _fail("render_completed_rate 与计数不一致。", "重新运行 outcome aggregate。")
    if payload["adoption_published_rate"] != expected_publish_rate:
        raise _fail("adoption_published_rate 与计数不一致。", "重新运行 outcome aggregate。")
    if payload.get("privacy") != _AGGREGATE_PRIVACY:
        raise _fail("outcome aggregate 隐私声明不匹配。", "重新运行 outcome aggregate。")
    return dict(payload)


def write_outcome_aggregate(path: Path | str, aggregate: Mapping[str, Any]) -> None:
    _write_exclusive(path, _validate_aggregate(aggregate), label="outcome aggregate")
