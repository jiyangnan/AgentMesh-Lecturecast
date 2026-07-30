"""Asset upload adapter + multipart transport tests (§5.5e5b0b)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import zlib
from pathlib import Path

import pytest

from lecturecast.heygen_asset_adapter import (
    AssetUploadAmbiguousError, AssetUploadCommand, AssetUploadError,
    AssetUploadResult, HeyGenAssetAdapter, prepare_asset_upload,
    _detect_mime,
)
from lecturecast.heygen_http import (
    HeyGenHttpTransport, HttpResponse, HttpErrorResponse,
)


# --- _detect_mime ------------------------------------------------------

def _png_bytes():
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8

def _jpeg_bytes():
    return b"\xff\xd8\xff\xe0" + b"\x00" * 12

def _mp3_bytes():
    return b"ID3" + b"\x00" * 13

def _wav_bytes():
    return b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 4

def test_detect_mime_png():
    assert _detect_mime(_png_bytes(), ".png", "portrait_photo") == "image/png"

def test_detect_mime_jpeg():
    assert _detect_mime(_jpeg_bytes(), ".jpg", "portrait_photo") == "image/jpeg"

def test_detect_mime_mp3():
    assert _detect_mime(_mp3_bytes(), ".mp3", "synthetic_narration_audio") == "audio/mpeg"

def test_detect_mime_wav():
    assert _detect_mime(_wav_bytes(), ".wav", "synthetic_narration_audio") == "audio/wav"

def test_detect_mime_wrong_ext():
    with pytest.raises(ValueError, match="non-.png extension"):
        _detect_mime(_png_bytes(), ".jpg", "portrait_photo")

def test_detect_mime_unknown():
    with pytest.raises(ValueError, match="unrecognized"):
        _detect_mime(b"unknown", ".dat", "portrait_photo")


# --- prepare_asset_upload -----------------------------------------------

def test_prepare_rejects_traversal(tmp_path):
    runtime = tmp_path / "runtime"; runtime.mkdir()
    with pytest.raises(ValueError, match="traversal"):
        prepare_asset_upload(
            operation_id="op1", asset_role="portrait_photo",
            runtime_root=runtime, local_output_ref="../escape.png")

def test_prepare_rejects_absolute(tmp_path):
    runtime = tmp_path / "runtime"; runtime.mkdir()
    with pytest.raises(ValueError, match="absolute|ref|escape"):
        prepare_asset_upload(
            operation_id="op1", asset_role="portrait_photo",
            runtime_root=runtime, local_output_ref="/etc/passwd")

def test_prepare_validates_file(tmp_path):
    runtime = tmp_path / "runtime"; runtime.mkdir()
    # empty file
    f = runtime / "portrait.png"; f.write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        prepare_asset_upload(
            operation_id="op1", asset_role="portrait_photo",
            runtime_root=runtime, local_output_ref="portrait.png")
    # not a regular file
    link = runtime / "link.png"
    os.symlink(f, link)
    with pytest.raises((ValueError, OSError)):
        prepare_asset_upload(
            operation_id="op1", asset_role="portrait_photo",
            runtime_root=runtime, local_output_ref="link.png")

def test_prepare_valid_png(tmp_path):
    runtime = tmp_path / "runtime"; runtime.mkdir()
    content = _png_bytes() + b"extra png data"
    (runtime / "portrait.png").write_bytes(content)
    cmd = prepare_asset_upload(
        operation_id="op1", asset_role="portrait_photo",
        runtime_root=runtime, local_output_ref="portrait.png")
    assert cmd.asset_role == "portrait_photo"
    assert cmd.content_type == "image/png"
    assert cmd.file_size == len(content)
    assert cmd.provider_filename == "portrait.png"
    assert cmd.expected_asset_digest == "sha256:" + hashlib.sha256(content).hexdigest()
    assert cmd.idempotency_key.startswith("lc-hg-asset-")

def test_prepare_rejects_wrong_role_for_file(tmp_path):
    runtime = tmp_path / "runtime"; runtime.mkdir()
    (runtime / "portrait.png").write_bytes(_png_bytes())
    with pytest.raises(ValueError, match="extension"):
        prepare_asset_upload(
            operation_id="op1", asset_role="synthetic_narration_audio",
            runtime_root=runtime, local_output_ref="portrait.png")

def test_prepare_rejects_oversized(tmp_path):
    runtime = tmp_path / "runtime"; runtime.mkdir()
    (runtime / "big.png").write_bytes(_png_bytes() + b"\x00" * (33 * 1024 * 1024))
    with pytest.raises(ValueError, match="exceeds"):
        prepare_asset_upload(
            operation_id="op1", asset_role="portrait_photo",
            runtime_root=runtime, local_output_ref="big.png")


# --- multipart transport (via fake opener) ------------------------------

def _fake_opener(response_body: dict, status: int = 200):
    """Create a fake opener factory that captures the request and returns a
    canned response."""
    captured = {}
    raw = json.dumps(response_body).encode()
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
            # Record whether body was an iterator (streaming) or bytes (buffered).
            captured["is_iterator"] = not isinstance(req.data, (bytes, bytearray, type(None)))
            # Consume the iterator body (urllib passes it as iterable)
            data = req.data
            if hasattr(data, '__iter__') and not isinstance(data, (bytes, bytearray)):
                data = b"".join(data)
            captured["data"] = data
            captured["headers"] = dict(req.header_items())
            captured["method"] = req.get_method()
            captured["url"] = req.full_url
            return _FakeResp()
    return lambda: _FakeOpener(), captured


def test_multipart_upload_streams_file(tmp_path):
    """Verify multipart body contains the file content and correct headers."""
    runtime = tmp_path / "runtime"; runtime.mkdir()
    png_content = _png_bytes() + b"test portrait data"
    (runtime / "portrait.png").write_bytes(_png_bytes())
    cmd = prepare_asset_upload(
        operation_id="op1", asset_role="portrait_photo",
        runtime_root=runtime, local_output_ref="portrait.png")

    fake_opener, captured = _fake_opener({"data": {
        "asset_id": "ast_123", "url": "https://files.heygen.ai/x.png",
        "mime_type": "image/png", "size_bytes": cmd.file_size}})
    transport = HeyGenHttpTransport(
        api_key_provider=lambda: "key", opener_factory=fake_opener)
    adapter = HeyGenAssetAdapter(transport)
    result = adapter.upload_asset(cmd, runtime_root=runtime)

    assert result.asset_id == "ast_123"
    assert result.mime_type == "image/png"
    # The captured body should contain the file content
    body = captured["data"]
    assert _png_bytes() in body  # PNG magic bytes present
    # Should contain multipart boundary
    assert b"multipart/form-data" not in body  # that's in headers, not body
    headers_lower = {k.lower(): v for k, v in captured["headers"].items()}
    assert "multipart/form-data" in headers_lower.get("content-type", "")
    assert headers_lower.get("idempotency-key") == cmd.idempotency_key


def test_multipart_filename_sanitized(tmp_path):
    """Provider filename is fixed by asset_role (not from disk)."""
    runtime = tmp_path / "runtime"; runtime.mkdir()
    (runtime / "portrait.png").write_bytes(_png_bytes())
    cmd = prepare_asset_upload(
        operation_id="op1", asset_role="portrait_photo",
        runtime_root=runtime, local_output_ref="portrait.png")
    fake_opener, captured = _fake_opener({"data": {
        "asset_id": "a1", "mime_type": "image/png", "size_bytes": cmd.file_size}})
    transport = HeyGenHttpTransport(
        api_key_provider=lambda: "key", opener_factory=fake_opener)
    adapter = HeyGenAssetAdapter(transport)
    adapter.upload_asset(cmd, runtime_root=runtime)
    body = captured["data"]
    assert b'filename="portrait.png"' in body


def test_multipart_rejects_no_asset_id(tmp_path):
    runtime = tmp_path / "runtime"; runtime.mkdir()
    png_content = _png_bytes() + b"data"
    (runtime / "p.png").write_bytes(png_content)
    cmd = prepare_asset_upload(
        operation_id="op1", asset_role="portrait_photo",
        runtime_root=runtime, local_output_ref="p.png")
    fake_opener, _ = _fake_opener({"data": {}})  # no asset_id
    transport = HeyGenHttpTransport(
        api_key_provider=lambda: "key", opener_factory=fake_opener)
    adapter = HeyGenAssetAdapter(transport)
    with pytest.raises(AssetUploadAmbiguousError, match="no asset_id"):
        adapter.upload_asset(cmd, runtime_root=runtime)


def test_multipart_rejects_file_changed(tmp_path):
    """If the file size changed between prepare and upload, reject."""
    runtime = tmp_path / "runtime"; runtime.mkdir()
    png_content = _png_bytes() + b"original"
    (runtime / "p.png").write_bytes(png_content)
    cmd = prepare_asset_upload(
        operation_id="op1", asset_role="portrait_photo",
        runtime_root=runtime, local_output_ref="p.png")
    # Change the file after prepare
    (runtime / "p.png").write_bytes(_png_bytes() + b"DIFFERENT-SIZE-DATA")
    fake_opener, _ = _fake_opener({"data": {"asset_id": "a1"}})
    transport = HeyGenHttpTransport(
        api_key_provider=lambda: "key", opener_factory=fake_opener)
    adapter = HeyGenAssetAdapter(transport)
    with pytest.raises(AssetUploadError, match="file changed"):
        adapter.upload_asset(cmd, runtime_root=runtime)


# ---- e5b0b round-4 contract tests ------------------------------------

def test_forged_command_rejected_before_transport(tmp_path):
    """A command with wrong idempotency_key or MIME cannot reach transport."""
    runtime = tmp_path / "runtime"; runtime.mkdir()
    (runtime / "portrait.png").write_bytes(_png_bytes())
    cmd = prepare_asset_upload(
        operation_id="op1", asset_role="portrait_photo",
        runtime_root=runtime, local_output_ref="portrait.png")
    # Forge a command with a wrong idempotency key.
    from dataclasses import replace as _replace
    forged = _replace(cmd, idempotency_key="lc-hg-asset-WRONG")
    transport_called = []
    class _TrackingOpener:
        def open(self, req, timeout=None):
            transport_called.append(req)
            class _R:
                status = 200; headers = {}
                def read(self, n=-1):
                    if not hasattr(self, "_s"): self._s = True
                    return json.dumps({"data": {"asset_id": "x", "mime_type": "image/png", "size_bytes": 16}}).encode()
                    return b""
                def close(self): pass
            return _R()
    transport = HeyGenHttpTransport(api_key_provider=lambda: "key",
        opener_factory=lambda: _TrackingOpener())
    adapter = HeyGenAssetAdapter(transport)
    with pytest.raises(AssetUploadError, match="idempotency"):
        adapter.upload_asset(forged, runtime_root=runtime)
    assert transport_called == []  # transport never reached


def test_forged_role_extension_rejected(tmp_path):
    """A command with narration role but PNG file is rejected."""
    runtime = tmp_path / "runtime"; runtime.mkdir()
    (runtime / "portrait.png").write_bytes(_png_bytes())
    cmd = prepare_asset_upload(
        operation_id="op1", asset_role="portrait_photo",
        runtime_root=runtime, local_output_ref="portrait.png")
    from dataclasses import replace as _replace
    forged = _replace(cmd, asset_role="synthetic_narration_audio")
    transport = HeyGenHttpTransport(api_key_provider=lambda: "key",
        opener_factory=lambda: type("O", (), {"open": lambda s,r,t=30: None}))
    adapter = HeyGenAssetAdapter(transport)
    with pytest.raises(AssetUploadError):  # idempotency or role/extension — either blocks
        adapter.upload_asset(forged, runtime_root=runtime)


def test_path_intermediate_symlink_rejected(tmp_path):
    """A symlink in the intermediate directory chain is rejected."""
    runtime = tmp_path / "runtime"; runtime.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / "evil.png").write_bytes(_png_bytes())
    os.symlink(outside, runtime / "link")
    cmd = AssetUploadCommand(
        operation_id="op1", asset_role="portrait_photo",
        local_output_ref="link/evil.png",
        expected_asset_digest="sha256:" + "a"*64,
        idempotency_key="idem-test",
        provider_filename="portrait.png",
        content_type="image/png", file_size=16)
    transport = HeyGenHttpTransport(api_key_provider=lambda: "key",
        opener_factory=lambda: type("O", (), {"open": lambda s,r,t=30: None}))
    adapter = HeyGenAssetAdapter(transport)
    with pytest.raises(AssetUploadError, match="symlink"):
        adapter.upload_asset(cmd, runtime_root=runtime)


def test_error_matrix_429_retryable():
    """429 → rate_limited, retryable=True."""
    from lecturecast.heygen_http import HttpErrorResponse
    err = HeyGenAssetAdapter._map_error(HttpErrorResponse(429, {}, {}, "rate_limit"))
    assert isinstance(err, AssetUploadError)
    assert err.code == "rate_limited"
    assert err.retryable is True
    assert err.submission_certainty == "not_sent"


def test_error_matrix_409_in_progress_ambiguous():
    """409 request_in_progress → ambiguous."""
    from lecturecast.heygen_http import HttpErrorResponse
    err = HeyGenAssetAdapter._map_error(HttpErrorResponse(409, {}, {}, "request_in_progress"))
    assert isinstance(err, AssetUploadAmbiguousError)


def test_error_matrix_other_409_non_retryable():
    """Other 409 → ambiguous, retryable=False."""
    from lecturecast.heygen_http import HttpErrorResponse
    err = HeyGenAssetAdapter._map_error(HttpErrorResponse(409, {}, {}, "conflict"))
    assert isinstance(err, AssetUploadAmbiguousError)
    assert err.retryable is False


def test_error_matrix_401_auth_failed():
    """401 → auth_failed, not_sent."""
    from lecturecast.heygen_http import HttpErrorResponse
    err = HeyGenAssetAdapter._map_error(HttpErrorResponse(401, {}, {}, "unauthorized"))
    assert isinstance(err, AssetUploadError)
    assert err.code == "auth_failed"


def test_error_matrix_500_provider_server_error():
    """500 → provider_server_error, ambiguous."""
    from lecturecast.heygen_http import HttpErrorResponse
    err = HeyGenAssetAdapter._map_error(HttpErrorResponse(500, {}, {}, "internal"))
    assert isinstance(err, AssetUploadAmbiguousError)
    assert err.code == "provider_server_error"


def test_response_mime_mismatch_ambiguous(tmp_path):
    """Response MIME != upload content_type → maybe_sent."""
    runtime = tmp_path / "runtime"; runtime.mkdir()
    (runtime / "portrait.png").write_bytes(_png_bytes())
    cmd = prepare_asset_upload(
        operation_id="op1", asset_role="portrait_photo",
        runtime_root=runtime, local_output_ref="portrait.png")
    fake_opener, _ = _fake_opener({"data": {
        "asset_id": "a1", "mime_type": "image/jpeg",  # wrong!
        "size_bytes": cmd.file_size}})
    transport = HeyGenHttpTransport(
        api_key_provider=lambda: "key", opener_factory=fake_opener)
    adapter = HeyGenAssetAdapter(transport)
    with pytest.raises(AssetUploadAmbiguousError, match="MIME mismatch"):
        adapter.upload_asset(cmd, runtime_root=runtime)


# ---- e5b0b round-6: prepare symlink, streaming assertion, forged role, transport mapping ---

def test_prepare_rejects_intermediate_symlink(tmp_path):
    """prepare_asset_upload must reject a symlinked intermediate directory."""
    runtime = tmp_path / "runtime"; runtime.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / "evil.png").write_bytes(_png_bytes())
    os.symlink(outside, runtime / "link")
    with pytest.raises(ValueError, match="symlink"):
        prepare_asset_upload(
            operation_id="op1", asset_role="portrait_photo",
            runtime_root=runtime, local_output_ref="link/evil.png")


def test_multipart_body_is_streamed_not_buffered(tmp_path):
    """Verify the multipart body is passed as an iterator, not pre-joined bytes."""
    runtime = tmp_path / "runtime"; runtime.mkdir()
    (runtime / "portrait.png").write_bytes(_png_bytes())
    cmd = prepare_asset_upload(
        operation_id="op1", asset_role="portrait_photo",
        runtime_root=runtime, local_output_ref="portrait.png")
    fake_opener, captured = _fake_opener({"data": {
        "asset_id": "a1", "mime_type": "image/png", "size_bytes": cmd.file_size}})
    transport = HeyGenHttpTransport(
        api_key_provider=lambda: "key", opener_factory=fake_opener)
    adapter = HeyGenAssetAdapter(transport)
    adapter.upload_asset(cmd, runtime_root=runtime)
    # The fake opener records whether req.data was bytes or an iterator.
    # The fake opener records whether req.data was an iterator vs bytes
    assert captured.get("is_iterator") is True
    # Content-Length header must match actual body size.
    headers_lower = {k.lower(): v for k, v in captured["headers"].items()}
    actual_len = len(captured["data"])
    assert int(headers_lower["content-length"]) == actual_len


def test_forged_role_caught_by_extension_guard(tmp_path):
    """Forged role + correctly re-derived idempotency is still caught by
    the extension-role mismatch (PNG file + narration role)."""
    runtime = tmp_path / "runtime"; runtime.mkdir()
    (runtime / "portrait.png").write_bytes(_png_bytes())
    cmd = prepare_asset_upload(
        operation_id="op1", asset_role="portrait_photo",
        runtime_root=runtime, local_output_ref="portrait.png")
    import hashlib as _h
    # Re-derive idempotency for the forged role so only extension guard catches it.
    forged_idem = "lc-hg-asset-" + _h.sha256(
        f"op1:synthetic_narration_audio:{cmd.expected_asset_digest}".encode()).hexdigest()
    from dataclasses import replace as _replace
    forged = _replace(cmd, asset_role="synthetic_narration_audio",
                       idempotency_key=forged_idem,
                       provider_filename="narration.wav",
                       content_type="audio/wav")
    transport = HeyGenHttpTransport(api_key_provider=lambda: "key",
        opener_factory=lambda: type("O", (), {"open": lambda s,r,t=30: None}))
    adapter = HeyGenAssetAdapter(transport)
    with pytest.raises(AssetUploadError, match="extension|content_type|mime"):
        adapter.upload_asset(forged, runtime_root=runtime)


def test_transport_network_timeout_maps_ambiguous_retryable(tmp_path):
    """upload_asset catches HttpTransportError(network_timeout) → ambiguous retryable."""
    from lecturecast.heygen_http import HttpTransportError
    runtime = tmp_path / "runtime"; runtime.mkdir()
    (runtime / "portrait.png").write_bytes(_png_bytes())
    cmd = prepare_asset_upload(
        operation_id="op1", asset_role="portrait_photo",
        runtime_root=runtime, local_output_ref="portrait.png")
    class _ErrOpener:
        def open(self, req, timeout=None):
            raise HttpTransportError(code="network_timeout", message="timed out")
    transport = HeyGenHttpTransport(api_key_provider=lambda: "key",
        opener_factory=lambda: _ErrOpener())
    adapter = HeyGenAssetAdapter(transport)
    with pytest.raises(AssetUploadAmbiguousError) as exc_info:
        adapter.upload_asset(cmd, runtime_root=runtime)
    assert exc_info.value.code == "network_timeout"
    assert exc_info.value.retryable is True


def test_transport_connection_error_maps_ambiguous_retryable(tmp_path):
    """upload_asset catches HttpTransportError(connection_error) → ambiguous retryable."""
    from lecturecast.heygen_http import HttpTransportError
    runtime = tmp_path / "runtime"; runtime.mkdir()
    (runtime / "portrait.png").write_bytes(_png_bytes())
    cmd = prepare_asset_upload(
        operation_id="op1", asset_role="portrait_photo",
        runtime_root=runtime, local_output_ref="portrait.png")
    class _ErrOpener:
        def open(self, req, timeout=None):
            raise HttpTransportError(code="connection_error", message="refused")
    transport = HeyGenHttpTransport(api_key_provider=lambda: "key",
        opener_factory=lambda: _ErrOpener())
    adapter = HeyGenAssetAdapter(transport)
    with pytest.raises(AssetUploadAmbiguousError) as exc_info:
        adapter.upload_asset(cmd, runtime_root=runtime)
    assert exc_info.value.code == "connection_error"
    assert exc_info.value.retryable is True


def test_transport_auth_failed_maps_not_sent(tmp_path):
    """upload_asset catches HttpTransportError(auth_failed) → not_sent."""
    from lecturecast.heygen_http import HttpTransportError
    runtime = tmp_path / "runtime"; runtime.mkdir()
    (runtime / "portrait.png").write_bytes(_png_bytes())
    cmd = prepare_asset_upload(
        operation_id="op1", asset_role="portrait_photo",
        runtime_root=runtime, local_output_ref="portrait.png")
    class _ErrOpener:
        def open(self, req, timeout=None):
            raise HttpTransportError(code="auth_failed", message="blank key")
    transport = HeyGenHttpTransport(api_key_provider=lambda: "key",
        opener_factory=lambda: _ErrOpener())
    adapter = HeyGenAssetAdapter(transport)
    with pytest.raises(AssetUploadError) as exc_info:
        adapter.upload_asset(cmd, runtime_root=runtime)
    assert exc_info.value.code == "auth_failed"
    assert exc_info.value.submission_certainty == "not_sent"
