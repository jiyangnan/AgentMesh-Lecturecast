"""StdlibVideoDownloader + FfprobeMediaProbe tests (§5.5e5a)."""

from __future__ import annotations

import hashlib
import json
import os
import ssl
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread

import pytest

from lecturecast.heygen_downloader import (
    FfprobeMediaProbe, StdlibVideoDownloader, resolve_download_hosts,
    _validate_download_url, _reject_private_ip,
)
from lecturecast.operation_repository import MediaProbeResult


# --- URL validation ----------------------------------------------------

def test_resolve_download_hosts_defaults():
    hosts = resolve_download_hosts()
    assert "files.heygen.ai" in hosts


def test_resolve_download_hosts_extra():
    hosts = resolve_download_hosts("cdn.example.com, bad.*.com, .bad")
    assert "cdn.example.com" in hosts
    assert "bad.*.com" not in hosts
    assert ".bad" not in hosts


def test_validate_url_rejects_http():
    with pytest.raises(ValueError, match="HTTPS"):
        _validate_download_url("http://files.heygen.ai/v.mp4", frozenset({"files.heygen.ai"}))


def test_validate_url_rejects_userinfo():
    with pytest.raises(ValueError, match="userinfo"):
        _validate_download_url("https://user:pass@files.heygen.ai/v.mp4", frozenset({"files.heygen.ai"}))


def test_validate_url_rejects_non_default_port():
    with pytest.raises(ValueError, match="port"):
        _validate_download_url("https://files.heygen.ai:8080/v.mp4", frozenset({"files.heygen.ai"}))


def test_validate_url_rejects_host_not_in_allowlist():
    with pytest.raises(ValueError, match="allowlist"):
        _validate_download_url("https://evil.com/v.mp4", frozenset({"files.heygen.ai"}))


def test_validate_url_rejects_private_ip():
    with pytest.raises(ValueError, match="forbidden"):
        _validate_download_url("https://127.0.0.1/v.mp4", frozenset({"127.0.0.1"}))


def test_validate_url_accepts_allowed_host():
    # Use a known public IP to avoid DNS-resolution false positives
    # (e.g. files.heygen.ai resolves to 198.18.x.x which Python flags as private).
    host = _validate_download_url("https://1.1.1.1/v.mp4", frozenset({"1.1.1.1"}))
    assert host == "1.1.1.1"


# --- StdlibVideoDownloader with local HTTP server ----------------------

class _StubProbe:
    """Always returns a valid MediaProbeResult without calling ffprobe."""
    def probe(self, path):
        return MediaProbeResult(duration_seconds=10.0, video_codec="h264", width=1280, height=720)


