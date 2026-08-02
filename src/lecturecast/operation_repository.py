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
    ConsentError,
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
from lecturecast.heygen_asset_adapter import (
    derive_asset_identity, AssetUploadCommand, AssetUploadResult,
    AssetUploadError, AssetUploadAmbiguousError,
    AssetDeleteResult, AssetReadError,
)

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


@dataclass(frozen=True)
class AssetDeletionClaim:
    """Handle returned by claim_asset_deletion_in_tx. Fenced on the asset's OWN
    lease columns (heygen_asset_uploads.lease_*), unlike video's operation lease."""
    upload_id: str
    resource_id: int | None
    status: str          # "claimed" | "busy" | "retry_wait" | "not_ready"
    fence: int
    remote_id: str | None


@dataclass(frozen=True)
class AssetDeletionOutcome:
    upload_id: str
    resource_id: int
    status: str          # "deleted" | "failed" | "fence_conflict"
    fence: int
    last_error: str | None
    next_retry_at: str | None


@dataclass(frozen=True)
class AssetDeletionOnceResult:
    claim: AssetDeletionClaim
    outcome: AssetDeletionOutcome | None


# --- deletion plan (§5.5e5b0c3c-c2) --------------------------------------
#
# resolve_deletion_plan_in_tx is a PURE read-only planning primitive: given an
# operation + force flag, it returns the §3.5-ordered list of resources that
# are structurally in scope to ATTEMPT deleting now. It deliberately does NOT
# re-derive per-resource eligibility (verified gate / manual_force / retry
# backoff / matrix / topology) — that stays authoritative inside each claim
# (video claim_deletion_in_tx / c1 claim_asset_deletion_in_tx), per the c1
# lesson of reusing locked invariants instead of building a looser parallel
# one. The resolver owns only what claims cannot: cross-resource ORDER (§3.5
# fixed sequence), structural scope (reusable_avatar skip, force excludes
# video, already-deleted skip), and the §3.5 sequencing gate (normal mode
# holds audio/portrait behind video to protect the deliverable).

@dataclass(frozen=True)
class DeletionPlanEntry:
    """One candidate resource in a DeletionPlan. deletion_status is advisory
    (the claim is authoritative on whether it can actually advance)."""
    resource_id: int
    resource_kind: str            # video / audio_asset / portrait_asset
    upload_id: str | None         # set for assets (AssetDeletionProcessor); None for video
    retention_mode: str           # ephemeral (reusable_avatar is already filtered out)
    deletion_status: str
    order_key: int                # 0=video, 1=audio, 2=portrait (deterministic)


@dataclass(frozen=True)
class DeletionPlan:
    operation_id: str
    force: bool
    video_download_status: str | None   # op.download_status, context for the coordinator
    entries: tuple[DeletionPlanEntry, ...]   # §3.5-ordered, deterministic


# §3.5 fixed deletion order (lower first). Unknown kinds land in a trailing
# bucket (order_key 9) so they are surfaced to the coordinator, never silently
# dropped; reusable_avatar rows are filtered out before this applies.
_DELETION_ORDER_KEY = {"video": 0, "audio_asset": 1, "portrait_asset": 2}


# --- deletion pass result (§5.5e5b0c3c-c3) --------------------------------
#
# DeletionCoordinator drives one §3.5-ordered pass over a single operation:
# it resolves the c2 DeletionPlan in its OWN (immediately-closed) tx, then
# routes each frozen entry to the c1/video processor by resource_kind. It is a
# DUMB iterator — eligibility stays authoritative in each processor's claim
# (c1 lesson: reuse locked invariants, build no parallel gate). Counts below
# derive from `attempts` so they cannot drift from the per-entry record.

@dataclass(frozen=True)
class DeletionEntryAttempt:
    """One routed entry in a deletion pass. ``routed`` names what happened:
    video/asset = a processor was driven; skipped_* = the resolver surfaced
    the entry but it is not drivable (never silently dropped); alerted =
    the processor raised an untyped exception, so the remote result is
    unknowable — NOTHING is written for this resource and the held lease
    expires naturally (never a fabricated phantom outcome)."""
    entry: DeletionPlanEntry
    routed: str                   # video | asset | skipped_no_upload_id | skipped_unknown_kind | alerted_exception
    claim_status: str | None      # processor claim status; None when skipped/alerted
    outcome_status: str | None    # processor outcome status; None when not claimed/skipped/alerted
    last_error: str | None
    next_retry_at: str | None


