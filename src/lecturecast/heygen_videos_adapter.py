"""HeyGen Videos v3 adapter — submit/poll/list/delete (§5.5e5b1, round-2).

Implements HeyGenVideoAdapter + DeleteVideoAdapter Protocols using the shared
HeyGenHttpTransport. Hardened per Codex round-2 review:

- submit body uses a CLOSED descriptor (exact allowed-key set; output_format
  locked to mp4; aspect_ratio vocab-checked) — no arbitrary passthrough.
- every remote id (in commands AND in responses) is validated against the
  safe-id regex before it is trusted or injected into a path.
- poll/delete bind the response resource: the returned id must equal the
  requested remote_id, else malformed_response.
- title reconciliation is fail-closed: unknown status / non-string fields /
  duplicate ids / unknown timestamps raise TitleQueryAdapterError instead of
  being silently skipped (a skip could fabricate a false no-match).
- paging detects any token cycle (A→B→A), not only a consecutive repeat.
- error mapping is per-interface: query failures raise TitleQueryAdapterError,
  not PollAdapterError; auth_failed is never maybe-sent/retryable.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Mapping

from lecturecast.heygen_http import (
    HeyGenHttpTransport, HttpResponse, HttpErrorResponse, HttpTransportError,
)
from lecturecast.heygen_adapter import (
    HeyGenAdapterError, SubmitAccepted, SubmitVideoCommand,
    PollResult, PollAdapterError,
    TitleQuery, TitleCandidate, TitleQueryResult, TitleQueryAdapterError,
    DeleteResult, DeleteAdapterError,
    PROVIDER_STATUS,
)

# Stable provider id vocabulary: chars HeyGen uses for video/asset ids.
# Applied to every remote id — inbound in responses and outbound in commands —
# before it is trusted or placed in a URL path.
_SAFE_REMOTE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")

_MAX_LIST_PAGES = 10
_MAX_CANDIDATES = 500
_LIST_LIMIT = 100

# Closed submit descriptor. Anything outside this set is rejected, so a forged
# command cannot smuggle provider-only fields (callback, script, voice_id, ...).
_ALLOWED_DESCRIPTOR_KEYS = frozenset({
    "schema_version", "type",
    "image_asset_id", "audio_asset_id",
    "aspect_ratio", "output_format",
})
_ALLOWED_ASPECT_RATIOS = frozenset({"16:9", "9:16", "1:1"})

# Raw provider submit-status → normalized vocabulary. An unknown raw status is
# treated as a malformed response (fail-closed), never silently accepted.
_SUBMIT_STATUS_MAP = {
    "pending": "queued", "waiting": "queued", "queued": "queued",
    "submitted": "submitted", "processing": "processing",
    "completed": "completed", "failed": "failed",
}

# Raw provider poll/list status → normalized vocabulary.
_POLL_STATUS_MAP = {
    "pending": "queued", "waiting": "queued", "queued": "queued",
    "submitted": "submitted", "processing": "processing",
    "completed": "completed", "failed": "failed", "not_found": "not_found",
}

# Transport-level codes that mean "we never reached HeyGen" → not_sent.
_NOT_SENT_TRANSPORT_CODES = frozenset({"auth_failed", "validation_error"})


def _validate_remote_id(remote_id: str) -> str:
    if not _SAFE_REMOTE_ID_RE.fullmatch(remote_id or ""):
        raise ValueError(f"invalid remote_id: {remote_id!r}")
    return remote_id


class HeyGenVideosAdapter:
    """HeyGen Videos v3 API adapter. Inject the transport for testing."""

    def __init__(self, transport: HeyGenHttpTransport) -> None:
        self._transport = transport

    # -- submit -----------------------------------------------------------

    def submit_video(self, command: SubmitVideoCommand) -> SubmitAccepted:
        descriptor = command.request_descriptor
        if not isinstance(descriptor, Mapping):
            raise ValueError("request_descriptor must be a mapping")
        _validate_descriptor(descriptor)
        body = _build_submit_body(descriptor, command.heygen_title)
        try:
            resp = self._transport.request_json(
                method="POST", path="/v3/videos",
                json_body=body, idempotency_key=command.idempotency_key,
            )
        except HttpErrorResponse as exc:
            raise _map_submit_http_error(exc) from None
        except HttpTransportError as exc:
            raise _map_submit_transport_error(exc) from None

        data = resp.body.get("data")
        if not isinstance(data, dict):
            raise HeyGenAdapterError(
                code="malformed_response", retryable=False,
                submission_certainty="maybe_sent",
                message="submit response data is not an object")
        video_id = data.get("video_id")
        if not isinstance(video_id, str) or not video_id.strip():
            raise HeyGenAdapterError(
                code="malformed_response", retryable=False,
                submission_certainty="maybe_sent",
                message="submit succeeded but video_id is missing")
        try:
            _validate_remote_id(video_id.strip())
        except ValueError:
            raise HeyGenAdapterError(
                code="malformed_response", retryable=False,
                submission_certainty="maybe_sent",
                message="submit returned an invalid video_id") from None
        raw_status = data.get("status")
        # Status is informational; absent is acceptable (""), unknown is not.
        if raw_status is None:
            mapped = ""
        elif isinstance(raw_status, str):
            mapped = _SUBMIT_STATUS_MAP.get(raw_status.strip().lower())
            if mapped is None:
                raise HeyGenAdapterError(
                    code="malformed_response", retryable=False,
                    submission_certainty="maybe_sent",
                    message=f"submit returned unknown status: {raw_status!r}")
        else:
            raise HeyGenAdapterError(
                code="malformed_response", retryable=False,
                submission_certainty="maybe_sent",
                message="submit returned non-string status")
        return SubmitAccepted(remote_id=video_id.strip(), provider_status=mapped)

    # -- poll -------------------------------------------------------------

    def poll_video(self, remote_id: str) -> PollResult:
        _validate_remote_id(remote_id)
        try:
            resp = self._transport.request_json(
                method="GET", path=f"/v3/videos/{remote_id}",
            )
        except HttpErrorResponse as exc:
            if exc.status == 404:
                return PollResult(provider_status="not_found")
            raise _map_poll_http_error(exc) from None
        except HttpTransportError as exc:
            raise _map_get_transport_error(exc, PollAdapterError) from None
        data = resp.body.get("data")
        if not isinstance(data, dict):
            raise PollAdapterError(
                code="malformed_response", retryable=False,
                message="poll response data is not an object")
        # Bind the response to the resource we asked about.
        returned_id = data.get("id")
        if not isinstance(returned_id, str) or returned_id != remote_id:
            raise PollAdapterError(
                code="malformed_response", retryable=False,
                message="poll response id does not match requested remote_id")
        raw_status = data.get("status")
        if not isinstance(raw_status, str):
            raise PollAdapterError(
                code="malformed_response", retryable=False,
                message="poll returned non-string status")
        mapped = _POLL_STATUS_MAP.get(raw_status.strip().lower())
        if mapped is None:
            raise PollAdapterError(
                code="malformed_response", retryable=False,
                message=f"poll returned unknown status: {raw_status!r}")
        video_url = data.get("video_url")
        if mapped == "completed":
            if not isinstance(video_url, str) or not video_url.strip():
                raise PollAdapterError(
                    code="malformed_response", retryable=False,
                    message="completed status but no video_url")
            return PollResult(provider_status="completed",
                              video_url=video_url.strip())
        return PollResult(provider_status=mapped)

    # -- query by title ---------------------------------------------------

    def query_videos_by_title(self, query: TitleQuery) -> TitleQueryResult:
        candidates: list[TitleCandidate] = []
        seen_ids: set[str] = set()
        token: str | None = None
        seen_tokens: set[str] = set()   # cycle detection beyond consecutive repeat
        query_complete = True

        for _ in range(_MAX_LIST_PAGES):
            params = {"title": query.heygen_title, "limit": str(_LIST_LIMIT)}
            if token:
                params["token"] = token
            try:
                resp = self._transport.request_json(
                    method="GET", path="/v3/videos", params=params,
                )
            except HttpErrorResponse as exc:
                raise _map_title_query_http_error(exc) from None
            except HttpTransportError as exc:
                raise _map_get_transport_error(exc, TitleQueryAdapterError) from None

            data = resp.body.get("data")
            if not isinstance(data, list):
                raise TitleQueryAdapterError(
                    code="malformed_response", retryable=False,
                    message="list response data is not a list")
            has_more = resp.body.get("has_more")
            if type(has_more) is not bool:
                raise TitleQueryAdapterError(
                    code="malformed_response", retryable=False,
                    message="has_more is not a bool")
            next_token = resp.body.get("next_token")

            for item in data:
                candidate = _parse_candidate(item)  # raises TitleQueryAdapterError
                if candidate.remote_id in seen_ids:
                    raise TitleQueryAdapterError(
                        code="malformed_response", retryable=False,
                        message=f"duplicate remote_id in listing: "
                                f"{candidate.remote_id}")
                seen_ids.add(candidate.remote_id)
                candidates.append(candidate)
                if len(candidates) >= _MAX_CANDIDATES:
                    query_complete = False
                    break

            if len(candidates) >= _MAX_CANDIDATES:
                query_complete = False
                break
            if not has_more:
                break
            # next_token must be a non-empty string; absent/stray type is malformed.
            if not isinstance(next_token, str) or not next_token.strip():
                query_complete = False
                break
            if next_token in seen_tokens:   # cycle (incl. A→B→A)
                query_complete = False
                break
            seen_tokens.add(next_token)
            token = next_token
        else:
            # Ran out of pages without a clean stop → search was inconclusive.
            query_complete = False

        return TitleQueryResult(query_complete=query_complete,
                                candidates=tuple(candidates))

    # -- delete -----------------------------------------------------------

    def delete_video(self, remote_id: str) -> DeleteResult:
        _validate_remote_id(remote_id)
        try:
            resp = self._transport.request_json(
                method="DELETE", path=f"/v3/videos/{remote_id}",
            )
        except HttpErrorResponse as exc:
            if exc.status == 404:
                return DeleteResult(status="already_absent")
            raise _map_delete_http_error(exc) from None
        except HttpTransportError as exc:
            raise _map_get_transport_error(exc, DeleteAdapterError) from None
        data = resp.body.get("data")
        if not isinstance(data, dict):
            raise DeleteAdapterError(
                code="malformed_response", retryable=False,
                message="delete response data is not an object")
        # If the provider echoes an id, it must be the one we asked to delete.
        returned_id = data.get("id")
        if isinstance(returned_id, str) and returned_id != remote_id:
            raise DeleteAdapterError(
                code="malformed_response", retryable=False,
                message="delete response id does not match requested remote_id")
        if data.get("deleted") is not True:
            raise DeleteAdapterError(
                code="malformed_response", retryable=False,
                message="delete succeeded but deleted is not True")
        return DeleteResult(status="deleted")


# --- descriptor + body ------------------------------------------------------

def _validate_descriptor(descriptor: Mapping) -> None:
    extra = set(descriptor.keys()) - _ALLOWED_DESCRIPTOR_KEYS
    if extra:
        raise ValueError(f"unknown keys in request_descriptor: {sorted(extra)}")
    if descriptor.get("schema_version") != "heygen.video-submit.v1":
        raise ValueError("descriptor schema_version must be heygen.video-submit.v1")
    if descriptor.get("type") != "image":
        raise ValueError("descriptor type must be 'image'")
    for key in ("image_asset_id", "audio_asset_id"):
        val = descriptor.get(key)
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"descriptor {key} must be a non-empty string")
        if not _SAFE_REMOTE_ID_RE.fullmatch(val):
            raise ValueError(f"descriptor {key} is not a valid remote ID")
    aspect = descriptor.get("aspect_ratio", "16:9")
    if aspect not in _ALLOWED_ASPECT_RATIOS:
        raise ValueError(f"descriptor aspect_ratio not allowed: {aspect!r}")
    if descriptor.get("output_format", "mp4") != "mp4":
        raise ValueError("descriptor output_format must be 'mp4'")


def _build_submit_body(descriptor: Mapping, title: str) -> dict:
    return {
        "type": "image",
        "title": title,
        "image_asset_id": descriptor["image_asset_id"],
        "audio_asset_id": descriptor["audio_asset_id"],
        "aspect_ratio": descriptor.get("aspect_ratio", "16:9"),
        "output_format": "mp4",
    }


# --- candidate parsing ------------------------------------------------------

def _parse_candidate(item: object) -> TitleCandidate:
    """Parse one listing item fail-closed. Every malformed field raises
    TitleQueryAdapterError(malformed_response) rather than being skipped."""
    if not isinstance(item, dict):
        raise TitleQueryAdapterError(
            code="malformed_response", retryable=False,
            message="list item is not an object")
    rid = item.get("id")
    if not isinstance(rid, str) or not rid.strip():
        raise TitleQueryAdapterError(
            code="malformed_response", retryable=False,
            message="list item id is missing")
    try:
        _validate_remote_id(rid)
    except ValueError:
        raise TitleQueryAdapterError(
            code="malformed_response", retryable=False,
            message=f"list item has invalid id: {rid!r}") from None
    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        raise TitleQueryAdapterError(
            code="malformed_response", retryable=False,
            message="list item title is missing")
    raw_status = item.get("status")
    if not isinstance(raw_status, str):
        raise TitleQueryAdapterError(
            code="malformed_response", retryable=False,
            message="list item status is not a string")
    mapped = _POLL_STATUS_MAP.get(raw_status.strip().lower())
    if mapped is None:
        raise TitleQueryAdapterError(
            code="malformed_response", retryable=False,
            message=f"list item has unknown status: {raw_status!r}")
    created_at = _to_iso(item.get("created_at"))  # raises on bad timestamp
    return TitleCandidate(
        remote_id=rid, title=title,
        created_at=created_at, provider_status=mapped,
    )


def _to_iso(raw: object) -> str:
    if isinstance(raw, bool) or raw is None:
        raise TitleQueryAdapterError(
            code="malformed_response", retryable=False,
            message=f"invalid created_at: {raw!r}")
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=timezone.utc).isoformat()
    if isinstance(raw, str) and raw.strip():
        candidate = raw.strip()
        # Validate strictly here so a bad timestamp surfaces as a structured
        # TitleQueryAdapterError, not a raw ValueError from TitleCandidate.
        try:
            dt = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            raise TitleQueryAdapterError(
                code="malformed_response", retryable=False,
                message=f"invalid created_at: {raw!r}") from None
        if dt.tzinfo is None:
            raise TitleQueryAdapterError(
                code="malformed_response", retryable=False,
                message=f"created_at lacks timezone: {raw!r}")
        return candidate
    raise TitleQueryAdapterError(
        code="malformed_response", retryable=False,
        message=f"invalid created_at: {raw!r}")


# --- error mapping ----------------------------------------------------------
#
# malformed_response is always non-retryable: a garbled body is not something a
# blind retry fixes, and for a submit it risks duplication (route to reconcile).
# auth_failed is always not_sent + non-retryable, including when it surfaces as
# a transport-level code.

def _provider_code(exc: HttpErrorResponse) -> str:
    return exc.provider_code or "unknown"


def _map_submit_http_error(exc: HttpErrorResponse) -> HeyGenAdapterError:
    code = _provider_code(exc)
    if exc.status == 429:
        return HeyGenAdapterError(code="rate_limited", retryable=True,
            submission_certainty="not_sent", provider_code=code,
            message=f"HTTP 429 ({code})")
    if exc.status == 409:
        if code == "request_in_progress":
            return HeyGenAdapterError(code="unknown", retryable=True,
                submission_certainty="maybe_sent", provider_code=code,
                message="HTTP 409 request_in_progress")
        return HeyGenAdapterError(code="unknown", retryable=False,
            submission_certainty="maybe_sent", provider_code=code,
            message=f"HTTP 409 ({code})")
    if exc.status in (401, 403):
        return HeyGenAdapterError(code="auth_failed", retryable=False,
            submission_certainty="not_sent", provider_code=code,
            message=f"HTTP {exc.status} ({code})")
    if exc.status in (400, 422):
        return HeyGenAdapterError(code="validation_error", retryable=False,
            submission_certainty="not_sent", provider_code=code,
            message=f"HTTP {exc.status} ({code})")
    if 400 <= exc.status < 500:
        return HeyGenAdapterError(code="unknown", retryable=False,
            submission_certainty="not_sent", provider_code=code,
            message=f"HTTP {exc.status} ({code})")
    return HeyGenAdapterError(code="provider_server_error", retryable=True,
        submission_certainty="maybe_sent", provider_code=code,
        message=f"HTTP {exc.status} ({code})")


def _map_submit_transport_error(exc: HttpTransportError) -> HeyGenAdapterError:
    # A connection-level failure means the request may or may not have landed.
    if exc.code in _NOT_SENT_TRANSPORT_CODES:
        return HeyGenAdapterError(code=exc.code, retryable=False,
            submission_certainty="not_sent",
            message=f"transport error: {exc}")
    return HeyGenAdapterError(code=_stable_code(exc.code), retryable=True,
        submission_certainty="maybe_sent",
        message=f"transport error: {exc}")


def _map_poll_http_error(exc: HttpErrorResponse) -> PollAdapterError:
    code = _provider_code(exc)
    if exc.status in (401, 403):
        return PollAdapterError(code="auth_failed", retryable=False,
            provider_code=code, message=f"HTTP {exc.status} ({code})")
    if exc.status == 429:
        return PollAdapterError(code="rate_limited", retryable=True,
            provider_code=code, message=f"HTTP 429 ({code})")
    if exc.status >= 500:
        return PollAdapterError(code="provider_server_error", retryable=True,
            provider_code=code, message=f"HTTP {exc.status} ({code})")
    return PollAdapterError(code="unknown", retryable=False,
        provider_code=code, message=f"HTTP {exc.status} ({code})")


def _map_title_query_http_error(exc: HttpErrorResponse) -> TitleQueryAdapterError:
    code = _provider_code(exc)
    if exc.status in (401, 403):
        return TitleQueryAdapterError(code="auth_failed", retryable=False,
            provider_code=code, message=f"HTTP {exc.status} ({code})")
    if exc.status == 429:
        return TitleQueryAdapterError(code="rate_limited", retryable=True,
            provider_code=code, message=f"HTTP 429 ({code})")
    if exc.status >= 500:
        return TitleQueryAdapterError(code="provider_server_error", retryable=True,
            provider_code=code, message=f"HTTP {exc.status} ({code})")
    return TitleQueryAdapterError(code="unknown", retryable=False,
        provider_code=code, message=f"HTTP {exc.status} ({code})")


def _map_delete_http_error(exc: HttpErrorResponse) -> DeleteAdapterError:
    code = _provider_code(exc)
    if exc.status in (401, 403):
        return DeleteAdapterError(code="auth_failed", retryable=False,
            provider_code=code, message=f"HTTP {exc.status} ({code})")
    if exc.status == 429:
        return DeleteAdapterError(code="rate_limited", retryable=True,
            provider_code=code, message=f"HTTP 429 ({code})")
    if exc.status >= 500:
        return DeleteAdapterError(code="provider_server_error", retryable=True,
            provider_code=code, message=f"HTTP {exc.status} ({code})")
    return DeleteAdapterError(code="unknown", retryable=False,
        provider_code=code, message=f"HTTP {exc.status} ({code})")


def _map_get_transport_error(exc: HttpTransportError, error_cls: type):
    """Transport errors for poll/query/delete. auth_failed → not-retryable."""
    if exc.code in _NOT_SENT_TRANSPORT_CODES:
        return error_cls(code=exc.code, retryable=False,
                         message=f"transport error: {exc}")
    retryable = exc.code in ("network_timeout", "connection_error", "rate_limited")
    return error_cls(code=_stable_code(exc.code), retryable=retryable,
                     message=f"transport error: {exc}")


def _stable_code(code: str) -> str:
    mapping = {
        "auth_failed": "auth_failed",
        "network_timeout": "network_timeout",
        "connection_error": "connection_error",
        "rate_limited": "rate_limited",
        "malformed_response": "malformed_response",
        "provider_server_error": "provider_server_error",
    }
    return mapping.get(code, "unknown")
