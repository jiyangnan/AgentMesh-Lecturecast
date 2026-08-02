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
from .operation_repository import (
    LEASE_MAX_SECONDS,
    LEASE_MIN_SECONDS,
    DeletionCoordinator,
    OperationRepository,
    _check_lease_seconds,
    _parse_utc,
    _require_lease_owner,
)

#: Default lease owner (stable across runs so SKIP LOCKED lease reclaim works
#: across overlapping maintenance invocations). Matches the shape of the
#: canary's ``lecturecast-canary``; passes ``_require_lease_owner`` (op_repo:106).
LEASE_OWNER_DEFAULT: str = "lecturecast-maintenance"

#: Default lease TTL (seconds). Within [LEASE_MIN_SECONDS, LEASE_MAX_SECONDS].
LEASE_SECONDS_DEFAULT: int = 300

#: The exact 8 keys ``DeletionCoordinator.recover_deletions`` returns
#: (op_repo:4246). Used to validate the tally shape so a malformed/empty/
#: non-dict coordinator return cannot masquerade as a clean sweep (Codex
#: round-1: ``deletion_recovery={}, network_skipped=False`` must NOT be clean;
#: Codex round-2: ``recover_deletions`` returning ``None``/list/string must NOT
#: raise ``AttributeError`` out of the recovery try-block as an exit-1 escape).
_DEL_TALLY_KEYS: frozenset[str] = frozenset({
    "ops_driven", "ops_empty", "ops_alerted",
    "attempted", "deleted", "failed", "skipped", "alerted",
})

#: The exact 5 keys ``recover_withdrawn_asset_cleanups`` returns (op_repo:3064).
#: Codex round-2: the DB tally shape was previously NOT enforced — a partial/
#: malformed ``db_recovery`` dict could pass ``clean`` because missing
#: ``manual``/``left_uploading`` defaulted to zero via ``.get``. Validate the
#: shape symmetrically with the deletion tally.
_DB_TALLY_KEYS: frozenset[str] = frozenset({
    "cancelled", "cleanup_required", "manual", "kept", "left_uploading",
})


def _valid_tally(tally: object, keys: frozenset[str]) -> bool:
    """True iff ``tally`` is a dict with EXACTLY the ``keys`` set AND every
    value is a non-negative ``int`` (``type(v) is int`` — rejects bool, which
    is an int subclass; rejects negative counts a real coordinator cannot
    produce). Codex round-2 blocker 2: this is TYPE-STABLE — a non-dict
    (None/list/str) returns False instead of raising ``AttributeError`` out of
    ``clean`` / the recovery try-block, so a malformed coordinator return
    becomes a structured skip report (exit 2), never an exit-1 escape."""
    if not isinstance(tally, dict):
        return False
    if set(tally.keys()) != keys:
        return False
    return all(type(v) is int and v >= 0 for v in tally.values())