@dataclass(frozen=True)
class DeletionPassResult:
    """Result of one deletion pass over a single operation. ``attempts`` is
    authoritative; the counts are derived from it."""
    operation_id: str
    force: bool
    video_download_status: str | None
    attempts: tuple[DeletionEntryAttempt, ...]

    @property
    def attempted(self) -> int:
        return len(self.attempts)

    @property
    def deleted(self) -> int:
        return sum(1 for a in self.attempts if a.outcome_status == "deleted")

    @property
    def failed(self) -> int:
        return sum(1 for a in self.attempts if a.outcome_status == "failed")

    @property
    def not_advanced(self) -> int:
        # claimed-but-fence_conflict, or non-claimed (busy/retry_wait/not_ready).
        return sum(1 for a in self.attempts
                   if a.claim_status is not None
                   and a.outcome_status in (None, "fence_conflict"))

    @property
    def skipped(self) -> int:
        return sum(1 for a in self.attempts if a.routed.startswith("skipped"))

    @property
    def alerted(self) -> int:
        return sum(1 for a in self.attempts if a.routed == "alerted_exception")


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
            # manual_force is the operator-only integrity path — never
            # auto-deleted. The deletion_pending/deletion_failed branches
            # below both reject manual_force, and the asset claim's
            # not_started branch raises on ANY non-NULL reason; this branch
            # was reason-blind (round-10 P1): a schema-legal
            # (not_started, manual_force) video, driven into the sweep by a
            # sibling B2 asset witness (which authorizes the op while the
            # resolver returns the not_started video as the tail gate), was
            # claimed here and then had its marker erased to post_download at
            # the reason-seeding line below — apply's post_download
            # single-video recheck then passed and the operator-only resource
            # was deleted. manual_force must be left for the operator.
            if res["deletion_reason"] == "manual_force":
                return DeletionClaim(operation_id, resource_id, "not_ready", op["lease_fence"], None)
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
                                     lease_owner, fence, now_iso, outcome, *,
                                     expected_remote_id,
                                     max_attempts=DELETION_MAX_ATTEMPTS):
        """Apply a deletion outcome (fenced on operation lease + resource
        deletion_status='deletion_pending'). Maps DeleteResult/DeleteAdapterError
        to deleted/failed states. ``expected_remote_id`` is the id the adapter
        actually DELETEd (claim.remote_id); tx2 re-binds it to defend a remote_id
        swap between the (closed) claim tx and this apply tx — mirror asset
        apply (:2130)."""
        from lecturecast.heygen_adapter import DeleteResult, DeleteAdapterError
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        now_c = _canonical(_parse_utc(now_iso))
        if type(max_attempts) is not int or not (1 <= max_attempts <= 10):
            raise ValueError("max_attempts must be an int in [1, 10]")
        # Defense against a None/empty expected_remote_id that would silently
        # disable the remote_id re-bind below (mirror asset apply :2152-2156):
        # a legit claimed video always carries a non-empty claim.remote_id, so a
        # missing/blank value here is a caller bug, not a deletable row.
        if not isinstance(expected_remote_id, str) or not expected_remote_id:
            raise ValueError("expected_remote_id must be a non-empty string")
        # Fence CAS on operation. download_status is read here (not just the
        # fence) because post_download cleanup is OP-LEVEL-authorized by a
        # verified delivery, and claim and apply are separate txs — a between-tx
        # op download_status swap must be re-checked in tx2 (round-12 F1b).
        op = conn.execute(
            "SELECT lease_fence, download_status FROM heygen_operations "
            "WHERE operation_id=? AND lease_owner=? AND lease_fence=?",
            (operation_id, lease_owner, fence)).fetchone()
        if op is None:
            return DeletionOutcome(operation_id, resource_id, "fence_conflict", fence, None, None)
        # Re-verify FULL exclusive topology (claim and apply are separate txs) AND
        # read every authorization field apply relies on in tx2: deletion_attempts
        # (retry bookkeeping), deletion_reason (round-11 allow-set gate),
        # retention_mode (round-12 F1a — post_download requires 'ephemeral'), and
        # remote_id (round-12 F2 — re-bind the id the adapter actually deleted;
        # r.remote_id=? in the WHERE makes a between-tx rename return no row). The
        # topology SELECT confirms the row in this same tx, so reason/retention/
        # remote_id read here are guaranteed non-None downstream.
        res = conn.execute(
            "SELECT r.deletion_attempts, r.deletion_reason, r.retention_mode, "
            "r.remote_id FROM heygen_remote_resources r "
            "WHERE r.resource_id=? AND r.resource_kind='video' "
            "AND r.deletion_status='deletion_pending' "
            "AND r.created_by_operation_id=? "
            "AND r.remote_id=? "
            "AND r.credential_profile_id=(SELECT o.credential_profile_id "
            "  FROM heygen_operations o WHERE o.operation_id=?) "
            "AND EXISTS (SELECT 1 FROM heygen_resource_operation_refs ref "
            "  WHERE ref.resource_id=r.resource_id AND ref.operation_id=?) "
            "AND NOT EXISTS (SELECT 1 FROM heygen_resource_operation_refs ref2 "
            "  WHERE ref2.resource_id=r.resource_id AND ref2.operation_id<>?)",
            (resource_id, operation_id, expected_remote_id, operation_id,
             operation_id, operation_id)).fetchone()
        if res is None:
            return DeletionOutcome(operation_id, resource_id, "fence_conflict", fence, None, None)
        # Round-11 reason gate (now read from res, no separate SELECT): only
        # post_download / consent_withdrawal authorize recording a deletion.
        # manual_force is the operator-only integrity path: claim returns
        # not_ready and never carries a lease, so it reaches apply ONLY via a
        # reason swap between the claim tx and this apply tx. Mirror asset apply
        # (:2206) and reject before ANY outcome path (success OR failure) — never
        # silently record a deletion nor clear the operation lease for a
        # manual_force resource.
        if res["deletion_reason"] not in ("post_download", "consent_withdrawal"):
            return DeletionOutcome(operation_id, resource_id, "fence_conflict", fence, None, None)
        # Round-12 F1: post_download is OP-LEVEL-authorized by a verified delivery
        # of a SINGLE deliverable video on an EPHEMERAL op. claim verified all
        # three in tx1; apply MUST re-verify them in tx2 because claim's tx1 is
        # already closed. consent_withdrawal is delivery-independent, so it is
        # exempt from retention / download_status / single-video (confirmed by the
        # CTRL-consent probe case). A between-tx mutation of any of the three
        # returns fence_conflict (lease preserved) before any outcome path.
        if res["deletion_reason"] == "post_download":
            if (res["retention_mode"] != "ephemeral"
                    or op["download_status"] != "verified"
                    or not self._single_video(conn, operation_id)):
                return DeletionOutcome(
                    operation_id, resource_id, "fence_conflict", fence, None, None)
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

    # === asset deletion lifecycle (§5.5e5b0c3c) ==========================
    #
    # One-shot assets (portrait_photo / synthetic_narration_audio) are deleted
    # via their OWN lease columns on heygen_asset_uploads (not the operation
    # lease video uses). The claim flips asset uploaded→cleanup_required AND
    # resource not_started→deletion_pending(post_download) in the SAME tx so
    # the asset↔resource correspondence matrix (_check_asset_resource_consistency)
    # is self-consistent at every recoverable point. manual_force resources
    # (the fenced-apply integrity path's durable record) are NOT auto-deleted:
    # claim returns not_ready, leaving them for human reconciliation — mirroring
    # video claim_deletion_in_tx. Crash-safety: claim(tx1) → adapter.DELETE
    # (outside tx) → apply(tx2); a crash at any point leaves a row the next run
    # can reclaim (or, exhausted, mark manual).

    def claim_asset_deletion_in_tx(
        self, conn: sqlite3.Connection, *, upload_id: str,
        lease_owner: str, now_iso: str, lease_seconds: int,
        max_attempts: int = DELETION_MAX_ATTEMPTS,
        force: bool = False,
    ) -> AssetDeletionClaim:
        """Claim an asset-deletion lease on the asset's own lease columns.
        Eligibility: asset status ∈ {uploaded, cleanup_required} AND resource
        deletion_status ∈ {not_started, deletion_pending(non-manual),
        deletion_failed(retryable)}. Bumps asset.lease_fence +
        resource.deletion_attempts; flips asset uploaded→cleanup_required and
        resource not_started→deletion_pending(post_download). Half lease →
        OperationIntegrityError; active lease → busy; manual_force → not_ready."""
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        _require_lease_owner(lease_owner)
        if type(max_attempts) is not int or not (1 <= max_attempts <= 10):
            raise ValueError("max_attempts must be an int in [1, 10]")
        # force is the §3.5 operator emergency override (bypasses the video-first
        # order + delivery gate); a truthy non-bool would silently skip the
        # round-13 op-level re-check, so reject it (never coerce / branch on
        # truthiness). Threaded from the coordinator's already-guarded force.
        if type(force) is not bool:
            raise ValueError("force must be a bool")
        now = _parse_utc(now_iso)
        now_c = _canonical(now)
        _check_lease_seconds(lease_seconds)
        expires_iso = _canonical(now + timedelta(seconds=lease_seconds))

        row = conn.execute(
            "SELECT upload_id, parent_operation_id, asset_role, status, "
            "remote_resource_id, lease_owner, lease_expires_at, lease_fence, "
            "attempt_started_at, last_error_code "
            "FROM heygen_asset_uploads WHERE upload_id=?",
            (upload_id,)).fetchone()
        if row is None:
            return AssetDeletionClaim(upload_id, None, "not_ready", 0, None)
        status = row["status"]
        if status not in ("uploaded", "cleanup_required"):
            # upload_pending/uploading/failed/cancelled/reconciliation_required/
            # manual_reconciliation_required/deleted are not deletion-eligible.
            return AssetDeletionClaim(upload_id, row["remote_resource_id"],
                                      "not_ready", row["lease_fence"], None)
        rid = row["remote_resource_id"]
        if rid is None:
            raise OperationIntegrityError(
                f"asset upload {upload_id!r} status {status!r} without "
                f"remote_resource_id")

        # Identity topology (fail-closed): re-verify the bound resource still
        # belongs to this upload's parent, single-referenced, matching
        # kind/credential/retention. A foreign/tampered resource_id is rejected.
        op = conn.execute(
            "SELECT credential_profile_id, download_status FROM heygen_operations "
            "WHERE operation_id=?", (row["parent_operation_id"],)).fetchone()
        if op is None:
            raise OperationIntegrityError(
                f"parent op missing for {upload_id!r}")
        _validate_asset_binding(
            conn, remote_resource_id=rid,
            parent_operation_id=row["parent_operation_id"],
            asset_role=row["asset_role"],
            credential_profile_id=op["credential_profile_id"],
            expected_remote_id=None)

        res = conn.execute(
            "SELECT deletion_status, deletion_reason, deletion_attempts, "
            "deletion_next_retry_at, last_deletion_error, remote_id "
            "FROM heygen_remote_resources WHERE resource_id=?",
            (rid,)).fetchone()
        if res is None:
            raise OperationIntegrityError(
                f"asset upload {upload_id!r} bound resource {rid} missing")

        ds = res["deletion_status"]
        reason = res["deletion_reason"]
        # Fail-closed matrix check BEFORE any eligibility branch: a tampered
        # asset↔resource pair (e.g. uploaded+deletion_pending, or
        # cleanup_required+deleted) is an integrity error, never silently
        # "corrected" into a legal combo (round-1 blocker #2). A legitimately
        # deleted resource always pairs with an asset at 'deleted', which the
        # status gate above already returned not_ready — so a deleted resource
        # can never reach here with a matrix-valid live asset. This supersedes
        # the former ad-hoc "never resurrect" branch with the locked matrix.
        _check_asset_resource_consistency(
            status, ds, deletion_reason=reason,
            last_error_code=row["last_error_code"])
        if ds == "deletion_pending":
            # manual_force is the integrity path's durable record — never
            # auto-delete (mirror video claim). consent_withdrawal /
            # post_download pending are eligible reclaims.
            if reason == "manual_force":
                return AssetDeletionClaim(upload_id, rid, "not_ready",
                                          row["lease_fence"], None)
            if reason not in ("post_download", "consent_withdrawal"):
                raise OperationIntegrityError(
                    f"resource {rid} deletion_pending unknown reason {reason!r}")
        elif ds == "deletion_failed":
            lec = res["last_deletion_error"]
            if lec in _DELETION_MANUAL_CODES:
                return AssetDeletionClaim(upload_id, rid, "not_ready",
                                          row["lease_fence"], None)
            if res["deletion_attempts"] >= max_attempts:
                return AssetDeletionClaim(upload_id, rid, "not_ready",
                                          row["lease_fence"], None)
            nr = res["deletion_next_retry_at"]
            if nr is not None and _parse_utc(nr) > now:
                return AssetDeletionClaim(upload_id, rid, "retry_wait",
                                          row["lease_fence"], None)
            # Inherit the original reason on retry; manual_force never retries.
            if reason == "manual_force":
                return AssetDeletionClaim(upload_id, rid, "not_ready",
                                          row["lease_fence"], None)
            if reason not in ("post_download", "consent_withdrawal"):
                raise OperationIntegrityError(
                    f"resource {rid} deletion_failed unknown reason {reason!r}")
        elif ds == "not_started":
            # Normal post-download entry point. c2/c3 resolver gates ordering
            # + receipt; c1 only requires the matrix to be advanceable. A
            # not_started resource must carry no reason.
            if reason is not None:
                raise OperationIntegrityError(
                    f"resource {rid} not_started with reason {reason!r}")
        else:
            raise OperationIntegrityError(
                f"resource {rid} unknown deletion_status {ds!r}")

        # round-13 op-level eligibility (F3/F4/F5). The asset claim is the
        # eligibility authority for the asset lifecycle; the witness B2 + resolver
        # authorize candidacy/order in an already-closed tx, so a between-tx
        # download_status->not_started / double-video insert / live-video appear
        # must be re-checked HERE (before the adapter is ever called) against
        # CURRENT state. post_download is determined from ds/reason (a not_started
        # resource seeds post_download at the UPDATE below; a pending/failed
        # resource carries it). consent_withdrawal is delivery-/structure-
        # independent and stays exempt. not_ready mirrors the video claim. force
        # (§3.5 privacy emergency) is an explicit operator override of the
        # video-first order AND the delivery gate, so it skips this re-check.
        if not force and ((ds == "not_started") or (reason == "post_download")) \
                and not self._asset_post_download_op_level_ok(
                    conn, row["parent_operation_id"], op["download_status"]):
            return AssetDeletionClaim(upload_id, rid, "not_ready",
                                      row["lease_fence"], None)

        # Half lease → integrity error (never trust a half-state).
        owner = row["lease_owner"]; exp = row["lease_expires_at"]
        att = row["attempt_started_at"]
        if (owner is None) != (exp is None):
            raise OperationIntegrityError(
                f"asset upload {upload_id!r} half lease")
        if owner is not None and att is None:
            raise OperationIntegrityError(
                f"asset upload {upload_id!r} lease without attempt")
        # Active lease → busy.
        if owner is not None and _parse_utc(exp) > now:
            return AssetDeletionClaim(upload_id, rid, "busy",
                                      row["lease_fence"], None)

        new_fence = row["lease_fence"] + 1
        new_reason = "post_download" if ds == "not_started" else reason
        # Resource: advance to deletion_pending, inherit reason (or seed
        # post_download on first claim), bump attempts. GATED on the observed
        # deletion_status so a concurrent mutation cannot resurrect a deleted
        # row (rowcount 0 → busy, never a blind overwrite).
        cur = conn.execute(
            "UPDATE heygen_remote_resources SET deletion_status='deletion_pending', "
            "deletion_reason=?, deletion_attempts=deletion_attempts+1, updated_at=? "
            "WHERE resource_id=? AND deletion_status=?",
            (new_reason, now_c, rid, ds))
        if cur.rowcount == 0:
            return AssetDeletionClaim(upload_id, rid, "busy", new_fence, None)
        # Asset: uploaded→cleanup_required (first claim) or stay cleanup_required
        # (reclaim); acquire the lease on the asset's OWN columns. lease_fence is
        # PRESERVED across the upload lifecycle (upload apply does not clear it),
        # so this monotonic bump spans upload→delete. last_error_code is NOT in
        # the SET — the manual_force marker (consent_integrity_failure) must
        # persist, and the granted/withdrawn path's NULL stays NULL.
        conn.execute(
            "UPDATE heygen_asset_uploads SET status='cleanup_required', "
            "lease_owner=?, lease_expires_at=?, lease_fence=?, attempt_started_at=?, "
            "next_retry_at=NULL, updated_at=? WHERE upload_id=?",
            (lease_owner, expires_iso, new_fence, now_c, now_c, upload_id))
        return AssetDeletionClaim(upload_id, rid, "claimed", new_fence,
                                  res["remote_id"])

    def apply_asset_deletion_outcome_in_tx(
        self, conn: sqlite3.Connection, *, upload_id: str, resource_id: int,
        lease_owner: str, fence: int, now_iso: str, result,
        expected_remote_id: str,
        max_attempts: int = DELETION_MAX_ATTEMPTS,
        force: bool = False,
    ) -> AssetDeletionOutcome:
        """Apply an asset-deletion outcome, fenced on the asset's OWN lease
        (status='cleanup_required' AND lease_owner AND lease_fence AND
        attempt_started_at IS NOT NULL). Re-verifies the FULL topology —
        INCLUDING the resource's remote_id against expected_remote_id (the
        remote_id the adapter actually DELETEd, from AssetDeletionClaim) —
        plus the correspondence matrix and resource deletion_status='deletion_pending'
        (claim and apply are separate txs; a foreign/tampered resource OR a
        renamed remote_id between them is rejected, never silently applied).
        Maps AssetDeleteResult→deleted (200/404 are both idempotent success),
        retryable AssetReadError→deletion_failed+backoff (asset stays
        cleanup_required for reclaim), terminal→deletion_failed + manual code."""
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        now_c = _canonical(_parse_utc(now_iso))
        if type(max_attempts) is not int or not (1 <= max_attempts <= 10):
            raise ValueError("max_attempts must be an int in [1, 10]")
        # Mirror the claim's force guard: a truthy non-bool would skip the
        # round-13 op-level re-check. Threaded from the coordinator's force.
        if type(force) is not bool:
            raise ValueError("force must be a bool")
        # 'required' only prevents OMISSION; a caller can still pass None
        # explicitly, and _validate_asset_binding treats None as "skip the
        # remote_id check". Reject at the entry guard so the remote-identity
        # binding (round-2 blocker) cannot be silently disabled, and so a
        # bogus/empty id never reaches topology (round-3 blocker).
        if (not isinstance(expected_remote_id, str)
                or not _ASSET_REMOTE_ID_RE.fullmatch(expected_remote_id)):
            raise ValueError("expected_remote_id must be a non-empty asset id")
        # Fence CAS on the asset's own lease columns. remote_resource_id is
        # bound to the claim's resource_id: if the asset's remote_resource_id is
        # swapped between claim and apply (to a topology-valid sibling), the CAS
        # no longer matches and the DELETE outcome cannot be recorded against
        # the wrong resource (round-1 blocker #1).
        row = conn.execute(
            "SELECT remote_resource_id, asset_role, parent_operation_id, "
            "last_error_code FROM heygen_asset_uploads "
            "WHERE upload_id=? AND lease_owner=? AND lease_fence=? "
            "AND status='cleanup_required' AND attempt_started_at IS NOT NULL "
            "AND remote_resource_id=?",
            (upload_id, lease_owner, fence, resource_id)).fetchone()
        if row is None:
            return AssetDeletionOutcome(upload_id, resource_id, "fence_conflict",
                                        fence, None, None)
        rid = row["remote_resource_id"]
        if rid is None:
            raise OperationIntegrityError(
                f"asset upload {upload_id!r} held lease without remote_resource_id")

        # Re-verify full topology (claim/apply gap). expected_remote_id binds
        # the resource's CURRENT remote_id to the one the adapter DELETEd: a
        # rename of the resource row's remote_id between claim and apply (without
        # touching resource_id) is rejected here, so the DELETE of the old
        # remote_id is never recorded against the renamed row (round-2 blocker).
        op = conn.execute(
            "SELECT credential_profile_id, download_status FROM heygen_operations "
            "WHERE operation_id=?", (row["parent_operation_id"],)).fetchone()
        if op is None:
            raise OperationIntegrityError(
                f"parent op missing for {upload_id!r}")
        _validate_asset_binding(
            conn, remote_resource_id=rid,
            parent_operation_id=row["parent_operation_id"],
            asset_role=row["asset_role"],
            credential_profile_id=op["credential_profile_id"],
            expected_remote_id=expected_remote_id)
        res = conn.execute(
            "SELECT deletion_status, deletion_reason, deletion_attempts "
            "FROM heygen_remote_resources WHERE resource_id=?",
            (rid,)).fetchone()
        if res is None:
            raise OperationIntegrityError(
                f"asset upload {upload_id!r} bound resource {rid} missing")
        # Fail-closed matrix check on the CURRENT asset/resource state before
        # persisting any outcome: a tampered reason (e.g. flipped to
        # manual_force) or a stale error code is rejected here, never silently
        # applied, and never has its integrity marker cleared (round-1 blocker
        # #2). Asset status is 'cleanup_required' (enforced by the CAS above).
        _check_asset_resource_consistency(
            "cleanup_required", res["deletion_status"],
            deletion_reason=res["deletion_reason"],
            last_error_code=row["last_error_code"])
        # Only reasons a claim can execute reach apply. manual_force is claim
        # not_ready (so it carries no lease); defend in depth against a reason
        # swap to manual_force between claim and apply.
        if res["deletion_reason"] not in ("post_download", "consent_withdrawal"):
            return AssetDeletionOutcome(upload_id, rid, "fence_conflict",
                                        fence, None, None)
        if res["deletion_status"] != "deletion_pending":
            return AssetDeletionOutcome(upload_id, rid, "fence_conflict",
                                        fence, None, None)
        # round-13 op-level cross-tx re-verify (F3/F4/F5). The claim (tx1) CLOSED
        # before this apply (tx2) opened; the op-level authorization the witness B2
        # + resolver seeded is NOT carried across the boundary. A between-tx
        # download_status->not_started / double-video insert / live-video appear
        # must fence_conflict HERE (lease preserved, never recorded 'deleted'),
        # mirroring the VIDEO apply's post_download block (round-12). consent is
        # exempt (delivery-/structure-independent). force (§3.5 privacy emergency)
        # is an operator override of the order + delivery gate, so it skips this.
        # Sits before the outcome branch so it gates success + retryable + exhausted.
        if not force and res["deletion_reason"] == "post_download" and not \
                self._asset_post_download_op_level_ok(
                    conn, row["parent_operation_id"], op["download_status"]):
            return AssetDeletionOutcome(upload_id, rid, "fence_conflict",
                                        fence, None, None)

        if isinstance(result, AssetDeleteResult):
            # 200 (deleted) or 404 (already_absent) — both are idempotent
            # success per spec §3.5.
            cur = conn.execute(
                "UPDATE heygen_remote_resources SET deletion_status='deleted', "
                "deleted_at=?, deletion_next_retry_at=NULL, last_deletion_error=NULL, "
                "updated_at=? WHERE resource_id=? AND deletion_status='deletion_pending'",
                (now_c, now_c, rid))
            if cur.rowcount == 0:
                return AssetDeletionOutcome(upload_id, rid, "fence_conflict",
                                            fence, None, None)
            # Flip asset → deleted, clear lease (fence preserved). last_error_code
            # cleared: only post_download/consent_withdrawal resources reach apply
            # (manual_force is claim not_ready), and the matrix for deleted +
            # those reasons requires no error code.
            conn.execute(
                "UPDATE heygen_asset_uploads SET status='deleted', "
                "lease_owner=NULL, lease_expires_at=NULL, attempt_started_at=NULL, "
                "next_retry_at=NULL, last_error_code=NULL, updated_at=? "
                "WHERE upload_id=?",
                (now_c, upload_id))
            return AssetDeletionOutcome(upload_id, rid, "deleted", fence, None, None)

        # AssetReadError — retryable vs terminal.
        attempts = res["deletion_attempts"]
        if result.retryable and attempts < max_attempts:
            retry = _canonical(_parse_utc(now_iso)
                               + timedelta(seconds=DELETION_BACKOFF_SECONDS))
            cur = conn.execute(
                "UPDATE heygen_remote_resources SET deletion_status='deletion_failed', "
                "last_deletion_error=?, deletion_next_retry_at=?, updated_at=? "
                "WHERE resource_id=? AND deletion_status='deletion_pending'",
                (result.code, retry, now_c, rid))
            if cur.rowcount == 0:
                return AssetDeletionOutcome(upload_id, rid, "fence_conflict",
                                            fence, None, None)
            # Asset stays cleanup_required; clear the lease (fence preserved) so
            # the next run can reclaim after backoff. last_error_code untouched.
            conn.execute(
                "UPDATE heygen_asset_uploads SET lease_owner=NULL, "
                "lease_expires_at=NULL, attempt_started_at=NULL, next_retry_at=NULL, "
                "updated_at=? WHERE upload_id=?",
                (now_c, upload_id))
            return AssetDeletionOutcome(upload_id, rid, "failed", fence,
                                        result.code, retry)
        # Exhausted or permanent → terminal manual code on the resource.
        code = ("deletion_retry_exhausted" if result.retryable
                else "deletion_reconciliation_required")
        cur = conn.execute(
            "UPDATE heygen_remote_resources SET deletion_status='deletion_failed', "
            "last_deletion_error=?, deletion_next_retry_at=NULL, updated_at=? "
            "WHERE resource_id=? AND deletion_status='deletion_pending'",
            (code, now_c, rid))
        if cur.rowcount == 0:
            return AssetDeletionOutcome(upload_id, rid, "fence_conflict",
                                        fence, None, None)
        conn.execute(
            "UPDATE heygen_asset_uploads SET lease_owner=NULL, "
            "lease_expires_at=NULL, attempt_started_at=NULL, next_retry_at=NULL, "
            "updated_at=? WHERE upload_id=?",
            (now_c, upload_id))
        return AssetDeletionOutcome(upload_id, rid, "failed", fence, code, None)

    def resolve_deletion_plan_in_tx(
        self, conn: sqlite3.Connection, *, operation_id: str,
        force: bool = False,
    ) -> DeletionPlan:
        """Pure read-only §3.5 deletion planner. Returns the ordered list of
        resources structurally in scope to ATTEMPT deleting now — eligibility
        (verified gate / manual_force / retry / matrix / topology) stays
        authoritative in each claim; this only owns cross-resource ORDER,
        structural scope, and the §3.5 sequencing gate.

        Normal mode (force=False): if a non-deleted video resource exists, ONLY
        that video is returned (audio/portrait are gated behind it — §3.5
        protects the deliverable by deleting video first); once the video is
        deleted, audio→portrait become available. Force mode (force=True):
        video is excluded entirely (privacy emergency bypasses the video stage,
        §3.5 force-cleanup); audio→portrait returned. reusable_avatar rows are
        always skipped (retention — revocation is via the HeyGen dashboard).
        Unknown op → empty plan (never raises). Deterministic ordering."""
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("operation_id must be a non-empty string")
        # Strict bool guard: Python treats "false" / 1 / [] as truthy, which
        # would silently enter the force branch, exclude video, and release
        # audio/portrait ahead of the §3.5 order. force is this resolver's OWN
        # scope/order authorization (not eligibility the claim re-checks), so a
        # truthy non-bool cannot be recovered downstream — reject it here.
        if type(force) is not bool:
            raise ValueError("force must be a bool")
        op = conn.execute(
            "SELECT download_status FROM heygen_operations WHERE operation_id=?",
            (operation_id,)).fetchone()
        if op is None:
            return DeletionPlan(operation_id, force, None, ())
        download_status = op["download_status"]

        # All resources created by this op (video + one-shot assets + any avatar),
        # LEFT JOIN asset uploads so asset entries carry their upload_id for the
        # coordinator to route to AssetDeletionProcessor.
        rows = conn.execute(
            "SELECT r.resource_id, r.resource_kind, r.retention_mode, "
            "r.deletion_status, u.upload_id "
            "FROM heygen_remote_resources r "
            "LEFT JOIN heygen_asset_uploads u ON u.remote_resource_id = r.resource_id "
            "WHERE r.created_by_operation_id = ? "
            "ORDER BY r.resource_id",
            (operation_id,)).fetchall()

        video_entry = None
        audio_entries: list[DeletionPlanEntry] = []
        portrait_entries: list[DeletionPlanEntry] = []
        other_entries: list[DeletionPlanEntry] = []
        for r in rows:
            # Structural scope filters (the authority here; eligibility is the
            # claim's job).
            if r["retention_mode"] == "reusable_avatar":
                continue  # retention: kept across ops, revocation via dashboard
            if r["deletion_status"] == "deleted":
                continue  # already done — nothing to attempt
            kind = r["resource_kind"]
            entry = DeletionPlanEntry(
                resource_id=r["resource_id"], resource_kind=kind,
                upload_id=r["upload_id"], retention_mode=r["retention_mode"],
                deletion_status=r["deletion_status"],
                order_key=_DELETION_ORDER_KEY.get(kind, 9))
            if kind == "video":
                # _single_video contract: at most one video per op; last wins if
                # data is corrupt (the video claim will fail-closed on doubles).
                video_entry = entry
            elif kind == "audio_asset":
                audio_entries.append(entry)
            elif kind == "portrait_asset":
                portrait_entries.append(entry)
            else:
                # Unexpected ephemeral kind (avatar_look/group are normally
                # reusable and filtered above); surface, don't silently drop.
                other_entries.append(entry)
        # Deterministic intra-bucket order.
        audio_entries.sort(key=lambda e: e.upload_id or "")
        portrait_entries.sort(key=lambda e: e.upload_id or "")
        other_entries.sort(key=lambda e: e.resource_id)
        tail = (tuple(audio_entries) + tuple(portrait_entries)
                + tuple(other_entries))

        if force:
            # §3.5 force-cleanup: bypass the video stage; video excluded.
            return DeletionPlan(operation_id, True, download_status, tail)
        # Normal mode: §3.5 fixed order video→audio→portrait. A non-deleted
        # video gates audio/portrait (return only the video this pass); once it
        # is deleted the tail becomes available.
        if video_entry is not None:
            return DeletionPlan(operation_id, False, download_status,
                                (video_entry,))
        return DeletionPlan(operation_id, False, download_status, tail)

    @staticmethod
    def _single_video(conn, operation_id):
        count = conn.execute(
            "SELECT COUNT(*) FROM heygen_remote_resources r "
            "JOIN heygen_resource_operation_refs ref ON ref.resource_id=r.resource_id "
            "WHERE ref.operation_id=? AND r.resource_kind='video'",
            (operation_id,)).fetchone()[0]
        return count == 1

    @staticmethod
    def _asset_post_download_op_level_ok(conn, operation_id, download_status):
        """round-13 cross-tx op-level re-verify for a post_download ASSET. The
        asset lifecycle (claim_asset_deletion_in_tx / apply_asset_deletion_outcome_in_tx)
        fences on the asset's OWN lease and historically read NEITHER
        op.download_status NOR the video topology — every op-level invariant lived
        only in the witness B2 / resolver, which close EARLIEST in the sweep. Between
        that closed tx and the asset claim/apply, a separate connection can mutate
        op.download_status -> 'not_started' (F3), insert a 2nd video (F4), or
        appear/resurrect a non-deleted video (F5). claim and apply must re-verify
        all three against CURRENT state, exactly as the VIDEO claim/apply do (rounds
        8/9/12). consent_withdrawal never calls this (delivery-/structure-independent).

        F3: op.download_status='verified' — a post_download asset is explicitly
            delivery-authorized (c1's "asset processor is delivery-agnostic" layering
            is NOT a boundary once post_download ties cleanup to a verified delivery).
        F4: COUNT(video) <= 1 over the REFS topology — B2's `1 >= COUNT` / the
            _single_video claim invariant mirror (both count ref-bound videos, NOT
            ==1: a legit 0-video op stays sweepable; round-9). Domain is refs because
            F4's authority is _single_video (the claim invariant that actually executes
            on the live-video path), NOT the resolver. Switching F4 to created_by would
            over-block: an op with two DELETED created_by videos (only one ref-bound)
            has the resolver correctly release the tail (it skips deleted), yet a
            created_by COUNT would see 2 and freeze — wrong.
        F5: no non-deleted non-reusable video over the CREATED_BY domain — the resolver
            (resolve_deletion_plan_in_tx) releases the asset tail iff video_entry is
            None, and it selects video_entry by created_by_operation_id (NO ref row
            required, @2399-2406). So a video with created_by_operation_id=op but NO
            ref row GATES the resolver yet is invisible to a refs-JOIN. F5 must inspect
            the resolver's exact population (created_by, kind=video, non-deleted,
            non-reusable) or it fails to re-verify the ordering decision it claims to
            (Codex round-13). F4 and F5 deliberately count over DIFFERENT domains because
            their authorities differ (claim _single_video vs resolver tail-release).
        Returns True iff all three hold (fail-closed on NULL/missing op state)."""
        if download_status != "verified":
            return False
        row = conn.execute(
            "SELECT (SELECT COUNT(*) FROM heygen_remote_resources rv"
            " JOIN heygen_resource_operation_refs refv ON refv.resource_id=rv.resource_id"
            " WHERE refv.operation_id=? AND rv.resource_kind='video') AS video_count,"
            " (SELECT 1 FROM heygen_remote_resources rv"
            " WHERE rv.created_by_operation_id=? AND rv.resource_kind='video'"
            " AND rv.deletion_status!='deleted'"
            " AND rv.retention_mode!='reusable_avatar' LIMIT 1) AS live_video",
            (operation_id, operation_id)).fetchone()
        if row is None:
            return False
        return row["video_count"] <= 1 and row["live_video"] is None

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
        # Re-derive the canonical journal identity from the immutable triple and
        # reject any non-canonical upload_id / idempotency_key from the caller.
        expected_upload_id, expected_idem = derive_asset_identity(
            parent_operation_id, asset_role, content_digest)
        if upload_id != expected_upload_id or idempotency_key != expected_idem:
            raise OperationIntegrityError(
                f"non-canonical asset identity for {parent_operation_id!r}/"
                f"{asset_role!r} (upload_id or idempotency_key does not match "
                f"the derived value)")
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
        status = row["status"]
        owner, lease_exp, att = (row["lease_owner"], row["lease_expires_at"],
                                 row["attempt_started_at"])
        maybe_sent, idem_exp = row["maybe_sent_at"], row["idempotency_expires_at"]

        # --- topology integrity (blocker #2) ---
        if (owner is None) != (lease_exp is None):
            raise OperationIntegrityError(
                f"asset upload {upload_id!r} has a half lease state")
        if owner is not None and att is None:
            raise OperationIntegrityError(
                f"asset upload {upload_id!r} has a lease without an attempt")
        if status == "uploading" and att is None:
            raise OperationIntegrityError(
                f"asset upload {upload_id!r} is uploading without an attempt")
        if status == "reconciliation_required" and (maybe_sent is None or idem_exp is None):
            raise OperationIntegrityError(
                f"asset upload {upload_id!r} reconciliation_required is missing "
                f"maybe_sent_at/idempotency_expires_at")

        if status == "uploaded":
            if row["remote_resource_id"] is None:
                raise OperationIntegrityError(
                    f"uploaded asset upload {upload_id!r} has no remote_resource_id")
            parent = conn.execute(
                "SELECT credential_profile_id FROM heygen_operations "
                "WHERE operation_id=?", (parent_operation_id,),
            ).fetchone()
            if parent is None or not parent["credential_profile_id"]:
                raise OperationStateError(
                    f"parent operation {parent_operation_id!r} has no credential")
            _validate_asset_binding(
                conn, remote_resource_id=row["remote_resource_id"],
                parent_operation_id=parent_operation_id, asset_role=asset_role,
                credential_profile_id=parent["credential_profile_id"],
                expected_remote_id=None)  # claim has no asset_id; topology only
            return AssetClaimResult(upload_id, "done", row["lease_fence"],
                                   row["attempts"], None, row["remote_resource_id"])
        if status in ("failed", "cancelled", "manual_reconciliation_required"):
            return AssetClaimResult(upload_id, "terminal", row["lease_fence"],
                                   row["attempts"], None, None)

        # --- frozen 24h replay deadline (blocker #1) ---
        # Anchor the deadline at the FIRST possible-send attempt. Once set it is
        # never recomputed: a reclaim resets attempt_started_at to now, but the
        # deadline stays anchored to the original send so repeated crashes cannot
        # extend HeyGen's 24h replay window (t0 send → t23 reclaim → t46 must
        # still be past the t0+24h deadline).
        if idem_exp is None and att is not None:
            idem_exp = _canonical(_parse_utc(att) + timedelta(
                seconds=_ASSET_IDEMPOTENCY_WINDOW_SECONDS))
            maybe_sent = maybe_sent or att
        if idem_exp is not None and _parse_utc(idem_exp) <= now:
            # Past the frozen deadline (reconciliation_required OR a crashed
            # uploading attempt) → manual, never re-touch the provider.
            conn.execute(
                "UPDATE heygen_asset_uploads SET "
                "status='manual_reconciliation_required', "
                "maybe_sent_at=COALESCE(maybe_sent_at, ?), "
                "idempotency_expires_at=COALESCE(idempotency_expires_at, ?), "
                "lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
                "WHERE upload_id=?",
                (maybe_sent, idem_exp, now_iso, upload_id))
            return AssetClaimResult(upload_id, "terminal", row["lease_fence"],
                                   row["attempts"], None, None)

        # Retry-backoff gate.
        nr = row["next_retry_at"]
        if nr is not None and _parse_utc(nr) > now:
            return AssetClaimResult(upload_id, "retry_wait", row["lease_fence"],
                                   row["attempts"], None, None)
        # Active lease → busy.
        if att is not None and owner is not None and _parse_utc(lease_exp) > now:
            return AssetClaimResult(upload_id, "busy", row["lease_fence"],
                                   row["attempts"], lease_exp, None)
        # Reclaim: bump fence + attempts, set a fresh lease, and PRESERVE the
        # frozen maybe_sent_at + idempotency_expires_at (COALESCE) so the next
        # crash classification uses the original deadline, not the new attempt.
        new_fence = row["lease_fence"] + 1
        new_attempts = row["attempts"] + 1
        conn.execute(
            "UPDATE heygen_asset_uploads SET status='uploading', attempts=?, "
            "lease_owner=?, lease_expires_at=?, lease_fence=?, attempt_started_at=?, "
            "next_retry_at=NULL, maybe_sent_at=COALESCE(maybe_sent_at, ?), "
            "idempotency_expires_at=COALESCE(idempotency_expires_at, ?), updated_at=? "
            "WHERE upload_id=?",
            (new_attempts, lease_owner, expires_iso, new_fence, now_iso,
             maybe_sent, idem_exp, now_iso, upload_id),
        )
        return AssetClaimResult(upload_id, "claimed", new_fence, new_attempts,
                               expires_iso, None)

    def apply_asset_outcome_in_tx(
        self, conn: sqlite3.Connection, *, upload_id: str, asset_id: str,
        now_iso: str, lease_owner: str, expected_fence: int,
    ) -> AssetApplyOutcome:
        """On a successful adapter upload: insert the remote_resource + parent-op
        ref, backfill remote_resource_id, and flip the upload status — all fenced
        on the claim's lease_owner+fence. AT FENCED-APPLY TIME the parent
        receipt is re-checked (the withdrawn/integrity race closure): if consent
        is no longer granted, the resource is still recorded (never orphaned) but
        marked deletion_pending with the matching reason, and the upload goes
        cleanup_required (NOT a consumable uploaded). credential_profile_id is
        read from the parent op; resource_kind/retention_mode are derived. An
        idempotent re-apply validates the full binding topology and returns the
        real outcome (uploaded/cleanup_required/deleted)."""
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        _require_lease_owner(lease_owner)
        if not isinstance(expected_fence, int) or expected_fence < 0:
            raise ValueError("expected_fence must be a non-negative int")
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise ValueError("asset_id must be a non-empty string")
        row = conn.execute(
            "SELECT parent_operation_id, asset_role, status, "
            "remote_resource_id, last_error_code "
            "FROM heygen_asset_uploads WHERE upload_id = ?",
            (upload_id,),
        ).fetchone()
        if row is None:
            raise OperationStateError(f"no asset upload {upload_id!r}")
        parent = conn.execute(
            "SELECT credential_profile_id FROM heygen_operations "
            "WHERE operation_id = ?", (row["parent_operation_id"],),
        ).fetchone()
        if parent is None or not parent["credential_profile_id"]:
            raise OperationStateError(
                f"parent operation {row['parent_operation_id']!r} has no "
                f"credential_profile_id")
        credential_profile_id = parent["credential_profile_id"]
        resource_kind = _asset_resource_kind(row["asset_role"])
        retention_mode = _asset_retention_mode(row["asset_role"])
        remote_id = asset_id.strip()

        if row["status"] in ("uploaded", "cleanup_required", "deleted"):
            # Idempotent replay — validate topology + report the REAL outcome.
            if row["remote_resource_id"] is None:
                raise OperationIntegrityError(
                    f"asset upload {upload_id!r} terminal without remote_resource_id")
            res = conn.execute(
                "SELECT deletion_status, deletion_reason "
                "FROM heygen_remote_resources "
                "WHERE resource_id=?", (row["remote_resource_id"],),
            ).fetchone()
            if res is None:
                raise OperationIntegrityError(
                    f"asset upload {upload_id!r} bound resource missing")
            _validate_asset_binding(
                conn, remote_resource_id=row["remote_resource_id"],
                parent_operation_id=row["parent_operation_id"],
                asset_role=row["asset_role"],
                credential_profile_id=credential_profile_id,
                expected_remote_id=remote_id)
            # Strict asset-status ↔ resource-deletion correspondence (blocker
            # #3) + deletion_reason/last_error_code matrix (round-3 #2).
            _check_asset_resource_consistency(
                row["status"], res["deletion_status"],
                deletion_reason=res["deletion_reason"],
                last_error_code=row["last_error_code"])
            return AssetApplyOutcome(
                status=_outcome_status_for_resource_deletion(res["deletion_status"]),
                resource_id=row["remote_resource_id"])
        if row["status"] != "uploading":
            raise OperationStateError(
                f"asset upload {upload_id!r} status {row['status']!r} not applyable")

        # Re-check consent at fenced-apply time, with FULL receipt integrity
        # (a tampered digest/binding must not be trusted as granted → integrity
        # path records the remote asset + docks manual). The remote asset is
        # ALWAYS recorded, even on integrity failure (never orphaned).
        op_row = conn.execute(
            "SELECT * FROM heygen_operations WHERE operation_id = ?",
            (row["parent_operation_id"],),
        ).fetchone()
        receipt = conn.execute(
            "SELECT * FROM heygen_consent_receipts WHERE operation_id = ?",
            (row["parent_operation_id"],),
        ).fetchone()
        try:
            if op_row is None or receipt is None:
                raise ConsentError("missing operation or receipt")
            ConsentService._validate_existing_integrity(receipt, op_row, conn)
            consent, asset_error_code = _classify_apply_consent(receipt)
        except ConsentError:
            consent, asset_error_code = "integrity", _CONSENT_INTEGRITY_ERROR_CODE
        if consent == "granted":
            resource_deletion = "not_started"
            deletion_reason = None
            upload_status = "uploaded"
        elif consent == "withdrawn":
            resource_deletion = "deletion_pending"
            deletion_reason = "consent_withdrawal"
            upload_status = "cleanup_required"
        else:  # integrity (declined / missing / corrupt)
            resource_deletion = "deletion_pending"
            deletion_reason = "manual_force"
            upload_status = "cleanup_required"

        try:
            cur = conn.execute(
                "INSERT INTO heygen_remote_resources ("
                "  credential_profile_id, resource_kind, remote_id, retention_mode,"
                "  created_by_operation_id, deletion_status, deletion_reason,"
                "  created_at, updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?)",
                (credential_profile_id, resource_kind, remote_id, retention_mode,
                 row["parent_operation_id"], resource_deletion, deletion_reason,
                 now_iso, now_iso),
            )
        except sqlite3.IntegrityError as exc:
            raise OperationIntegrityError(
                f"remote asset id {remote_id!r} already exists for "
                f"{resource_kind}/{credential_profile_id}") from exc
        resource_id = cur.lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO heygen_resource_operation_refs "
            "(resource_id, operation_id, created_at) VALUES (?,?,?)",
            (resource_id, row["parent_operation_id"], now_iso),
        )
        cur = conn.execute(
            "UPDATE heygen_asset_uploads SET status=?, remote_resource_id=?, "
            "uploaded_at=?, lease_owner=NULL, lease_expires_at=NULL, "
            "attempt_started_at=NULL, next_retry_at=NULL, last_error_code=?, "
            "updated_at=? WHERE upload_id=? AND status='uploading' AND "
            "lease_owner=? AND lease_fence=? AND attempt_started_at IS NOT NULL",
            (upload_status, resource_id, now_iso, asset_error_code, now_iso,
             upload_id, lease_owner, expected_fence),
        )
        if cur.rowcount != 1:
            raise OperationIntegrityError(
                f"asset upload {upload_id!r} fence conflict — lease no longer held "
                f"(owner={lease_owner!r}, fence={expected_fence})")
        return AssetApplyOutcome(
            status=upload_status, resource_id=resource_id)

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

    # === consent-withdrawal cleanup enqueue (§5.5e5b0c3b) =================

    def enqueue_consent_withdrawal_cleanup_in_tx(
        self, conn: sqlite3.Connection, *, parent_operation_id: str, now_iso: str,
    ) -> dict[str, int]:
        """Idempotent, DB-only (NO network): mark every asset upload for this
        parent for consent-withdrawal cleanup, per the locked state machine:
          cleanup_required/deleted/cancelled → kept (idempotent terminal)
          uploading                          → left intact (its fenced apply
                                              re-checks consent → cleanup_required;
                                              never re-send multipart after withdraw)
          reconciliation_required w/ resource → cleanup_required
          reconciliation_required w/o asset_id → manual (ambiguous, no re-send)
          upload_pending/failed, PROVABLY no remote side-effect → cancelled
          upload_pending/failed w/ any maybe-sent trace → manual
          uploaded                           → cleanup_required
          manual_reconciliation_required     → manual (pre-existing; already docked
                                              manual by a prior crash/recovery —
                                              STILL needs human reconciliation, so
                                              count under `manual` NOT `kept`. Codex
                                              e5d-c round-2: previously conflated
                                              into `kept`, hiding pre-existing manual
                                              rows from maintenance.clean's exit-0
                                              gate = over-claim. `kept` is reserved
                                              for resolved/terminal idempotent rows.)
        Returns a tally of actions taken. Network deletion runs in a later
        maintenance pass; this only flips journal state."""
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        # Verify the parent receipt is GENUINELY withdrawn (full integrity +
        # pointer cleared + correct binding) before touching any asset — an
        # internal mis-call on a still-granted operation must not enqueue.
        op = conn.execute(
            "SELECT * FROM heygen_operations WHERE operation_id=?",
            (parent_operation_id,)).fetchone()
        receipt = conn.execute(
            "SELECT * FROM heygen_consent_receipts WHERE operation_id=?",
            (parent_operation_id,)).fetchone()
        if op is None:
            raise OperationStateError(f"no parent operation {parent_operation_id!r}")
        if receipt is None:
            raise OperationStateError(
                f"no consent receipt for {parent_operation_id!r}")
        try:
            ConsentService._validate_existing_integrity(receipt, op, conn)
        except ConsentError as exc:
            raise OperationStateError(
                f"receipt for {parent_operation_id!r} failed integrity: {exc}") from None
        if receipt["status"] != "withdrawn":
            raise OperationStateError(
                f"receipt for {parent_operation_id!r} is {receipt['status']!r}, "
                f"not withdrawn — will not enqueue cleanup")
        if op["consent_receipt_digest"] is not None:
            raise OperationStateError(
                f"withdrawn receipt for {parent_operation_id!r} still has an "
                f"active consent pointer")
        now = _parse_utc(now_iso)   # validates tz-aware canonical ISO (blocker #5)
        now_c = _canonical(now)     # direct cleanup-writes stamp canonical time (round-3 #3)
        rows = conn.execute(
            "SELECT upload_id, parent_operation_id, asset_role, status, "
            "remote_resource_id, maybe_sent_at, idempotency_expires_at, "
            "attempt_started_at, lease_owner, lease_expires_at "
            "FROM heygen_asset_uploads WHERE parent_operation_id=?",
            (parent_operation_id,),
        ).fetchall()
        tally = {"cancelled": 0, "cleanup_required": 0, "manual": 0,
                 "kept": 0, "left_uploading": 0}
        for row in rows:
            st = row["status"]
            if st in ("cleanup_required", "deleted", "cancelled"):
                tally["kept"] += 1
            elif st == "uploading":
                # Active lease → left intact (fenced apply will catch the
                # withdraw). Expired lease (worker crashed) → manual. Half-lease
                # / uploading-without-attempt → integrity error (blocker #5).
                owner, lexp = row["lease_owner"], row["lease_expires_at"]
                if (owner is None) != (lexp is None):
                    raise OperationIntegrityError(
                        f"asset upload {row['upload_id']!r} half lease on withdraw")
                if row["attempt_started_at"] is None:
                    raise OperationIntegrityError(
                        f"asset upload {row['upload_id']!r} uploading w/o attempt")
                if owner is not None and _parse_utc(lexp) > now:
                    tally["left_uploading"] += 1   # active lease, fenced apply catches it
                else:
                    self._to_manual_in_tx(conn, upload_id=row["upload_id"], now_iso=now_iso)
                    tally["manual"] += 1
            elif st == "uploaded":
                self._mark_asset_cleanup_in_tx(
                    conn, row=row, reason="consent_withdrawal", now_iso=now_iso)
                tally["cleanup_required"] += 1
            elif st == "reconciliation_required":
                if row["remote_resource_id"] is not None:
                    self._mark_asset_cleanup_in_tx(
                        conn, row=row, reason="consent_withdrawal", now_iso=now_iso)
                    tally["cleanup_required"] += 1
                else:
                    self._to_manual_in_tx(conn, upload_id=row["upload_id"],
                                          now_iso=now_iso)
                    tally["manual"] += 1
            elif st in ("upload_pending", "failed"):
                # Cancel ONLY if we can PROVE no remote side-effect ever happened.
                provably_clean = (
                    row["remote_resource_id"] is None
                    and row["maybe_sent_at"] is None
                    and row["idempotency_expires_at"] is None
                    and row["attempt_started_at"] is None
                    and row["lease_owner"] is None
                    and row["lease_expires_at"] is None)
                if provably_clean:
                    conn.execute(
                        "UPDATE heygen_asset_uploads SET status='cancelled', "
                        "lease_owner=NULL, lease_expires_at=NULL, attempt_started_at=NULL, "
                        "next_retry_at=NULL, updated_at=? WHERE upload_id=?",
                        (now_c, row["upload_id"]))
                    tally["cancelled"] += 1
                else:
                    self._to_manual_in_tx(conn, upload_id=row["upload_id"],
                                          now_iso=now_iso)
                    tally["manual"] += 1
            elif st == "manual_reconciliation_required":
                # already docked manual by a prior crash/recovery; STILL needs
                # human reconciliation → count under `manual` (NOT `kept`). Codex
                # e5d-c round-2 B2: previously conflated into `kept`, hiding
                # pre-existing manual rows from maintenance.clean's exit-0 gate
                # (over-claim). `kept` is reserved for resolved/terminal rows.
                tally["manual"] += 1
            else:
                # An unknown status must NOT fall into a catch-all "kept" — that
                # would silently swallow a future status and leak a remote asset
                # past a withdrawal. Fail closed (round-3 #3).
                raise OperationIntegrityError(
                    f"asset upload {row['upload_id']!r} unknown status {st!r} "
                    f"on consent-withdrawal cleanup")
        return tally

    def _mark_asset_cleanup_in_tx(
        self, conn, *, row, reason: str, now_iso: str,
    ) -> None:
        """Flip an asset upload to cleanup_required and mark its bound resource
        deletion_pending with the given reason. The resource UPDATE is GATED on
        its current deletion_status (only not_started is accepted — never revive
        a deleted/deletion_failed resource) and its rowcount checked; identity
        topology is re-verified so a foreign resource_id cannot be touched.
        Stamps canonical time regardless of the caller's now_iso form (round-3 #3)."""
        now_c = _canonical(_parse_utc(now_iso))
        rid = row["remote_resource_id"]
        if rid is None:
            raise OperationIntegrityError(
                f"asset upload {row['upload_id']!r} has no remote_resource_id")
        # Re-verify identity topology before touching the resource.
        op = conn.execute(
            "SELECT credential_profile_id FROM heygen_operations "
            "WHERE operation_id=?", (row["parent_operation_id"],)).fetchone()
        if op is None:
            raise OperationIntegrityError(
                f"parent op missing for {row['upload_id']!r}")
        _validate_asset_binding(
            conn, remote_resource_id=rid,
            parent_operation_id=row["parent_operation_id"],
            asset_role=row["asset_role"],
            credential_profile_id=op["credential_profile_id"],
            expected_remote_id=None)
        cur = conn.execute(
            "UPDATE heygen_remote_resources SET deletion_status='deletion_pending', "
            "deletion_reason=?, updated_at=? WHERE resource_id=? "
            "AND deletion_status='not_started'",
            (reason, now_c, rid))
        if cur.rowcount != 1:
            raise OperationIntegrityError(
                f"resource_id={rid} not in not_started (cannot mark cleanup)")
        conn.execute(
            "UPDATE heygen_asset_uploads SET status='cleanup_required', "
            "lease_owner=NULL, lease_expires_at=NULL, attempt_started_at=NULL, "
            "next_retry_at=NULL, last_error_code=NULL, updated_at=? WHERE upload_id=?",
            (now_c, row["upload_id"]))

    @staticmethod
    def _to_manual_in_tx(conn, *, upload_id: str, now_iso: str) -> None:
        now_c = _canonical(_parse_utc(now_iso))   # canonical time (round-3 #3)
        conn.execute(
            "UPDATE heygen_asset_uploads SET status='manual_reconciliation_required', "
            "lease_owner=NULL, lease_expires_at=NULL, attempt_started_at=NULL, "
            "next_retry_at=NULL, updated_at=? WHERE upload_id=?",
            (now_c, upload_id))

    def recover_withdrawn_asset_cleanups(self, *, now_iso: str) -> dict[str, int]:
        """Maintenance recovery: for every operation with a withdrawn receipt,
        re-run the consent-withdrawal cleanup enqueue. Idempotent — covers the
        crash window where ConsentService.withdraw committed the receipt flip but
        crashed before/during the enqueue. Network deletion itself runs in a
        later pass; this only reconciles journal state. Returns an aggregate
        tally across all recovered operations."""
        aggregate = {"cancelled": 0, "cleanup_required": 0, "manual": 0,
                     "kept": 0, "left_uploading": 0}
        with self.begin_immediate() as conn:
            rows = conn.execute(
                "SELECT DISTINCT operation_id FROM heygen_consent_receipts "
                "WHERE status='withdrawn'").fetchall()
            for r in rows:
                tally = self.enqueue_consent_withdrawal_cleanup_in_tx(
                    conn, parent_operation_id=r["operation_id"], now_iso=now_iso)
                for key, val in tally.items():
                    aggregate[key] = aggregate.get(key, 0) + val
        return aggregate

    def count_recovery_attention(self) -> dict[str, int]:
        """Read-only post-recovery ATTENTION AUDIT (Codex e5d-c round-3 + round-5).

        The two recovery primitives have DELIBERATELY SCOPED mandates (locked
        across many review rounds — their scopes must NOT be broadened silently):
          - ``recover_withdrawn_asset_cleanups`` selects ONLY operations whose
            receipt is ``withdrawn`` (op_repo:3080). A manual_reconciliation_
            required asset row on a NON-withdrawn op — produced by the frozen
            24h replay-deadline expiry (op_repo:2637) or upload-failure handling
            (op_repo:2855) — is INVISIBLE to the DB pass's ``manual`` tally.
          - ``recover_deletions`` excludes ``manual_force`` from its candidate
            SELECT (op_repo:4030 — the operator-only integrity path; c1 claims
            it ``not_ready``, never auto-deleted). A non-deleted ``manual_force``
            resource is INVISIBLE to the deletion tally.

        Both classes are operator-attention-needed but neither recovery primitive
        touches them. Without this audit, the maintenance exit-0 contract
        ("nothing needs attention") would be谎报 — exit 0 with a stuck manual row
        or a pending ``manual_force`` resource (fail-closed violation:
        宁可少报绝不虚报). This read-only post-pass query counts them so the
        maintenance exit code is HONEST about the journal's FINAL attention state.

        Counts:
          - ``manual_uploads``: every ``heygen_asset_uploads`` row in
            ``manual_reconciliation_required`` (ALL rows — the withdrawn-ops'
            rows overlap the DB pass's ``manual`` tally; the non-withdrawn rows
            are the gap this audit closes). Each needs human reconciliation
            (the system cannot determine whether the upload hit HeyGen).
          - ``manual_force_resources``: every NON-deleted ``heygen_remote_
            resources`` row with ``deletion_reason='manual_force'``. Each needs
            explicit operator action (never auto-recovered).
          - ``unrecoverable_resources`` (Codex round-5 + round-6 + round-7): every
            NON-deleted, NON-manual_force, NON-reusable ``heygen_remote_resources``
            row the deletion candidate SELECT refuses to drive this sweep. Three
            domains — ALL legitimately attention-needed (exit 2 is honest; the
            deletion genuinely cannot proceed this sweep):
              (a) ORPHAN — ``created_by_operation_id IS NULL``. The outer
                  candidate filter requires IS NOT NULL (op_repo:4177/4399), so
                  the row is never swept; and ``heygen_operations`` is never
                  DELETEd (no code path does it), so the FK ``ON DELETE SET
                  NULL`` never fires in normal flow → primitive-unreachable
                  (static-corruption class; zero on any producer-valid journal).
              (b) ANOMALOUS STATE-MATRIX — ``(not_started)+(non-NULL reason)``
                  OR ``(deletion_pending|deletion_failed)+(NULL reason)``. The
                  claim is the ONLY primitive that sets a reason + it sets
                  ``deletion_pending`` in the SAME UPDATE, so these states are
                  primitive-unreachable (static-corruption class; zero on any
                  producer-valid journal). Mirrors the candidate SELECT's witness
                  STATE-MATRIX gate (branch A admits only ``not_started+NULL``
                  and ``(pending|failed)+(post_download|consent_withdrawal)``).
              (c) Codex round-6 (NO-WITNESS / NON-SELECTABLE) — a CLAIM-ELIGIBLE
                  resource (``pending|failed + post_download|consent_withdrawal``)
                  on an op whose witness predicate is currently UNSATISFIED: no r2
                  on the op satisfies the FULL witness predicate. The no-witness
                  condition has two typical causes (Codex round-7 distinguished
                  them): broken topology (c1) — missing/foreign ref, credential
                  mismatch, wrong kind, broken upload-binding; OR an ACTIVE op-
                  lease (c2), which branch B's ``o.lease_owner IS NULL`` gate
                  rejects. NOTE: ``no video`` is NOT an independent cause — branch
                  B2 explicitly admits a zero-video op (``COUNT(video) <= 1``, not
                  ``== 1``; see the constant @4091-4134), so a properly-bound
                  asset witnesses via B2 even with zero video rows. Round-5
                  counted only domains (a)+(b); such a resource escapes BOTH
                  (normal state-matrix) AND the per-op pass (never selected →
                  ``ops_alerted`` stays 0) → round-5 虚报'd exit 0 over it. The
                  two cases:
                    (c1) STATIC CORRUPTION — broken topology via direct INSERT
                        evading validate-then-INSERT (the SAME threat model as
                        (b); primitive-unreachable, zero on producer-valid
                        journals). NOT the documented journal-replacement TOCTOU
                        class (the row EXISTS at audit time; no concurrent
                        mutation).
                    (c2) TRANSIENT BLOCKED-PENDING (producer-valid) — a cleanup
                        (``consent_withdrawal`` or ``post_download``) claimed an
                        asset on an op whose lease is still active. ``withdraw``
                        (consent.py:591-673) UPDATEs the receipt AND the op's
                        consent-lifecycle fields (``status`` for pristine ops,
                        ``consent_receipt_digest``, ``updated_at``) but does NOT
                        clear or modify the op lease (``lease_owner`` /
                        ``lease_expires_at`` / ``lease_fence`` /
                        ``attempt_started_at``); the enqueue then sits the asset
                        ``deletion_pending`` on a leased op. Branch B's
                        ``o.lease_owner IS NULL`` gate makes the op non-selectable
                        THIS sweep (NOT a missing video — see the note above);
                        the asset is genuinely pending deletion but blocked. This
                        is NOT corruption and the count is NOT guaranteed zero on
                        every producer-valid journal — it is zero on SETTLED
                        journals (lease cleared) and non-zero during the brief
                        active-lease window, which is CORRECT (the deletion cannot
                        proceed this sweep; exit 2 is honest). It auto-resolves:
                        once the lease clears independently (expired / fenced /
                        released), branch B's lease gate passes; a properly-bound
                        asset then witnesses via B2 (``consent_withdrawal`` is
                        delivery-independent; ``post_download`` requires
                        ``download_status='verified'`` + ``COUNT(video) <= 1``,
                        both satisfiable with zero video rows), the op becomes
                        selectable, the coordinator drives it, and the count
                        returns to 0.
                Domain (c) mirrors the FULL witness predicate via the SHARED
                ``_DELETION_WITNESS_SUBQUERY_SQL`` constant (the SAME constant
                ``recover_deletions``'s candidate SELECT uses) — eliminating the
                hand-mirroring drift risk Codex round-4/5 flagged across the 6+
                topology classes. (c) is RESTRICTED to claim-eligible states so a
                normal in-flight ``not_started+NULL`` pre-video portrait (default
                sweep deliberately excludes pre-video assets — the resolver has
                not released the tail) is NOT counted.
            Each such row is operator-attention-needed AND invisible to both
            recovery primitives. Without this count, exit 0 would 谎报 "clean"
            over a row the deletion subsystem itself refuses to drive — a direct
            inconsistency between the exit-0 contract and the candidate SELECT's
            own fail-closed posture.

        NOT counted (in-flight pipeline states that WILL resolve on a future
        sweep — NOT stuck attention): ``deletion_pending`` resources with an
        auto-recoverable reason (``post_download`` / ``consent_withdrawal``) on a
        SELECTABLE op (witness satisfied) correctly waiting behind §3.5 video-
        first ordering — the per-op pass will claim them this sweep; ``not_started
        +NULL`` (fresh resource, never claimed); active ``uploading`` leases (the
        DB pass's ``left_uploading`` tally already flags these per-sweep). The
        SAME ``post_download``/``consent_withdrawal`` reason on a NON-selectable
        op (no witness — typically an ACTIVE LEASE; ``no video`` alone does NOT
        make an op non-selectable, since branch B2 admits zero-video ops) IS
        counted — that is domain (c2) above, a genuinely blocked deletion.
        Counting the selectable-op case would make exit 0 unreachable during
        normal multi-resource consent-withdrawal cleanup (video must delete
        before assets), conflating "progressing" with "stuck" (Codex round-3 #3
        — pushed back: this is §3.5 ordering, not a strand).

        Opens ``file:<escaped>?mode=ro`` (mirrors ``capabilities._journal_state``
        at line 340) — creates / migrates / writes NOTHING. Read-only by
        construction; safe to call after either recovery pass commits. Raises
        on DB/schema error so maintenance fails-closed (exit 2 with
        ``attention_audit_failed``) rather than silently over-claiming clean.
        """
        import urllib.parse

        resolved = self._db_path.resolve().as_posix()
        uri = "file:" + urllib.parse.quote(resolved) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            manual_uploads = conn.execute(
                "SELECT COUNT(*) FROM heygen_asset_uploads "
                "WHERE status='manual_reconciliation_required'").fetchone()[0]
            manual_force = conn.execute(
                "SELECT COUNT(*) FROM heygen_remote_resources "
                "WHERE deletion_reason='manual_force' "
                "AND deletion_status != 'deleted'").fetchone()[0]
            # Codex round-5 + round-6 + round-7 — unrecoverable resources. Three
            # domains; (a)/(b) are ZERO on any producer-valid journal, (c) splits:
            #   (a) ORPHAN: ``created_by_operation_id IS NULL`` (primitive-unreach-
            #       able; zero on producer-valid).
            #   (b) ANOMALOUS STATE-MATRIX: ``not_started+reason`` OR ``pending/
            #       failed+NULL`` (the claim always sets status+reason in the SAME
            #       UPDATE — primitive-unreachable; zero on producer-valid). Mirrors
            #       the candidate SELECT's witness STATE-MATRIX gate.
            #   (c) Codex round-6 (broken-topology gap) + round-7 (c1/c2 split): a
            #       CLAIM-ELIGIBLE resource (``pending/failed + post_download/
            #       consent_withdrawal``) on an op the candidate SELECT REJECTS —
            #       NO r2 on the op satisfies the FULL witness predicate. Such a
            #       resource has a NORMAL state-matrix (escapes (b)); it is never
            #       selected (``ops_alerted`` stays 0); round-5 虚报'd exit 0 over
            #       it. TWO sub-cases, SAME shared-predicate gate:
            #       (c1) STATIC CORRUPTION: missing/foreign ref, credential mis-
            #           match, wrong kind, broken upload-binding — a direct INSERT
            #           evades the primitives' validate-then-INSERT. Primitive-un-
            #           reachable; zero on producer-valid. SAME threat model as (b).
            #       (c2) TRANSIENT BLOCKED-PENDING (PRODUCER-VALID, round-7/8):
            #           a cleanup (``consent_withdrawal`` or ``post_download``)
            #           claimed an asset on an op whose lease is still active.
            #           ``withdraw`` (consent.py:591-673) UPDATEs the receipt AND
            #           the op's consent-lifecycle fields (``status`` for pristine
            #           ops, ``consent_receipt_digest``, ``updated_at``) but does
            #           NOT clear/modify the op lease. The enqueue sits the asset
            #           ``deletion_pending`` on a leased op; branch B's
            #           ``o.lease_owner IS NULL`` gate then makes it non-selectable
            #           THIS sweep. NOTE: ``no video`` is NOT an independent cause
            #           — branch B2 admits a zero-video op (``COUNT(video) <= 1``;
            #           see the constant @4091-4134), so a properly-bound asset
            #           witnesses via B2 with zero video rows once the lease
            #           clears. This is NOT corruption — it is an honest transient
            #           "blocked-pending" the operator may legitimately see mid-
            #           flight. NOT guaranteed zero on every producer-valid journal;
            #           only zero on SETTLED journals (lease cleared). Counting it
            #           (exit 2) is the fail-closed / 宁可少报绝不虚报 choice — never
            #           under-report.
            # Mirroring the FULL witness predicate (not just state-matrix) via the
            # SHARED _DELETION_WITNESS_SUBQUERY_SQL constant eliminates the drift
            # risk Codex round-4/5 flagged (hand-mirroring 6+ topology classes by
            # hand inevitably drifts). Domain (c) is RESTRICTED to claim-eligible
            # states so a normal in-flight ``not_started+NULL`` pre-video portrait
            # (the default sweep deliberately excludes pre-video assets — the
            # resolver has not yet released the tail) is NOT counted.
            # The outer row is aliased ``r`` because _DELETION_WITNESS_SUBQUERY_SQL
            # correlates on ``r.created_by_operation_id`` (the SAME alias the
            # candidate SELECT uses). ``deletion_reason IS NULL OR != 'manual_force'``
            # is the SQL idiom admitting NULL (``NULL != 'manual_force'`` is
            # NULL/falsy, which would wrongly exclude the pending+NULL anomaly in
            # domain b).
            unrecoverable = conn.execute(
                "SELECT COUNT(*) FROM heygen_remote_resources r "
                "WHERE r.deletion_status != 'deleted' "
                "AND (r.deletion_reason IS NULL OR r.deletion_reason != 'manual_force') "
                "AND r.retention_mode != 'reusable_avatar' "
                "AND ("
                " r.created_by_operation_id IS NULL"
                " OR (r.deletion_status = 'not_started'"
                "     AND r.deletion_reason IS NOT NULL)"
                " OR (r.deletion_status IN ('deletion_pending', 'deletion_failed')"
                "     AND r.deletion_reason IS NULL)"
                " OR (r.deletion_status IN ('deletion_pending', 'deletion_failed')"
                "     AND r.deletion_reason IN ('post_download', 'consent_withdrawal')"
                "     AND r.created_by_operation_id IS NOT NULL"
                "     AND NOT EXISTS (" + _DELETION_WITNESS_SUBQUERY_SQL + "))"
                ")").fetchone()[0]
        finally:
            conn.close()
        return {"manual_uploads": int(manual_uploads),
                "manual_force_resources": int(manual_force),
                "unrecoverable_resources": int(unrecoverable)}


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


