"""HeyGen Videos v3 adapter tests (§5.5e5b1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lecturecast.heygen_videos_adapter import HeyGenVideosAdapter, _validate_remote_id
from lecturecast.heygen_http import HeyGenHttpTransport, HttpResponse, HttpErrorResponse, HttpTransportError
from lecturecast.heygen_adapter import (
    SubmitVideoCommand, SubmitAccepted, PollResult, PollAdapterError,
    TitleQuery, TitleQueryResult, TitleQueryAdapterError,
    DeleteResult, DeleteAdapterError, HeyGenAdapterError,
)


def _fake_transport(response_body=None, status=200, error=None, raise_exc=None):
    """Build a transport whose opener returns a canned response or raises."""
    captured = {}
    raw = json.dumps(response_body or {}).encode() if response_body else b""
    class _FakeResp:
        def __init__(self):
            self.status = status
        def read(self, n=-1):
            if not hasattr(self, "_sent"):
                self._sent = True
                return raw
            return b""
        def close(self): pass
        headers = {}
    class _FakeOpener:
        def open(self, req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["headers"] = dict(req.header_items())
            if req.data is not None:
                captured["body"] = req.data
            if raise_exc:
                raise raise_exc
            return _FakeResp()
    return HeyGenHttpTransport(
        api_key_provider=lambda: "key", opener_factory=lambda: _FakeOpener()), captured


# --- submit -------------------------------------------------------------

def test_submit_success():
    transport, cap = _fake_transport({"data": {"video_id": "vid_123", "status": "pending"}})
    adapter = HeyGenVideosAdapter(transport)
    cmd = SubmitVideoCommand(
        request_descriptor={"schema_version": "heygen.video-submit.v1", "type": "image",
                            "image_asset_id": "img_1", "audio_asset_id": "aud_1"},
        heygen_title="lecturecast:op1", idempotency_key="idem-1")
    result = adapter.submit_video(cmd)
    assert result.remote_id == "vid_123"
    assert result.provider_status == "queued"  # mapped from raw "pending"
    # Body must not contain forbidden keys
    body = json.loads(cap["body"])
    assert "callback_url" not in body
    assert "script" not in body
    assert body["title"] == "lecturecast:op1"
    assert body["image_asset_id"] == "img_1"
    assert body["audio_asset_id"] == "aud_1"


def test_submit_rejects_forbidden_descriptor_keys():
    transport, _ = _fake_transport()
    adapter = HeyGenVideosAdapter(transport)
    cmd = SubmitVideoCommand(
        request_descriptor={"schema_version": "heygen.video-submit.v1", "type": "image",
                            "image_asset_id": "img_1", "audio_asset_id": "aud_1",
                            "callback_url": "https://evil.com/cb"},
        heygen_title="t", idempotency_key="idem-1")
    with pytest.raises(ValueError, match="forbidden"):
        adapter.submit_video(cmd)


def test_submit_no_video_id_ambiguous():
    transport, _ = _fake_transport({"data": {}})
    adapter = HeyGenVideosAdapter(transport)
    cmd = SubmitVideoCommand(
        request_descriptor={"schema_version": "heygen.video-submit.v1", "type": "image",
                            "image_asset_id": "img_1", "audio_asset_id": "aud_1"},
        heygen_title="t", idempotency_key="idem-1")
    with pytest.raises(HeyGenAdapterError) as exc:
        adapter.submit_video(cmd)
    assert exc.value.submission_certainty == "maybe_sent"


def test_submit_429_not_sent_retryable():
    err = HttpErrorResponse(429, {}, {}, "rate_limit")
    transport, _ = _fake_transport(raise_exc=err)
    adapter = HeyGenVideosAdapter(transport)
    cmd = SubmitVideoCommand(
        request_descriptor={"schema_version": "heygen.video-submit.v1", "type": "image",
                            "image_asset_id": "img_1", "audio_asset_id": "aud_1"},
        heygen_title="t", idempotency_key="idem-1")
    with pytest.raises(HeyGenAdapterError) as exc:
        adapter.submit_video(cmd)
    assert exc.value.submission_certainty == "not_sent"
    assert exc.value.retryable is True


def test_submit_409_in_progress_maybe_sent():
    err = HttpErrorResponse(409, {}, {}, "request_in_progress")
    transport, _ = _fake_transport(raise_exc=err)
    adapter = HeyGenVideosAdapter(transport)
    cmd = SubmitVideoCommand(
        request_descriptor={"schema_version": "heygen.video-submit.v1", "type": "image",
                            "image_asset_id": "img_1", "audio_asset_id": "aud_1"},
        heygen_title="t", idempotency_key="idem-1")
    with pytest.raises(HeyGenAdapterError) as exc:
        adapter.submit_video(cmd)
    assert exc.value.submission_certainty == "maybe_sent"
    assert exc.value.retryable is True


# --- poll ---------------------------------------------------------------

def test_poll_processing():
    transport, _ = _fake_transport({"data": {"id": "vid_1", "status": "processing"}})
    adapter = HeyGenVideosAdapter(transport)
    result = adapter.poll_video("vid_1")
    assert result.provider_status == "processing"


def test_poll_completed_with_url():
    transport, _ = _fake_transport({"data": {"id": "vid_1", "status": "completed",
        "video_url": "https://files.heygen.ai/v.mp4"}})
    adapter = HeyGenVideosAdapter(transport)
    result = adapter.poll_video("vid_1")
    assert result.provider_status == "completed"
    assert result.video_url == "https://files.heygen.ai/v.mp4"


def test_poll_completed_no_url_fail_closed():
    transport, _ = _fake_transport({"data": {"id": "vid_1", "status": "completed"}})
    adapter = HeyGenVideosAdapter(transport)
    with pytest.raises(PollAdapterError, match="video_url"):
        adapter.poll_video("vid_1")


def test_poll_404_not_found():
    err = HttpErrorResponse(404, {}, {}, "not_found")
    transport, _ = _fake_transport(raise_exc=err)
    adapter = HeyGenVideosAdapter(transport)
    result = adapter.poll_video("vid_1")
    assert result.provider_status == "not_found"


def test_poll_unknown_status_malformed():
    transport, _ = _fake_transport({"data": {"id": "vid_1", "status": "banana"}})
    adapter = HeyGenVideosAdapter(transport)
    with pytest.raises(PollAdapterError, match="unknown"):
        adapter.poll_video("vid_1")


def test_poll_rejects_bad_remote_id():
    transport, _ = _fake_transport()
    adapter = HeyGenVideosAdapter(transport)
    with pytest.raises(ValueError):
        adapter.poll_video("../etc/passwd")


# --- query by title -----------------------------------------------------

def test_query_single_page_complete():
    transport, _ = _fake_transport({
        "data": [{"id": "v1", "title": "lecturecast:op1", "status": "processing",
                  "created_at": 1700000000}],
        "has_more": False,
    })
    adapter = HeyGenVideosAdapter(transport)
    query = TitleQuery(heygen_title="lecturecast:op1",
                       created_after="2023-01-01T00:00:00Z",
                       created_before="2026-01-01T00:00:00Z")
    result = adapter.query_videos_by_title(query)
    assert result.query_complete is True
    assert len(result.candidates) == 1
    assert result.candidates[0].remote_id == "v1"
    assert result.candidates[0].provider_status == "processing"


def test_query_truncated_at_max_candidates():
    items = [{"id": f"v{i}", "title": "t", "status": "completed",
              "created_at": 1700000000} for i in range(600)]
    transport, _ = _fake_transport({"data": items, "has_more": False})
    adapter = HeyGenVideosAdapter(transport)
    from lecturecast.heygen_videos_adapter import _MAX_CANDIDATES
    query = TitleQuery(heygen_title="t", created_after="2023-01-01T00:00:00Z",
                       created_before="2026-01-01T00:00:00Z")
    result = adapter.query_videos_by_title(query)
    assert len(result.candidates) == _MAX_CANDIDATES
    assert result.query_complete is False


# --- delete -------------------------------------------------------------

def test_delete_success():
    transport, _ = _fake_transport({"data": {"deleted": True}})
    adapter = HeyGenVideosAdapter(transport)
    result = adapter.delete_video("vid_1")
    assert result.status == "deleted"


def test_delete_404_already_absent():
    err = HttpErrorResponse(404, {}, {}, "not_found")
    transport, _ = _fake_transport(raise_exc=err)
    adapter = HeyGenVideosAdapter(transport)
    result = adapter.delete_video("vid_1")
    assert result.status == "already_absent"


def test_delete_not_deleted_flag():
    transport, _ = _fake_transport({"data": {"deleted": False}})
    adapter = HeyGenVideosAdapter(transport)
    with pytest.raises(DeleteAdapterError, match="deleted"):
        adapter.delete_video("vid_1")
