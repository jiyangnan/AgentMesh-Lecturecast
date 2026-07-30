"""HeyGen operation repository — claim/lease/fence primitives (§5.5e3a).

A thin SQL/lease/fence layer over the journal. It holds NO product strategy and
no protocol-model knowledge: the submit consent guard (ConsentService) and the
adapter (e3b) are orchestrated around it by a coordinator, all inside one
BEGIN IMMEDIATE transaction (use repository.begin_immediate()) so there is no
guard→claim race window.

Fence rules (per Codex e3 plan):
- A new claim/reclaim bumps lease_fence by 1.
- A renewal keeps the fence, only extends lease_expires_at.
- An outcome transition is gated by (operation_id, lease_owner, lease_fence,
  expected_status); on success it clears owner/expires but RETAINS the fence.
- The fence never resets to 0; a stale worker's rowcount=0 UPDATE cannot
  overwrite a newer owner.

Critical safety invariant: an operation with attempt_started_at set (a submit
that may have reached HeyGen) is NEVER re-claimable for submit. If the lease is
still active that is reported as 'busy' (another worker is on it — wait); if the
lease expired it is 'ambiguous' (a maybe-sent attempt — route to reconciliation,
never a blind re-submit).

All timestamps are parsed to timezone-aware datetimes, compared as datetimes
(not lexically), and written back in a canonical UTC isoformat.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lecturecast.consent import (
    ConsentService,
    CreativeBriefV1_1,
    OrchestrationPlanV1_1,
    PreparedOperation,
    PresenterPlanV1_1,
    ProductionManifest,
    SubmitConsentResult,
)
from lecturecast.heygen_adapter import (DeleteAdapterError, DeleteResult,
    HeyGenAdapterError, PollAdapterError, PollResult, SubmitAccepted, SubmitOutcome,
    TitleCandidate, TitleQuery, TitleQueryAdapterError, TitleQueryResult)
from lecturecast.heygen_journal import _chmod_secure, init_database

_RUNTIME_DB = Path(".lecturecast") / "runtime" / "heygen-operations.db"
_LEASE_OWNER_RE = re.compile(r"^[A-Za-z0-9_:.\-]{3,96}$")
LEASE_MIN_SECONDS = 30
LEASE_MAX_SECONDS = 3600
NEXT_RETRY_BACKOFF_SECONDS = 60  # linear submit retry backoff (anti-hotloop is e3c)
POLL_BACKOFF_SECONDS = 30        # transient poll-error backoff
POLL_INTERVAL_SECONDS = 30       # minimum gap between successful polls (anti-hotloop)
POLLABLE_STATUSES = frozenset({"submitted", "processing", "reconciliation_required"})
# A video in active deletion is not meaningfully pollable.
_UNPOLLABLE_DELETION = frozenset({"deletion_pending", "deleted"})
# Crash-recovery title-reconciliation candidates: an attempt may have reached
# HeyGen (attempt_started_at set) but the outcome was never recorded — either
# still submit_pending (worker crashed before the outcome write) or already
# flagged reconciliation_required. They have NO known video resource.
RECONCILE_STATUSES = frozenset({"submit_pending", "reconciliation_required"})
RECONCILE_SEARCH_WINDOW_SECONDS = 24 * 3600  # fixed 24h HeyGen idempotency/search window
RECONCILE_CLOCK_SKEW_SECONDS = 300            # 5m clock-skew margin
RECONCILE_BACKOFF_SECONDS = 300               # indeterminate reconcile retry backoff
_RECONCILE_NO_MATCH = "reconciliation_no_match"
_RECONCILE_WITHDRAWN = "consent_withdrawn_cleanup_required"
# A permanent title-search failure parks the operation for manual recovery; the
# candidate scan and claim exclude this code so maintenance does not hot-loop it.
_MANUAL_RECONCILE_CODE = "manual_reconciliation_required"
DOWNLOAD_BACKOFF_SECONDS = 120        # failed-download retry backoff
# Download error codes that park the operation for manual recovery — the claim
# and classify exclude these so maintenance does not hot-loop them.
_DOWNLOAD_MANUAL_CODES = frozenset({
    "download_reconciliation_required", "download_file_missing",
    "consent_withdrawn_cleanup_required",
})
DELETION_MAX_ATTEMPTS = 3
DELETION_BACKOFF_SECONDS = 120
_DELETION_MANUAL_CODES = frozenset({
    "deletion_retry_exhausted", "deletion_reconciliation_required",
})


class OperationError(RuntimeError):
    """Base for operation-layer errors."""


class OperationStateError(OperationError):
    """The requested transition is not allowed from the current state, or the
    journal is in an anomalous topology the repository refuses to overwrite."""


class OperationIntegrityError(OperationError):
    """A stored row is internally inconsistent (e.g. consent pointer ≠ receipt
    digest). Fail-closed rather than trusting the row."""


def _require_lease_owner(owner: str) -> None:
    if not _LEASE_OWNER_RE.fullmatch(owner or ""):
        raise ValueError(f"invalid lease_owner: {owner!r}")


def _check_lease_seconds(seconds: int) -> None:
    if not isinstance(seconds, int) or not (LEASE_MIN_SECONDS <= seconds <= LEASE_MAX_SECONDS):
        raise ValueError(
            f"lease_seconds must be an int in [{LEASE_MIN_SECONDS}, {LEASE_MAX_SECONDS}]"
        )


def _parse_utc(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"not ISO-8601: {value!r}") from exc
    if dt.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware: {value!r}")
    return dt.astimezone(timezone.utc)


def _canonical(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# --- result types ------------------------------------------------------


@dataclass(frozen=True)
class ClaimResult:
    operation_id: str
    status: str  # "claimed" | "busy" | "ambiguous" | "not_ready"
    fence: int
    submit_attempts: int
    lease_expires_at: str | None


@dataclass(frozen=True)
class RenewResult:
    operation_id: str
    status: str  # "renewed" | "not_held" | "expired"
    fence: int
    lease_expires_at: str | None


@dataclass(frozen=True)
class SubmitClaim:
    """The combined output of a coordinator claim: the consent authorization
    proof plus the leased claim handle the worker uses for the outcome write."""

    consent: SubmitConsentResult
    claim: ClaimResult


@dataclass(frozen=True)
class PollClaim:
    operation_id: str
    status: str          # "claimed" | "busy" | "retry_wait" | "not_ready"
    fence: int
    remote_id: str | None


@dataclass(frozen=True)
class PollOutcome:
    operation_id: str
    status: str          # submitted | processing | completed | failed | reconciliation_required | keep | fence_conflict
    fence: int
    last_error_code: str | None
    next_retry_at: str | None
    video_url: str | None  # transient download locator for completed (handed to e4)


@dataclass(frozen=True)
class PollOnceResult:
    claim: PollClaim
    outcome: PollOutcome | None


@dataclass(frozen=True)
class ReconcileClaim:
    operation_id: str
    status: str          # "claimed" | "busy" | "retry_wait" | "not_ready"
    fence: int
    heygen_title: str | None
    attempt_started_at: str | None


@dataclass(frozen=True)
class ReconcileOutcome:
    operation_id: str
    verdict: str         # exact_found | definitive_no_match | indeterminate | cleanup_required | fence_conflict
    fence: int
    target_status: str
    last_error_code: str | None
    next_retry_at: str | None
    written_remote_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconcileOnceResult:
    claim: ReconcileClaim
    outcome: ReconcileOutcome | None


@dataclass(frozen=True)
class DownloadClaim:
    operation_id: str
    status: str          # "claimed" | "finalize" | "busy" | "retry_wait" | "not_ready" | "consent_withdrawn"
    fence: int
    remote_id: str | None
    resource_id: int | None


@dataclass(frozen=True)
class MediaProbeResult:
    duration_seconds: float
    video_codec: str
    width: int
    height: int


@dataclass(frozen=True)
class PreparedDownload:
    """A downloaded, hashed, ffprobe-verified temp file staged for atomic
    publication. local_output_ref is a fixed runtime-root-relative path
    (outputs/heygen/<operation_id>.mp4); the caller cannot choose it."""

    temp_path_str: str
    local_output_ref: str
    digest: str
    size_bytes: int
    media: MediaProbeResult


@dataclass(frozen=True)
class DownloadOutcome:
    operation_id: str
    status: str          # verified | failed | reconcile | consent_withdrawn | fence_conflict
    fence: int
    last_error_code: str | None
    next_retry_at: str | None


@dataclass(frozen=True)
class DownloadOnceResult:
    claim: DownloadClaim
    outcome: DownloadOutcome | None


@dataclass(frozen=True)
class DeletionClaim:
    operation_id: str
    resource_id: int
    status: str          # "claimed" | "busy" | "retry_wait" | "not_ready"
    fence: int
    remote_id: str | None


@dataclass(frozen=True)
class DeletionOutcome:
    operation_id: str
    resource_id: int
    status: str          # "deleted" | "failed" | "fence_conflict"
    fence: int
    last_error: str | None
    next_retry_at: str | None


@dataclass(frozen=True)
class DeletionOnceResult:
    claim: DeletionClaim
    outcome: DeletionOutcome | None


# --- repository --------------------------------------------------------


class OperationRepository:
    """SQL/lease/fence primitives. Use begin_immediate() to open the transaction
    the in_tx primitives require; it guarantees the connection is bound to THIS
    project's journal."""

    def __init__(self, project_dir: str | Path) -> None:
        self._project_dir = Path(project_dir)
        self._db_path = self._project_dir / _RUNTIME_DB

    @contextmanager
    def begin_immediate(self):
        conn = init_database(self._project_dir)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            _rollback(conn)
            raise
        finally:
            _chmod_secure(self._db_path)
            conn.close()

    def _require_tx(self, conn: sqlite3.Connection) -> None:
        if not conn.in_transaction:
            raise OperationStateError(
                "operation repository primitives require an active transaction"
            )
        # Reject a connection from a different project's journal.
        rows = conn.execute("PRAGMA database_list").fetchall()
        main_file = next((r[2] for r in rows if r[1] == "main"), None)
        if main_file is None:
            raise OperationStateError("connection has no main database")
        try:
            same = Path(main_file).resolve() == self._db_path.resolve()
        except (OSError, ValueError):
            same = False
        if not same:
            raise OperationStateError(
                "connection is not bound to this project's journal"
            )

    def claim_submit_in_tx(
        self,
        conn: sqlite3.Connection,
        operation_id: str,
        lease_owner: str,
        now_iso: str,
        lease_seconds: int,
    ) -> ClaimResult:
        """Claim a granted operation for a submit attempt. Bumps fence + attempts
        + attempt_started_at atomically. Classification:
          - active lease + attempt started → 'busy' (wait for the lease)
          - expired/absent lease + attempt started → 'ambiguous' (reconcile)
          - lease present without an attempt → fail-closed (anomalous state)
          - no consent / not submit_pending → 'not_ready'
        A lost concurrent CAS re-reads and classifies the new state rather than
        guessing."""
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        _require_lease_owner(lease_owner)
        now = _parse_utc(now_iso)
        _check_lease_seconds(lease_seconds)
        expires_iso = _canonical(now + timedelta(seconds=lease_seconds))

        def _fetch():
            row = conn.execute(
                "SELECT status, consent_receipt_digest, attempt_started_at, lease_owner, "
                "lease_expires_at, lease_fence, submit_attempts, next_retry_at "
                "FROM heygen_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            receipt = conn.execute(
                "SELECT status, receipt_digest FROM heygen_consent_receipts "
                "WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            return row, receipt

        def _classify(row, receipt):
            if row is None or row["status"] != "submit_pending":
                return ClaimResult(operation_id, "not_ready", 0, 0, None)
            ptr = row["consent_receipt_digest"]
            if ptr is None or receipt is None:
                return ClaimResult(operation_id, "not_ready", 0, 0, None)
            if receipt["status"] != "granted":
                return ClaimResult(operation_id, "not_ready", 0, 0, None)
            if receipt["receipt_digest"] != ptr:
                raise OperationIntegrityError(
                    "consent pointer does not match the receipt digest"
                )
            if row["attempt_started_at"] is not None:
                owner = row["lease_owner"]
                exp = row["lease_expires_at"]
                # A half lease (one of owner/expires set, the other NULL) is a
                # corrupt topology — fail-closed, never silently classify.
                if (owner is None) != (exp is None):
                    raise OperationIntegrityError(
                        f"operation {operation_id} has a half lease state "
                        f"(owner={owner!r}, expires={exp!r})"
                    )
                if owner is None and exp is None:
                    # Attempt with no lease at all — a maybe-sent attempt with
                    # no active holder. Ambiguous; route to reconciliation.
                    return ClaimResult(operation_id, "ambiguous",
                                       row["lease_fence"], row["submit_attempts"], None)
                # Both set — busy if still valid, ambiguous if expired.
                active = _parse_utc(exp) > now
                status = "busy" if active else "ambiguous"
                return ClaimResult(operation_id, status,
                                   row["lease_fence"], row["submit_attempts"], exp)
            # No attempt — a lease without an attempt is an anomalous state we
            # refuse to silently overwrite.
            if row["lease_owner"] is not None or row["lease_expires_at"] is not None:
                raise OperationStateError(
                    f"operation {operation_id} has a lease without an attempt"
                )
            # Retry backoff gate: a not_sent-retryable outcome set next_retry_at;
            # do not claim again until it has elapsed.
            nr = row["next_retry_at"]
            if nr is not None:
                if _parse_utc(nr) > now:
                    return ClaimResult(operation_id, "retry_wait",
                                       row["lease_fence"], row["submit_attempts"], None)
            return None  # eligible

        row, receipt = _fetch()
        verdict = _classify(row, receipt)
        if verdict is not None:
            return verdict

        new_fence = row["lease_fence"] + 1
        cur = conn.execute(
            "UPDATE heygen_operations SET lease_owner = ?, lease_expires_at = ?, "
            "lease_fence = ?, submit_attempts = ?, attempt_started_at = ?, "
            "updated_at = ? WHERE operation_id = ? AND status = 'submit_pending' "
            "AND attempt_started_at IS NULL AND lease_owner IS NULL "
            "AND lease_expires_at IS NULL AND lease_fence = ?",
            (lease_owner, expires_iso, new_fence, row["submit_attempts"] + 1,
             _canonical(now), _canonical(now), operation_id, row["lease_fence"]),
        )
        if cur.rowcount == 0:
            # Lost a concurrent claim — re-read and classify the new state.
            row2, receipt2 = _fetch()
            v2 = _classify(row2, receipt2)
            if v2 is None:
                # Eligible again after a lost CAS means a concurrent tx rolled
                # back its claim — a state we cannot explain; refuse to guess.
                raise OperationStateError(
                    f"operation {operation_id} became eligible again after a lost CAS"
                )
            return v2
        return ClaimResult(operation_id, "claimed", new_fence,
                           row["submit_attempts"] + 1, expires_iso)

    def renew_lease_in_tx(
        self,
        conn: sqlite3.Connection,
        operation_id: str,
        lease_owner: str,
        fence: int,
        now_iso: str,
        lease_seconds: int,
    ) -> RenewResult:
        """Extend the lease a submit worker still holds (fence unchanged). The
        lease must belong to the caller, the operation must still be
        submit_pending with an in-flight attempt and a live consent pointer, and
        the lease must not yet have expired (expires > now)."""
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        _require_lease_owner(lease_owner)
        now = _parse_utc(now_iso)
        _check_lease_seconds(lease_seconds)

        row = conn.execute(
            "SELECT status, lease_owner, lease_fence, lease_expires_at, "
            "attempt_started_at, consent_receipt_digest FROM heygen_operations "
            "WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if (
            row is None
            or row["lease_owner"] != lease_owner
            or row["lease_fence"] != fence
        ):
            return RenewResult(operation_id, "not_held", 0, None)
        # A submit renewal only makes sense for an in-flight submit attempt.
        if (
            row["status"] != "submit_pending"
            or row["attempt_started_at"] is None
            or row["consent_receipt_digest"] is None
        ):
            return RenewResult(operation_id, "not_held", fence, row["lease_expires_at"])
        if row["lease_expires_at"] is None or _parse_utc(row["lease_expires_at"]) <= now:
            return RenewResult(operation_id, "expired", fence, row["lease_expires_at"])
        new_expires = _canonical(now + timedelta(seconds=lease_seconds))
        conn.execute(
            "UPDATE heygen_operations SET lease_expires_at = ?, updated_at = ? "
            "WHERE operation_id = ? AND lease_owner = ? AND lease_fence = ?",
            (new_expires, _canonical(now), operation_id, lease_owner, fence),
        )
        return RenewResult(operation_id, "renewed", fence, new_expires)

    # -- submit outcome (§5.5e3b) ---------------------------------------

    def apply_submit_outcome_in_tx(
        self,
        conn: sqlite3.Connection,
        operation_id: str,
        lease_owner: str,
        fence: int,
        now_iso: str,
        outcome: SubmitAccepted | HeyGenAdapterError,
    ) -> SubmitOutcome:
        """Apply a submit outcome behind a fenced CAS
        (operation_id + lease_owner + lease_fence + status='submit_pending' +
        attempt_started_at set). Clears the lease on every terminal outcome;
        the fence is retained.

        Outcome → status mapping (per Codex e3 plan):
          SubmitAccepted(remote_id)            → submitted (+ atomic video resource/ref)
          SubmitAccepted(empty remote_id)      → reconciliation_required (ambiguous)
          HeyGenAdapterError maybe_sent        → reconciliation_required
          HeyGenAdapterError not_sent retryable→ submit_pending (reset attempt + next_retry_at)
          HeyGenAdapterError not_sent permanent→ failed
        A fence mismatch writes nothing and returns status='fence_conflict'."""
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        _require_lease_owner(lease_owner)
        now = _parse_utc(now_iso)
        now_c = _canonical(now)

        target, remote_id, provider_status, last_error, next_retry = self._plan_submit_outcome(
            outcome, now
        )

        # Every transition explicitly sets the full audit column set so stale
        # values from a previous round (old last_error_code / next_retry_at /
        # submitted_at / completed_at / provider_status) never bleed across.
        always = {
            "status": target,
            "lease_owner": None,
            "lease_expires_at": None,
            "updated_at": now_c,
        }
        if target == "submitted":
            always.update(submitted_at=now_c, provider_status=provider_status or "",
                          next_retry_at=None, last_error_code=None, completed_at=None)
        elif target == "submit_pending":  # not_sent retryable: reset for re-claim
            always.update(attempt_started_at=None, next_retry_at=next_retry,
                          last_error_code=last_error, submitted_at=None,
                          completed_at=None, provider_status="")
        elif target == "failed":
            always.update(last_error_code=last_error, completed_at=now_c,
                          next_retry_at=None, submitted_at=None, provider_status="")
        elif target == "reconciliation_required":
            always.update(last_error_code=last_error, next_retry_at=None,
                          submitted_at=None, completed_at=None, provider_status="")
        set_clause = ", ".join(f"{col} = ?" for col in always)
        params = list(always.values()) + [operation_id, lease_owner, fence]
        sql = (
            f"UPDATE heygen_operations SET {set_clause} "
            "WHERE operation_id = ? AND lease_owner = ? AND lease_fence = ? "
            "AND status = 'submit_pending' AND attempt_started_at IS NOT NULL"
        )
        cur = conn.execute(sql, params)
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT lease_fence FROM heygen_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            cur_fence = row["lease_fence"] if row is not None else 0
            return SubmitOutcome("fence_conflict", cur_fence, None, None, None)

        remote_resource_id = None
        if target == "submitted" and remote_id:
            remote_resource_id = self._write_video_resource(
                conn, operation_id, remote_id, now_c
            )
        return SubmitOutcome(target, fence, remote_resource_id, last_error, next_retry)

    @staticmethod
    def _plan_submit_outcome(outcome, now):
        """Map a typed outcome to (target_status, remote_id, provider_status,
        last_error_code, next_retry_iso)."""
        if isinstance(outcome, SubmitAccepted):
            # SubmitAccepted guarantees a non-empty remote_id at construction.
            return ("submitted", outcome.remote_id, outcome.provider_status, None, None)
        if isinstance(outcome, HeyGenAdapterError):
            if outcome.submission_certainty == "maybe_sent":
                return ("reconciliation_required", None, "", outcome.code, None)
            # not_sent
            if outcome.retryable:
                retry = _canonical(now + timedelta(seconds=NEXT_RETRY_BACKOFF_SECONDS))
                return ("submit_pending", None, "", outcome.code, retry)
            return ("failed", None, "", outcome.code, None)
        raise TypeError(f"unsupported submit outcome type: {type(outcome)!r}")

    @staticmethod
    def _write_video_resource(conn, operation_id: str, remote_id: str, now_c: str,
                              deletion_status: str = 'not_started') -> int:
        """Record the remote video resource + its operation ref. For an ephemeral
        video a remote_id must belong to at most one operation — a collision with
        another operation's resource is fail-closed (the whole tx rolls back).
        credential_profile_id is read from the operation row, never hardcoded."""
        op_row = conn.execute(
            "SELECT credential_profile_id FROM heygen_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if op_row is None:
            raise OperationIntegrityError(f"no operation {operation_id}")
        profile = op_row["credential_profile_id"]
        existing = conn.execute(
            "SELECT resource_id, created_by_operation_id, credential_profile_id "
            "FROM heygen_remote_resources "
            "WHERE credential_profile_id = ? AND resource_kind = 'video' AND remote_id = ?",
            (profile, remote_id),
        ).fetchone()
        if existing is not None:
            if existing["created_by_operation_id"] != operation_id:
                raise OperationIntegrityError(
                    f"remote video {remote_id!r} already belongs to another operation"
                )
            other_ref = conn.execute(
                "SELECT 1 FROM heygen_resource_operation_refs "
                "WHERE resource_id = ? AND operation_id <> ?",
                (existing["resource_id"], operation_id),
            ).fetchone()
            if other_ref is not None:
                raise OperationIntegrityError(
                    f"remote video {remote_id!r} is referenced by another operation"
                )
            resource_id = existing["resource_id"]
        else:
            cur = conn.execute(
                "INSERT INTO heygen_remote_resources "
                "(credential_profile_id, resource_kind, remote_id, retention_mode, "
                "created_by_operation_id, deletion_status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (profile, "video", remote_id, "ephemeral", operation_id, deletion_status, now_c, now_c),
            )
            resource_id = cur.lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO heygen_resource_operation_refs "
            "(resource_id, operation_id, created_at) VALUES (?, ?, ?)",
            (resource_id, operation_id, now_c),
        )
        return resource_id

    # -- known-id poll (§5.5e3c) ----------------------------------------

    def claim_poll_in_tx(
        self,
        conn: sqlite3.Connection,
        operation_id: str,
        lease_owner: str,
        now_iso: str,
        lease_seconds: int,
    ) -> PollClaim:
        """Claim a short poll lease on an operation that has a known remote video
        id and is in a pollable state (submitted/processing/reconciliation_required).
        Bumps fence; does NOT touch attempt_started_at/submit_attempts (those
        belong to the submit attempt). Anti-hotloop: an active lease → 'busy',
        and an unelapsed next_retry_at → 'retry_wait'."""
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        _require_lease_owner(lease_owner)
        now = _parse_utc(now_iso)
        _check_lease_seconds(lease_seconds)
        expires_iso = _canonical(now + timedelta(seconds=lease_seconds))

        row = conn.execute(
            "SELECT status, lease_owner, lease_expires_at, lease_fence, next_retry_at, "
            "credential_profile_id FROM heygen_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return PollClaim(operation_id, "not_ready", 0, None)
        if row["status"] not in POLLABLE_STATUSES:
            return PollClaim(operation_id, "not_ready", row["lease_fence"], None)

        # Exactly one video resource, exclusively owned by this operation.
        resources = conn.execute(
            "SELECT r.resource_id, r.remote_id, r.created_by_operation_id, "
            "r.credential_profile_id, r.deletion_status FROM heygen_remote_resources r "
            "JOIN heygen_resource_operation_refs ref ON ref.resource_id = r.resource_id "
            "WHERE ref.operation_id = ? AND r.resource_kind = 'video'",
            (operation_id,),
        ).fetchall()
        if len(resources) == 0:
            return PollClaim(operation_id, "not_ready", row["lease_fence"], None)
        if len(resources) > 1:
            raise OperationIntegrityError(
                f"operation {operation_id} has {len(resources)} video resources"
            )
        res = resources[0]
        if res["created_by_operation_id"] != operation_id:
            raise OperationIntegrityError(
                f"operation {operation_id} polls a video owned by another operation"
            )
        if res["credential_profile_id"] != row["credential_profile_id"]:
            raise OperationIntegrityError(
                f"operation {operation_id} credential_profile_id mismatch on video resource"
            )
        other_ref = conn.execute(
            "SELECT 1 FROM heygen_resource_operation_refs "
            "WHERE resource_id = ? AND operation_id <> ?",
            (res["resource_id"], operation_id),
        ).fetchone()
        if other_ref is not None:
            raise OperationIntegrityError(
                f"operation {operation_id} polls a video referenced by another operation"
            )
        if res["deletion_status"] in _UNPOLLABLE_DELETION:
            return PollClaim(operation_id, "not_ready", row["lease_fence"], None)
        remote_id = res["remote_id"]

        verdict = self._classify_poll_row(row, operation_id, now)
        if verdict is not None:
            return verdict

        new_fence = row["lease_fence"] + 1
        placeholders = ",".join("?" for _ in POLLABLE_STATUSES)
        cur = conn.execute(
            "UPDATE heygen_operations SET lease_owner = ?, lease_expires_at = ?, "
            "lease_fence = ?, updated_at = ? WHERE operation_id = ? "
            "AND lease_fence = ? AND status IN (" + placeholders + ")",
            (lease_owner, expires_iso, new_fence, _canonical(now),
             operation_id, row["lease_fence"], *POLLABLE_STATUSES),
        )
        if cur.rowcount == 0:
            # Lost a concurrent claim/reclaim — re-read and classify with the
            # SAME function (real fence, half-lease detection).
            row2 = conn.execute(
                "SELECT status, lease_owner, lease_expires_at, lease_fence, next_retry_at "
                "FROM heygen_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row2 is None:
                return PollClaim(operation_id, "not_ready", new_fence, None)
            v2 = self._classify_poll_row(row2, operation_id, now)
            if v2 is None:
                raise OperationStateError(
                    f"operation {operation_id} became eligible again after a lost poll CAS"
                )
            return v2
        return PollClaim(operation_id, "claimed", new_fence, remote_id)

    @staticmethod
    def _classify_poll_row(row, operation_id: str, now) -> PollClaim | None:
        """Unified poll classification (used on the fast path and after a lost
        CAS). Returns a terminal PollClaim (busy/retry_wait/not_ready) or None
        if the operation is eligible to claim. Half lease → fail-closed."""
        if row["status"] not in POLLABLE_STATUSES:
            return PollClaim(operation_id, "not_ready", row["lease_fence"], None)
        owner = row["lease_owner"]
        exp = row["lease_expires_at"]
        if (owner is None) != (exp is None):
            raise OperationIntegrityError(
                f"operation {operation_id} has a half poll lease state "
                f"(owner={owner!r}, expires={exp!r})"
            )
        if owner is not None and _parse_utc(exp) > now:
            return PollClaim(operation_id, "busy", row["lease_fence"], None)
        nr = row["next_retry_at"]
        if nr is not None and _parse_utc(nr) > now:
            return PollClaim(operation_id, "retry_wait", row["lease_fence"], None)
        return None

    def apply_poll_outcome_in_tx(
        self,
        conn: sqlite3.Connection,
        operation_id: str,
        lease_owner: str,
        fence: int,
        now_iso: str,
        outcome: PollResult | PollAdapterError,
    ) -> PollOutcome:
        """Apply a poll outcome behind a fenced CAS (operation_id + lease_owner +
        lease_fence + status in POLLABLE_STATUSES). Clears the poll lease on every
        outcome; the fence is retained.

        Outcome → status mapping (per Codex e3 plan):
          PollResult queued/submitted   → submitted  (next_retry = poll interval)
          PollResult processing         → processing (next_retry = poll interval)
          PollResult completed          → completed (requires video_url; download is e4)
          PollResult failed             → failed
          PollResult not_found          → reconciliation_required
          PollAdapterError retryable    → keep status + next_retry (transient backoff)
          PollAdapterError not retryable→ reconciliation_required
        Every non-terminal poll (submitted/processing/keep) sets next_retry_at to
        the next permissible poll time so a caller cannot hot-loop. A fence
        mismatch writes nothing and returns status='fence_conflict'."""
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        _require_lease_owner(lease_owner)
        now = _parse_utc(now_iso)
        now_c = _canonical(now)

        target, provider_status, last_error, next_retry, video_url = self._plan_poll_outcome(outcome, now)
        poll_again = _canonical(now + timedelta(seconds=POLL_INTERVAL_SECONDS))

        always = {
            "lease_owner": None,
            "lease_expires_at": None,
            "updated_at": now_c,
        }
        if target == "keep":
            # transient poll error — preserve status, back off
            always.update(next_retry_at=next_retry, last_error_code=last_error,
                          provider_status="")
        elif target == "submitted":
            always.update(status="submitted", provider_status=provider_status or "",
                          next_retry_at=poll_again, last_error_code=None, completed_at=None)
        elif target == "processing":
            always.update(status="processing", provider_status=provider_status or "",
                          next_retry_at=poll_again, last_error_code=None, completed_at=None)
        elif target == "completed":
            always.update(status="completed", provider_status=provider_status or "",
                          completed_at=now_c, next_retry_at=None, last_error_code=None)
        elif target == "failed":
            always.update(status="failed", provider_status=provider_status or "",
                          completed_at=now_c, next_retry_at=None, last_error_code=last_error)
        elif target == "reconciliation_required":
            always.update(status="reconciliation_required", provider_status="",
                          next_retry_at=None, last_error_code=last_error, completed_at=None)
        set_clause = ", ".join(f"{col} = ?" for col in always)
        params = list(always.values()) + [operation_id, lease_owner, fence]
        placeholders = ",".join("?" for _ in POLLABLE_STATUSES)
        sql = (
            f"UPDATE heygen_operations SET {set_clause} "
            f"WHERE operation_id = ? AND lease_owner = ? AND lease_fence = ? "
            f"AND status IN ({placeholders})"
        )
        cur = conn.execute(sql, params + list(POLLABLE_STATUSES))
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT lease_fence FROM heygen_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            cur_fence = row["lease_fence"] if row is not None else 0
            return PollOutcome(operation_id, "fence_conflict", cur_fence, None, None, None)
        return PollOutcome(operation_id, target, fence, last_error,
                           next_retry if target == "keep" else (poll_again if target in ("submitted", "processing") else None),
                           video_url)

    @staticmethod
    def _plan_poll_outcome(outcome, now):
        """Map a typed poll outcome to (target, provider_status, last_error,
        next_retry_iso, video_url)."""
        if isinstance(outcome, PollResult):
            ps = outcome.provider_status
            if ps in ("queued", "submitted"):
                return ("submitted", ps, None, None, None)
            if ps == "processing":
                return ("processing", ps, None, None, None)
            if ps == "completed":
                # PollResult.__post_init__ guarantees a non-empty video_url here.
                return ("completed", ps, None, None, outcome.video_url)
            if ps == "failed":
                return ("failed", ps, None, None, None)
            # not_found — provider no longer knows this id
            return ("reconciliation_required", "", "provider_not_found", None, None)
        if isinstance(outcome, PollAdapterError):
            if outcome.retryable:
                return ("keep", "", outcome.code,
                        _canonical(now + timedelta(seconds=POLL_BACKOFF_SECONDS)), None)
            return ("reconciliation_required", "", outcome.code, None, None)
        raise TypeError(f"unsupported poll outcome type: {type(outcome)!r}")

    # -- crash-recovery title reconciliation (§5.5e3d1) ------------------

    def find_reconciliation_candidates(
        self, now_iso: str
    ) -> list[ReconcileClaim]:
        """Hint scan for operations that may have reached HeyGen without a
        recorded outcome (a maybe-sent submit): in RECONCILE_STATUSES with
        attempt_started_at set, no active lease, and backoff elapsed. This is
        advisory only — claim_reconcile_in_tx re-validates eligibility under
        BEGIN IMMEDIATE before any write."""
        now = _parse_utc(now_iso)
        with self.begin_immediate() as conn:
            rows = conn.execute(
                "SELECT operation_id, lease_fence, heygen_title, attempt_started_at "
                "FROM heygen_operations "
                "WHERE status IN ('submit_pending', 'reconciliation_required') "
                "AND attempt_started_at IS NOT NULL "
                "AND (lease_owner IS NULL OR lease_expires_at IS NULL "
                "     OR lease_expires_at < ?) "
                "AND (next_retry_at IS NULL OR next_retry_at <= ?) "
                "AND (last_error_code IS NULL OR last_error_code <> ?) "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM heygen_resource_operation_refs ref "
                "  JOIN heygen_remote_resources r ON r.resource_id = ref.resource_id "
                "  WHERE ref.operation_id = heygen_operations.operation_id "
                "  AND r.resource_kind = 'video' "
                "  AND r.deletion_status NOT IN ('deletion_pending', 'deleted'))",
                (_canonical(now), _canonical(now), _MANUAL_RECONCILE_CODE),
            ).fetchall()
            return [
                ReconcileClaim(
                    operation_id=r["operation_id"], status="not_ready",
                    fence=r["lease_fence"], heygen_title=r["heygen_title"],
                    attempt_started_at=r["attempt_started_at"],
                )
                for r in rows
            ]

    def claim_reconcile_in_tx(
        self,
        conn: sqlite3.Connection,
        operation_id: str,
        lease_owner: str,
        now_iso: str,
        lease_seconds: int,
    ) -> ReconcileClaim:
        """Claim a reconciliation lease on a maybe-sent submit (an attempt that
        may have reached HeyGen but has no recorded outcome / known remote id).
        Eligibility: status ∈ RECONCILE_STATUSES + attempt_started_at set + NO
        video resource + no active lease + backoff elapsed. A half lease →
        OperationIntegrityError; an active lease → busy. Claim atomically flips
        an ambiguous submit_pending to reconciliation_required and bumps the
        fence. A lost CAS re-classifies with the same rules."""
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        _require_lease_owner(lease_owner)
        now = _parse_utc(now_iso)
        _check_lease_seconds(lease_seconds)
        expires_iso = _canonical(now + timedelta(seconds=lease_seconds))

        row = conn.execute(
            "SELECT status, attempt_started_at, lease_owner, lease_expires_at, "
            "lease_fence, next_retry_at, heygen_title, consent_receipt_digest, "
            "last_error_code FROM heygen_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return ReconcileClaim(operation_id, "not_ready", 0, None, None)
        if row["status"] not in RECONCILE_STATUSES or row["attempt_started_at"] is None:
            return ReconcileClaim(operation_id, "not_ready", row["lease_fence"],
                                  row["heygen_title"], row["attempt_started_at"])
        # A parked permanent-failure op waits for manual recovery — do not
        # hot-loop it.
        if row["last_error_code"] == _MANUAL_RECONCILE_CODE:
            return ReconcileClaim(operation_id, "not_ready", row["lease_fence"],
                                  row["heygen_title"], row["attempt_started_at"])
        # Consent topology: a reconciliation candidate must have a coherent
        # receipt. Reuse the full in-transaction integrity validator (recomputes
        # the receipt digest + checks the receipt↔operation binding), not just a
        # pointer comparison. Tampered receipt content → fail-closed (no query).
        receipt = conn.execute(
            "SELECT * FROM heygen_consent_receipts WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        op_full = conn.execute(
            "SELECT * FROM heygen_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if receipt is None:
            return ReconcileClaim(operation_id, "not_ready", row["lease_fence"],
                                  row["heygen_title"], row["attempt_started_at"])
        if receipt["status"] not in ("granted", "withdrawn"):
            return ReconcileClaim(operation_id, "not_ready", row["lease_fence"],
                                  row["heygen_title"], row["attempt_started_at"])
        ConsentService._validate_existing_integrity(receipt, op_full, conn)
        receipt_is_withdrawn = receipt["status"] == "withdrawn"
        # Video resources: a granted candidate must have NONE (unknown-id path;
        # known-id uses poll). A withdrawn candidate may carry its own cleanup
        # resources (deletion_pending/deleted) so it can keep reconciling for
        # more copies — but no ACTIVE (not_started/failed-deletion) resource.
        active_video = conn.execute(
            "SELECT 1 FROM heygen_remote_resources r "
            "JOIN heygen_resource_operation_refs ref ON ref.resource_id = r.resource_id "
            "WHERE ref.operation_id = ? AND r.resource_kind = 'video' "
            "AND r.deletion_status NOT IN ('deletion_pending', 'deleted') LIMIT 1",
            (operation_id,),
        ).fetchone()
        if active_video is not None or (not receipt_is_withdrawn and _has_any_video(conn, operation_id)):
            return ReconcileClaim(operation_id, "not_ready", row["lease_fence"],
                                  row["heygen_title"], row["attempt_started_at"])

        verdict = self._classify_reconcile_row(row, operation_id, now)
        if verdict is not None:
            return verdict

        new_fence = row["lease_fence"] + 1
        placeholders = ",".join("?" for _ in RECONCILE_STATUSES)
        # Flip ambiguous submit_pending → reconciliation_required as we claim.
        cur = conn.execute(
            "UPDATE heygen_operations SET status = 'reconciliation_required', "
            "lease_owner = ?, lease_expires_at = ?, lease_fence = ?, updated_at = ? "
            "WHERE operation_id = ? AND lease_fence = ? "
            "AND status IN (" + placeholders + ") AND attempt_started_at IS NOT NULL",
            (lease_owner, expires_iso, new_fence, _canonical(now),
             operation_id, row["lease_fence"], *RECONCILE_STATUSES),
        )
        if cur.rowcount == 0:
            row2 = conn.execute(
                "SELECT status, attempt_started_at, lease_owner, lease_expires_at, "
                "lease_fence, next_retry_at, heygen_title FROM heygen_operations "
                "WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row2 is None:
                return ReconcileClaim(operation_id, "not_ready", new_fence, None, None)
            v2 = self._classify_reconcile_row(row2, operation_id, now)
            if v2 is None:
                raise OperationStateError(
                    f"operation {operation_id} became eligible again after a lost reconcile CAS"
                )
            return v2
        return ReconcileClaim(operation_id, "claimed", new_fence,
                              row["heygen_title"], row["attempt_started_at"])

    @staticmethod
    def _classify_reconcile_row(row, operation_id: str, now) -> ReconcileClaim | None:
        if row["status"] not in RECONCILE_STATUSES or row["attempt_started_at"] is None:
            return ReconcileClaim(operation_id, "not_ready", row["lease_fence"],
                                  row["heygen_title"], row["attempt_started_at"])
        owner = row["lease_owner"]
        exp = row["lease_expires_at"]
        if (owner is None) != (exp is None):
            raise OperationIntegrityError(
                f"operation {operation_id} has a half reconcile lease state"
            )
        if owner is not None and _parse_utc(exp) > now:
            return ReconcileClaim(operation_id, "busy", row["lease_fence"],
                                  row["heygen_title"], row["attempt_started_at"])
        nr = row["next_retry_at"]
        if nr is not None and _parse_utc(nr) > now:
            return ReconcileClaim(operation_id, "retry_wait", row["lease_fence"],
                                  row["heygen_title"], row["attempt_started_at"])
        return None

    def apply_reconcile_outcome_in_tx(
        self,
        conn: sqlite3.Connection,
        operation_id: str,
        lease_owner: str,
        fence: int,
        now_iso: str,
        outcome_input,  # TitleQueryResult | TitleQueryAdapterError
    ) -> ReconcileOutcome:
        """Apply a title-reconciliation verdict behind a fenced CAS
        (operation_id + lease_owner + lease_fence + status='reconciliation_required'
        + attempt set). Verdicts:
          exact_found          → write video resource/ref + map candidate status
                                 (completed is NOT finalized here — it lands
                                 submitted/processing and hands to e3c poll to
                                 re-fetch the required video_url)
          definitive_no_match  → cancelled (the granted+cancelled+NULL-pointer
                                 carve-out, last_error_code=reconciliation_no_match)
          cleanup_required     → receipt withdrawn: record every precise candidate
                                 as deletion_pending, cancel, no delivery
          indeterminate        → stay reconciliation_required (+ backoff)
        A fence mismatch writes nothing and returns verdict='fence_conflict'."""
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        _require_lease_owner(lease_owner)
        now = _parse_utc(now_iso)
        now_c = _canonical(now)

        op = conn.execute(
            "SELECT status, attempt_started_at, heygen_title, consent_receipt_digest "
            "FROM heygen_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        receipt = conn.execute(
            "SELECT status FROM heygen_consent_receipts WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if op is None:
            return ReconcileOutcome(operation_id, "indeterminate", 0, "reconciliation_required",
                                    "missing_operation", None, ())
        receipt_status = receipt["status"] if receipt is not None else None
        verdict = self._plan_reconcile(
            outcome_input, op["heygen_title"], op["attempt_started_at"],
            receipt_status, now,
        )

        always = {"lease_owner": None, "lease_expires_at": None, "updated_at": now_c}
        target = verdict["target"]
        if target == "cancelled":
            always.update(status="cancelled", consent_receipt_digest=None,
                          completed_at=now_c, next_retry_at=None,
                          last_error_code=verdict["last_error"])
        elif verdict["verdict"] == "exact_found":
            always.update(status=target, next_retry_at=None, last_error_code=None,
                          completed_at=(now_c if target == "failed" else None))
        else:  # indeterminate — stay reconciliation_required
            always.update(status="reconciliation_required", next_retry_at=verdict["next_retry"],
                          last_error_code=verdict["last_error"], completed_at=None)
        set_clause = ", ".join(f"{col} = ?" for col in always)
        params = list(always.values()) + [operation_id, lease_owner, fence]
        cur = conn.execute(
            f"UPDATE heygen_operations SET {set_clause} "
            "WHERE operation_id = ? AND lease_owner = ? AND lease_fence = ? "
            "AND status = 'reconciliation_required' AND attempt_started_at IS NOT NULL",
            params,
        )
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT lease_fence FROM heygen_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            cur_fence = row["lease_fence"] if row is not None else 0
            return ReconcileOutcome(operation_id, "fence_conflict", cur_fence,
                                    "reconciliation_required", None, None, ())

        written: list[str] = []
        for remote_id, del_status in verdict["register"]:
            self._write_video_resource(conn, operation_id, remote_id, now_c, del_status)
            written.append(remote_id)
        return ReconcileOutcome(
            operation_id, verdict["verdict"], fence, verdict["target"],
            verdict["last_error"], verdict["next_retry"], tuple(written),
        )

    @staticmethod
    def _plan_reconcile(outcome_input, heygen_title, attempt_started_at,
                        receipt_status, now) -> dict:
        """Three-way title verdict. Returns a dict with verdict/target/register
        (list of (remote_id, deletion_status) to write)/last_error/next_retry.
        See apply_reconcile_outcome_in_tx for the contract."""
        a = _parse_utc(attempt_started_at)
        created_after = a - timedelta(seconds=RECONCILE_CLOCK_SKEW_SECONDS)
        window_end = a + timedelta(seconds=RECONCILE_SEARCH_WINDOW_SECONDS + RECONCILE_CLOCK_SKEW_SECONDS)
        backoff = _canonical(now + timedelta(seconds=RECONCILE_BACKOFF_SECONDS))
        withdrawn = receipt_status == "withdrawn"
        window_closed = now >= window_end

        def in_window(c):
            # A precise match: exact title, a real status (a candidate carrying a
            # remote id cannot be 'not_found'), and within the search window.
            return (
                c.title == heygen_title
                and c.provider_status != "not_found"
                and created_after <= _parse_utc(c.created_at) <= window_end
            )

        if isinstance(outcome_input, TitleQueryAdapterError):
            if outcome_input.retryable:
                return {"verdict": "indeterminate", "target": "reconciliation_required",
                        "register": [], "last_error": outcome_input.code, "next_retry": backoff}
            # Permanent failure: park for manual recovery (find/claim exclude this
            # code so maintenance does not hot-loop it).
            return {"verdict": "indeterminate", "target": "reconciliation_required",
                    "register": [], "last_error": _MANUAL_RECONCILE_CODE, "next_retry": None}
        if not isinstance(outcome_input, TitleQueryResult):
            raise TypeError(f"unsupported reconcile outcome type: {type(outcome_input)!r}")

        precise = [c for c in outcome_input.candidates if in_window(c)]

        if withdrawn:
            # Register EVERY precise candidate as deletion_pending — even if the
            # query is incomplete, never lose a discovered remote copy. If the
            # query is incomplete, keep reconciling for more; if complete, cancel.
            register = [(c.remote_id, "deletion_pending") for c in precise]
            if precise and not outcome_input.query_complete:
                return {"verdict": "indeterminate", "target": "reconciliation_required",
                        "register": register, "last_error": _RECONCILE_WITHDRAWN, "next_retry": backoff}
            if precise:  # query complete → all registered, cancel, no delivery
                return {"verdict": "cleanup_required", "target": "cancelled",
                        "register": register, "last_error": _RECONCILE_WITHDRAWN, "next_retry": None}
            if outcome_input.query_complete and window_closed:
                return {"verdict": "definitive_no_match", "target": "cancelled", "register": [],
                        "last_error": _RECONCILE_NO_MATCH, "next_retry": None}
            return {"verdict": "indeterminate", "target": "reconciliation_required", "register": [],
                    "last_error": "title_query_incomplete" if not outcome_input.query_complete
                    else "search_window_open", "next_retry": backoff}

        # granted path
        if not outcome_input.query_complete:
            return {"verdict": "indeterminate", "target": "reconciliation_required", "register": [],
                    "last_error": "title_query_incomplete", "next_retry": backoff}
        if len(precise) == 0:
            if window_closed:
                return {"verdict": "definitive_no_match", "target": "cancelled", "register": [],
                        "last_error": _RECONCILE_NO_MATCH, "next_retry": None}
            return {"verdict": "indeterminate", "target": "reconciliation_required", "register": [],
                    "last_error": "search_window_open", "next_retry": backoff}
        if len(precise) == 1:
            ps = precise[0].provider_status
            target = "failed" if ps == "failed" else ("processing" if ps == "processing" else "submitted")
            return {"verdict": "exact_found", "target": target,
                    "register": [(precise[0].remote_id, "not_started")],
                    "last_error": None, "next_retry": None}
        # Multiple precise matches — never pick arbitrarily.
        return {"verdict": "indeterminate", "target": "reconciliation_required", "register": [],
                "last_error": "multiple_matches", "next_retry": backoff}

    # -- download claim (§5.5e4a1) -------------------------------------

    def claim_download_in_tx(
        self,
        conn: sqlite3.Connection,
        operation_id: str,
        lease_owner: str,
        now_iso: str,
        lease_seconds: int,
    ) -> DownloadClaim:
        """Claim a download lease on a completed operation with exactly one
        exclusive video resource. Eligible download_status: not_started; failed
        with backoff elapsed; or downloading with an EXPIRED lease (crash
        recovery reclaim). A withdrawn receipt (user withdrew after completion,
        before download) refuses the download and flips the resource into
        consent-cleanup (deletion_pending + consent_withdrawal) for e4b. Guards:
        operation status=completed, granted receipt with valid pointer/integrity
        (full validator), exactly one exclusive non-deleted video resource, no
        active lease / backoff. Bumps fence + download_attempts; sets
        download_status='downloading'."""
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        _require_lease_owner(lease_owner)
        now = _parse_utc(now_iso)
        _check_lease_seconds(lease_seconds)
        expires_iso = _canonical(now + timedelta(seconds=lease_seconds))

        row = conn.execute(
            "SELECT status, download_status, download_attempts, lease_owner, "
            "lease_expires_at, lease_fence, next_retry_at, last_error_code "
            "FROM heygen_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None or row["status"] != "completed":
            return DownloadClaim(operation_id, "not_ready", 0, None, None)

        receipt = conn.execute(
            "SELECT * FROM heygen_consent_receipts WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        op_full = conn.execute(
            "SELECT * FROM heygen_operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if receipt is None:
            return DownloadClaim(operation_id, "not_ready", row["lease_fence"], None, None)
        ConsentService._validate_existing_integrity(receipt, op_full, conn)

        # Exactly one exclusive, non-deleted video resource — verified BEFORE any
        # deletion mutation, so a corrupt/shared topology can never send another
        # operation's resource into cleanup.
        resources = conn.execute(
            "SELECT r.resource_id, r.remote_id, r.created_by_operation_id, "
            "r.credential_profile_id, r.deletion_status FROM heygen_remote_resources r "
            "JOIN heygen_resource_operation_refs ref ON ref.resource_id = r.resource_id "
            "WHERE ref.operation_id = ? AND r.resource_kind = 'video'",
            (operation_id,),
        ).fetchall()
        if len(resources) != 1:
            return DownloadClaim(operation_id, "not_ready", row["lease_fence"], None, None)
        res = resources[0]
        if res["created_by_operation_id"] != operation_id:
            raise OperationIntegrityError(
                f"operation {operation_id} downloads a video owned by another operation"
            )
        if res["credential_profile_id"] != op_full["credential_profile_id"]:
            raise OperationIntegrityError("credential_profile_id mismatch on video resource")
        other_ref = conn.execute(
            "SELECT 1 FROM heygen_resource_operation_refs WHERE resource_id = ? AND operation_id <> ?",
            (res["resource_id"], operation_id),
        ).fetchone()
        if other_ref is not None:
            raise OperationIntegrityError("video resource referenced by another operation")
        if res["deletion_status"] in ("deletion_pending", "deleted"):
            return DownloadClaim(operation_id, "not_ready", row["lease_fence"], None, None)

        # Withdrawn after completion → no delivery. Now that the resource is
        # verified exclusive, flip THIS resource_id (not a broad created_by scan)
        # into consent-cleanup for e4b.
        if receipt["status"] == "withdrawn":
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_status = 'deletion_pending', "
                "deletion_reason = 'consent_withdrawal', updated_at = ? WHERE resource_id = ?",
                (_canonical(now), res["resource_id"]),
            )
            return DownloadClaim(operation_id, "consent_withdrawn", row["lease_fence"], None, None)
        if receipt["status"] != "granted":
            return DownloadClaim(operation_id, "not_ready", row["lease_fence"], None, None)

        verdict = self._classify_download_row(row, operation_id, now)
        if verdict is not None:
            return verdict

        new_fence = row["lease_fence"] + 1
        if row["download_status"] == "downloaded":
            # Finalize-recovery: a staged-but-unpublished download. Claim without
            # re-downloading; the processor goes straight to the finalize pass.
            cur = conn.execute(
                "UPDATE heygen_operations SET lease_owner = ?, lease_expires_at = ?, "
                "lease_fence = ?, updated_at = ? WHERE operation_id = ? "
                "AND lease_fence = ? AND status = 'completed' "
                "AND download_status = 'downloaded'",
                (lease_owner, expires_iso, new_fence, _canonical(now),
                 operation_id, row["lease_fence"]),
            )
            if cur.rowcount == 0:
                row2 = conn.execute(
                    "SELECT status, download_status, lease_owner, lease_expires_at, "
                    "lease_fence, next_retry_at FROM heygen_operations WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if row2 is None or row2["status"] != "completed":
                    return DownloadClaim(operation_id, "not_ready", new_fence, None, None)
                v2 = self._classify_download_row(row2, operation_id, now)
                if v2 is None:
                    raise OperationStateError(
                        f"operation {operation_id} became eligible again after a lost finalize CAS"
                    )
                return v2
            return DownloadClaim(operation_id, "finalize", new_fence,
                                 res["remote_id"], res["resource_id"])

        cur = conn.execute(
            "UPDATE heygen_operations SET lease_owner = ?, lease_expires_at = ?, "
            "lease_fence = ?, download_status = 'downloading', "
            "download_attempts = download_attempts + 1, updated_at = ? "
            "WHERE operation_id = ? AND lease_fence = ? AND status = 'completed' "
            "AND download_status IN ('not_started', 'downloading', 'failed')",
            (lease_owner, expires_iso, new_fence, _canonical(now),
             operation_id, row["lease_fence"]),
        )
        if cur.rowcount == 0:
            # Lost a concurrent claim/reclaim — re-read and classify with the
            # SAME function (real fence, half-lease detection, full status set).
            row2 = conn.execute(
                "SELECT status, download_status, lease_owner, lease_expires_at, "
                "lease_fence, next_retry_at FROM heygen_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row2 is None or row2["status"] != "completed":
                return DownloadClaim(operation_id, "not_ready", new_fence, None, None)
            v2 = self._classify_download_row(row2, operation_id, now)
            if v2 is None:
                raise OperationStateError(
                    f"operation {operation_id} became eligible again after a lost download CAS"
                )
            return v2
        return DownloadClaim(operation_id, "claimed", new_fence, res["remote_id"], res["resource_id"])

    @staticmethod
    def _classify_download_row(row, operation_id: str, now) -> DownloadClaim | None:
        """Unified download classification (fast path + lost CAS). Returns a
        terminal DownloadClaim (busy/retry_wait/not_ready) or None if eligible.
        downloaded/verified → not_ready; half lease → fail-closed; returns the
        REAL database fence."""
        ds = row["download_status"]
        if ds == "verified":
            return DownloadClaim(operation_id, "not_ready", row["lease_fence"], None, None)
        # downloaded = staged but not yet published → eligible to claim a
        # finalize pass (lease classification still applies: active → busy).
        owner = row["lease_owner"]
        exp = row["lease_expires_at"]
        if (owner is None) != (exp is None):
            raise OperationIntegrityError(f"operation {operation_id} half download lease")
        if owner is not None and _parse_utc(exp) > now:
            return DownloadClaim(operation_id, "busy", row["lease_fence"], None, None)
        if ds == "failed":
            # Parked for manual recovery → never auto-retry.
            lec = row["last_error_code"] if "last_error_code" in row.keys() else None
            if lec in _DOWNLOAD_MANUAL_CODES:
                return DownloadClaim(operation_id, "not_ready", row["lease_fence"], None, None)
            nr = row["next_retry_at"]
            if nr is not None and _parse_utc(nr) > now:
                return DownloadClaim(operation_id, "retry_wait", row["lease_fence"], None, None)
        return None  # eligible: not_started, failed+backoff-elapsed, or downloading+expired

    # -- download two-phase: stage + finalize (§5.5e4a2) -----------------

    def stage_download_in_tx(self, conn, operation_id, lease_owner, fence,
                             now_iso, prepared, max_bytes=536_870_912):
        """Phase 1: stage the downloaded temp file's ref + digest in the journal
        (download_status='downloaded'). Validates the PreparedDownload as
        UNTRUSTED: exact ref match, exact deterministic temp path, containment,
        non-symlink, digest recompute, strict numeric types, size cap. A
        withdrawn receipt triggers a fenced cleanup (stable terminal state)."""
        import hashlib as _h
        import math as _math
        import os as _os
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        now_c = _canonical(_parse_utc(now_iso))
        op = conn.execute("SELECT * FROM heygen_operations WHERE operation_id=?",
                          (operation_id,)).fetchone()
        receipt = conn.execute("SELECT * FROM heygen_consent_receipts WHERE operation_id=?",
                               (operation_id,)).fetchone()
        if (op is None or op["download_status"] != "downloading"
                or op["lease_owner"] != lease_owner or op["lease_fence"] != fence):
            return DownloadOutcome(operation_id, "fence_conflict", fence, None, None)
        ConsentService._validate_existing_integrity(receipt, op, conn)
        if receipt["status"] == "withdrawn":
            self._cleanup_download_withdrawn(conn, operation_id, lease_owner, fence, now_c)
            return DownloadOutcome(operation_id, "consent_withdrawn", fence,
                                   _RECONCILE_WITHDRAWN, None)
        if receipt["status"] != "granted":
            return DownloadOutcome(operation_id, "fence_conflict", fence, None, None)
        # --- Validate the downloader's output (FULLY UNTRUSTED) ---
        expected_ref = _output_ref(operation_id)
        if prepared.local_output_ref != expected_ref:
            raise OperationIntegrityError(
                f"local_output_ref must be {expected_ref!r}, got {prepared.local_output_ref!r}")
        # Strict numeric types (bool is not int).
        if type(prepared.size_bytes) is not int or prepared.size_bytes <= 0 \
                or prepared.size_bytes > max_bytes:
            raise OperationIntegrityError("invalid size_bytes")
        if type(prepared.media.duration_seconds) is not float \
                or not _math.isfinite(prepared.media.duration_seconds) \
                or prepared.media.duration_seconds <= 0:
            raise OperationIntegrityError("invalid duration_seconds")
        if type(prepared.media.width) is not int or prepared.media.width <= 0:
            raise OperationIntegrityError("invalid width")
        if type(prepared.media.height) is not int or prepared.media.height <= 0:
            raise OperationIntegrityError("invalid height")
        if not isinstance(prepared.media.video_codec, str) or not prepared.media.video_codec.strip():
            raise OperationIntegrityError("invalid video_codec")
        if not prepared.digest.startswith("sha256:") or len(prepared.digest) != 71:
            raise OperationIntegrityError("invalid digest format")
        # Exact deterministic temp path (lexical comparison, not resolve).
        expected_temp = self._project_dir / ".lecturecast" / "runtime" / (expected_ref + ".tmp")
        runtime_root = self._project_dir / ".lecturecast" / "runtime"
        _verify_containment(Path(prepared.temp_path_str), runtime_root)
        import os as _os_mod
        if _os_mod.path.normpath(prepared.temp_path_str) != _os_mod.path.normpath(str(expected_temp)):
            raise OperationIntegrityError("temp path is not the deterministic location")
        temp = Path(prepared.temp_path_str)
        if temp.is_symlink() or not temp.is_file():
            raise OperationIntegrityError("temp must be a regular file, not a symlink")
        # Recompute digest from the actual file.
        h = _h.sha256()
        actual_size = 0
        with open(str(temp), "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
                actual_size += len(chunk)
        if "sha256:" + h.hexdigest() != prepared.digest:
            raise OperationIntegrityError("digest mismatch on recompute")
        if actual_size != prepared.size_bytes:
            raise OperationIntegrityError("size mismatch")
        # --- Stage (fenced CAS) ---
        cur = conn.execute(
            "UPDATE heygen_operations SET download_status='downloaded', "
            "local_output_ref=?, local_output_digest=?, updated_at=? "
            "WHERE operation_id=? AND lease_owner=? AND lease_fence=? "
            "AND download_status='downloading'",
            (prepared.local_output_ref, prepared.digest, now_c,
             operation_id, lease_owner, fence))
        if cur.rowcount == 0:
            return DownloadOutcome(operation_id, "fence_conflict", fence, None, None)
        return DownloadOutcome(operation_id, "staged", fence, None, None)

    def finalize_download_in_tx(self, conn, operation_id, lease_owner, fence,
                                now_iso):
        """Phase 2: publish the staged file and mark verified. Uses ONLY
        deterministic paths derived from self._project_dir + operation_id —
        never trusts external project_dir/temp_path or journal-stored ref beyond
        a strict equality check. Re-validates containment, symlink, digest, and
        consent on the recovery path. Withdrawn → fenced cleanup. Missing file →
        fenced manual-recovery park."""
        import hashlib as _h
        import os as _os
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        now_c = _canonical(_parse_utc(now_iso))
        op = conn.execute("SELECT * FROM heygen_operations WHERE operation_id=?",
                          (operation_id,)).fetchone()
        receipt = conn.execute("SELECT * FROM heygen_consent_receipts WHERE operation_id=?",
                               (operation_id,)).fetchone()
        if (op is None or op["download_status"] != "downloaded"
                or op["lease_owner"] != lease_owner or op["lease_fence"] != fence):
            return DownloadOutcome(operation_id, "fence_conflict",
                                   op["lease_fence"] if op else 0, None, None)
        ConsentService._validate_existing_integrity(receipt, op, conn)
        staged_ref = op["local_output_ref"]
        staged_digest = op["local_output_digest"]
        if receipt["status"] == "withdrawn":
            self._cleanup_download_withdrawn(conn, operation_id, lease_owner, fence, now_c)
            derived_temp = self._project_dir / ".lecturecast" / "runtime" / (_output_ref(operation_id) + ".tmp")
            # Verify containment before unlink — a symlinked intermediate must
            # not let us delete a file outside runtime.
            try:
                _verify_containment(derived_temp, self._project_dir / ".lecturecast" / "runtime")
            except OperationIntegrityError:
                pass  # path is unsafe; skip unlink rather than touch an external file
            else:
                if derived_temp.exists() and not derived_temp.is_symlink():
                    _os.unlink(str(derived_temp))
            return DownloadOutcome(operation_id, "consent_withdrawn", fence,
                                   _RECONCILE_WITHDRAWN, None)
        if receipt["status"] != "granted":
            return DownloadOutcome(operation_id, "fence_conflict", fence, None, None)
        # Strict ref equality — never trust a tampered journal ref for path building.
        expected_ref = _output_ref(operation_id)
        if staged_ref != expected_ref:
            raise OperationIntegrityError("staged local_output_ref mismatch")
        if not staged_digest or not staged_digest.startswith("sha256:"):
            raise OperationIntegrityError("staged digest format invalid")
        runtime = self._project_dir / ".lecturecast" / "runtime"
        temp = runtime / (expected_ref + ".tmp")
        final = runtime / expected_ref
        # Containment + symlink checks on BOTH paths.
        for p in (final, temp):
            if p.is_symlink():
                raise OperationIntegrityError(f"path is a symlink: {p}")
        if _os.path.exists(str(final)):
            _verify_containment(final, runtime)
            if not final.is_file():
                raise OperationIntegrityError("final is not a regular file")
            h = _h.sha256()
            with open(str(final), "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            if h.hexdigest() != staged_digest.split(":")[-1]:
                raise OperationIntegrityError(f"final digest mismatch for {operation_id}")
        elif temp.exists() and not temp.is_symlink() and temp.is_file():
            _verify_containment(temp, runtime)
            _verify_containment(final, runtime)
            # Recompute temp digest BEFORE replace — a temp swapped between stage
            # and recovery must not be published as verified.
            h = _h.sha256()
            with open(str(temp), "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            if "sha256:" + h.hexdigest() != staged_digest:
                raise OperationIntegrityError("temp digest mismatch before replace")
            final.parent.mkdir(parents=True, exist_ok=True)
            _os.replace(str(temp), str(final))
        else:
            # Neither temp nor final — fenced manual-recovery park.
            conn.execute(
                "UPDATE heygen_operations SET download_status='failed', "
                "last_error_code='download_file_missing', next_retry_at=NULL, "
                "lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
                "WHERE operation_id=? AND lease_owner=? AND lease_fence=? "
                "AND download_status='downloaded'",
                (now_c, operation_id, lease_owner, fence))
            return DownloadOutcome(operation_id, "failed", fence,
                                   "download_file_missing", None)
        cur = conn.execute(
            "UPDATE heygen_operations SET download_status='verified', "
            "download_verified_at=?, lease_owner=NULL, lease_expires_at=NULL, "
            "updated_at=? WHERE operation_id=? AND lease_owner=? AND lease_fence=? "
            "AND download_status='downloaded'",
            (now_c, now_c, operation_id, lease_owner, fence))
        if cur.rowcount == 0:
            return DownloadOutcome(operation_id, "fence_conflict", fence, None, None)
        return DownloadOutcome(operation_id, "verified", fence, None, None)

    def _cleanup_download_withdrawn(self, conn, operation_id, lease_owner,
                                    fence, now_c):
        """Fenced atomic cleanup: flip to a stable terminal 'failed' state,
        clear the lease, and route the video resource to deletion_pending. The
        full exclusive-ownership check runs FIRST; ANY anomaly (wrong count,
        wrong owner, credential mismatch, shared ref, wrong deletion_status)
        raises OperationIntegrityError so the whole tx rolls back — no
        half-completed state reaches the journal."""
        # --- Validate topology BEFORE mutating ---
        op_row = conn.execute(
            "SELECT credential_profile_id FROM heygen_operations WHERE operation_id=?",
            (operation_id,)).fetchone()
        if op_row is None:
            raise OperationIntegrityError(f"operation {operation_id} vanished during cleanup")
        resources = conn.execute(
            "SELECT r.resource_id, r.created_by_operation_id, "
            "r.credential_profile_id, r.deletion_status FROM heygen_remote_resources r "
            "JOIN heygen_resource_operation_refs ref ON ref.resource_id=r.resource_id "
            "WHERE ref.operation_id=? AND r.resource_kind='video'",
            (operation_id,)).fetchall()
        if len(resources) != 1:
            raise OperationIntegrityError(
                f"expected exactly 1 video resource for {operation_id}, found {len(resources)}")
        res = resources[0]
        if res["created_by_operation_id"] != operation_id:
            raise OperationIntegrityError("cleanup resource owned by another operation")
        if res["credential_profile_id"] != op_row["credential_profile_id"]:
            raise OperationIntegrityError("cleanup resource credential mismatch")
        if res["deletion_status"] != "not_started":
            raise OperationIntegrityError(
                f"cleanup resource already in deletion state: {res['deletion_status']!r}")
        other_ref = conn.execute(
            "SELECT 1 FROM heygen_resource_operation_refs "
            "WHERE resource_id=? AND operation_id<>?",
            (res["resource_id"], operation_id)).fetchone()
        if other_ref is not None:
            raise OperationIntegrityError(
                f"resource {res['resource_id']} referenced by another operation during cleanup")
        # --- All checks pass: atomic update operation + resource ---
        conn.execute(
            "UPDATE heygen_operations SET download_status='failed', "
            "last_error_code='consent_withdrawn_cleanup_required', next_retry_at=NULL, "
            "lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
            "WHERE operation_id=? AND lease_owner=? AND lease_fence=?",
            (now_c, operation_id, lease_owner, fence))
        conn.execute(
            "UPDATE heygen_remote_resources SET deletion_status='deletion_pending', "
            "deletion_reason='consent_withdrawal', updated_at=? WHERE resource_id=?",
            (now_c, res["resource_id"]))

    def apply_download_failure_in_tx(self, conn, operation_id, lease_owner,
                                     fence, now_iso, error_code, next_retry_iso):
        """Mark a download attempt failed (URL not ready / poll error / download
        error). Clears the lease; sets backoff or parks for manual recovery."""
        self._require_tx(conn)
        now_c = _canonical(_parse_utc(now_iso))
        cur = conn.execute(
            "UPDATE heygen_operations SET download_status='failed', "
            "last_error_code=?, next_retry_at=?, lease_owner=NULL, "
            "lease_expires_at=NULL, updated_at=? WHERE operation_id=? "
            "AND lease_owner=? AND lease_fence=? AND status='completed' "
            "AND download_status='downloading'",
            (error_code, next_retry_iso, now_c, operation_id, lease_owner, fence))
        if cur.rowcount == 0:
            return DownloadOutcome(operation_id, "fence_conflict", fence, None, None)
        return DownloadOutcome(operation_id, "failed", fence, error_code, next_retry_iso)

    @staticmethod
    def _exclusive_video(conn, operation_id):
        rows = conn.execute(
            "SELECT r.resource_id FROM heygen_remote_resources r "
            "JOIN heygen_resource_operation_refs ref ON ref.resource_id=r.resource_id "
            "WHERE ref.operation_id=? AND r.resource_kind='video'",
            (operation_id,)).fetchall()
        if len(rows) != 1:
            return None
        return rows[0]

    # -- deletion lifecycle (§5.5e4b) -----------------------------------

    def claim_deletion_in_tx(self, conn, operation_id, resource_id, lease_owner,
                             now_iso, lease_seconds, max_attempts=DELETION_MAX_ATTEMPTS):
        """Claim a per-resource deletion lease. Eligibility:
        - not_started + ephemeral + op.download_status=verified → post_download
        - deletion_pending (consent_withdrawal or post_download reclaim) → eligible
        - deletion_failed + backoff elapsed + < max + not manual → retry
        Bumps fence + deletion_attempts; sets deletion_status='deletion_pending'."""
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        _require_lease_owner(lease_owner)
        if type(max_attempts) is not int or not (1 <= max_attempts <= 10):
            raise ValueError("max_attempts must be an int in [1, 10]")
        now = _parse_utc(now_iso)
        _check_lease_seconds(lease_seconds)
        expires_iso = _canonical(now + timedelta(seconds=lease_seconds))

        op = conn.execute(
            "SELECT download_status, lease_owner, lease_expires_at, lease_fence, "
            "credential_profile_id FROM heygen_operations WHERE operation_id=?",
            (operation_id,)).fetchone()
        if op is None:
            return DeletionClaim(operation_id, resource_id, "not_ready", 0, None)
        res = conn.execute(
            "SELECT resource_id, remote_id, deletion_status, deletion_reason, "
            "retention_mode, created_by_operation_id, credential_profile_id, "
            "deletion_attempts, deletion_next_retry_at, last_deletion_error "
            "FROM heygen_remote_resources WHERE resource_id=? AND resource_kind='video'",
            (resource_id,)).fetchone()
        if res is None or res["created_by_operation_id"] != operation_id:
            return DeletionClaim(operation_id, resource_id, "not_ready", 0, None)
        if res["credential_profile_id"] != op["credential_profile_id"]:
            raise OperationIntegrityError("credential mismatch on deletion resource")
        # This operation's ref must exist (exclusive binding).
        own_ref = conn.execute(
            "SELECT 1 FROM heygen_resource_operation_refs WHERE resource_id=? AND operation_id=?",
            (resource_id, operation_id)).fetchone()
        if own_ref is None:
            return DeletionClaim(operation_id, resource_id, "not_ready", op["lease_fence"], None)
        other_ref = conn.execute(
            "SELECT 1 FROM heygen_resource_operation_refs WHERE resource_id=? AND operation_id<>?",
            (resource_id, operation_id)).fetchone()
        if other_ref is not None:
            raise OperationIntegrityError("resource referenced by another operation")

        ds = res["deletion_status"]
        # Eligibility gate
        if ds == "deleted":
            return DeletionClaim(operation_id, resource_id, "not_ready", op["lease_fence"], None)
        if ds == "not_started":
            if res["retention_mode"] != "ephemeral" or op["download_status"] != "verified":
                return DeletionClaim(operation_id, resource_id, "not_ready", op["lease_fence"], None)
            # Normal post-download: exactly one deliverable video per operation.
            if not self._single_video(conn, operation_id):
                return DeletionClaim(operation_id, resource_id, "not_ready", op["lease_fence"], None)
        elif ds == "deletion_failed":
            lec = res["last_deletion_error"]
            if lec in _DELETION_MANUAL_CODES:
                return DeletionClaim(operation_id, resource_id, "not_ready", op["lease_fence"], None)
            if res["deletion_attempts"] >= max_attempts:
                return DeletionClaim(operation_id, resource_id, "not_ready", op["lease_fence"], None)
            nr = res["deletion_next_retry_at"]
            if nr is not None and _parse_utc(nr) > now:
                return DeletionClaim(operation_id, resource_id, "retry_wait", op["lease_fence"], None)
            # Inherit the original reason's eligibility (a retry must still
            # satisfy the same gate that allowed the first attempt).
            freason = res["deletion_reason"]
            if freason == "consent_withdrawal":
                pass
            elif freason == "post_download":
                if res["retention_mode"] != "ephemeral" or op["download_status"] != "verified":
                    return DeletionClaim(operation_id, resource_id, "not_ready", op["lease_fence"], None)
                if not self._single_video(conn, operation_id):
                    return DeletionClaim(operation_id, resource_id, "not_ready", op["lease_fence"], None)
            elif freason == "manual_force":
                return DeletionClaim(operation_id, resource_id, "not_ready", op["lease_fence"], None)
            else:
                raise OperationIntegrityError(f"deletion_failed with unknown reason: {freason!r}")
        elif ds == "deletion_pending":
            # Gate by deletion_reason — not all pending resources are auto-deletable.
            reason = res["deletion_reason"]
            if reason == "consent_withdrawal":
                pass  # eligible regardless of retention/verified
            elif reason == "post_download":
                if res["retention_mode"] != "ephemeral" or op["download_status"] != "verified":
                    return DeletionClaim(operation_id, resource_id, "not_ready", op["lease_fence"], None)
                if not self._single_video(conn, operation_id):
                    return DeletionClaim(operation_id, resource_id, "not_ready", op["lease_fence"], None)
            elif reason == "manual_force":
                return DeletionClaim(operation_id, resource_id, "not_ready", op["lease_fence"], None)
            else:
                raise OperationIntegrityError(f"unknown/null deletion_reason: {reason!r}")
        # Lease classification
        owner = op["lease_owner"]; exp = op["lease_expires_at"]
        if (owner is None) != (exp is None):
            raise OperationIntegrityError(f"operation {operation_id} half lease")
        if owner is not None and _parse_utc(exp) > now:
            return DeletionClaim(operation_id, resource_id, "busy", op["lease_fence"], None)

        new_fence = op["lease_fence"] + 1
        reason = "post_download" if ds == "not_started" else res["deletion_reason"]
        cur = conn.execute(
            "UPDATE heygen_remote_resources SET deletion_status='deletion_pending', "
            "deletion_reason=?, deletion_attempts=deletion_attempts+1, updated_at=? "
            "WHERE resource_id=? AND deletion_status=?",
            (reason, _canonical(now), resource_id, ds))
        if cur.rowcount == 0:
            return DeletionClaim(operation_id, resource_id, "busy", new_fence, None)
        conn.execute(
            "UPDATE heygen_operations SET lease_owner=?, lease_expires_at=?, "
            "lease_fence=?, updated_at=? WHERE operation_id=?",
            (lease_owner, expires_iso, new_fence, _canonical(now), operation_id))
        return DeletionClaim(operation_id, resource_id, "claimed", new_fence, res["remote_id"])

    def apply_deletion_outcome_in_tx(self, conn, operation_id, resource_id,
                                     lease_owner, fence, now_iso, outcome,
                                     max_attempts=DELETION_MAX_ATTEMPTS):
        """Apply a deletion outcome (fenced on operation lease + resource
        deletion_status='deletion_pending'). Maps DeleteResult/DeleteAdapterError
        to deleted/failed states."""
        from lecturecast.heygen_adapter import DeleteResult, DeleteAdapterError
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        now_c = _canonical(_parse_utc(now_iso))
        if type(max_attempts) is not int or not (1 <= max_attempts <= 10):
            raise ValueError("max_attempts must be an int in [1, 10]")
        # Fence CAS on operation
        op = conn.execute(
            "SELECT lease_fence FROM heygen_operations WHERE operation_id=? "
            "AND lease_owner=? AND lease_fence=?", (operation_id, lease_owner, fence)).fetchone()
        if op is None:
            return DeletionOutcome(operation_id, resource_id, "fence_conflict", fence, None, None)
        # Re-verify FULL exclusive topology (claim and apply are separate txs).
        res = conn.execute(
            "SELECT r.deletion_attempts FROM heygen_remote_resources r "
            "WHERE r.resource_id=? AND r.resource_kind='video' "
            "AND r.deletion_status='deletion_pending' "
            "AND r.created_by_operation_id=? "
            "AND r.credential_profile_id=(SELECT o.credential_profile_id "
            "  FROM heygen_operations o WHERE o.operation_id=?) "
            "AND EXISTS (SELECT 1 FROM heygen_resource_operation_refs ref "
            "  WHERE ref.resource_id=r.resource_id AND ref.operation_id=?) "
            "AND NOT EXISTS (SELECT 1 FROM heygen_resource_operation_refs ref2 "
            "  WHERE ref2.resource_id=r.resource_id AND ref2.operation_id<>?)",
            (resource_id, operation_id, operation_id, operation_id, operation_id)).fetchone()
        if res is None:
            return DeletionOutcome(operation_id, resource_id, "fence_conflict", fence, None, None)
        # If the resource's deletion_reason is post_download, re-check single-video.
        reason_row = conn.execute(
            "SELECT deletion_reason FROM heygen_remote_resources WHERE resource_id=?",
            (resource_id,)).fetchone()
        if reason_row and reason_row["deletion_reason"] == "post_download"                 and not self._single_video(conn, operation_id):
            return DeletionOutcome(operation_id, resource_id, "fence_conflict", fence, None, None)
        # Gated UPDATE condition shared by all outcomes.
        gate = ("resource_kind='video' AND created_by_operation_id=? "
                "AND deletion_status='deletion_pending'")

        if isinstance(outcome, DeleteResult):
            cur = conn.execute(
                "UPDATE heygen_remote_resources SET deletion_status='deleted', deleted_at=?, "
                "deletion_next_retry_at=NULL, last_deletion_error=NULL, updated_at=? "
                "WHERE resource_id=? AND " + gate,
                (now_c, now_c, resource_id, operation_id))
            if cur.rowcount == 0:
                return DeletionOutcome(operation_id, resource_id, "fence_conflict", fence, None, None)
            self._clear_operation_lease(conn, operation_id, now_c)
            return DeletionOutcome(operation_id, resource_id, "deleted", fence, None, None)
        # DeleteAdapterError
        attempts = res["deletion_attempts"]
        if outcome.retryable and attempts < max_attempts:
            retry = _canonical(_parse_utc(now_iso) + timedelta(seconds=DELETION_BACKOFF_SECONDS))
            cur = conn.execute(
                "UPDATE heygen_remote_resources SET deletion_status='deletion_failed', "
                "last_deletion_error=?, deletion_next_retry_at=?, updated_at=? "
                "WHERE resource_id=? AND " + gate,
                (outcome.code, retry, now_c, resource_id, operation_id))
            if cur.rowcount == 0:
                return DeletionOutcome(operation_id, resource_id, "fence_conflict", fence, None, None)
            self._clear_operation_lease(conn, operation_id, now_c)
            return DeletionOutcome(operation_id, resource_id, "failed", fence, outcome.code, retry)
        # Exhausted or permanent
        code = "deletion_retry_exhausted" if outcome.retryable else "deletion_reconciliation_required"
        cur = conn.execute(
            "UPDATE heygen_remote_resources SET deletion_status='deletion_failed', "
            "last_deletion_error=?, deletion_next_retry_at=NULL, updated_at=? "
            "WHERE resource_id=? AND " + gate,
            (code, now_c, resource_id, operation_id))
        if cur.rowcount == 0:
            return DeletionOutcome(operation_id, resource_id, "fence_conflict", fence, None, None)
        self._clear_operation_lease(conn, operation_id, now_c)
        return DeletionOutcome(operation_id, resource_id, "failed", fence, code, None)

    @staticmethod
    def _single_video(conn, operation_id):
        count = conn.execute(
            "SELECT COUNT(*) FROM heygen_remote_resources r "
            "JOIN heygen_resource_operation_refs ref ON ref.resource_id=r.resource_id "
            "WHERE ref.operation_id=? AND r.resource_kind='video'",
            (operation_id,)).fetchone()[0]
        return count == 1

    @staticmethod
    def _clear_operation_lease(conn, operation_id, now_c):
        conn.execute(
            "UPDATE heygen_operations SET lease_owner=NULL, lease_expires_at=NULL, "
            "updated_at=? WHERE operation_id=?", (now_c, operation_id))

    # === asset upload lifecycle (§5.5e5b0c) ==============================

    def claim_asset_upload_in_tx(
        self, conn: sqlite3.Connection, *, upload_id: str,
        parent_operation_id: str, asset_role: str, content_digest: str,
        local_ref: str, content_type: str, size_bytes: int,
        provider_filename: str, idempotency_key: str,
        lease_owner: str, now_iso: str, lease_seconds: int,
    ) -> AssetClaimResult:
        """Claim an asset upload row for one attempt. Idempotent on upload_id:
        every immutable field must match on replay (not just the idempotency
        key). Classification:
          uploaded          → 'done' (carry remote_resource_id; verified non-null)
          failed/cancelled/
          manual_reconcile  → 'terminal'
          reconciliation_required
            within 24h      → reclaimable ('claimed' on expiry)
            past 24h         → promoted to manual ('terminal') before provider call
          active lease      → 'busy'
          backoff not elapsed → 'retry_wait'
          else              → 'claimed' (bump fence + attempts, set lease)
        Half lease states (one of owner/expires set) fail closed.
        """
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        _require_lease_owner(lease_owner)
        now = _parse_utc(now_iso)
        _check_lease_seconds(lease_seconds)
        expires_iso = _canonical(now + timedelta(seconds=lease_seconds))
        row = conn.execute(
            "SELECT * FROM heygen_asset_uploads WHERE upload_id = ?",
            (upload_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO heygen_asset_uploads ("
                "  upload_id, parent_operation_id, asset_role, content_digest,"
                "  local_ref, content_type, size_bytes, provider_filename,"
                "  idempotency_key, status, attempts, lease_owner, lease_expires_at,"
                "  lease_fence, attempt_started_at, created_at, updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?, 'uploading', 1, ?, ?, 1, ?, ?, ?)",
                (upload_id, parent_operation_id, asset_role, content_digest,
                 local_ref, content_type, size_bytes, provider_filename,
                 idempotency_key, lease_owner, expires_iso, now_iso, now_iso, now_iso),
            )
            return AssetClaimResult(upload_id, "claimed", 1, 1, expires_iso, None)
        # Replay: every immutable field must match the stored row.
        for col, val in {
            "parent_operation_id": parent_operation_id, "asset_role": asset_role,
            "content_digest": content_digest, "local_ref": local_ref,
            "content_type": content_type, "size_bytes": size_bytes,
            "provider_filename": provider_filename,
            "idempotency_key": idempotency_key,
        }.items():
            if row[col] != val:
                raise OperationIntegrityError(
                    f"asset upload {col} mismatch on replay for {upload_id!r}")
        if row["status"] == "uploaded":
            if row["remote_resource_id"] is None:
                raise OperationIntegrityError(
                    f"uploaded asset upload {upload_id!r} has no remote_resource_id")
            return AssetClaimResult(upload_id, "done", row["lease_fence"],
                                   row["attempts"], None, row["remote_resource_id"])
        if row["status"] in ("failed", "cancelled", "manual_reconciliation_required"):
            return AssetClaimResult(upload_id, "terminal", row["lease_fence"],
                                   row["attempts"], None, None)
        # reconciliation_required past the 24h replay window → promote to manual
        # (terminal) BEFORE attempting any provider call (no blind retransmit).
        if row["status"] == "reconciliation_required":
            exp = row["idempotency_expires_at"]
            if exp is not None and _parse_utc(exp) <= now:
                conn.execute(
                    "UPDATE heygen_asset_uploads SET "
                    "status='manual_reconciliation_required', updated_at=? "
                    "WHERE upload_id=?", (now_iso, upload_id))
                return AssetClaimResult(upload_id, "terminal", row["lease_fence"],
                                       row["attempts"], None, None)
        # Retry-backoff gate.
        nr = row["next_retry_at"]
        if nr is not None and _parse_utc(nr) > now:
            return AssetClaimResult(upload_id, "retry_wait", row["lease_fence"],
                                   row["attempts"], None, None)
        # Half lease → corrupt topology, fail closed.
        owner, lease_exp, att = (row["lease_owner"], row["lease_expires_at"],
                                 row["attempt_started_at"])
        if (owner is None) != (lease_exp is None):
            raise OperationIntegrityError(
                f"asset upload {upload_id!r} has a half lease state")
        if att is not None and owner is not None and _parse_utc(lease_exp) > now:
            return AssetClaimResult(upload_id, "busy", row["lease_fence"],
                                   row["attempts"], lease_exp, None)
        # Crash-after-send protection: a worker that died after the provider
        # call but before apply_failure leaves the row in 'uploading' with an
        # expired lease and no idempotency_expires_at. If the attempt started
        # more than 24h ago, the HeyGen replay window has closed — we MUST NOT
        # reclaim and re-upload (that could create a duplicate remote asset).
        # Promote to manual_reconciliation_required instead.
        if att is not None:
            attempt_send_cutoff = _parse_utc(att) + timedelta(
                seconds=_ASSET_IDEMPOTENCY_WINDOW_SECONDS)
            if now >= attempt_send_cutoff:
                conn.execute(
                    "UPDATE heygen_asset_uploads SET "
                    "status='manual_reconciliation_required', "
                    "maybe_sent_at=COALESCE(maybe_sent_at, attempt_started_at), "
                    "idempotency_expires_at=COALESCE(idempotency_expires_at, ?), "
                    "lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
                    "WHERE upload_id=?",
                    (_canonical(attempt_send_cutoff), now_iso, upload_id))
                return AssetClaimResult(upload_id, "terminal", row["lease_fence"],
                                       row["attempts"], None, None)
        new_fence = row["lease_fence"] + 1
        new_attempts = row["attempts"] + 1
        conn.execute(
            "UPDATE heygen_asset_uploads SET status='uploading', attempts=?, "
            "lease_owner=?, lease_expires_at=?, lease_fence=?, attempt_started_at=?, "
            "next_retry_at=NULL, updated_at=? WHERE upload_id=?",
            (new_attempts, lease_owner, expires_iso, new_fence, now_iso, now_iso,
             upload_id),
        )
        return AssetClaimResult(upload_id, "claimed", new_fence, new_attempts,
                               expires_iso, None)

    def apply_asset_outcome_in_tx(
        self, conn: sqlite3.Connection, *, upload_id: str, asset_id: str,
        retention_mode: str, credential_profile_id: str, now_iso: str,
        lease_owner: str, expected_fence: int,
    ) -> int:
        """On a successful upload: insert the remote_resource + a parent-op ref,
        backfill remote_resource_id, and mark the upload 'uploaded' — all in the
        caller's fenced tx. The status flip is guarded by a CAS on the claim's
        lease_owner + fence (a stale worker cannot overwrite a newer claim).
        resource_kind is derived from asset_role (portrait_photo→portrait_asset,
        audio→audio_asset). Idempotent: a re-apply on an already-uploaded row is
        a no-op returning the existing resource_id (asset_id must match)."""
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        _require_lease_owner(lease_owner)
        if not isinstance(expected_fence, int) or expected_fence < 0:
            raise ValueError("expected_fence must be a non-negative int")
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise ValueError("asset_id must be a non-empty string")
        row = conn.execute(
            "SELECT parent_operation_id, asset_role, status, "
            "remote_resource_id FROM heygen_asset_uploads WHERE upload_id = ?",
            (upload_id,),
        ).fetchone()
        if row is None:
            raise OperationStateError(f"no asset upload {upload_id!r}")
        if row["status"] == "uploaded":
            existing = conn.execute(
                "SELECT remote_id FROM heygen_remote_resources WHERE resource_id=?",
                (row["remote_resource_id"],),
            ).fetchone()
            if existing is None or existing["remote_id"] != asset_id.strip():
                raise OperationIntegrityError(
                    f"asset upload {upload_id!r} already bound to a different remote id")
            return row["remote_resource_id"]
        resource_kind = _asset_resource_kind(row["asset_role"])
        cur = conn.execute(
            "INSERT INTO heygen_remote_resources ("
            "  credential_profile_id, resource_kind, remote_id, retention_mode,"
            "  created_by_operation_id, created_at, updated_at"
            ") VALUES (?,?,?,?,?,?,?)",
            (credential_profile_id, resource_kind, asset_id.strip(), retention_mode,
             row["parent_operation_id"], now_iso, now_iso),
        )
        resource_id = cur.lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO heygen_resource_operation_refs "
            "(resource_id, operation_id, created_at) VALUES (?,?,?)",
            (resource_id, row["parent_operation_id"], now_iso),
        )
        cur = conn.execute(
            "UPDATE heygen_asset_uploads SET status='uploaded', remote_resource_id=?, "
            "uploaded_at=?, lease_owner=NULL, lease_expires_at=NULL, "
            "attempt_started_at=NULL, next_retry_at=NULL, last_error_code=NULL, "
            "updated_at=? WHERE upload_id=? AND status='uploading' AND lease_owner=? "
            "AND lease_fence=? AND attempt_started_at IS NOT NULL",
            (resource_id, now_iso, now_iso, upload_id, lease_owner, expected_fence),
        )
        if cur.rowcount != 1:
            raise OperationIntegrityError(
                f"asset upload {upload_id!r} fence conflict — lease no longer held "
                f"(owner={lease_owner!r}, fence={expected_fence})")
        return resource_id

    def apply_asset_upload_failure_in_tx(
        self, conn: sqlite3.Connection, *, upload_id: str, error_code: str,
        submission_certainty: str, retryable: bool, now_iso: str,
        lease_owner: str, expected_fence: int, backoff_seconds: int = 30,
    ) -> str:
        """Record a failed upload attempt, guarded by the same lease CAS as
        apply_asset_outcome (a stale worker cannot flip a newer claim's state).
        attempts/fence increment only at claim, never here. maybe_sent →
        reconciliation_required within the 24h idempotency window (or
        manual_reconciliation_required past it — never blind-retransmit);
        not_sent → upload_pending (retryable backoff, attempt cleared) or failed
        (permanent). Returns the new status."""
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        _require_lease_owner(lease_owner)
        if submission_certainty not in ("not_sent", "maybe_sent"):
            raise ValueError(f"unknown submission_certainty: {submission_certainty!r}")
        row = conn.execute(
            "SELECT status, maybe_sent_at, idempotency_expires_at, "
            "attempt_started_at FROM heygen_asset_uploads WHERE upload_id=?",
            (upload_id,),
        ).fetchone()
        if row is None:
            raise OperationStateError(f"no asset upload {upload_id!r}")
        if row["status"] == "uploaded":
            return "uploaded"
        now = _parse_utc(now_iso)
        if submission_certainty == "maybe_sent":
            # Freeze the 24h replay window at the FIRST possible-send moment:
            # the attempt's start time (earlier of maybe_sent_at /
            # attempt_started_at), never the failure-land time.
            maybe_sent = row["maybe_sent_at"] or row["attempt_started_at"] or now_iso
            expires = row["idempotency_expires_at"] or _canonical(
                _parse_utc(maybe_sent) + timedelta(
                    seconds=_ASSET_IDEMPOTENCY_WINDOW_SECONDS))
            if _parse_utc(expires) <= now:
                status = "manual_reconciliation_required"
                next_retry = None
            else:
                status = "reconciliation_required"
                next_retry = _canonical(now + timedelta(seconds=backoff_seconds))
        elif retryable:
            status = "upload_pending"
            next_retry = _canonical(now + timedelta(seconds=backoff_seconds))
        else:
            status = "failed"
            next_retry = None
        cur = conn.execute(
            "UPDATE heygen_asset_uploads SET status=?, last_error_code=?, "
            "maybe_sent_at=COALESCE(maybe_sent_at, ?), idempotency_expires_at=COALESCE("
            "idempotency_expires_at, ?), next_retry_at=?, lease_owner=NULL, "
            "lease_expires_at=NULL, attempt_started_at=NULL, updated_at=? "
            "WHERE upload_id=? AND status='uploading' AND lease_owner=? AND "
            "lease_fence=? AND attempt_started_at IS NOT NULL",
            (status, error_code, maybe_sent if submission_certainty == "maybe_sent"
             else None, expires if submission_certainty == "maybe_sent"
             else None, next_retry, now_iso, upload_id, lease_owner, expected_fence),
        )
        if cur.rowcount != 1:
            raise OperationIntegrityError(
                f"asset upload {upload_id!r} fence conflict — lease no longer held "
                f"(owner={lease_owner!r}, fence={expected_fence})")
        return status


def _output_ref(operation_id: str) -> str:
    return f"outputs/heygen/{operation_id}.mp4"


def _verify_containment(path: Path, runtime_root: Path) -> None:
    """Two-layer path-safety check:
    Layer 1 (lexical): path.relative_to(runtime_root) on the NON-resolved paths;
    then lstat each intermediate directory component — reject any symlink
    (catches a symlinked outputs/ redirecting the deterministic leaf).
    Layer 2 (resolved): path.resolve() must still be under
    runtime_root.resolve() (catches ancestor symlinks like /var → /private/var
    that are legitimate, while rejecting anything that genuinely escapes)."""
    # Layer 1: lexical containment.
    try:
        rel_parts = path.relative_to(runtime_root).parts
    except ValueError:
        raise OperationIntegrityError(f"path escapes runtime (lexical): {path}")
    # lstat each intermediate directory component (runtime/outputs, runtime/outputs/heygen).
    current = runtime_root
    for part in rel_parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise OperationIntegrityError(f"symlink in directory chain: {current}")
    # Layer 2: resolved containment.
    if not path.resolve().is_relative_to(runtime_root.resolve()):
        raise OperationIntegrityError(f"path escapes runtime (resolved): {path.resolve()}")


# --- asset upload lifecycle (§5.5e5b0c) ---------------------------------
#
# Asset uploads have their own claim/apply/failure primitives, mirroring the
# video submit flow but on heygen_asset_uploads. Crash recovery: a lost
# response (maybe_sent) is reconciled by re-issuing the idempotent upload
# within HeyGen's 24h window; past the window with no asset_id the upload
# goes manual_reconciliation_required (never blind-retransmit → no duplicates).

_ASSET_ROLE_TO_RESOURCE_KIND = {
    "portrait_photo": "portrait_asset",
    "synthetic_narration_audio": "audio_asset",
}

# Default HeyGen idempotency-key replay window.
_ASSET_IDEMPOTENCY_WINDOW_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class AssetClaimResult:
    upload_id: str
    status: str  # "claimed" | "busy" | "done" | "terminal" | "retry_wait"
    fence: int
    attempts: int
    lease_expires_at: str | None
    remote_resource_id: int | None


def _asset_resource_kind(asset_role: str) -> str:
    kind = _ASSET_ROLE_TO_RESOURCE_KIND.get(asset_role)
    if kind is None:
        raise OperationIntegrityError(f"no resource_kind mapping for {asset_role!r}")
    return kind


# --- coordinator -------------------------------------------------------


class ReconcileProcessor:
    """One title-reconciliation step of an unknown-id maybe-sent operation:
    claim → query HeyGen by title outside any tx → fenced verdict apply."""

    def __init__(self, project_dir: str | Path) -> None:
        self._project_dir = Path(project_dir)
        self._repository = OperationRepository(self._project_dir)

    def reconcile_once(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        adapter,
        now_iso: str,
        lease_seconds: int,
    ) -> ReconcileOnceResult:
        with self._repository.begin_immediate() as conn:
            claim = self._repository.claim_reconcile_in_tx(
                conn, operation_id, lease_owner, now_iso, lease_seconds,
            )
        if claim.status != "claimed" or not claim.heygen_title or not claim.attempt_started_at:
            return ReconcileOnceResult(claim=claim, outcome=None)
        a = _parse_utc(claim.attempt_started_at)
        created_after = _canonical(a - timedelta(seconds=RECONCILE_CLOCK_SKEW_SECONDS))
        created_before = _canonical(
            a + timedelta(seconds=RECONCILE_SEARCH_WINDOW_SECONDS + RECONCILE_CLOCK_SKEW_SECONDS)
        )
        query = TitleQuery(heygen_title=claim.heygen_title,
                           created_after=created_after, created_before=created_before)
        try:
            outcome_input = adapter.query_videos_by_title(query)
        except TitleQueryAdapterError as exc:
            outcome_input = exc
        with self._repository.begin_immediate() as conn:
            outcome = self._repository.apply_reconcile_outcome_in_tx(
                conn, operation_id, lease_owner, claim.fence, now_iso, outcome_input,
            )
        return ReconcileOnceResult(claim=claim, outcome=outcome)


class PollProcessor:
    """One poll step of a known-remote-id operation. The lease is claimed in one
    tx, the adapter is called outside any tx, and the outcome is applied in a
    second fenced tx. No scheduler loop here — callers drive poll_once."""

    def __init__(self, project_dir: str | Path) -> None:
        self._project_dir = Path(project_dir)
        self._repository = OperationRepository(self._project_dir)

    def poll_once(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        adapter,
        now_iso: str,
        lease_seconds: int,
    ) -> PollOnceResult:
        with self._repository.begin_immediate() as conn:
            claim = self._repository.claim_poll_in_tx(
                conn, operation_id, lease_owner, now_iso, lease_seconds,
            )
        if claim.status != "claimed" or claim.remote_id is None:
            return PollOnceResult(claim=claim, outcome=None)
        try:
            outcome_input = adapter.poll_video(claim.remote_id)
        except PollAdapterError as exc:
            outcome_input = exc
        with self._repository.begin_immediate() as conn:
            outcome = self._repository.apply_poll_outcome_in_tx(
                conn, operation_id, lease_owner, claim.fence, now_iso, outcome_input,
            )
        return PollOnceResult(claim=claim, outcome=outcome)


class SubmitProcessor:
    """Records the outcome of a submit attempt. The adapter call happens
    OUTSIDE any transaction (the worker holds the claim lease across it); this
    method opens its own fenced transaction to apply the outcome."""

    def __init__(self, project_dir: str | Path) -> None:
        self._project_dir = Path(project_dir)
        self._repository = OperationRepository(self._project_dir)

    def record_submit_outcome(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        fence: int,
        now_iso: str,
        outcome: SubmitAccepted | HeyGenAdapterError,
    ) -> SubmitOutcome:
        with self._repository.begin_immediate() as conn:
            return self._repository.apply_submit_outcome_in_tx(
                conn, operation_id, lease_owner, fence, now_iso, outcome,
            )


class SubmitCoordinator:
    """Orchestrates the submit consent guard and the operation claim in ONE
    transaction (repository.begin_immediate), then hands control to the caller
    to invoke the adapter outside the transaction."""

    def __init__(self, project_dir: str | Path) -> None:
        self._project_dir = Path(project_dir)
        self._repository = OperationRepository(self._project_dir)
        self._consent = ConsentService(self._project_dir)

    def claim_for_submit(
        self,
        *,
        prepared: PreparedOperation,
        brief: CreativeBriefV1_1,
        manifest: ProductionManifest,
        presenter_plan: PresenterPlanV1_1,
        orchestration_plan: OrchestrationPlanV1_1,
        request_descriptor,
        lease_owner: str,
        now_iso: str,
        lease_seconds: int,
    ) -> SubmitClaim:
        """One transaction: run the full consent guard, then claim. If the guard
        fails or the operation is not claimable, nothing is written. Returns the
        consent proof + claim handle the worker needs to record the outcome."""
        with self._repository.begin_immediate() as conn:
            consent = self._consent.validate_submit_consent_in_tx(
                conn,
                prepared=prepared, brief=brief, manifest=manifest,
                presenter_plan=presenter_plan, orchestration_plan=orchestration_plan,
                request_descriptor=request_descriptor,
            )
            claim = self._repository.claim_submit_in_tx(
                conn, prepared.operation_id, lease_owner, now_iso, lease_seconds,
            )
            if claim.status != "claimed":
                raise OperationStateError(
                    f"operation {prepared.operation_id} not claimable: {claim.status}"
                )
        return SubmitClaim(consent=consent, claim=claim)


class DownloadProcessor:
    """Two-phase download of a completed operation's video: claim → re-poll for
    URL → stream-download + verify → stage → finalize. Handles crash recovery
    (downloaded state → finalize-only claim) and consent withdrawal races."""

    def __init__(self, project_dir: str | Path) -> None:
        self._project_dir = Path(project_dir)
        self._repository = OperationRepository(self._project_dir)

    def download_once(self, *, operation_id, lease_owner, adapter, downloader,
                      now_iso, lease_seconds, max_bytes=536_870_912,
                      probe=None):
        """One download attempt. Returns DownloadOnceResult. The adapter and
        downloader are called OUTSIDE any transaction; stage + finalize are
        fenced. ``downloader`` must implement VideoDownloader; ``adapter`` must
        implement HeyGenVideoAdapter (only poll_video is used)."""
        with self._repository.begin_immediate() as conn:
            claim = self._repository.claim_download_in_tx(
                conn, operation_id, lease_owner, now_iso, lease_seconds,
            )
        if claim.status not in ("claimed", "finalize"):
            return DownloadOnceResult(claim=claim, outcome=None)

        # Finalize-only path (crash recovery: already staged).
        if claim.status == "finalize":
            with self._repository.begin_immediate() as conn:
                outcome = self._repository.finalize_download_in_tx(
                    conn, operation_id, lease_owner, claim.fence, now_iso,
                )
            return DownloadOnceResult(claim=claim, outcome=outcome)

        # Normal path: re-poll for the transient URL.
        try:
            poll = adapter.poll_video(claim.remote_id)
        except PollAdapterError as exc:
            poll = exc
        # Map poll result to action.
        if isinstance(poll, PollAdapterError):
            code = poll.code if poll.retryable else "download_reconciliation_required"
            retry = _canonical(_parse_utc(now_iso) + timedelta(seconds=DOWNLOAD_BACKOFF_SECONDS)) \
                if poll.retryable else None
            with self._repository.begin_immediate() as conn:
                outcome = self._repository.apply_download_failure_in_tx(
                    conn, operation_id, lease_owner, claim.fence, now_iso, code, retry,
                )
            return DownloadOnceResult(claim=claim, outcome=outcome)
        # PollResult
        if poll.provider_status == "completed" and poll.video_url:
            url = poll.video_url
        elif poll.provider_status in ("queued", "submitted", "processing"):
            retry = _canonical(_parse_utc(now_iso) + timedelta(seconds=DOWNLOAD_BACKOFF_SECONDS))
            with self._repository.begin_immediate() as conn:
                outcome = self._repository.apply_download_failure_in_tx(
                    conn, operation_id, lease_owner, claim.fence, now_iso,
                    "provider_output_not_ready", retry,
                )
            return DownloadOnceResult(claim=claim, outcome=outcome)
        else:  # not_found / failed — contradiction with local 'completed'
            with self._repository.begin_immediate() as conn:
                outcome = self._repository.apply_download_failure_in_tx(
                    conn, operation_id, lease_owner, claim.fence, now_iso,
                    "download_reconciliation_required", None,
                )
            return DownloadOnceResult(claim=claim, outcome=outcome)

        # Download + verify (outside tx). The downloader derives its temp path
        # from the deterministic local_output_ref; it never chooses an arbitrary
        # filename. The URL is NEVER persisted.
        ref = _output_ref(operation_id)
        try:
            prepared = downloader.download_and_verify(
                url, str(self._project_dir / ".lecturecast" / "runtime"),
                ref, max_bytes, probe,
            )
        except Exception:
            retry = _canonical(_parse_utc(now_iso) + timedelta(seconds=DOWNLOAD_BACKOFF_SECONDS))
            with self._repository.begin_immediate() as conn:
                outcome = self._repository.apply_download_failure_in_tx(
                    conn, operation_id, lease_owner, claim.fence, now_iso,
                    "download_failed", retry,
                )
            return DownloadOnceResult(claim=claim, outcome=outcome)

        # Stage (tx1).
        with self._repository.begin_immediate() as conn:
            staged = self._repository.stage_download_in_tx(
                conn, operation_id, lease_owner, claim.fence, now_iso, prepared, max_bytes,
            )
        if staged.status not in ("staged",):
            return DownloadOnceResult(claim=claim, outcome=staged)

        # Finalize (tx2): publish + verify.
        with self._repository.begin_immediate() as conn:
            outcome = self._repository.finalize_download_in_tx(
                conn, operation_id, lease_owner, claim.fence, now_iso,
            )
        return DownloadOnceResult(claim=claim, outcome=outcome)


class DeleteProcessor:
    """One deletion step of a video resource: claim → delete outside tx →
    fenced apply."""

    def __init__(self, project_dir):
        self._project_dir = Path(project_dir)
        self._repository = OperationRepository(self._project_dir)

    def delete_once(self, *, operation_id, resource_id, lease_owner, deleter,
                    now_iso, lease_seconds, max_attempts=DELETION_MAX_ATTEMPTS):
        with self._repository.begin_immediate() as conn:
            claim = self._repository.claim_deletion_in_tx(
                conn, operation_id, resource_id, lease_owner, now_iso, lease_seconds, max_attempts)
        if claim.status != "claimed":
            return DeletionOnceResult(claim=claim, outcome=None)
        try:
            result = deleter.delete_video(claim.remote_id)
        except DeleteAdapterError as exc:
            result = exc
        with self._repository.begin_immediate() as conn:
            outcome = self._repository.apply_deletion_outcome_in_tx(
                conn, operation_id, resource_id, lease_owner, claim.fence,
                now_iso, result, max_attempts)
        return DeletionOnceResult(claim=claim, outcome=outcome)


def _has_any_video(conn: sqlite3.Connection, operation_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM heygen_remote_resources r "
        "JOIN heygen_resource_operation_refs ref ON ref.resource_id = r.resource_id "
        "WHERE ref.operation_id = ? AND r.resource_kind = 'video' LIMIT 1",
        (operation_id,),
    ).fetchone()
    return row is not None


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass
