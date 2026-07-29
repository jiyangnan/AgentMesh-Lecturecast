"""HeyGen video adapter — Protocol + domain types (§5.5e3b).

The adapter is the ONLY boundary that talks to HeyGen. It is injected into the
submit/poll/reconcile processors so they can be tested with a stub and so the
real implementation (e5) can be swapped without touching the state machine.

Hard rules (per Codex e3 plan):
- submit_video represents ONLY the final /v3/videos request. It must NOT hide a
  photo/audio asset upload inside one unauditable call — assets are a separate,
  recoverable operation.
- A successful submit MUST return a non-empty remote_id; a missing remote_id is
  treated by the processor as ambiguous (reconciliation_required).
- Errors are structured (HeyGenAdapterError), never a raw exception string. The
  processor decides the next state from `submission_certainty`:
    not_sent   → we know HeyGen never received it (retry or fail permanently)
    maybe_sent → it may have reached HeyGen (timeout / lost response) → reconcile
- The adapter never persists anything to the journal and never logs the API key.
- video_url is a transient download locator only; it is never written to the
  journal (re-fetch via poll on recovery).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Protocol


def _require_tz_iso(value: str) -> None:
    try:
        dt = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"not ISO-8601: {value!r}") from exc
    if dt.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware: {value!r}")


# Closed, stable adapter error codes. The journal stores these in
# last_error_code; never a free-form provider string.
ADAPTER_ERROR_CODES = frozenset({
    "auth_failed",
    "rate_limited",
    "validation_error",
    "network_timeout",
    "connection_error",
    "provider_server_error",
    "malformed_response",
    "unknown",
})

# How certain we are that a failed submit reached HeyGen.
SUBMISSION_CERTAINTY = frozenset({"not_sent", "maybe_sent"})


@dataclass(frozen=True)
class SubmitVideoCommand:
    """Everything the adapter needs to issue the final /v3/videos request. The
    request_descriptor is the canonical object whose digest the consent guard
    bound; the adapter must construct the outgoing HTTP body uniquely from it."""

    request_descriptor: Mapping[str, object]
    heygen_title: str            # lecturecast:<operation_id> — deterministic
    idempotency_key: str         # lc-hg-<full identity digest>


@dataclass(frozen=True)
class SubmitAccepted:
    """A submit the provider acknowledged. remote_id is REQUIRED — a missing/
    empty remote_id is ambiguous and the processor routes it to reconciliation."""

    remote_id: str
    provider_status: str = ""    # transient, informational only

    def __post_init__(self) -> None:
        if not (self.remote_id or "").strip():
            raise ValueError("SubmitAccepted.remote_id is required")
        ps = self.provider_status or ""
        if ps and ps not in PROVIDER_STATUS:
            raise ValueError(f"unknown provider_status: {ps!r}")


class HeyGenAdapterError(Exception):
    """A structured adapter failure. `code` is a closed stable code; `provider_code`
    is an optional sanitized provider-side code (no secrets, truncated)."""

    def __init__(self, *, code: str, retryable: bool, submission_certainty: str,
                 provider_code: str | None = None, message: str = "") -> None:
        if code not in ADAPTER_ERROR_CODES:
            raise ValueError(f"unknown adapter error code: {code!r}")
        if type(retryable) is not bool:
            raise TypeError("retryable must be a bool")
        if submission_certainty not in SUBMISSION_CERTAINTY:
            raise ValueError(f"unknown submission_certainty: {submission_certainty!r}")
        self.code = code
        self.retryable = retryable
        self.submission_certainty = submission_certainty
        self.provider_code = provider_code
        super().__init__(message or code)


# --- the protocol (e5 implements; tests inject a stub) -----------------


class HeyGenVideoAdapter(Protocol):
    def submit_video(self, command: SubmitVideoCommand) -> SubmitAccepted:
        """Issue the final /v3/videos request. Return SubmitAccepted with a
        non-empty remote_id on success; raise HeyGenAdapterError on failure."""
        ...

    def poll_video(self, remote_id: str) -> "PollResult":
        """Fetch the current status of one video by its provider remote id."""
        ...

    def query_videos_by_title(self, query: "TitleQuery") -> "TitleQueryResult":
        """Search HeyGen for videos matching a title (crash recovery)."""
        ...


# Provider-side status vocabulary the adapter normalizes to.
PROVIDER_STATUS = frozenset({
    "queued", "submitted", "processing", "completed", "failed", "not_found",
})


@dataclass(frozen=True)
class PollResult:
    """One poll of a known remote_id. `provider_status` is normalized to the
    closed PROVIDER_STATUS vocabulary; `video_url` is a transient download
    locator (never persisted) and is REQUIRED when provider_status is
    'completed' (a completion without a URL is meaningless)."""

    provider_status: str
    video_url: str | None = None
    provider_code: str | None = None

    def __post_init__(self) -> None:
        if self.provider_status not in PROVIDER_STATUS:
            raise ValueError(f"unknown provider_status: {self.provider_status!r}")
        if self.provider_status == "completed" and not (self.video_url or "").strip():
            raise ValueError("a completed poll must carry a video_url")


class PollAdapterError(Exception):
    """A structured failure of a GET poll (distinct from a submit, so it carries
    no submission_certainty). retryable transient errors keep the operation's
    status and back off; non-retryable ones route to reconciliation."""

    def __init__(self, *, code: str, retryable: bool,
                 provider_code: str | None = None, message: str = "") -> None:
        if code not in ADAPTER_ERROR_CODES:
            raise ValueError(f"unknown adapter error code: {code!r}")
        if type(retryable) is not bool:
            raise TypeError("retryable must be a bool")
        self.code = code
        self.retryable = retryable
        self.provider_code = provider_code
        super().__init__(message or code)


@dataclass(frozen=True)
class TitleQuery:
    """A crash-recovery title search. The coordinator applies exact-title,
    time-window, and multiplicity judgment to the candidates."""

    heygen_title: str
    created_after: str          # ISO-8601 lower bound (inclusive)
    created_before: str         # ISO-8601 upper bound (inclusive)


@dataclass(frozen=True)
class TitleCandidate:
    remote_id: str
    title: str
    created_at: str
    provider_status: str

    def __post_init__(self) -> None:
        if not (self.remote_id or "").strip():
            raise ValueError("TitleCandidate.remote_id is required")
        if not (self.title or "").strip():
            raise ValueError("TitleCandidate.title is required")
        if self.provider_status not in PROVIDER_STATUS:
            raise ValueError(f"unknown provider_status: {self.provider_status!r}")
        _require_tz_iso(self.created_at)


@dataclass(frozen=True)
class TitleQueryResult:
    """query_complete must be a real bool; candidates is a frozen tuple with
    unique remote_ids. query_complete=False means the search itself was
    inconclusive (paging incomplete, provider error) — the coordinator never
    treats an incomplete query as a definitive no-match."""

    query_complete: bool
    candidates: tuple[TitleCandidate, ...] = ()

    def __post_init__(self) -> None:
        if type(self.query_complete) is not bool:
            raise TypeError("query_complete must be a bool")
        object.__setattr__(self, "candidates", tuple(self.candidates))
        rids = [c.remote_id for c in self.candidates]
        if len(set(rids)) != len(rids):
            raise ValueError("duplicate remote_id in candidates")


class TitleQueryAdapterError(Exception):
    """A structured failure of a title search. retryable → reconciliation
    backoff; a permanent error keeps reconciliation_required with a fixed code
    (never guesses a no-match)."""

    def __init__(self, *, code: str, retryable: bool,
                 provider_code: str | None = None, message: str = "") -> None:
        if code not in ADAPTER_ERROR_CODES:
            raise ValueError(f"unknown adapter error code: {code!r}")
        if type(retryable) is not bool:
            raise TypeError("retryable must be a bool")
        self.code = code
        self.retryable = retryable
        self.provider_code = provider_code
        super().__init__(message or code)


# --- submit outcome (what the processor records) -----------------------


@dataclass(frozen=True)
class SubmitOutcome:
    """The journal status the processor reached after a submit attempt."""

    status: str                 # submitted | submit_pending | failed | reconciliation_required
    fence: int
    remote_resource_id: int | None
    last_error_code: str | None
    next_retry_at: str | None


# --- download + media probe (e4a2) -------------------------------------


class MediaProbe(Protocol):
    """Probe a downloaded file for media validity. Production uses subprocess
    ffprobe; tests inject a fake. Returns a structured result the download
    processor validates (≥1 video stream, finite positive duration, positive
    dimensions, non-empty codec)."""

    def probe(self, path: str) -> "MediaProbeResult":
        ...


class VideoDownloader(Protocol):
    """Download a video URL to a safe runtime-local temp file with streaming
    SHA-256, size enforcement, and media validation. Returns a PreparedDownload
    for atomic publication. Production uses a stdlib HTTPS implementation; tests
    inject a fake. The URL is NEVER persisted or logged."""

    def download_and_verify(self, url: str, runtime_dir: str,
                            local_output_ref: str, max_bytes: int,
                            probe: MediaProbe) -> "PreparedDownload":
        ...


# --- deletion (e4b) ----------------------------------------------------


@dataclass(frozen=True)
class DeleteResult:
    """Result of deleting a remote video. The adapter normalizes a provider 404
    to already_absent (idempotent success); the processor never parses HTTP."""
    status: str  # "deleted" | "already_absent"


class DeleteAdapterError(Exception):
    """A structured deletion failure (same closed code vocabulary as the rest)."""
    def __init__(self, *, code: str, retryable: bool,
                 provider_code: str | None = None, message: str = "") -> None:
        if code not in ADAPTER_ERROR_CODES:
            raise ValueError(f"unknown adapter error code: {code!r}")
        if type(retryable) is not bool:
            raise TypeError("retryable must be a bool")
        self.code = code
        self.retryable = retryable
        self.provider_code = provider_code
        super().__init__(message or code)


class DeleteVideoAdapter(Protocol):
    def delete_video(self, remote_id: str) -> DeleteResult:
        """Delete one remote video. Return DeleteResult on success; raise
        DeleteAdapterError on failure. A provider 404 MUST be normalized to
        DeleteResult(status='already_absent')."""
        ...