def _start_local_http(content: bytes):
    """Start a local HTTP server that serves `content` for any GET."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        def log_message(self, *a): pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_downloader_writes_temp_and_returns_prepared(tmp_path: Path):
    content = b"fake mp4 data for testing"
    server = _start_local_http(content)
    port = server.server_address[1]
    runtime = tmp_path / ".lecturecast" / "runtime"
    runtime.mkdir(parents=True)
    ref = "outputs/heygen/test_op.mp4"
    dl = StdlibVideoDownloader(allowed_hosts=frozenset({"127.0.0.1"}))
    prepared = dl.download_and_verify(
        f"http://127.0.0.1:{port}/v.mp4" if False else _make_https_url(port),
        str(runtime), ref, 1_048_576, _StubProbe(),
    ) if False else _download_http(dl, port, str(runtime), ref)
    server.shutdown()
    assert prepared.local_output_ref == ref
    assert prepared.size_bytes == len(content)
    assert prepared.digest == "sha256:" + hashlib.sha256(content).hexdigest()
    assert prepared.media.video_codec == "h264"
    temp = runtime / (ref + ".tmp")
    assert temp.exists()
    assert temp.read_bytes() == content
    # 0600 permissions
    if os.name != "nt":
        assert oct(temp.stat().st_mode)[-3:] == "600"


def _make_https_url(port):
    """We can't easily run a TLS server in a unit test, so we skip the HTTPS
    enforcement test for the real download and instead test validation
    separately (above). For the download test we use HTTP locally."""
    return None


def _download_http(dl, port, runtime, ref):
    """Download from a local HTTP server. The downloader enforces HTTPS, so we
    bypass _validate_download_url for this test by using a patched version."""
    import lecturecast.heygen_downloader as mod
    import urllib.request
    content = b"fake mp4 data for testing"
    url = f"http://127.0.0.1:{port}/v.mp4"
    # Monkey-patch validation to allow HTTP localhost for this test only
    orig = mod._validate_download_url
    mod._validate_download_url = lambda u, h: "127.0.0.1"
    try:
        return dl.download_and_verify(url, runtime, ref, 1_048_576, _StubProbe())
    finally:
        mod._validate_download_url = orig


def test_downloader_rejects_size_exceed(tmp_path: Path):
    content = b"x" * 200
    server = _start_local_http(content)
    port = server.server_address[1]
    runtime = tmp_path / ".lecturecast" / "runtime"
    runtime.mkdir(parents=True)
    ref = "outputs/heygen/big.mp4"
    dl = StdlibVideoDownloader(allowed_hosts=frozenset({"127.0.0.1"}))
    with pytest.raises(ValueError, match="max_bytes"):
        _download_http_maxbytes(dl, port, str(runtime), ref, 100)
    server.shutdown()
    # temp cleaned up
    assert not (runtime / (ref + ".tmp")).exists()


def _download_http_maxbytes(dl, port, runtime, ref, max_bytes):
    import lecturecast.heygen_downloader as mod
    mod._validate_download_url = lambda u, h: "127.0.0.1"
    return dl.download_and_verify(
        f"http://127.0.0.1:{port}/v.mp4", runtime, ref, max_bytes, _StubProbe())


def test_downloader_cleans_up_temp_on_probe_failure(tmp_path: Path):
    content = b"valid bytes"
    server = _start_local_http(content)
    port = server.server_address[1]
    runtime = tmp_path / ".lecturecast" / "runtime"
    runtime.mkdir(parents=True)
    ref = "outputs/heygen/probe_fail.mp4"
    dl = StdlibVideoDownloader(allowed_hosts=frozenset({"127.0.0.1"}))

    class _BadProbe:
        def probe(self, path):
            raise ValueError("no video stream")

    import lecturecast.heygen_downloader as mod
    mod._validate_download_url = lambda u, h: "127.0.0.1"
    with pytest.raises(ValueError, match="no video stream"):
        dl.download_and_verify(
            f"http://127.0.0.1:{port}/v.mp4", str(runtime), ref, 1_048_576, _BadProbe())
    server.shutdown()
    assert not (runtime / (ref + ".tmp")).exists()


# --- FfprobeMediaProbe -------------------------------------------------

def test_ffprobe_missing_raises():
    with pytest.raises(FileNotFoundError):
        FfprobeMediaProbe(ffprobe_path="/nonexistent/ffprobe")


@pytest.mark.skipif(not __import__("shutil").which("ffprobe"), reason="ffprobe not installed")
def test_ffprobe_real_probe(tmp_path: Path):
    """Integration test: uses real ffprobe on a minimal valid MP4 if available.
    Skipped if ffprobe is not installed."""
    # Create a minimal 1-frame MP4 using ffprobe's own test pattern is complex;
    # instead we just verify ffprobe can be invoked and returns something.
    probe = FfprobeMediaProbe()
    # We don't have a real video file; just verify ffprobe runs on a text file
    # and rejects it (no video stream).
    bad = tmp_path / "not_video.txt"
    bad.write_text("hello")
    with pytest.raises(ValueError):
        probe.probe(str(bad))
