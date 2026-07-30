"""HeyGen Videos v3 adapter tests (§5.5e5b1, round-2 hardened).

Covers the closed descriptor, response-resource binding, fail-closed title
reconciliation, token-cycle detection, and the per-interface error matrix
demanded by the Codex round-2 review.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from lecturecast.heygen_videos_adapter import (
    HeyGenVideosAdapter, _validate_remote_id, _MAX_CANDIDATES,
)
from lecturecast.heygen_http import (
    HeyGenHttpTransport, HttpErrorResponse, HttpTransportError,
)
from lecturecast.heygen_adapter import (
    SubmitVideoCommand, SubmitAccepted, PollResult, PollAdapterError,
    TitleQuery, TitleQueryResult, TitleQueryAdapterError,
    DeleteResult, DeleteAdapterError, HeyGenAdapterError,
)


# --- fake transport ---------------------------------------------------------

class _FakeResp:
    def __init__(self, status, raw):
        self.status = status
        self.headers = {}
        self._raw = raw
        self._sent = False
    def read(self, n=-1):
        if not self._sent:
            self._sent = True
            return self._raw
        return b""
    def close(self): pass


class _FakeOpener:
    """A configurable opener. `responder(call)` returns either (status, bytes)
    or an Exception instance to raise. Each call is recorded."""
    def __init__(self, responder):
        self._responder = responder
        self.calls: list[dict] = []
    def open(self, req, timeout=None):
        parsed = urlparse(req.full_url)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        call = {
            "path": parsed.path, "method": req.get_method(),
            "params": params,
            "headers": dict(req.header_items()),
        }
        if req.data is not None:
            call["body"] = req.data
        self.calls.append(call)
        result = self._responder(call)
        if isinstance(result, Exception):
            raise result
        status, raw = result
        return _FakeResp(status, raw)


def _transport(responder):
    opener = _FakeOpener(responder)
    transport = HeyGenHttpTransport(
        api_key_provider=lambda: "key", opener_factory=lambda: opener)
    return transport, opener


def _single(body=None, status=200):
    raw = json.dumps(body or {}).encode() if body is not None else b""
    return lambda call: (status, raw)


def _sequence(items):
    """items: list of (status, body) or Exception instances, popped in order."""
    it = iter(items)
    def responder(call):
        item = next(it)
        if isinstance(item, Exception):
            return item
        status, body = item
        raw = json.dumps(body or {}).encode()
        return (status, raw)
    return responder


def _cmd(**overrides):
    base = dict(
        request_descriptor={
            "schema_version": "heygen.video-submit.v1", "type": "image",
            "image_asset_id": "img_1", "audio_asset_id": "aud_1",
        },
        heygen_title="lecturecast:op1", idempotency_key="idem-1",
    )
    base.update(overrides)
    return SubmitVideoCommand(**base)


# === submit: closed descriptor =============================================

def test_submit_success_maps_pending_to_queued():
    transport, opener = _transport(_single({"data": {"video_id": "vid_123",
                                                     "status": "pending"}}))
    result = HeyGenVideosAdapter(transport).submit_video(_cmd())
    assert result.remote_id == "vid_123"
    assert result.provider_status == "queued"
    body = json.loads(opener.calls[0]["body"])
    assert body["title"] == "lecturecast:op1"
    assert body["image_asset_id"] == "img_1"
    assert body["audio_asset_id"] == "aud_1"
    assert body["output_format"] == "mp4"
    # No passthrough / provider-only fields.
    assert "callback_url" not in body
    assert "script" not in body
    assert "voice_id" not in body


@pytest.mark.parametrize("extra_key", [
    "callback_url", "script", "voice_id", "audio_url", "image_url", "test",
])
def test_submit_rejects_unknown_descriptor_key(extra_key):
    transport, _ = _transport(_single())
    descriptor = dict(_cmd().request_descriptor)
    descriptor[extra_key] = "x"
    with pytest.raises(ValueError, match="unknown keys"):
        HeyGenVideosAdapter(transport).submit_video(
            _cmd(request_descriptor=descriptor))


def test_submit_rejects_non_mp4_output_format():
    transport, _ = _transport(_single())
    descriptor = dict(_cmd().request_descriptor)
    descriptor["output_format"] = "webm"
    with pytest.raises(ValueError, match="output_format"):
        HeyGenVideosAdapter(transport).submit_video(
            _cmd(request_descriptor=descriptor))


def test_submit_rejects_bad_aspect_ratio():
    transport, _ = _transport(_single())
    descriptor = dict(_cmd().request_descriptor)
    descriptor["aspect_ratio"] = "4:3"
    with pytest.raises(ValueError, match="aspect_ratio"):
        HeyGenVideosAdapter(transport).submit_video(
            _cmd(request_descriptor=descriptor))


def test_submit_accepts_allowed_aspect_ratios():
    for ratio in ("16:9", "9:16", "1:1"):
        transport, _ = _transport(_single({"data": {"video_id": "v", "status": "queued"}}))
        descriptor = dict(_cmd().request_descriptor)
        descriptor["aspect_ratio"] = ratio
        HeyGenVideosAdapter(transport).submit_video(_cmd(request_descriptor=descriptor))


def test_submit_rejects_bad_asset_id():
    transport, _ = _transport(_single())
    descriptor = dict(_cmd().request_descriptor)
    descriptor["image_asset_id"] = "../etc/passwd"
    with pytest.raises(ValueError, match="valid remote ID"):
        HeyGenVideosAdapter(transport).submit_video(
            _cmd(request_descriptor=descriptor))


# === submit: response strictness ===========================================

def test_submit_data_not_dict_is_malformed_maybe_sent():
    transport, _ = _transport(_single({"data": [1, 2, 3]}))
    with pytest.raises(HeyGenAdapterError) as exc:
        HeyGenVideosAdapter(transport).submit_video(_cmd())
    assert exc.value.code == "malformed_response"
    assert exc.value.submission_certainty == "maybe_sent"
    assert exc.value.retryable is False


def test_submit_missing_video_id_is_malformed():
    transport, _ = _transport(_single({"data": {}}))
    with pytest.raises(HeyGenAdapterError) as exc:
        HeyGenVideosAdapter(transport).submit_video(_cmd())
    assert exc.value.submission_certainty == "maybe_sent"


def test_submit_invalid_video_id_is_malformed():
    transport, _ = _transport(_single({"data": {"video_id": "../x", "status": "queued"}}))
    with pytest.raises(HeyGenAdapterError, match="invalid video_id"):
        HeyGenVideosAdapter(transport).submit_video(_cmd())


def test_submit_unknown_status_is_malformed():
    transport, _ = _transport(_single({"data": {"video_id": "v1", "status": "banana"}}))
    with pytest.raises(HeyGenAdapterError, match="unknown status"):
        HeyGenVideosAdapter(transport).submit_video(_cmd())


def test_submit_non_string_status_is_malformed():
    transport, _ = _transport(_single({"data": {"video_id": "v1", "status": 5}}))
    with pytest.raises(HeyGenAdapterError, match="non-string status"):
        HeyGenVideosAdapter(transport).submit_video(_cmd())


def test_submit_absent_status_is_accepted():
    transport, _ = _transport(_single({"data": {"video_id": "v1"}}))
    result = HeyGenVideosAdapter(transport).submit_video(_cmd())
    assert result.remote_id == "v1"
    assert result.provider_status == ""


# === submit: HTTP error matrix =============================================

def test_submit_429_not_sent_retryable():
    transport, _ = _transport(lambda call: HttpErrorResponse(429, {}, {}, "rate_limit"))
    with pytest.raises(HeyGenAdapterError) as exc:
        HeyGenVideosAdapter(transport).submit_video(_cmd())
    assert exc.value.code == "rate_limited"
    assert exc.value.submission_certainty == "not_sent"
    assert exc.value.retryable is True


def test_submit_auth_is_not_sent_not_retryable():
    for status in (401, 403):
        transport, _ = _transport(lambda call, s=status: HttpErrorResponse(s, {}, {}, "unauth"))
        with pytest.raises(HeyGenAdapterError) as exc:
            HeyGenVideosAdapter(transport).submit_video(_cmd())
        assert exc.value.code == "auth_failed"
        assert exc.value.submission_certainty == "not_sent"
        assert exc.value.retryable is False


def test_submit_409_in_progress_maybe_sent_retryable():
    transport, _ = _transport(lambda call: HttpErrorResponse(409, {}, {}, "request_in_progress"))
    with pytest.raises(HeyGenAdapterError) as exc:
        HeyGenVideosAdapter(transport).submit_video(_cmd())
    assert exc.value.submission_certainty == "maybe_sent"
    assert exc.value.retryable is True


def test_submit_409_other_maybe_sent_not_retryable():
    transport, _ = _transport(lambda call: HttpErrorResponse(409, {}, {}, "conflict"))
    with pytest.raises(HeyGenAdapterError) as exc:
        HeyGenVideosAdapter(transport).submit_video(_cmd())
    assert exc.value.submission_certainty == "maybe_sent"
    assert exc.value.retryable is False


def test_submit_5xx_maybe_sent_retryable():
    transport, _ = _transport(lambda call: HttpErrorResponse(503, {}, {}, "unavailable"))
    with pytest.raises(HeyGenAdapterError) as exc:
        HeyGenVideosAdapter(transport).submit_video(_cmd())
    assert exc.value.code == "provider_server_error"
    assert exc.value.submission_certainty == "maybe_sent"
    assert exc.value.retryable is True


def test_submit_422_validation_not_sent():
    transport, _ = _transport(lambda call: HttpErrorResponse(422, {}, {}, "bad_request"))
    with pytest.raises(HeyGenAdapterError) as exc:
        HeyGenVideosAdapter(transport).submit_video(_cmd())
    assert exc.value.code == "validation_error"
    assert exc.value.submission_certainty == "not_sent"


def test_submit_transport_auth_failed_is_not_sent():
    transport, _ = _transport(lambda call: HttpTransportError(code="auth_failed", message="x"))
    with pytest.raises(HeyGenAdapterError) as exc:
        HeyGenVideosAdapter(transport).submit_video(_cmd())
    assert exc.value.code == "auth_failed"
    assert exc.value.submission_certainty == "not_sent"
    assert exc.value.retryable is False


def test_submit_transport_timeout_maybe_sent():
    transport, _ = _transport(lambda call: HttpTransportError(code="network_timeout", message="x"))
    with pytest.raises(HeyGenAdapterError) as exc:
        HeyGenVideosAdapter(transport).submit_video(_cmd())
    assert exc.value.submission_certainty == "maybe_sent"
    assert exc.value.retryable is True


# === poll ===================================================================

def test_poll_processing():
    transport, _ = _transport(_single({"data": {"id": "vid_1", "status": "processing"}}))
    assert HeyGenVideosAdapter(transport).poll_video("vid_1").provider_status == "processing"


def test_poll_completed_with_url():
    transport, _ = _transport(_single({"data": {"id": "vid_1", "status": "completed",
        "video_url": "https://files.heygen.ai/v.mp4"}}))
    r = HeyGenVideosAdapter(transport).poll_video("vid_1")
    assert r.provider_status == "completed"
    assert r.video_url == "https://files.heygen.ai/v.mp4"


def test_poll_completed_no_url_fail_closed():
    transport, _ = _transport(_single({"data": {"id": "vid_1", "status": "completed"}}))
    with pytest.raises(PollAdapterError, match="video_url"):
        HeyGenVideosAdapter(transport).poll_video("vid_1")


def test_poll_404_not_found():
    transport, _ = _transport(lambda call: HttpErrorResponse(404, {}, {}, "not_found"))
    assert HeyGenVideosAdapter(transport).poll_video("vid_1").provider_status == "not_found"


def test_poll_unknown_status_malformed():
    transport, _ = _transport(_single({"data": {"id": "vid_1", "status": "banana"}}))
    with pytest.raises(PollAdapterError, match="unknown"):
        HeyGenVideosAdapter(transport).poll_video("vid_1")


def test_poll_id_mismatch_malformed():
    transport, _ = _transport(_single({"data": {"id": "vid_other", "status": "processing"}}))
    with pytest.raises(PollAdapterError, match="does not match"):
        HeyGenVideosAdapter(transport).poll_video("vid_1")


def test_poll_data_not_dict_malformed():
    transport, _ = _transport(_single({"data": "nope"}))
    with pytest.raises(PollAdapterError, match="not an object"):
        HeyGenVideosAdapter(transport).poll_video("vid_1")


def test_poll_rejects_bad_remote_id():
    transport, _ = _transport(_single())
    with pytest.raises(ValueError):
        HeyGenVideosAdapter(transport).poll_video("../etc/passwd")


def test_poll_auth_not_retryable():
    transport, _ = _transport(lambda call: HttpErrorResponse(401, {}, {}, "unauth"))
    with pytest.raises(PollAdapterError) as exc:
        HeyGenVideosAdapter(transport).poll_video("vid_1")
    assert exc.value.code == "auth_failed"
    assert exc.value.retryable is False


def test_poll_5xx_retryable():
    transport, _ = _transport(lambda call: HttpErrorResponse(500, {}, {}, "err"))
    with pytest.raises(PollAdapterError) as exc:
        HeyGenVideosAdapter(transport).poll_video("vid_1")
    assert exc.value.retryable is True


# === query by title =========================================================

_QUERY = TitleQuery(heygen_title="lecturecast:op1",
                    created_after="2023-01-01T00:00:00Z",
                    created_before="2026-01-01T00:00:00Z")


def test_query_single_page_complete():
    transport, _ = _transport(_single({
        "data": [{"id": "v1", "title": "lecturecast:op1", "status": "processing",
                  "created_at": 1700000000}],
        "has_more": False}))
    r = HeyGenVideosAdapter(transport).query_videos_by_title(_QUERY)
    assert r.query_complete is True
    assert len(r.candidates) == 1
    assert r.candidates[0].remote_id == "v1"
    assert r.candidates[0].provider_status == "processing"


def test_query_two_page_pagination_uses_token():
    responses = _sequence([
        (200, {"data": [{"id": "v1", "title": "t", "status": "completed",
                         "created_at": 1700000000}], "has_more": True,
               "next_token": "page2"}),
        (200, {"data": [{"id": "v2", "title": "t", "status": "processing",
                         "created_at": 1700000001}], "has_more": False}),
    ])
    transport, opener = _transport(responses)
    r = HeyGenVideosAdapter(transport).query_videos_by_title(_QUERY)
    assert r.query_complete is True
    assert [c.remote_id for c in r.candidates] == ["v1", "v2"]
    # Second request carried the token from the first response.
    assert opener.calls[1]["params"].get("token") == "page2"


def test_query_token_cycle_aborts_incomplete():
    # A→B→A: page1 returns token A, page2 (token A) returns token B,
    # page3 (token B) returns token A again → cycle, stop incomplete.
    responses = _sequence([
        (200, {"data": [], "has_more": True, "next_token": "A"}),
        (200, {"data": [], "has_more": True, "next_token": "B"}),
        (200, {"data": [], "has_more": True, "next_token": "A"}),
    ])
    transport, opener = _transport(responses)
    r = HeyGenVideosAdapter(transport).query_videos_by_title(_QUERY)
    assert r.query_complete is False
    # Must not loop forever: at most 3 calls before cycle detection.
    assert len(opener.calls) <= 3


def test_query_truncated_at_max_candidates():
    items = [{"id": f"v{i}", "title": "t", "status": "completed",
              "created_at": 1700000000} for i in range(600)]
    transport, _ = _transport(_single({"data": items, "has_more": False}))
    r = HeyGenVideosAdapter(transport).query_videos_by_title(_QUERY)
    assert len(r.candidates) == _MAX_CANDIDATES
    assert r.query_complete is False


def test_query_data_not_list_malformed():
    transport, _ = _transport(_single({"data": {"id": "x"}, "has_more": False}))
    with pytest.raises(TitleQueryAdapterError, match="not a list"):
        HeyGenVideosAdapter(transport).query_videos_by_title(_QUERY)


def test_query_has_more_not_bool_malformed():
    transport, _ = _transport(_single({"data": [], "has_more": "yes"}))
    with pytest.raises(TitleQueryAdapterError, match="has_more"):
        HeyGenVideosAdapter(transport).query_videos_by_title(_QUERY)


def test_query_item_not_dict_malformed():
    transport, _ = _transport(_single({"data": [42], "has_more": False}))
    with pytest.raises(TitleQueryAdapterError, match="not an object"):
        HeyGenVideosAdapter(transport).query_videos_by_title(_QUERY)


def test_query_bad_candidate_id_malformed():
    transport, _ = _transport(_single({"data": [
        {"id": "../x", "title": "t", "status": "completed", "created_at": 1700000000}],
        "has_more": False}))
    with pytest.raises(TitleQueryAdapterError, match="invalid id"):
        HeyGenVideosAdapter(transport).query_videos_by_title(_QUERY)


def test_query_unknown_candidate_status_malformed_not_silently_skipped():
    transport, _ = _transport(_single({"data": [
        {"id": "v1", "title": "t", "status": "banana", "created_at": 1700000000}],
        "has_more": False}))
    with pytest.raises(TitleQueryAdapterError, match="unknown status"):
        HeyGenVideosAdapter(transport).query_videos_by_title(_QUERY)


def test_query_bad_timestamp_malformed():
    transport, _ = _transport(_single({"data": [
        {"id": "v1", "title": "t", "status": "completed", "created_at": "not-a-date"}],
        "has_more": False}))
    with pytest.raises(TitleQueryAdapterError):
        HeyGenVideosAdapter(transport).query_videos_by_title(_QUERY)


def test_query_duplicate_id_malformed_not_silently_dropped():
    transport, _ = _transport(_single({"data": [
        {"id": "v1", "title": "t", "status": "completed", "created_at": 1700000000},
        {"id": "v1", "title": "t", "status": "completed", "created_at": 1700000001}],
        "has_more": False}))
    with pytest.raises(TitleQueryAdapterError, match="duplicate"):
        HeyGenVideosAdapter(transport).query_videos_by_title(_QUERY)


def test_query_auth_uses_title_error_type():
    transport, _ = _transport(lambda call: HttpErrorResponse(401, {}, {}, "unauth"))
    with pytest.raises(TitleQueryAdapterError) as exc:
        HeyGenVideosAdapter(transport).query_videos_by_title(_QUERY)
    assert exc.value.code == "auth_failed"
    assert exc.value.retryable is False


def test_query_5xx_uses_title_error_type_retryable():
    transport, _ = _transport(lambda call: HttpErrorResponse(502, {}, {}, "bad"))
    with pytest.raises(TitleQueryAdapterError) as exc:
        HeyGenVideosAdapter(transport).query_videos_by_title(_QUERY)
    assert exc.value.code == "provider_server_error"
    assert exc.value.retryable is True


# === delete ================================================================

def test_delete_success():
    transport, _ = _transport(_single({"data": {"deleted": True}}))
    assert HeyGenVideosAdapter(transport).delete_video("vid_1").status == "deleted"


def test_delete_404_already_absent():
    transport, _ = _transport(lambda call: HttpErrorResponse(404, {}, {}, "not_found"))
    assert HeyGenVideosAdapter(transport).delete_video("vid_1").status == "already_absent"


def test_delete_not_deleted_flag():
    transport, _ = _transport(_single({"data": {"deleted": False}}))
    with pytest.raises(DeleteAdapterError, match="deleted"):
        HeyGenVideosAdapter(transport).delete_video("vid_1")


def test_delete_id_mismatch_malformed():
    transport, _ = _transport(_single({"data": {"id": "vid_other", "deleted": True}}))
    with pytest.raises(DeleteAdapterError, match="does not match"):
        HeyGenVideosAdapter(transport).delete_video("vid_1")


def test_delete_data_not_dict_malformed():
    transport, _ = _transport(_single({"data": []}))
    with pytest.raises(DeleteAdapterError, match="not an object"):
        HeyGenVideosAdapter(transport).delete_video("vid_1")


def test_delete_auth_not_retryable():
    transport, _ = _transport(lambda call: HttpErrorResponse(403, {}, {}, "nope"))
    with pytest.raises(DeleteAdapterError) as exc:
        HeyGenVideosAdapter(transport).delete_video("vid_1")
    assert exc.value.code == "auth_failed"
    assert exc.value.retryable is False