def _asset_retention_mode(asset_role: str) -> str:
    """Both one-shot asset roles are ephemeral (deleted after the delivery
    completes). A reusable_avatar retention mode is a future, separate
    lifecycle (photo avatar kept across operations) — not inferred here."""
    if asset_role not in _ASSET_ROLE_TO_RESOURCE_KIND:
        raise OperationIntegrityError(f"no retention mapping for {asset_role!r}")
    return "ephemeral"


def _validate_asset_binding(
    conn: sqlite3.Connection, *, remote_resource_id: int,
    parent_operation_id: str, asset_role: str, credential_profile_id: str,
    expected_remote_id: str | None,
) -> int:
    """Shared fail-closed topology check for an already-uploaded asset binding,
    used by both claim's 'done' path and apply's idempotent replay. Validates
    remote_id (when expected_remote_id given), resource_kind, created_by,
    credential_profile, retention_mode, deletion_status, and that exactly the
    parent operation references the resource (no foreign refs). Returns the
    resource_id or raises OperationIntegrityError."""
    conn.row_factory = sqlite3.Row
    res = conn.execute(
        "SELECT remote_id, resource_kind, created_by_operation_id, "
        "credential_profile_id, retention_mode, deletion_status "
        "FROM heygen_remote_resources WHERE resource_id=?",
        (remote_resource_id,),
    ).fetchone()
    if res is None:
        raise OperationIntegrityError(
            f"asset binding resource_id={remote_resource_id} missing")
    expected_kind = _asset_resource_kind(asset_role)
    expected_retention = _asset_retention_mode(asset_role)
    if (res["resource_kind"] != expected_kind
            or res["created_by_operation_id"] != parent_operation_id
            or res["credential_profile_id"] != credential_profile_id
            or res["retention_mode"] != expected_retention
            or res["deletion_status"] not in
            ("not_started", "deletion_pending", "deleted", "deletion_failed")
            or (expected_remote_id is not None
                and res["remote_id"] != expected_remote_id)):
        raise OperationIntegrityError(
            f"asset binding resource_id={remote_resource_id} topology changed")
    refs = conn.execute(
        "SELECT operation_id FROM heygen_resource_operation_refs "
        "WHERE resource_id=?", (remote_resource_id,),
    ).fetchall()
    if len(refs) != 1 or refs[0]["operation_id"] != parent_operation_id:
        raise OperationIntegrityError(
            f"asset binding resource_id={remote_resource_id} has foreign or "
            f"missing operation refs")
    return remote_resource_id