@dataclass(frozen=True)
class MaintenanceReport:
    """Aggregate maintenance result.

    ``db_recovery`` is the ``recover_withdrawn_asset_cleanups`` tally (DB-only
    pass; ``{}`` when the DB pass did not complete — see ``db_recovery_failed``).
    ``db_recovery_failed`` is True iff the DB pass RAISED (Codex round-1
    blocker 4): the tx rolled back, the network pass did not run, and the
    skip_reason carries the error. ``deletion_recovery`` is the
    ``recover_deletions`` tally (network pass; ``{}`` when skipped).
    ``network_skipped`` is True iff the network pass did not run (non-current
    journal, missing/whitespace key, DB-pass failure, or ``recover_deletions``
    raised / returned a malformed tally). The ``force`` field is the literal
    validated bool forwarded — guaranteed bool by the entry guard.
    """

    db_recovery: dict[str, int] = field(default_factory=dict)
    deletion_recovery: dict[str, int] = field(default_factory=dict)
    network_skipped: bool = False
    skip_reason: str | None = None
    force: bool = False
    db_recovery_failed: bool = False

    @property
    def clean(self) -> bool:
        """True iff the sweep ran AND resolved every candidate with no pending
        work (the exit-0 condition, Codex round-1 blockers 1+2, round-2 type-
        stability). A cron/``&&`` consumer gates on the exit code alone without
        parsing the payload.

        ``clean`` requires, ALL of:
          (a) the DB pass did not raise (``db_recovery_failed`` False);
          (b) the network pass ran (``network_skipped`` False);
          (c) ``deletion_recovery`` is a well-formed 8-key tally (dict, exact
              keys, non-negative int values — type-stable: a None/list/str/
              negative/non-int return is rejected, NOT raised as AttributeError;
              Codex round-2 blocker 2);
          (d) ``db_recovery`` is a well-formed 5-key tally (same validation —
              a partial/malformed DB dict cannot pass because missing keys
              default to zero; Codex round-2 blocker 2);
          (e) every deletion candidate was deleted: ``attempted == deleted``.
              This one predicate catches every non-deleted dimension the 8-key
              return can carry — ``failed``, ``skipped`` (skipped_no_upload_id /
              skipped_unknown_kind — surfaced anomalies needing attention),
              ``alerted`` (processor raised, remote result unknowable), AND the
              ``not_advanced`` class (busy / retry_wait / not_ready /
              fence_conflict) which ``recover_deletions`` does NOT aggregate
              into the 8-key return (op_repo:395) — it shows up only as
              ``attempted - (deleted+failed+skipped+alerted) > 0``;
              ``deleted > attempted`` (an inverted/impossible tally) is also
              caught (defense-in-depth);
          (f) no per-op exception (``ops_alerted`` 0);
          (g) no DB-side pending work: ``manual`` (asset needs human
              reconciliation — whether flipped THIS sweep OR pre-existing; the
              locked ``enqueue`` counts both under ``manual`` per the round-2
              op_repo:2995 fix) and ``left_uploading`` (active upload lease left
              intact — its fenced apply will catch the withdraw on the next
              upload attempt; maintenance correctly does not touch it, but it
              IS unresolved this sweep).
        """
        if self.db_recovery_failed or self.network_skipped:
            return False
        # (c)+(d) shape + value validation — type-stable (non-dict → False).
        if not _valid_tally(self.deletion_recovery, _DEL_TALLY_KEYS):
            return False
        if not _valid_tally(self.db_recovery, _DB_TALLY_KEYS):
            return False
        d = self.deletion_recovery
        # (f) no per-op exception.
        if d.get("ops_alerted", 0):
            return False
        # (e) every candidate deleted: failed/alerted/skipped all 0 (attempted
        # == deleted with the prior ops_alerted==0 gate ⟹ skipped + not_advanced
        # both 0). Also catches deleted > attempted (inverted) as not-clean.
        if d.get("attempted", 0) != d.get("deleted", 0):
            return False
        if d.get("failed", 0) or d.get("alerted", 0) or d.get("skipped", 0):
            return False
        # (g) DB-side pending work.
        db = self.db_recovery
        if db.get("manual", 0) or db.get("left_uploading", 0):
            return False
        return True


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

    # Codex round-1 (lease/now_iso entry validation): recover_deletions
    # validates lease_owner immediately + lease_seconds only inside processor
    # claim (op_repo:111) — so with ZERO deletion candidates an invalid
    # lease_seconds/now_iso would never be validated + the run could report a
    # clean empty sweep against invalid config. Validate ALL three lib-boundary
    # args at entry (programming-error guards, mirror the force guard: a bad
    # lib arg raises ValueError before any work; the CLI's own defaults are
    # always valid so this never fires via the CLI). type() is int for
    # lease_seconds (NOT isinstance — bool is an int subclass; _check_lease_
    # seconds's range check happens to reject True/False as 1/0, but the
    # explicit type guard gives the precise error + matches the force discipline).
    #
    # Codex round-2 (type-stability): guard the STR args with `type(...) is str`
    # FIRST so a non-string now_iso/lease_owner raises ValueError (not
    # AttributeError at `.replace()` / TypeError in the regex) — the lib's
    # contract is "raises ValueError on a bad arg"; honoring that uniformly lets
    # the CLI leaf + tests assert ValueError without catching stray exception
    # types. These never fire via the CLI (typer delivers str options + the CLI
    # constructs now_iso itself).
    if type(lease_owner) is not str:
        raise ValueError(
            f"lease_owner must be a str, got {type(lease_owner).__name__}")
    _require_lease_owner(lease_owner)
    if type(lease_seconds) is not int:
        raise ValueError(
            f"lease_seconds must be an int in [{LEASE_MIN_SECONDS}, "
            f"{LEASE_MAX_SECONDS}], got {type(lease_seconds).__name__}"
        )
    _check_lease_seconds(lease_seconds)
    if type(now_iso) is not str:
        raise ValueError(
            f"now_iso must be a tz-aware ISO-8601 str, got {type(now_iso).__name__}")
    _parse_utc(now_iso)  # raises ValueError if not tz-aware ISO-8601

    # M1: read-only journal gate. _journal_state opens a mode=ro URI + creates
    # / migrates / writes NOTHING. Only 'current' proceeds; every other class
    # skips WITHOUT calling init/recover — so a fresh project never receives
    # the durable prior-use sentinel (.lecturecast/heygen.used) that
    # init_database would touch (which would later fail-close the capability
    # probe after a runtime/ delete, requiring doctor --reset). When the gate
    # yields 'current', the recover methods' internal init is a schema no-op.
    #
    # Scope (Codex round-1 blocker 3 — TOCTOU, documented limitation): the gate
    # binds to the classification observed at THIS read; it does NOT bind the
    # read to the journal subsequently opened by recover (those are two file
    # opens with an unchecked window between). A CONCURRENT process that
    # deletes/replaces the journal in that window could let begin_immediate's
    # init_database recreate a fresh journal + touch the sentinel. This is a
    # single-user local CLI tool — concurrent journal mutation mid-maintenance
    # (e.g. `rm -rf .lecturecast` while maintenance runs) is not a supported
    # scenario, and fully closing it requires a recovery-only-open primitive in
    # operation_repository that opens WITHOUT init (a locked-primitive change,
    # deferred). The M1 invariant as intended — "never touch the sentinel on a
    # fresh/broken journal observed at gate time" — holds for every
    # classification _journal_state returns (verified: all 12 classes skip).
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
    #
    # Codex round-1 blocker 4: the DB pass is wrapped (mirroring the m3 network
    # wrap) so a raised OperationStateError/OperationIntegrityError (e.g. a
    # withdrawn receipt with corrupt topology) becomes a structured skip report
    # instead of escaping as a harness exit-1 with no MaintenanceReport. The tx
    # rolled back (begin_immediate), so nothing was over-claimed — this is a
    # reporting-honesty fix, not a correctness gap. db_recovery_failed=True
    # disambiguates "DB pass did not complete" from "DB pass ran with zero work"
    # (db_recovery={} would otherwise be ambiguous). The network pass does NOT
    # run on a half-recovered journal.
    op_repo = OperationRepository(project_dir)
    try:
        db_tally = op_repo.recover_withdrawn_asset_cleanups(now_iso=now_iso)
    except Exception as exc:
        return MaintenanceReport(
            db_recovery={},
            db_recovery_failed=True,
            network_skipped=True,
            skip_reason=(
                f"DB 状态恢复失败（recover_withdrawn_asset_cleanups 抛出）— "
                f"网络删除恢复未执行: {type(exc).__name__}: {exc}。"
                f"请运行 `lecturecast doctor` 排查 journal 完整性。"
            ),
            force=force,
        )

    # B1: build the transport + read ITS provider for an early-skip preflight
    # when the key is absent/blank — applies the SAME predicate the transport
    # applies per-request (heygen_http.py:107: not isinstance OR not .strip),
    # so a whitespace-only key is treated identically to an absent key
    # (fail-closed), consistent with the transport AND the capability probe
    # (capabilities.py:471/685, both .strip()). Reading the transport's own
    # provider (not a second bare env read) means the PREDICATE cannot drift
    # from the transport's per-request predicate (subsumes audit minor m7).
    #
    # Scope (Codex round-1 claim 1 — temporal drift, acknowledged): the provider
    # reads the env var fresh on each call, so this preflight samples the key at
    # gate time and the transport samples it again per HTTP request on the
    # network pass — "same provider" ≠ "same value" if HEYGEN_API_KEY changes
    # in the window between. This preflight is an EARLY-SKIP OPTIMIZATION, not
    # the authoritative gate: the transport's per-request check is authoritative
    # and will raise HttpTransportError(code="auth_failed") on a blank key at
    # request time → recover_deletions surfaces it → the m3 wrap below turns it
    # into a skip report → exit 2. So fail-closed holds either way; the preflight
    # just skips the network round-trip when the key is observably absent now.
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
    # Codex round-1/2 (malformed tally): the coordinator contract is the exact
    # 8-key dict of non-negative ints (op_repo:4246); validate shape AND value
    # types via _valid_tally (type-stable — a non-dict None/list/str return
    # becomes a skip report instead of AttributeError escaping the try-block as
    # exit-1; negative/non-int values are rejected too). ``clean`` re-checks
    # this as a backstop.
    if not _valid_tally(del_tally, _DEL_TALLY_KEYS):
        if not isinstance(del_tally, dict):
            got = f"non-dict {type(del_tally).__name__}"
            keys = []
        else:
            got = "畸形"
            keys = sorted(del_tally.keys())
        return MaintenanceReport(
            db_recovery=db_tally,
            network_skipped=True,
            skip_reason=(
                f"recover_deletions 返回畸形 tally（期望 8 键非负 int dict "
                f"{sorted(_DEL_TALLY_KEYS)}，实际 {got} {keys}）— "
                f"请运行 `lecturecast doctor`。"
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
