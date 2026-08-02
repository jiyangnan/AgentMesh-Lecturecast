"""§5.5e5d-c maintenance recovery driver.

Drives the two LOCKED maintenance primitives in the locked order (D10):

  1. ``OperationRepository.recover_withdrawn_asset_cleanups`` (DB-only, FIRST)
     — idempotently reconciles consent-withdrawal state into ``cleanup_required``
     rows (covers the crash window where ``ConsentService.withdraw`` committed
     the receipt flip but crashed before/during the enqueue).
  2. ``DeletionCoordinator.recover_deletions`` (network, AFTER) — sweeps every
     cleanup_required / deletion-eligible resource to ``deleted`` via the dual
     HeyGen adapters built from ONE shared transport.

Blind-prediction constraints (e5c/d, verbatim — all honored here)
-----------------------------------------------------------------
  (a)/(c) force is a typer bool, forwarded UNCHANGED to ``recover_deletions``;
      the lib rejects a non-bool force at its OWN boundary (defense-in-depth,
      mirroring the 5 ``type(force) is not bool`` guards in operation_repository
      at lines 2022/2210/2387/3815/3991) BEFORE any DB read, so the guard fires
      regardless of journal/key state. No new truthy force source
      (--aggressive/--unsafe forbidden).
  (d) ONLY locked entries are called: ``recover_withdrawn_asset_cleanups`` +
      ``recover_deletions``. NEVER ``adapter.delete_video`` / ``delete_asset``
      directly (constraint d, §3.11). The dual adapters' delete methods are
      reached ONLY via the locked ``recover_deletions`` → ``delete_pass_for_
      operation`` → ``_drive_video`` / ``_drive_asset`` chain.

Audit closures (8-lens adversarial design audit, 2026-08-02 — see
docs/e5cd-design.md §5.5e5d-c lock notes for the full finding matrix)
---------------------------------------------------------------------
  B1 (BLOCKER — whitespace-key fail-open): the key predicate now reads the
      transport's OWN provider (``transport._api_key_provider()``) + applies
      the SAME check the transport applies per-request (heygen_http.py:107:
      ``not isinstance(key, str) or not key.strip()``). A whitespace-only key
      is treated identically to an absent key (fail-closed), consistent with
      the transport AND the capability probe (capabilities.py:471/685, both
      ``.strip()``). Reading the transport's provider — not a second bare env
      read — is the single source of truth (subsumes audit minor m7: a second
      env read cannot drift from the value the transport will actually use).
  M1 (MAJOR — durable prior-use sentinel on a fresh project): a read-only
      ``_journal_state`` gate (mode=ro URI, creates/migrates/writes nothing)
      refuses every classification other than ``current`` BEFORE any init or
      recover call. So maintenance never reaches ``init_database`` on a
      fresh/broken journal — it therefore never touches the durable
      ``.lecturecast/heygen.used`` sentinel (heygen_journal.py:475) that would
      later fail-close the capability probe after a runtime/ delete. When the
      gate yields ``current``, the recover methods' internal init is a
      guaranteed schema no-op (head==6).
  M2 (MAJOR — exit-0 masks partial failure / skip): the CLI exit code now
      carries the recovery contract (0 clean full sweep / 2 partial-or-skip /
      1 reserved for harness exceptions). See commands/maintenance.py.
  M3 (MAJOR — message text unspecified / skip_reason understated): the human
      message surfaces db_recovery + deletion_recovery tallies AND the
      skip_reason verbatim; the no-key skip_reason states the deletion did NOT
      happen + what to do.
  m1 (MINOR — lib force entry guard): the entry guard at the top of
      ``run_maintenance`` makes D-T11's outcome key/journal-independent.
  m3 (MINOR — db_tally preserved on post-DB failure): the network section is
      wrapped so a ``recover_deletions`` failure still reports the committed
      db_tally (fail-closed intact — the network pass did not run on unknown
      state; this is a reporting improvement, not a correctness gap).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .capabilities import _journal_state
from .heygen_asset_adapter import HeyGenAssetAdapter
from .heygen_http import HeyGenHttpTransport
from .heygen_videos_adapter import HeyGenVideosAdapter
from .operation_repository import DeletionCoordinator, OperationRepository

#: Default lease owner (stable across runs so SKIP LOCKED lease reclaim works
#: across overlapping maintenance invocations). Matches the shape of the
#: canary's ``lecturecast-canary``; passes ``_require_lease_owner`` (op_repo:106).
LEASE_OWNER_DEFAULT: str = "lecturecast-maintenance"

#: Default lease TTL (seconds). Within [LEASE_MIN_SECONDS, LEASE_MAX_SECONDS].
LEASE_SECONDS_DEFAULT: int = 300


@dataclass(frozen=True)
class MaintenanceReport:
    """Aggregate maintenance result.

    ``db_recovery`` is the ``recover_withdrawn_asset_cleanups`` tally (DB-only
    pass; always populated when the journal is current). ``deletion_recovery``
    is the ``recover_deletions`` tally (network pass; ``{}`` when skipped).
    ``network_skipped`` is True iff the network pass did not run (non-current
    journal, missing/whitespace key, or ``recover_deletions`` raised). The
    ``force`` field is the literal validated bool forwarded — guaranteed bool
    by the entry guard (never a coerced non-bool, audit minor m1).
    """

    db_recovery: dict[str, int] = field(default_factory=dict)
    deletion_recovery: dict[str, int] = field(default_factory=dict)
    network_skipped: bool = False
    skip_reason: str | None = None
    force: bool = False

    @property
    def clean(self) -> bool:
        """True iff the sweep ran AND deleted every candidate with no failures
        (network_skipped False AND failed/alerted/ops_alerted all zero). This
        is the exit-0 condition (M2): a cron/``&&`` consumer can gate on the
        exit code alone without parsing the payload."""
        if self.network_skipped:
            return False
        d = self.deletion_recovery
        return not (
            d.get("failed", 0) or d.get("alerted", 0) or d.get("ops_alerted", 0)
        )


class MaintenanceError(RuntimeError):
    """Raised when maintenance cannot construct its own driver (not when
    recovery has partial failures — those are recorded, not raised)."""


# M1: classification → human skip reason. ``current`` is the ONLY class that
# proceeds; every other class skips WITHOUT calling init/recover (avoids the
# durable prior-use sentinel footgun — audit M1). 'fresh' is deliberately NOT
# in the proceed set: a fresh project has nothing to recover AND must not be
# init'd by maintenance (the capability probe's _JOURNAL_READY includes 'fresh'
# because it answers a different question — "can HeyGen be configured" — not
# "is there data to recover").
_JOURNAL_SKIP_REASONS: dict[str, str] = {
    "fresh": "无 HeyGen journal — 无可恢复数据（请先运行一次 HeyGen 操作）",
    "missing_prior_use": "journal 在既往使用后丢失 — 数据丢失，请运行 `lecturecast doctor`",
    "behind": "journal head < 当前版本 — 请运行 `lecturecast doctor` 显式迁移",
    "ahead": "journal head > 当前版本 — 客户端过旧，请升级",
    "symlink": "journal 路径含符号链接 — init_database 拒绝符号链接",
    "parent_unwritable": ".lecturecast/ 父目录不可写 — 请运行 `lecturecast doctor`",
    "runtime_unwritable": "runtime/ 不可写 — 请运行 `lecturecast doctor`",
    "db_readonly": "journal DB 只读 — 请运行 `lecturecast doctor`",
    "shape_mismatch": "journal schema 列集与 canonical v6 不符 — 请运行 `lecturecast doctor`",
    "canonical_unavailable": "canonical v6 schema 不可用 — 请运行 `lecturecast doctor`",
    "unreadable": "journal 不可读 — 请运行 `lecturecast doctor`",
}


def run_maintenance(
    project_dir,
    *,
    now_iso: str,
    lease_owner: str = LEASE_OWNER_DEFAULT,
    lease_seconds: int = LEASE_SECONDS_DEFAULT,
    force: bool = False,
) -> MaintenanceReport:
    """Run HeyGen maintenance recovery against a project dir.

    Order (D10): ``recover_withdrawn_asset_cleanups`` (DB-only) FIRST →
    ``recover_deletions`` (network) AFTER, with the dual adapters (deleter +
    adapter) built from ONE shared transport + both passed to
    ``recover_deletions``.

    The journal MUST be at the current head (classification=='current'); every
    other class skips WITHOUT writing (M1). ``force`` is forwarded as a literal
    bool (constraint a/c); a non-bool force is rejected at the lib boundary
    before any DB read (m1, defense-in-depth).
    """
    from pathlib import Path

    project_dir = Path(project_dir)

    # m1: entry guard BEFORE any DB read — the force invariant fires regardless
    # of journal/key state, so D-T11's outcome is deterministic + key-
    # independent. type() is bool (NOT isinstance — isinstance(True, int) is
    # True and would admit int 1). Mirrors operation_repository.py:3991-3992.
    if type(force) is not bool:
        raise ValueError("force must be a bool")

    # M1: read-only journal gate. _journal_state opens a mode=ro URI + creates
    # / migrates / writes NOTHING. Only 'current' proceeds; every other class
    # skips WITHOUT calling init/recover — so a fresh project never receives
    # the durable prior-use sentinel (.lecturecast/heygen.used) that
    # init_database would touch (which would later fail-close the capability
    # probe after a runtime/ delete, requiring doctor --reset). When the gate
    # yields 'current', the recover methods' internal init is a schema no-op.
    try:
        classification = _journal_state(project_dir)["classification"]
    except Exception as exc:  # pragma: no cover - _journal_state has its own backstop
        return MaintenanceReport(
            network_skipped=True,
            skip_reason=(
                f"journal 不可读 — _journal_state raised: "
                f"{type(exc).__name__}: {exc}"
            ),
            force=force,
        )
    if classification != "current":
        reason = _JOURNAL_SKIP_REASONS.get(classification) or (
            f"journal classification={classification!r} — 请运行 `lecturecast doctor`"
        )
        return MaintenanceReport(
            network_skipped=True,
            skip_reason=reason,
            force=force,
        )

    # D10: DB-only pass FIRST (locked entry; idempotent consent-withdrawal
    # cleanup reconciliation). Commits before return (begin_immediate at
    # operation_repository.py:429), so the cleanup_required rows are durable
    # + visible to the network pass's candidate SELECT.
    op_repo = OperationRepository(project_dir)
    db_tally = op_repo.recover_withdrawn_asset_cleanups(now_iso=now_iso)

    # B1: build the transport + read ITS provider as the single source of truth
    # for "is the key configured?" — applies the SAME check the transport
    # applies per-request (heygen_http.py:107: not isinstance OR not .strip).
    # A whitespace-only key is treated identically to an absent key
    # (fail-closed), consistent with the transport AND the capability probe
    # (capabilities.py:471/685, both .strip()). Reading the transport's own
    # provider (not a second bare env read) means this predicate CANNOT drift
    # from the value the transport will actually use on the network pass —
    # subsumes audit minor m7 (no duplicated env-var read).
    transport = HeyGenHttpTransport()
    key = transport._api_key_provider()  # noqa: SLF001 — same provider used per-request
    if not isinstance(key, str) or not key.strip():
        return MaintenanceReport(
            db_recovery=db_tally,
            network_skipped=True,
            skip_reason=(
                "HEYGEN_API_KEY 未配置或为空白 — 资产未从 HeyGen 删除；"
                "仅本地账本状态已恢复。配置 key 后重跑以执行真实删除。"
            ),
            force=force,
        )

    # D10: network pass AFTER — dual adapter from the ONE shared transport
    # (deleter=HeyGenVideosAdapter / adapter=HeyGenAssetAdapter; both passed to
    # recover_deletions). m3: wrap so a post-DB failure still reports the
    # committed db_tally (fail-closed intact — network did not run on unknown
    # state; reporting improvement only, not a correctness gap). The force
    # ValueError is NOT caught here — the m1 entry guard already rejected a
    # non-bool force at the top of run_maintenance, before db_tally existed.
    deleter = HeyGenVideosAdapter(transport)
    adapter = HeyGenAssetAdapter(transport)
    coord = DeletionCoordinator(project_dir)
    try:
        del_tally = coord.recover_deletions(
            deleter=deleter,
            adapter=adapter,
            lease_owner=lease_owner,
            now_iso=now_iso,
            lease_seconds=lease_seconds,
            force=force,
        )
    except Exception as exc:
        return MaintenanceReport(
            db_recovery=db_tally,
            network_skipped=True,
            skip_reason=(
                f"recover_deletions 抛出: {type(exc).__name__}: {exc}"
            ),
            force=force,
        )
    return MaintenanceReport(
        db_recovery=db_tally,
        deletion_recovery=del_tally,
        network_skipped=False,
        skip_reason=None,
        force=force,
    )