# Consent classification at fenced-apply time (the withdrawn/integrity race).
_CONSENT_INTEGRITY_ERROR_CODE = "consent_integrity_failure"


def _classify_apply_consent(receipt_row) -> tuple[str, str | None]:
    """Classify the parent receipt at fenced-apply time. Returns
    (classification, asset_error_code):
      granted    → upload normally, resource not_started
      withdrawn  → cleanup_required, resource deletion_pending/consent_withdrawal
      integrity  → declined/missing/corrupt → cleanup_required, resource
                   deletion_pending/manual_force + consent_integrity_failure
    """
    if receipt_row is None:
        return ("integrity", _CONSENT_INTEGRITY_ERROR_CODE)
    status = receipt_row["status"]
    if status == "granted":
        return ("granted", None)
    if status == "withdrawn":
        return ("withdrawn", None)
    # declined (or any non-granted/non-withdrawn) → integrity problem
    return ("integrity", _CONSENT_INTEGRITY_ERROR_CODE)


_OUTCOME_FOR_DELETION = {
    "not_started": "uploaded",
    "deleted": "deleted",
    "deletion_pending": "cleanup_required",
    "deletion_failed": "cleanup_required",
}


def _outcome_status_for_resource_deletion(deletion_status: str) -> str:
    """Map a resource's deletion_status to the AssetApplyOutcome.status that
    faithfully reports it (idempotent replay). Unknown values raise — never a
    silent catch-all (blocker #3)."""
    try:
        return _OUTCOME_FOR_DELETION[deletion_status]
    except KeyError:
        raise OperationIntegrityError(
            f"unknown resource deletion_status: {deletion_status!r}") from None


