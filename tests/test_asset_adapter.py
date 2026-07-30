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
    with pytest.raises(ValueError, match="\\.\\."):
        prepare_asset_upload(
            operation_id="op1", asset_role="portrait_photo",
            runtime_root=runtime, local_output_ref="../escape.png")

def test_prepare_rejects_absolute(tmp_path):
    runtime = tmp_path / "runtime"; runtime.mkdir()
    with pytest.raises(ValueError, match="ref"):
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
    (runtime / "portrait.png").write_bytes(png_content)
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
            captured["data"] = req.data
            captured["headers"] = dict(req.header_items())
            captured["method"] = req.get_method()
            captured["url"] = req.full_url
            return _FakeResp()
    return lambda: _FakeOpener(), captured


def test_multipart_upload_streams_file(tmp_path):
    """Verify multipart body contains the file content and correct headers."""
    runtime = tmp_path / "runtime"; runtime.mkdir()
    png_content = _png_bytes() + b"test portrait data"
    (runtime / "portrait.png").write_bytes(png_content)
    cmd = prepare_asset_upload(
        operation_id="op1", asset_role="portrait_photo",
        runtime_root=runtime, local_output_ref="portrait.png")

    fake_opener, captured = _fake_opener({"data": {
        "asset_id": "ast_123", "url": "https://files.heygen.ai/x.png",
        "mime_type": "image/png", "size_bytes": len(png_content)}})
    transport = HeyGenHttpTransport(
        api_key_provider=lambda: "key", opener_factory=fake_opener)
    adapter = HeyGenAssetAdapter(transport)
    result = adapter.upload_asset(cmd, runtime_root=runtime)

    assert result.asset_id == "ast_123"
    assert result.mime_type == "image/png"
    # The captured body should contain the file content
    body = captured["data"]
    assert png_content in body
    # Should contain multipart boundary
    assert b"multipart/form-data" not in body  # that's in headers, not body
    headers_lower = {k.lower(): v for k, v in captured["headers"].items()}
    assert "multipart/form-data" in headers_lower.get("content-type", "")
    assert headers_lower.get("idempotency-key") == cmd.idempotency_key


def test_multipart_filename_sanitized():
    """Provider filename is fixed by asset_role (not from disk)."""
    fake_opener, captured = _fake_opener({"data": {"asset_id": "a1"}})
    transport = HeyGenHttpTransport(
        api_key_provider=lambda: "key", opener_factory=fake_opener)
    png_content = _png_bytes()
    cmd = AssetUploadCommand(
        operation_id="op1", asset_role="portrait_photo",
        local_output_ref="portrait.png", expected_asset_digest="sha256:" + "a"*64,
        idempotency_key="idem-test", provider_filename="portrait.png",
        content_type="image/png", file_size=len(png_content))
    # Create the file in tmp and pass runtime_root
    runtime = Path("/tmp/test_multipart_runtime")
    runtime.mkdir(exist_ok=True)
    (runtime / "portrait.png").write_bytes(png_content)
    try:
        adapter = HeyGenAssetAdapter(transport)
        adapter.upload_asset(cmd, runtime_root=runtime)
        body = captured["data"]
        assert b'filename="portrait.png"' in body
    finally:
        (runtime / "portrait.png").unlink(missing_ok=True)
        runtime.rmdir()


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
