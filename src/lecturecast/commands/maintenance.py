"""§5.5e5d-c maintenance CLI leaf — `lecturecast maintenance`.

Runs HeyGen maintenance recovery against a project dir: DB state recovery
(``recover_withdrawn_asset_cleanups``) FIRST, then network deletion recovery
(``recover_deletions``) via the dual HeyGen adapters built from one shared
transport. Prints a Chinese summary by default; ``--json`` emits the full
report. ``--force`` forwards a literal bool to the coordinator
(``type(force) is bool``).

Exit codes (audit M2 — maintenance mutates remote state, so its exit code
carries a stronger contract than doctor's always-0 diagnostic):

  0 — clean full sweep (DB pass OK + well-formed, network ran + well-formed,
      post-pass attention audit ran + zero attention states; zero failures /
      alerts / zero pending manual / left_uploading / non-withdrawn manual
      uploads / manual_force resources).
  2 — partial failure (some ops failed/alerted/skipped/not-advanced) OR
      pending DB-side work (manual / left_uploading > 0) OR the network pass
      was skipped (non-current journal, missing/whitespace HEYGEN_API_KEY, DB
      pass raised → ``db_recovery_failed``, DB/network/attention primitive
      returned a malformed/non-dict tally, or the post-pass attention audit
      raised → ``attention_audit_failed``) OR the attention audit found operator-
      attention states outside the recovery primitives' scopes (non-withdrawn
      manual uploads, manual_force resources, unrecoverable anomalous/orphaned
      resources). db_recovery / deletion_recovery may still be non-empty.
  1 — reserved for programming/harness errors raised BEFORE ``emit``: a bad
      lib-boundary arg (non-bool force, non-int lease_seconds, non-str
      now_iso/lease_owner) from a DIRECT lib call raises ValueError before a
      report exists. The CLI itself constructs valid values, so exit 1 is never
      reached via the CLI leaf — only via direct ``run_maintenance`` misuse.
      Codex round-3: malformed DB/deletion tallies + the attention-audit
      failure are all normalized into structured exit-2 reports at the lib
      boundary, so NO malformed-return path reaches the CLI as exit 1.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import typer

from ..maintenance import MaintenanceReport, run_maintenance
from .output import emit


def maintenance(
    project_root: Path = typer.Option(
        Path("."),
        "--project-root",
        help="项目根目录（默认当前目录）。",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="强制恢复（透传给 coordinator；type(force) is bool）。",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """运行 HeyGen 维护恢复：先 DB 状态恢复（撤回资产清理），再网络删除恢复。"""
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    report = run_maintenance(project_root, now_iso=now_iso, force=force)

    payload = asdict(report)
    payload["clean"] = report.clean

    # M3: honest human message — surface db_recovery + deletion_recovery tallies
    # AND the skip_reason verbatim (not only in --json). A non-JSON reader sees
    # the partial outcome (deleted / failed / alerted) + the exact skip reason.
    message = "\n".join(_format_message(report)) or "维护恢复完成（无可操作数据）。"

    emit(payload, json_output=json_output, message=message)

    # M2: exit code carries the recovery contract (0 clean / 2 partial-or-skip).
    if not report.clean:
        raise typer.Exit(code=2)


def _format_message(report: MaintenanceReport) -> list[str]:
    lines: list[str] = []
    db = report.db_recovery
    # Codex round-1 (M3 completeness): surface EVERY authoritative tally field,
    # not only the failure/alert dimensions — attempted/skipped/ops_driven/
    # ops_empty + db manual/left_uploading. A skipped deletion or a busy claim
    # (attempted > deleted) must be visible to a non-JSON reader, not hidden
    # behind an exit code.
    # Codex round-3 (type-stable formatter): guard ``db`` with ``isinstance``
    # BEFORE ``.get`` — the lib boundary now guarantees ``db`` is either ``{}``
    # or a well-formed 5-key dict, but a DIRECT ``_format_message`` caller (or a
    # future field) could pass a non-dict; the guard keeps the formatter
    # type-stable (exit 2 via clean, never an exit-1 AttributeError).
    if report.db_recovery_failed:
        lines.append("⚠ DB 状态恢复失败（见 skip_reason）；网络删除恢复未执行。")
    elif type(db) is dict and db:
        lines.append(
            "DB 状态恢复（撤回资产清理）："
            f"cleanup_required={db.get('cleanup_required', 0)}、"
            f"cancelled={db.get('cancelled', 0)}、kept={db.get('kept', 0)}、"
            f"manual={db.get('manual', 0)}、left_uploading={db.get('left_uploading', 0)}。"
        )
    if not report.network_skipped:
        d = report.deletion_recovery
        # Same type-stable guard as ``db`` for defense-in-depth
        # (Codex round-4: type() is dict, NOT isinstance — a dict subclass
        # overriding get() to raise cannot crash the formatter; the lib
        # boundary rejects subclasses, but a direct _format_message caller
        # could pass one).
        if type(d) is dict:
            lines.append(
                "网络删除恢复："
                f"ops_driven={d.get('ops_driven', 0)}、ops_empty={d.get('ops_empty', 0)}、"
                f"ops_alerted={d.get('ops_alerted', 0)}；attempted={d.get('attempted', 0)}、"
                f"deleted={d.get('deleted', 0)}、failed={d.get('failed', 0)}、"
                f"skipped={d.get('skipped', 0)}、alerted={d.get('alerted', 0)}。"
            )
    # Codex round-3 (attention audit): surface the post-pass attention counts so
    # an operator sees WHY exit 2 fired when the recovery tallies are themselves
    # clean (a non-withdrawn manual upload / manual_force resource / round-5
    # unrecoverable anomalous-orphan resource sits in the journal outside the
    # recovery primitives' scopes).
    if report.attention_audit_failed:
        lines.append("⚠ 恢复后 attention 审计失败（见 skip_reason）；最终态无法核实。")
    elif not report.network_skipped and not report.db_recovery_failed:
        at = report.attention
        # type() is dict (Codex round-4 residual 1): the lib boundary now
        # validates the attention tally too, so a subclass cannot reach here via
        # run_maintenance — but a direct caller could pass one, and isinstance
        # would admit a subclass whose get() raises.
        if type(at) is dict and (at.get("manual_uploads", 0)
                                 or at.get("manual_force_resources", 0)
                                 or at.get("unrecoverable_resources", 0)):
            lines.append(
                "⚠ journal 仍有需人介入的态（恢复原语作用域之外）："
                f"manual_uploads={at.get('manual_uploads', 0)}（未解决的上传，"
                f"含撤回 op 的 manual 行）、"
                f"manual_force_resources={at.get('manual_force_resources', 0)}"
                f"（operator-only 删除资源）、"
                f"unrecoverable_resources={at.get('unrecoverable_resources', 0)}"
                f"（schema-legal 异常态/孤儿资源，删除子系统拒绝驱动）。"
                f"请人工核查后处理。"
            )
    if report.network_skipped and report.skip_reason:
        lines.append(f"⚠ 网络删除恢复未执行：{report.skip_reason}")
    elif report.skip_reason:
        lines.append(f"⚠ {report.skip_reason}")
    return lines