# Strict asset-status ↔ resource-deletion matrix. A mismatch (e.g. asset
# uploaded but resource deletion_pending) is an integrity error, never silently
# "corrected" (blocker #3).
_ASSET_STATUS_FOR_DELETION = {
    "not_started": "uploaded",
    "deletion_pending": "cleanup_required",
    "deletion_failed": "cleanup_required",
    "deleted": "deleted",
}

# Closed vocabulary for heygen_remote_resources.deletion_reason (mirrors the
# table's CHECK constraint). not_started requires NULL; every deletion-bearing
# state requires one of these three (round-3 #2).
_DELETION_REASON_VALUES = frozenset(
    {"post_download", "consent_withdrawal", "manual_force"}
)


def _check_asset_resource_consistency(
    asset_status: str, deletion_status: str, *,
    deletion_reason: str | None, last_error_code: str | None,
) -> None:
    """Strict asset-status ↔ resource (deletion_status + deletion_reason, and
    for the manual_force/integrity path, the asset's last_error_code)
    correspondence. A mismatch is an integrity error, never silently
    "corrected" (blocker #3, round-3 #2).

    The manual_force reason is produced ONLY by the fenced-apply integrity
    path, which records consent_integrity_failure on the asset. The resource
    row's deletion_reason is a generic cause marker that does NOT durably
    encode consent_integrity_failure, so the asset's last_error_code is the
    ONLY durable integrity-cause signal — and it must persist through EVERY
    deletion state (deletion_pending / deletion_failed / deleted). We do NOT
    stop re-checking it once the resource reaches 'deleted': a deleted
    resource with manual_force but a missing/cleared error code is still a
    forgery of the integrity path (round-4 #2)."""
    expected = _ASSET_STATUS_FOR_DELETION.get(deletion_status)
    if expected is None:
        raise OperationIntegrityError(
            f"unknown resource deletion_status: {deletion_status!r}")
    if asset_status != expected:
        raise OperationIntegrityError(
            f"asset status {asset_status!r} != resource deletion_status "
            f"{deletion_status!r} correspondence")
    if deletion_status == "not_started":
        if deletion_reason is not None:
            raise OperationIntegrityError(
                f"resource deletion_status not_started but deletion_reason="
                f"{deletion_reason!r}")
    else:
        # deletion_pending / deletion_failed / deleted → a known reason is required.
        if deletion_reason not in _DELETION_REASON_VALUES:
            raise OperationIntegrityError(
                f"resource deletion_status {deletion_status!r} requires a known "
                f"deletion_reason (one of {sorted(_DELETION_REASON_VALUES)}), "
                f"got {deletion_reason!r}")
        if (deletion_reason == "manual_force"
                and last_error_code != _CONSENT_INTEGRITY_ERROR_CODE):
            raise OperationIntegrityError(
                f"manual_force deletion_reason requires asset last_error_code="
                f"{_CONSENT_INTEGRITY_ERROR_CODE!r} (the durable integrity-cause "
                f"marker) on deletion_status {deletion_status!r}, "
                f"got {last_error_code!r}")


