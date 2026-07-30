"""HeyGen Videos v3 adapter — submit/poll/list/delete (§5.5e5b1).

Implements HeyGenVideoAdapter + DeleteVideoAdapter Protocols using the shared
HeyGenHttpTransport. Per Codex e5b1 plan:
- submit body uses closed descriptor v1 (no passthrough/callback_url/audio_url)
- all remote IDs validated against stable-id regex before path injection
- poll fail-closed on unknown status
- list uses token pagination (not page number), max pages/candidates
- delete 404 → already_absent, 200 validates data.deleted
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Mapping

from lecturecast.heygen_http import HeyGenHttpTransport, HttpResponse, HttpErrorResponse, HttpTransportError
from lecturecast.heygen_adapter import (
    HeyGenAdapterError, SubmitAccepted, SubmitVideoCommand,
    PollResult, PollAdapterError,
    TitleQuery, TitleCandidate, TitleQueryResult, TitleQueryAdapterError,
    DeleteResult, DeleteAdapterError,
    PROVIDER_STATUS,
)

_REMOTE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
_SAFE_REMOTE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
_MAX_LIST_PAGES = 10
_MAX_CANDIDATES = 500
_LIST_LIMIT = 100
_FORBIDDEN_DESCRIPTOR_KEYS = frozenset({
    "title", "callback_url", "callback_id", "image_url", "audio_url",
    "script", "voice_id", "test", "debug",
})


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
            raise _map_submit_error(exc) from None
        except HttpTransportError as exc:
            raise HeyGenAdapterError(
                code=_stable_code(exc.code), retryable=True,
                submission_certainty="maybe_sent",
                message=f"transport error: {exc}") from None
        data = resp.body.get("data", {})
        video_id = data.get("video_id", "")
        status = data.get("status", "")
        if not isinstance(video_id, str) or not video_id.strip():
            raise HeyGenAdapterError(
                code="malformed_response", retryable=False,
                submission_certainty="maybe_sent",
                message="submit succeeded but no video_id returned")
        # Map raw provider status to our vocabulary, or leave empty.
        mapped_status = ""
        if status:
            raw = str(status).lower().strip()
            mapped_status = {"pending": "queued", "waiting": "queued",
                "queued": "queued", "submitted": "submitted",
                "processing": "processing", "completed": "completed",
                "failed": "failed"}.get(raw, "")
        return SubmitAccepted(
            remote_id=video_id.strip(),
            provider_status=mapped_status,
        )

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
            raise _map_get_error(exc) from None
        except HttpTransportError as exc:
            raise PollAdapterError(
                code=_stable_code(exc.code), retryable=exc.code in ("network_timeout", "connection_error", "rate_limited"),
                message=f"transport error: {exc}") from None
        data = resp.body.get("data", {})
        if not isinstance(data, dict):
            raise PollAdapterError(code="malformed_response", retryable=False, message="poll data is not a dict")
        raw_status = str(data.get("status", "")).lower().strip()
        video_url = data.get("video_url")
        mapped = _map_poll_status(raw_status, video_url)
        return mapped

    # -- query by title ---------------------------------------------------

    def query_videos_by_title(self, query: TitleQuery) -> TitleQueryResult:
        candidates: list[TitleCandidate] = []
        token = None
        seen_ids: set[str] = set()
        query_complete = True
        for page in range(_MAX_LIST_PAGES):
            params = {"title": query.heygen_title, "limit": str(_LIST_LIMIT)}
            if token:
                params["token"] = token
            try:
                resp = self._transport.request_json(
                    method="GET", path="/v3/videos", params=params,
                )
            except HttpErrorResponse as exc:
                raise _map_get_error(exc) from None
            except HttpTransportError as exc:
                raise TitleQueryAdapterError(
                    code=_stable_code(exc.code),
                    retryable=exc.code in ("network_timeout", "connection_error"),
                    message=f"transport error: {exc}") from None
            data = resp.body.get("data")
            if not isinstance(data, list):
                raise TitleQueryAdapterError(code="malformed_response", retryable=False,
                    message="list response data is not a list")
            has_more = resp.body.get("has_more")
            if type(has_more) is not bool:
                raise TitleQueryAdapterError(code="malformed_response", retryable=False,
                    message="has_more is not a bool")
            next_token = resp.body.get("next_token")
            for item in data:
                if not isinstance(item, dict):
                    raise TitleQueryAdapterError(code="malformed_response", retryable=False,
                        message="list item is not a dict")
                rid = str(item.get("id", ""))
                if not rid or rid in seen_ids:
                    continue
                seen_ids.add(rid)
                title = str(item.get("title", ""))
                raw_created = item.get("created_at")
                created_iso = _to_iso(raw_created)
                raw_pstatus = str(item.get("status", "")).lower().strip()
                if raw_pstatus not in PROVIDER_STATUS:
                    continue
                candidates.append(TitleCandidate(
                    remote_id=rid, title=title,
                    created_at=created_iso, provider_status=raw_pstatus,
                ))
                if len(candidates) >= _MAX_CANDIDATES:
                    query_complete = False
                    break
            if len(candidates) >= _MAX_CANDIDATES:
                query_complete = False
                break
            if not has_more:
                break
            if not isinstance(next_token, str) or not next_token.strip():
                query_complete = False
                break
            if next_token == token:
                query_complete = False
                break
            token = next_token
        else:
            query_complete = False
        return TitleQueryResult(
            query_complete=query_complete,
            candidates=tuple(candidates),
        )

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
            raise _map_delete_error(exc) from None
        except HttpTransportError as exc:
            raise DeleteAdapterError(
                code=_stable_code(exc.code),
                retryable=exc.code in ("network_timeout", "connection_error"),
                message=f"transport error: {exc}") from None
        data = resp.body.get("data", {})
        if data.get("deleted") is not True:
            raise DeleteAdapterError(code="malformed_response", retryable=False,
                message="delete succeeded but deleted is not True")
        return DeleteResult(status="deleted")


# --- helpers ---------------------------------------------------------------

_SUBMIT_STATUS_MAP = {
    "pending": "queued", "waiting": "queued", "queued": "queued",
    "submitted": "submitted", "processing": "processing",
}


def _validate_descriptor(descriptor: Mapping) -> None:
    bad = _FORBIDDEN_DESCRIPTOR_KEYS & set(descriptor.keys())
    if bad:
        raise ValueError(f"forbidden keys in request_descriptor: {bad}")
    if descriptor.get("schema_version") != "heygen.video-submit.v1":
        raise ValueError("descriptor schema_version must be heygen.video-submit.v1")
    if descriptor.get("type") != "image":
        raise ValueError("descriptor type must be 'image'")
    for key in ("image_asset_id", "audio_asset_id"):
        val = descriptor.get(key)
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"descriptor {key} must be a non-empty string")
    if not _SAFE_REMOTE_ID_RE.fullmatch(descriptor["image_asset_id"]):
        raise ValueError("image_asset_id is not a valid remote ID")
    if not _SAFE_REMOTE_ID_RE.fullmatch(descriptor["audio_asset_id"]):
        raise ValueError("audio_asset_id is not a valid remote ID")


def _build_submit_body(descriptor: Mapping, title: str) -> dict:
    return {
        "type": "image",
        "title": title,
        "image_asset_id": descriptor["image_asset_id"],
        "audio_asset_id": descriptor["audio_asset_id"],
        "aspect_ratio": descriptor.get("aspect_ratio", "16:9"),
        "output_format": descriptor.get("output_format", "mp4"),
    }


def _map_poll_status(raw_status: str, video_url) -> PollResult:
    if raw_status in ("pending", "waiting", "queued"):
        return PollResult(provider_status="queued")
    if raw_status == "submitted":
        return PollResult(provider_status="submitted")
    if raw_status == "processing":
        return PollResult(provider_status="processing")
    if raw_status == "completed":
        if not isinstance(video_url, str) or not video_url.strip():
            raise PollAdapterError(code="malformed_response", retryable=False,
                message="completed status but no video_url")
        return PollResult(provider_status="completed", video_url=video_url.strip())
    if raw_status == "failed":
        return PollResult(provider_status="failed")
    if raw_status == "not_found":
        return PollResult(provider_status="not_found")
    raise PollAdapterError(code="malformed_response", retryable=False,
        message=f"unknown poll status: {raw_status!r}")


def _to_iso(raw) -> str:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return datetime.fromtimestamp(raw, tz=timezone.utc).isoformat()
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    raise TitleQueryAdapterError(code="malformed_response", retryable=False,
        message=f"invalid created_at: {raw!r}")


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


def _map_submit_error(exc: HttpErrorResponse) -> HeyGenAdapterError:
    provider_code = exc.provider_code or "unknown"
    if exc.status == 429:
        return HeyGenAdapterError(code="rate_limited", retryable=True,
            submission_certainty="not_sent", message=f"HTTP 429 ({provider_code})")
    if exc.status == 409:
        if provider_code == "request_in_progress":
            return HeyGenAdapterError(code="unknown", retryable=True,
                submission_certainty="maybe_sent", message="HTTP 409 request_in_progress")
        return HeyGenAdapterError(code="unknown", retryable=False,
            submission_certainty="maybe_sent", message=f"HTTP 409 ({provider_code})")
    if exc.status in (401, 403):
        return HeyGenAdapterError(code="auth_failed", retryable=False,
            submission_certainty="not_sent", message=f"HTTP {exc.status} ({provider_code})")
    if exc.status in (400, 422):
        return HeyGenAdapterError(code="validation_error", retryable=False,
            submission_certainty="not_sent", message=f"HTTP {exc.status} ({provider_code})")
    if 400 <= exc.status < 500:
        return HeyGenAdapterError(code="unknown", retryable=False,
            submission_certainty="not_sent", message=f"HTTP {exc.status} ({provider_code})")
    return HeyGenAdapterError(code="provider_server_error", retryable=True,
        submission_certainty="maybe_sent", message=f"HTTP {exc.status} ({provider_code})")


def _map_get_error(exc: HttpErrorResponse) -> Exception:
    provider_code = exc.provider_code or "unknown"
    retryable = exc.status in (429,) or exc.status >= 500
    if exc.status in (401, 403):
        return PollAdapterError(code="auth_failed", retryable=False,
            message=f"HTTP {exc.status} ({provider_code})")
    if exc.status == 429:
        return PollAdapterError(code="rate_limited", retryable=True,
            message=f"HTTP 429 ({provider_code})")
    if exc.status >= 500:
        return PollAdapterError(code="provider_server_error", retryable=True,
            message=f"HTTP {exc.status} ({provider_code})")
    return PollAdapterError(code="unknown", retryable=False,
        message=f"HTTP {exc.status} ({provider_code})")


def _map_delete_error(exc: HttpErrorResponse) -> DeleteAdapterError:
    provider_code = exc.provider_code or "unknown"
    retryable = exc.status in (429,) or exc.status >= 500
    if exc.status in (401, 403):
        return DeleteAdapterError(code="auth_failed", retryable=False,
            message=f"HTTP {exc.status} ({provider_code})")
    if exc.status == 429:
        return DeleteAdapterError(code="rate_limited", retryable=True,
            message=f"HTTP 429 ({provider_code})")
    if exc.status >= 500:
        return DeleteAdapterError(code="provider_server_error", retryable=retryable,
            message=f"HTTP {exc.status} ({provider_code})")
    return DeleteAdapterError(code="unknown", retryable=False,
        message=f"HTTP {exc.status} ({provider_code})")