# --- asset upload processor (§5.5e5b0c2) --------------------------------
#
# Orchestrates the crash-safe asset upload: guard+claim linearize in ONE
# BEGIN IMMEDIATE tx; the adapter call runs OUTSIDE any tx; the outcome is
# applied in a second fenced tx (lease_owner + expected_fence from the claim).
# A stale worker cannot overwrite a newer claim; a crash at any point leaves a
# row the next run can reclaim (or, past the 24h window, promote to manual).

_ASSET_REMOTE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


@dataclass(frozen=True)
class AssetUploadClaim:
    """Handle returned by claim_for_upload: the full claim classification plus
    the command (so apply can validate the adapter result against it)."""
    upload_id: str
    status: str            # AssetClaimResult.status (claimed/busy/done/terminal/retry_wait)
    fence: int
    attempts: int
    resource_id: int | None
    command: AssetUploadCommand


@dataclass(frozen=True)
class AssetApplyOutcome:
    """Result of applying a successful upload. status reflects whether the
    resource is consumable: uploaded (resource not_started), cleanup_required
    (resource deletion_pending/deletion_failed — consent withdrawn or integrity
    problem at apply time), or deleted. resource_id is always the bound row."""
    status: str  # "uploaded" | "cleanup_required" | "deleted"
    resource_id: int

    def __post_init__(self) -> None:
        if self.status not in ("uploaded", "cleanup_required", "deleted"):
            raise ValueError(f"unknown AssetApplyOutcome.status: {self.status!r}")
        if type(self.resource_id) is not int or self.resource_id <= 0:
            raise ValueError("resource_id must be a positive int")


@dataclass(frozen=True)
class AssetUploadOnceResult:
    status: str  # uploaded | reconciliation_required | failed | busy | terminal | retry_wait
    resource_id: int | None = None
    error_code: str | None = None


def _validate_asset_upload_result(result: AssetUploadResult,
                                  command: AssetUploadCommand) -> None:
    """An AssetUploadResult is a public dataclass — verify the adapter returned
    one that actually matches the prepared command before binding it."""
    if not isinstance(result, AssetUploadResult):
        raise OperationIntegrityError("adapter did not return an AssetUploadResult")
    if result.mime_type != command.content_type:
        raise OperationIntegrityError(
            f"adapter mime_type {result.mime_type!r} != command {command.content_type!r}")
    if result.size_bytes != command.file_size:
        raise OperationIntegrityError(
            f"adapter size {result.size_bytes} != command {command.file_size}")
    if not _ASSET_REMOTE_ID_RE.fullmatch(result.asset_id or ""):
        raise OperationIntegrityError(
            f"adapter returned invalid asset_id {result.asset_id!r}")


class AssetUploadProcessor:
    """Crash-safe asset upload orchestrator. Inject the ConsentService +
    OperationRepository via the project_dir; inject the adapter per call."""

    def __init__(self, project_dir: str | Path) -> None:
        self._project_dir = Path(project_dir)
        self._repository = OperationRepository(self._project_dir)
        self._consent = ConsentService(self._project_dir)

    def claim_for_upload(
        self, *, command: AssetUploadCommand,
        lease_owner: str, now_iso: str, lease_seconds: int,
    ) -> AssetUploadClaim:
        """ONE transaction: consent guard → claim. If consent fails, nothing is
        written (raises a Consent* error). Returns the FULL claim classification
        (claimed/busy/done/terminal/retry_wait) — the caller does not re-query."""
        with self._repository.begin_immediate() as conn:
            self._consent.validate_asset_upload_consent_in_tx(
                conn, parent_operation_id=command.operation_id,
                asset_role=command.asset_role,
                content_digest=command.expected_asset_digest)
            claim = self._repository.claim_asset_upload_in_tx(
                conn, upload_id=command.upload_id,
                parent_operation_id=command.operation_id,
                asset_role=command.asset_role,
                content_digest=command.expected_asset_digest,
                local_ref=command.local_output_ref,
                content_type=command.content_type,
                size_bytes=command.file_size,
                provider_filename=command.provider_filename,
                idempotency_key=command.idempotency_key,
                lease_owner=lease_owner, now_iso=now_iso,
                lease_seconds=lease_seconds)
        return AssetUploadClaim(
            upload_id=claim.upload_id, status=claim.status, fence=claim.fence,
            attempts=claim.attempts, resource_id=claim.remote_resource_id,
            command=command)

    def apply_outcome(
        self, *, claim: AssetUploadClaim, asset_result: AssetUploadResult,
        lease_owner: str, now_iso: str,
    ) -> AssetApplyOutcome:
        """Fenced apply of a successful upload (second tx). Validates the
        adapter result matches the command, then CAS on lease_owner+fence. The
        outcome.status is uploaded only if consent was still granted at apply
        time; withdrawn/integrity → cleanup_required (resource recorded but
        marked for deletion)."""
        _validate_asset_upload_result(asset_result, claim.command)
        with self._repository.begin_immediate() as conn:
            return self._repository.apply_asset_outcome_in_tx(
                conn, upload_id=claim.upload_id,
                asset_id=asset_result.asset_id, now_iso=now_iso,
                lease_owner=lease_owner, expected_fence=claim.fence)

    def apply_failure(
        self, *, claim: AssetUploadClaim, error: HeyGenAdapterError,
        lease_owner: str, now_iso: str, backoff_seconds: int = 30,
    ) -> str:
        """Fenced apply of a failed upload (second tx)."""
        with self._repository.begin_immediate() as conn:
            return self._repository.apply_asset_upload_failure_in_tx(
                conn, upload_id=claim.upload_id, error_code=error.code,
                submission_certainty=error.submission_certainty,
                retryable=error.retryable, now_iso=now_iso,
                lease_owner=lease_owner, expected_fence=claim.fence,
                backoff_seconds=backoff_seconds)

    def upload_once(
        self, *, command: AssetUploadCommand, adapter,
        runtime_root: Path, lease_owner: str, now_iso: str,
        lease_seconds: int,
    ) -> AssetUploadOnceResult:
        """One full attempt: claim (guard+claim tx) → adapter.upload_asset
        (outside tx) → fenced apply. The claim classification is forwarded
        verbatim (no re-query): done→uploaded+resource_id, busy/retry_wait/
        terminal are surfaced as-is. Unknown adapter exceptions propagate (the
        lease is left to expire — certainty is unknowable, never guess)."""
        claim = self.claim_for_upload(
            command=command, lease_owner=lease_owner, now_iso=now_iso,
            lease_seconds=lease_seconds)
        if claim.status == "done":
            return AssetUploadOnceResult(status="uploaded",
                                         resource_id=claim.resource_id)
        if claim.status in ("busy", "retry_wait", "terminal"):
            return AssetUploadOnceResult(status=claim.status)
        # claimed → invoke the adapter outside any tx.
        try:
            result = adapter.upload_asset(command, runtime_root=runtime_root)
        except AssetUploadAmbiguousError as exc:
            status = self.apply_failure(
                claim=claim, error=exc, lease_owner=lease_owner, now_iso=now_iso)
            return AssetUploadOnceResult(status=status, error_code=exc.code)
        except AssetUploadError as exc:
            status = self.apply_failure(
                claim=claim, error=exc, lease_owner=lease_owner, now_iso=now_iso)
            return AssetUploadOnceResult(status=status, error_code=exc.code)
        outcome = self.apply_outcome(
            claim=claim, asset_result=result, lease_owner=lease_owner, now_iso=now_iso)
        return AssetUploadOnceResult(status=outcome.status,
                                     resource_id=outcome.resource_id)


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
                now_iso, result, expected_remote_id=claim.remote_id,
                max_attempts=max_attempts)
        return DeletionOnceResult(claim=claim, outcome=outcome)


class AssetDeletionProcessor:
    """One deletion step of a one-shot asset (portrait_photo /
    synthetic_narration_audio) resource: claim → delete outside tx → fenced
    apply. Mirrors DeleteProcessor but fences on the asset's OWN lease columns
    and drives the asset GET/DELETE adapter (AssetDeleteResult /
    AssetReadError). The claim classification is forwarded verbatim — a
    not_ready/busy/retry_wait claim surfaces with no outcome (the caller, c2/c3
    resolver+coordinator, decides ordering and retries)."""

    def __init__(self, project_dir):
        self._project_dir = Path(project_dir)
        self._repository = OperationRepository(self._project_dir)

    def delete_once(self, *, upload_id, lease_owner, adapter, now_iso, lease_seconds,
                    max_attempts=DELETION_MAX_ATTEMPTS, force: bool = False):
        with self._repository.begin_immediate() as conn:
            claim = self._repository.claim_asset_deletion_in_tx(
                conn, upload_id=upload_id, lease_owner=lease_owner, now_iso=now_iso,
                lease_seconds=lease_seconds, max_attempts=max_attempts, force=force)
        if claim.status != "claimed":
            return AssetDeletionOnceResult(claim=claim, outcome=None)
        # Adapter call OUTSIDE any tx; an AssetReadError is captured as the
        # outcome so apply can map retryable/terminal uniformly. Any OTHER
        # exception propagates (certainty is unknowable — leave the lease to
        # expire, never guess a phantom outcome).
        try:
            result = adapter.delete_asset(claim.remote_id)
        except AssetReadError as exc:
            result = exc
        with self._repository.begin_immediate() as conn:
            outcome = self._repository.apply_asset_deletion_outcome_in_tx(
                conn, upload_id=upload_id, resource_id=claim.resource_id,
                lease_owner=lease_owner, fence=claim.fence, now_iso=now_iso,
                expected_remote_id=claim.remote_id,
                result=result, max_attempts=max_attempts, force=force)
        return AssetDeletionOnceResult(claim=claim, outcome=outcome)


#: The DEFAULT-mode deletion WITNESS subquery — the heart of the candidate
#: SELECT (``DeletionCoordinator.recover_deletions``) AND the round-6 attention
#: audit (``OperationRepository.count_recovery_attention``). Codex round-6 e5d-c:
#: the attention audit must mirror the EXACT witness topology the candidate SELECT
#: uses, NOT a hand-copied subset — factoring this ~140-line, 13-round-reviewed
#: predicate into ONE constant prevents the six-plus topology classes (refs /
#: credential / op-lease / single-video / download_status / upload-binding /
#: terminal-proof / role-kind) from drifting between the two callers. An op is a
#: valid DEFAULT-mode candidate IFF it has at least one non-reusable resource r2
#: satisfying this predicate (branch A = non-deleted video in an admitted state-
#: matrix; branch B = a tail-releasing witness mirroring the FULL claim topology).
#: The attention audit negates it (``NOT EXISTS``) to count claim-eligible
#: resources on ops the deletion subsystem refuses to select (broken topology).
#:
#: Correlates on the OUTER row's ``created_by_operation_id``; BOTH callers alias
#: their outer resource ``r``. Internal aliases (r2/o/ref/ref2/rv/refv/u) are
#: local to the subquery's SQLite scope — they do not collide with the outer query.
_DELETION_WITNESS_SUBQUERY_SQL = (
    " SELECT 1 FROM heygen_remote_resources r2 "
    " JOIN heygen_operations o"
    "   ON o.operation_id = r2.created_by_operation_id "
    " WHERE r2.created_by_operation_id = r.created_by_operation_id "
    " AND r2.retention_mode != 'reusable_avatar' "
    " AND ("
    # (A) SAFE video witness — a NON-DELETED video. The resolver
    # gates the tail behind it (returns only the video this pass)
    # and the video claim re-checks topology / download_status /
    # single-video / op-lease, so only the (status, reason) state
    # matrix + kind are needed here.
    "  (r2.resource_kind = 'video'"
    "   AND ((r2.deletion_status = 'not_started'"
    "         AND r2.deletion_reason IS NULL)"
    "        OR (r2.deletion_status IN ('deletion_pending',"
    "                                  'deletion_failed')"
    "            AND r2.deletion_reason IN ('post_download',"
    "                                      'consent_withdrawal'))))"
    "  OR"
    # (B) TAIL-RELEASING witness — a DELETED video or a NON-VIDEO
    # asset. The resolver skips a deleted video and, finding no
    # non-deleted video, releases the tail to the asset claim,
    # which does NOT re-check op.lease and runs after the witness
    # video is already gone. A DELETED witness therefore escapes
    # ALL downstream re-verification, so EVERY claim invariant the
    # live-video path would have enforced is restated here as a
    # full topology (round-6: topology / op-lease / single-video /
    # download_status / kind / upload-binding — 6 bypass classes,
    # one per un-mirrored invariant).
    "  (o.lease_owner IS NULL AND o.lease_expires_at IS NULL"
    "   AND r2.credential_profile_id = o.credential_profile_id"
    "   AND EXISTS (SELECT 1 FROM heygen_resource_operation_refs ref"
    "    WHERE ref.resource_id = r2.resource_id"
    "    AND ref.operation_id = r2.created_by_operation_id)"
    "   AND NOT EXISTS (SELECT 1 FROM heygen_resource_operation_refs"
    "    ref2 WHERE ref2.resource_id = r2.resource_id"
    "    AND ref2.operation_id <> r2.created_by_operation_id)"
    "   AND ("
    #  (B1) deleted video: count(video)==1 + op.download_status
    #  verified — but ONLY for post_download (consent_withdrawal
    #  cleanup is delivery-independent: legit on unverified ops).
    #  Round-7 P1: also require the apply TERMINAL PROOF — a real
    #  successful video delete (apply_deletion_outcome_in_tx) always
    #  sets deleted_at NOT NULL + deletion_attempts>=1 (the claim
    #  bumps it before apply) + deletion_next_retry_at IS NULL +
    #  last_deletion_error IS NULL. A 直插 'deleted' row with
    #  deleted_at=NULL/attempts=0 is schema-legal but unreachable
    #  via apply; without this gate it falsely releases the tail.
    #  Applies to BOTH reasons (apply sets deleted_at regardless).
    "    (r2.resource_kind = 'video'"
    "     AND r2.deletion_status = 'deleted'"
    "     AND r2.deletion_reason IN ('post_download',"
    "                               'consent_withdrawal')"
    "     AND r2.deleted_at IS NOT NULL"
    "     AND r2.deletion_attempts >= 1"
    "     AND r2.deletion_next_retry_at IS NULL"
    "     AND r2.last_deletion_error IS NULL"
    "     AND (r2.deletion_reason != 'post_download'"
    "          OR o.download_status = 'verified')"
    "     AND (r2.deletion_reason != 'post_download'"
    "          OR 1 = (SELECT COUNT(*) FROM heygen_remote_resources rv"
    "            JOIN heygen_resource_operation_refs refv"
    "              ON refv.resource_id = rv.resource_id"
    "            WHERE refv.operation_id = r2.created_by_operation_id"
    "            AND rv.resource_kind = 'video')))"
    "    OR"
    #  (B2) non-video pending/failed asset: kind restricted to
    #  audio_asset/portrait_asset (the only kinds with real upload
    #  bindings; avatar_look/group route to skipped_unknown_kind
    #  with no claim). Round-6 required the binding to EXIST; round-
    #  7 P1 requires the FULL binding the asset claim enforces —
    #  the asset↔resource matrix (deletion_pending <-> upload
    #  cleanup_required) AND the asset_role↔resource_kind pair the
    #  claim's _validate_asset_binding checks. A deletion_pending
    #  resource with an `uploaded` or role-mismatched upload is a
    #  matrix inconsistency the claim would raise on; without these
    #  gates it witnesses the op and the coordinator's dumb iterator
    #  deletes the sibling while the witness's own claim alerts.
    #  Round-8 P1 (B2 download_status mirror): B2 ALSO requires —
    #  identical to B1's clause directly above — that a post_download
    #  witness only authorize a download-verified op. The asset claim
    #  (claim_asset_deletion_in_tx) does NOT gate on op.download_status
    #  and the resolver only carries it as informational context, so
    #  when B2 authorizes an op with NO live/verified video (a B2-only
    #  op) no layer enforces "delivery was verified before asset
    #  cleanup." A 直插 pending/post_download asset witness on an
    #  unverified op would release the tail and the asset claim would
    #  delete a pre-delivery sibling (empirically confirmed, then
    #  closed). consent_withdrawal stays exempt (delivery-independent).
    "    (r2.resource_kind IN ('audio_asset','portrait_asset')"
    "     AND r2.deletion_status IN ('deletion_pending',"
    "                               'deletion_failed')"
    "     AND r2.deletion_reason IN ('post_download',"
    "                               'consent_withdrawal')"
    "     AND (r2.deletion_reason != 'post_download'"
    "          OR o.download_status = 'verified')"
    #  Round-9 P1 (B2 single-video mirror): B2 ALSO requires —
    #  identical to B1's clause directly above — that a
    #  post_download witness only authorize a SINGLE-VIDEO op.
    #  The video claim's _single_video gate is an OP-LEVEL
    #  invariant (resolver L2328 "at most one video per op"),
    #  enforced ONLY on the live-video path; the asset claim
    #  reads zero video count and the resolver only comments on
    #  it (trusting "the video claim will fail-closed on
    #  doubles" — a trust broken once the videos are already
    #  deleted/skipped). A 直插 double-video op (COUNT>=2) with
    #  both videos marked deleted routes around B1's count
    #  defense through a B2 asset witness: B1 refuses (COUNT==1
    #  fails its gate), B2 authorizes, the resolver skips both
    #  deleted videos -> releases the tail -> the coordinator
    #  sweeps individually-eligible assets on a structurally-
    #  corrupt op that B1's gate exists to freeze for human
    #  reconciliation. Same B1↔B2 asymmetry class as round-8's
    #  download_status (empirically confirmed, then closed).
    #  consent_withdrawal stays exempt (delivery/structure-
    #  independent (matching B1 and the VIDEO claim's consent
    #  exemption). NOTE: B2's mirror is COUNT(video) <= 1 (i.e.
    #  `1 >= COUNT`), NOT B1's exact `1 == COUNT`. The invariant
    #  is the resolver's "AT MOST one video per op" contract
    #  (L2328) — zero is allowed. B1's witness IS a deleted
    #  video, so COUNT>=1 is self-guaranteed and `==1` ⟺ `<=1`.
    #  B2's witness is a non-video ASSET, so the op may
    #  legitimately have 0 video rows (e.g. a post-delivery op
    #  whose video row was already hard-purged, or a consent
    #  cleanup); `==1` would wrongly freeze those legit 0-video
    #  sweeps. `<=1` blocks the corrupt >=2 case while keeping
    #  the legit 0- and 1-video cases (round-8 control).
    "     AND (r2.deletion_reason != 'post_download'"
    "          OR 1 >= (SELECT COUNT(*) FROM heygen_remote_resources rv"
    "            JOIN heygen_resource_operation_refs refv"
    "              ON refv.resource_id = rv.resource_id"
    "            WHERE refv.operation_id = r2.created_by_operation_id"
    "            AND rv.resource_kind = 'video'))"
    "     AND EXISTS (SELECT 1 FROM heygen_asset_uploads u"
    "      WHERE u.remote_resource_id = r2.resource_id"
    "      AND u.parent_operation_id = r2.created_by_operation_id"
    "      AND u.status = 'cleanup_required'"
    "      AND ((r2.resource_kind = 'audio_asset'"
    "            AND u.asset_role = 'synthetic_narration_audio')"
    "           OR (r2.resource_kind = 'portrait_asset'"
    "            AND u.asset_role = 'portrait_photo'))))"
    "   ))"
    " )"
)


class DeletionCoordinator:
    """§3.5 normal-order deletion coordinator (§5.5e5b0c3c-c3). Consumes the
    c2 DeletionPlan × the c1/video processors.

    A DUMB iterator: resolve the frozen, §3.5-ordered plan.entries in ONE
    (immediately-closed) transaction, then route each entry to a processor by
    resource_kind. Per-resource eligibility (verified gate / manual_force /
    retry / matrix / topology) STAYS authoritative inside each processor's
    claim — the coordinator never re-sorts, re-filters, or re-judges (c1
    lesson: reuse locked invariants, build no parallel gate). Each resource
    is driven in its own claim/apply transactions (owned by the processors),
    so a crash mid-pass leaves a clean, resumable state.

    No coordinator-level transaction is open while a processor runs: the plan
    is resolved in a `begin_immediate` that closes before the entry loop, and
    each processor opens its own. A single ``lease_owner`` / ``now_iso`` is
    forwarded to every processor call in the pass (no per-item drift)."""

    def __init__(self, project_dir: str | Path) -> None:
        self._project_dir = Path(project_dir)
        self._repository = OperationRepository(self._project_dir)
        self._video_processor = DeleteProcessor(self._project_dir)
        self._asset_processor = AssetDeletionProcessor(self._project_dir)

    def delete_pass_for_operation(
        self, *, operation_id: str, force: bool = False,
        deleter, adapter, lease_owner: str, now_iso: str,
        lease_seconds: int, max_attempts: int = DELETION_MAX_ATTEMPTS,
    ) -> DeletionPassResult:
        """Drive one §3.5-ordered deletion pass over ``operation_id``. Returns a
        DeletionPassResult whose attempts are the per-entry records (counts
        derive from them). Non-claimed / failed / skipped entries do NOT block
        later entries in the same pass — each is independent."""
        # Entry guards (defense in depth on top of the resolver's own guards).
        # force is this pass's OWN scope/order authorization (like the
        # resolver's), so a truthy non-bool cannot be recovered downstream —
        # reject before any DB read. Never coerce (bool(force)) and never
        # branch on `if force:` truthiness before forwarding.
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("operation_id must be a non-empty string")
        if type(force) is not bool:
            raise ValueError("force must be a bool")
        _require_lease_owner(lease_owner)

        # Resolve the §3.5 plan in its OWN tx, then CLOSE it (resolve is
        # read-only; the connection must not survive across the network DELETEs
        # driven by the processors below — each opens its own begin_immediate).
        with self._repository.begin_immediate() as conn:
            plan = self._repository.resolve_deletion_plan_in_tx(
                conn, operation_id=operation_id, force=force)
        # From here, no coordinator-level transaction is open while processors
        # run. Iterate the frozen entries verbatim — no re-sort / re-filter.
        attempts = tuple(
            self._attempt_entry(
                operation_id=plan.operation_id, entry=entry,
                deleter=deleter, adapter=adapter, lease_owner=lease_owner,
                now_iso=now_iso, lease_seconds=lease_seconds,
                max_attempts=max_attempts, force=force)
            for entry in plan.entries)
        return DeletionPassResult(
            operation_id=plan.operation_id, force=plan.force,
            video_download_status=plan.video_download_status, attempts=attempts)

    def _attempt_entry(self, *, operation_id, entry, deleter, adapter,
                       lease_owner, now_iso, lease_seconds,
                       max_attempts, force) -> DeletionEntryAttempt:
        """Route one frozen plan entry strictly by resource_kind. The kind
        dispatch is the ONLY routing authority here — never guess a route for
        an unknown kind, and never hand an asset without upload_id to the
        asset processor (upload_id is its claim key)."""
        kind = entry.resource_kind
        if kind == "video":
            return self._drive_video(
                operation_id=operation_id, entry=entry, deleter=deleter,
                lease_owner=lease_owner, now_iso=now_iso,
                lease_seconds=lease_seconds, max_attempts=max_attempts)
        if kind in ("audio_asset", "portrait_asset"):
            if entry.upload_id is None:
                # Bare asset resource (orphan row / broken LEFT JOIN): the
                # resolver surfaced it (deletion_status != 'deleted'); never
                # silently drop — the asset processor cannot execute without
                # upload_id (c2 observation #2).
                return DeletionEntryAttempt(
                    entry=entry, routed="skipped_no_upload_id",
                    claim_status=None, outcome_status=None,
                    last_error=None, next_retry_at=None)
            return self._drive_asset(
                entry=entry, adapter=adapter, lease_owner=lease_owner,
                now_iso=now_iso, lease_seconds=lease_seconds,
                max_attempts=max_attempts, force=force)
        # Unexpected ephemeral kind (order_key 9) — surfaced, not dropped.
        return DeletionEntryAttempt(
            entry=entry, routed="skipped_unknown_kind",
            claim_status=None, outcome_status=None,
            last_error=None, next_retry_at=None)

    @staticmethod
    def _attempt_from_video(entry, res) -> DeletionEntryAttempt:
        outcome = res.outcome
        return DeletionEntryAttempt(
            entry=entry, routed="video", claim_status=res.claim.status,
            outcome_status=outcome.status if outcome else None,
            last_error=outcome.last_error if outcome else None,
            next_retry_at=outcome.next_retry_at if outcome else None)

    @staticmethod
    def _attempt_from_asset(entry, res) -> DeletionEntryAttempt:
        outcome = res.outcome
        return DeletionEntryAttempt(
            entry=entry, routed="asset", claim_status=res.claim.status,
            outcome_status=outcome.status if outcome else None,
            last_error=outcome.last_error if outcome else None,
            next_retry_at=outcome.next_retry_at if outcome else None)

    def _drive_video(self, *, operation_id, entry, deleter, lease_owner, now_iso,
                     lease_seconds, max_attempts) -> DeletionEntryAttempt:
        try:
            res = self._video_processor.delete_once(
                operation_id=operation_id, resource_id=entry.resource_id,
                lease_owner=lease_owner, deleter=deleter, now_iso=now_iso,
                lease_seconds=lease_seconds, max_attempts=max_attempts)
        except Exception:
            # Untyped exception (non-DeleteAdapterError): the remote DELETE
            # result is unknowable. The processor's claim may have legitimately
            # written deletion_pending + acquired a lease; this path applies NO
            # outcome — it writes neither deleted nor failed, sets no error /
            # retry, and leaves the held lease to expire so the next pass
            # re-claims. Never fabricate a phantom outcome.
            return DeletionEntryAttempt(
                entry=entry, routed="alerted_exception",
                claim_status=None, outcome_status=None,
                last_error=None, next_retry_at=None)
        return self._attempt_from_video(entry, res)

    def _drive_asset(self, *, entry, adapter, lease_owner, now_iso, lease_seconds,
                     max_attempts, force) -> DeletionEntryAttempt:
        try:
            res = self._asset_processor.delete_once(
                upload_id=entry.upload_id, lease_owner=lease_owner,
                adapter=adapter, now_iso=now_iso, lease_seconds=lease_seconds,
                max_attempts=max_attempts, force=force)
        except Exception:
            # Same certainty-unknowable contract as the asset processor: a
            # non-AssetReadError raise leaves the result unknowable. The
            # processor's claim may have legitimately written deletion_pending +
            # acquired a lease; mirror the video path — apply NO outcome, write
            # neither deleted nor failed, leave the lease to expire.
            return DeletionEntryAttempt(
                entry=entry, routed="alerted_exception",
                claim_status=None, outcome_status=None,
                last_error=None, next_retry_at=None)
        return self._attempt_from_asset(entry, res)

    def recover_deletions(
        self, *, deleter, adapter, lease_owner: str, now_iso: str,
        lease_seconds: int, force: bool = False,
        max_attempts: int = DELETION_MAX_ATTEMPTS,
    ) -> dict[str, int]:
        """Maintenance deletion sweep (§5.5e5b0c3c-c3): list candidate
        operations, then drive ``delete_pass_for_operation`` for each. This is
        the network-bound counterpart to ``recover_withdrawn_asset_cleanups``
        (which only reconciles journal state); unlike that DB-only method, the
        candidate-listing transaction MUST be closed before any pass runs.

        ``force`` defaults to False — the sweep respects the §3.5 video-verified
        gate op by op. A force sweep (operator-only, audited) applies force to
        every candidate; that is §3.5 force-cleanup in sweep form and is never
        the default. Idempotent: a re-run only re-attempts ops with surviving
        non-deleted resources.

        Candidate gating differs by mode (Codex round-1 blocker): the DEFAULT
        sweep only visits DELETION-AUTHORIZED ops — those that have a video
        resource (generation produced a deliverable; the video claim then gates
        on download_status=verified, and the resolver holds assets behind the
        video) OR a resource already in the deletion pipeline
        (deletion_pending/deletion_failed — consent withdrawal / retry). This
        excludes in-flight ops whose only resources are pre-video assets at
        not_started: the resolver would release such assets (no video to gate
        them behind), deleting assets still in production use. ``force=True``
        (explicit operator authorization) broadens the candidate set to every
        non-deleted non-reusable resource.

        The authorization witness carries the SAME retention gate as the outer
        candidate row (Codex round-2 blocker): a reusable_avatar resource is
        resolver-filtered for every kind, so it can never authorize deleting
        sibling ephemeral assets — neither as a "video present" witness (a
        reusable video is skipped, leaving the tail ungated) nor as a
        "deletion pipeline" witness. Only a NON-reusable video (any status — a
        deleted ephemeral video still legitimates tail cleanup) or a
        NON-reusable pending/failed resource counts as authorization.

        The "deletion pipeline" witness is further restricted to AUTO-RECOVERABLE
        reasons (Codex round-3 blocker): manual_force is the operator-only
        integrity path (c1 claims it not_ready, never auto-deleted), so it must
        not authorize sweeping a sibling either — only post_download /
        consent_withdrawal do (the two reasons a pending/failed resource is
        claim-eligible).

        Finally, the witness is gated on the full (status, reason) STATE MATRIX
        (Codex round-4 + round-5 blockers), not on reason alone. manual_force is
        excluded from EVERY branch (round-4): the subsystem fails closed against
        schema-legal anomalous states (topology/matrix/retention all do); a
        deleted/manual_force video is schema-legal even though the current
        producer never makes one, and as a witness it would release the tail
        (resolver skips a deleted video). And the NULL-reason branch is
        restricted to not_started (round-5, Option B): a deleted/NULL-reason
        video is the same schema-legal anomalous state (corrupt/直插 — the legit
        flow never produces one: video apply inherits the claim's non-NULL
        reason), and as a witness it would release the tail just like a
        deleted/manual_force video. The admitted witness states are exactly:
        ``not_started+NULL`` (in-flight, never claimed → no reason) OR
        ``(pending/failed/deleted) + (post_download/consent_withdrawal)``. This
        also fail-closes pending/failed+NULL and not_started+reason (both
        anomalous). A not_started video (NULL reason) remains a legit witness —
        the resolver gates the tail behind a non-deleted video and the video
        claim gates on download_status."""
        if type(force) is not bool:
            raise ValueError("force must be a bool")
        _require_lease_owner(lease_owner)

        # Candidate SELECT in its own tx, then CLOSE it. The per-op
        # delete_pass_for_operation is network-bound; holding this tx across it
        # would nest begin_immediate (SQLite allows one writer) and deadlock.
        with self._repository.begin_immediate() as conn:
            if force:
                rows = conn.execute(
                    "SELECT DISTINCT r.created_by_operation_id AS op_id "
                    "FROM heygen_remote_resources r "
                    "WHERE r.deletion_status != 'deleted' "
                    "AND r.retention_mode != 'reusable_avatar' "
                    "AND r.created_by_operation_id IS NOT NULL "
                    "ORDER BY r.created_by_operation_id").fetchall()
            else:
                # Default sweep: deletion-AUTHORIZED ops only (see docstring).
                # The EXISTS witness r2 must carry the SAME retention gate as
                # the outer row (Codex round-2 P1): a reusable_avatar resource
                # is filtered by the resolver for every kind, so it can never
                # authorize deleting sibling ephemeral assets — neither as a
                # "video present" witness (a reusable video is skipped, leaving
                # the tail ungated) nor as a "deletion pipeline" witness (a
                # reusable pending/failed resource says nothing about siblings).
                # The "deletion pipeline" witness is further restricted to
                # AUTO-RECOVERABLE reasons (Codex round-3 P1): manual_force is
                # the operator-only integrity path — c1 claims it not_ready and
                # it must NEVER be auto-deleted, so it cannot authorize sweep-
                # ing a sibling either (else the manual_force asset wedges the
                # op into the candidate set and the resolver-released tail gets
                # deleted by the asset claim, which does not re-gate on
                # download_status). post_download / consent_withdrawal are the
                # only reasons a pending/failed resource is claim-eligible.
                # COMMON reason gate (Codex round-4 P1): manual_force must be
                # excluded in EVERY branch, including the video branch. The
                # whole deletion subsystem fails closed against schema-legal
                # anomalous states (topology/matrix/retention all do); a
                # deleted/manual_force video is schema-legal even though the
                # current producer never makes one, and as a witness it would
                # release the tail (resolver skips a deleted video) → sibling
                # deleted.
                # STATE-MATRIX gate (Codex round-5 P1, Option B): the round-4
                # reason-only gate (reason IS NULL OR reason IN auto-recoverable)
                # was still too permissive — it admitted a deleted/NULL-reason
                # video as a witness (NULL branch), which the resolver skips just
                # like a deleted/manual_force video → same tail release. The only
                # legit NULL-reason witness is a not_started video (in-flight,
                # never claimed → no reason); a deleted video must carry a non-
                # NULL auto-recoverable reason (video apply inherits the claim's
                # reason; claim from not_started always sets post_download). So
                # the witness is gated on the full (status, reason) STATE MATRIX,
                # not on reason alone: not_started+NULL OR (pending/failed/deleted
                # + auto-recoverable reason). This also fail-closes pending/failed
                # +NULL and not_started+reason (both anomalous). Same threat model
                # as round-4: schema-legal corrupt/直插 states, not just
                # producer-reachable ones.
                # FULL-TOPOLOGY mirror (Codex round-6 P1): the state matrix still
                # admitted a DELETED witness whose claim invariants were never
                # re-checked anywhere — a deleted video is skipped by the resolver
                # (never re-claimed), so it escapes topology / op-lease / single-
                # video / download_status re-verification, and the released tail
                # is deleted by the asset claim (which does NOT re-gate on op.lease).
                # An empirical + workflow enumeration found SIX bypass classes, one
                # per un-mirrored claim invariant: (a) topology (missing/foreign
                # ref, credential mismatch), (b) asset upload-binding (a bare 直插
                #  resource with no heygen_asset_uploads row), (c) download_status
                # (deleted/post_download video on an unverified op), (d) op-lease
                # (active/half op lease — the video claim's mutual-exclusion gate),
                # (e) resource_kind (avatar_look/avatar_group have no processor, so
                # a corrupt row is never re-verified), (f) single-video count
                # (COUNT(video)==1 — the claim/apply gate a double-video op refuses).
                # Round-7 P1 closed two more un-mirrored invariants: (g) the apply
                # TERMINAL PROOF for a deleted video (deleted_at NOT NULL +
                # deletion_attempts>=1 + next_retry/error NULL — a state only
                # apply_deletion_outcome_in_tx can produce), and (h) the FULL asset
                # binding for a non-video witness (the asset↔resource matrix
                # deletion_pending<->cleanup_required AND the asset_role↔kind pair
                # the claim's _validate_asset_binding checks — a bare "upload
                # exists" still witnessed an inconsistent/mismatched pair whose own
                # claim would raise). The witness is split into two branches: (A) a
                # NON-DELETED video is a SAFE witness (the resolver gates the tail
                # behind it and the video claim re-checks everything); (B) a DELETED
                # video or a NON-VIDEO asset is a TAIL-RELEASING witness and must
                # mirror the FULL claim topology — op clean-idle, credential match,
                # exactly-one own ref, and (B1) for a deleted video terminal-proof +
                # count==1+verified (post_download only; consent cleanup is delivery-
                # independent) or (B2) for a non-video asset a real, matrix-
                # consistent, role-kind-paired upload binding on an audio/portrait,
                # ALSO gated on op.download_status=verified for post_download.
                # Round-8 P1 (independent workflow audit) closed an asymmetry between
                # the two tail-releasing branches that round-7 missed: (i) B2 must
                # mirror op.download_status for post_download EXACTLY as B1 does.
                # The asset claim and the resolver do NOT enforce download_status, so
                # a B2-only op (no live/verified video) had no layer preventing pre-
                # delivery asset cleanup — a 直插 pending/post_download asset witness
                # on an unverified op released the tail and deleted a sibling while
                # the witness's own (download_status-blind) asset claim executed. The
                # fix mirrors B1's clause into B2; consent_withdrawal stays exempt.
                # (Round-7's "LAST two" framing was, again, over-optimistic — the
                # reliable boundary is per-field line-by-line enumeration, not a
                # count claimed after each round.)
                rows = conn.execute(
                    "SELECT DISTINCT r.created_by_operation_id AS op_id "
                    "FROM heygen_remote_resources r "
                    "WHERE r.deletion_status != 'deleted' "
                    "AND r.retention_mode != 'reusable_avatar' "
                    "AND r.created_by_operation_id IS NOT NULL "
                    # Codex round-6 e5d-c: the witness predicate is SHARED with
                    # count_recovery_attention's unrecoverable audit via the
                    # module-level _DELETION_WITNESS_SUBQUERY_SQL constant — the
                    # attention audit mirrors the EXACT candidate topology (no
                    # drift across the 6+ topology classes). See the constant's
                    # docstring above for the full branch A/B + round-6..13 notes.
                    "AND EXISTS (" + _DELETION_WITNESS_SUBQUERY_SQL + ") "
                    "ORDER BY r.created_by_operation_id").fetchall()
        op_ids = [r["op_id"] for r in rows]

        aggregate = {"ops_driven": 0, "ops_empty": 0, "ops_alerted": 0,
                     "attempted": 0, "deleted": 0, "failed": 0,
                     "skipped": 0, "alerted": 0}
        for op_id in op_ids:
            # Each op is independent (its own processor txs). An unexpected
            # raise for ONE op records an alert and continues — earlier ops'
            # writes are already committed, later ops are still driven.
            try:
                result = self.delete_pass_for_operation(
                    operation_id=op_id, force=force, deleter=deleter,
                    adapter=adapter, lease_owner=lease_owner, now_iso=now_iso,
                    lease_seconds=lease_seconds, max_attempts=max_attempts)
            except Exception:
                aggregate["ops_alerted"] += 1
                continue
            if not result.attempts:
                aggregate["ops_empty"] += 1
                continue
            aggregate["ops_driven"] += 1
            aggregate["attempted"] += result.attempted
            aggregate["deleted"] += result.deleted
            aggregate["failed"] += result.failed
            aggregate["skipped"] += result.skipped
            aggregate["alerted"] += result.alerted
        return aggregate


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
