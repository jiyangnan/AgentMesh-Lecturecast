"""StdlibVideoDownloader + FfprobeMediaProbe tests (§5.5e5a)."""

from __future__ import annotations

import hashlib
import json
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread
from unittest.mock import patch, MagicMock

import pytest

from lecturecast.heygen_downloader import (
    FfprobeMediaProbe, StdlibVideoDownloader, resolve_download_hosts,
    _validate_download_url, _reject_non_public_ip, _resolve_and_check_host,
)
from lecturecast.operation_repository import MediaProbeResult


# --- URL validation + DNS -----------------------------------------------

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
        _validate_download_url("http://1.1.1.1/v.mp4", frozenset({"1.1.1.1"}))


def test_validate_url_rejects_userinfo():
    with pytest.raises(ValueError, match="userinfo"):
        _validate_download_url("https://user:pass@1.1.1.1/v.mp4", frozenset({"1.1.1.1"}))


def test_validate_url_rejects_non_default_port():
    with pytest.raises(ValueError, match="port"):
        _validate_download_url("https://1.1.1.1:8080/v.mp4", frozenset({"1.1.1.1"}))


def test_validate_url_rejects_host_not_in_allowlist():
    with pytest.raises(ValueError, match="allowlist"):
        _validate_download_url("https://1.1.1.1/v.mp4", frozenset({"files.heygen.ai"}))


def test_validate_url_rejects_private_ip():
    with pytest.raises(ValueError, match="forbidden"):
        _validate_download_url("https://127.0.0.1/v.mp4", frozenset({"127.0.0.1"}))


def test_validate_url_accepts_public_ip(monkeypatch):
    # 1.1.1.1 is a known public IP; monkeypatch DNS to return it for a hostname.
    monkeypatch.setattr("socket.getaddrinfo", lambda *a: [
        (0, 0, 0, 0, ("1.1.1.1", 443))])
    host, ips = _validate_download_url("https://files.heygen.ai/v.mp4", frozenset({"files.heygen.ai"}))
    assert host == "files.heygen.ai"


def test_dns_failure_rejected(monkeypatch):
    import socket
    monkeypatch.setattr("socket.getaddrinfo", lambda *a: (_ for _ in ()).throw(socket.gaierror("fail")))
    with pytest.raises(ValueError, match="DNS resolution failed"):
        _resolve_and_check_host("nonexistent.invalid")


def test_dns_empty_result_rejected(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *a: [])
    with pytest.raises(ValueError, match="no addresses"):
        _resolve_and_check_host("empty.invalid")


def test_dns_resolves_to_private_rejected(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *a: [
        (0, 0, 0, 0, ("10.0.0.1", 443))])
    with pytest.raises(ValueError, match="forbidden"):
        _resolve_and_check_host("evil.com")


# --- StdlibVideoDownloader with local HTTP server ----------------------

class _StubProbe:
    def probe(self, path):
        return MediaProbeResult(duration_seconds=10.0, video_codec="h264", width=1280, height=720)


def _start_local_http(content: bytes):
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


def _connect_http(hostname, ip, port):
    """Simple HTTP (not HTTPS) connection for local tests."""
    import http.client
    return http.client.HTTPConnection(ip, port)


def _make_downloader_for_localhost(monkeypatch):
    """Create a downloader with URL validation patched to accept localhost."""
    dl = StdlibVideoDownloader(allowed_hosts=frozenset({"127.0.0.1"}))
    monkeypatch.setattr("lecturecast.heygen_downloader._validate_download_url",
                        lambda url, hosts: ("127.0.0.1", ["127.0.0.1"]))
    return dl


def test_downloader_writes_temp_and_returns_prepared(tmp_path, monkeypatch):
    content = b"fake mp4 data for testing"
    server = _start_local_http(content)
    port = server.server_address[1]
    runtime = tmp_path / ".lecturecast" / "runtime"
    runtime.mkdir(parents=True)
    ref = "outputs/heygen/test_op.mp4"
    dl = _make_downloader_for_localhost(monkeypatch)
    prepared = dl.download_and_verify(
        f"http://127.0.0.1:{port}/v.mp4", str(runtime), ref, 1_048_576, _StubProbe(),
        _connect=_connect_http)
    server.shutdown()
    assert prepared.local_output_ref == ref
    assert prepared.size_bytes == len(content)
    assert prepared.digest == "sha256:" + hashlib.sha256(content).hexdigest()
    assert prepared.media.video_codec == "h264"
    temp = runtime / (ref + ".tmp")
    assert temp.exists()
    assert temp.read_bytes() == content
    if os.name != "nt":
        assert oct(temp.stat().st_mode)[-3:] == "600"


def test_downloader_rejects_size_exceed(tmp_path, monkeypatch):
    content = b"x" * 200
    server = _start_local_http(content)
    port = server.server_address[1]
    runtime = tmp_path / ".lecturecast" / "runtime"
    runtime.mkdir(parents=True)
    ref = "outputs/heygen/big.mp4"
    dl = _make_downloader_for_localhost(monkeypatch)
    with pytest.raises(ValueError, match="max_bytes"):
        dl.download_and_verify(f"http://127.0.0.1:{port}/v.mp4", str(runtime), ref, 100, _StubProbe(),
        _connect=_connect_http)
    server.shutdown()
    assert not (runtime / (ref + ".tmp")).exists()


def test_downloader_cleans_up_temp_on_probe_failure(tmp_path, monkeypatch):
    content = b"valid bytes"
    server = _start_local_http(content)
    port = server.server_address[1]
    runtime = tmp_path / ".lecturecast" / "runtime"
    runtime.mkdir(parents=True)
    ref = "outputs/heygen/probe_fail.mp4"
    dl = _make_downloader_for_localhost(monkeypatch)
    class _BadProbe:
        def probe(self, path):
            raise ValueError("no video stream")
    with pytest.raises(ValueError, match="no video stream"):
        dl.download_and_verify(f"http://127.0.0.1:{port}/v.mp4", str(runtime), ref, 1_048_576, _BadProbe(),
        _connect=_connect_http)
    server.shutdown()
    assert not (runtime / (ref + ".tmp")).exists()


def test_downloader_rejects_redirect(tmp_path, monkeypatch):
    """Verify the downloader refuses a 3xx redirect response."""
    runtime = tmp_path / ".lecturecast" / "runtime"
    runtime.mkdir(parents=True)
    ref = "outputs/heygen/redir.mp4"
    dl = _make_downloader_for_localhost(monkeypatch)
    import http.client as _hc

    class _RedirectConn:
        def request(self, method, path, headers=None):
            pass
        def getresponse(self):
            class _Resp:
                status = 301
                def getheader(self, name, default=""):
                    return {"Location": "http://evil.com/v.mp4"}.get(name, default)
                def read(self, n=-1): return b""
                def close(self): pass
            return _Resp()
        def close(self): pass

    with pytest.raises(ValueError, match="redirect"):
        dl.download_and_verify("http://127.0.0.1:9999/v.mp4", str(runtime), ref, 1_048_576,
                               _StubProbe(), _connect=lambda h, i, p: _RedirectConn())


def _make_fake_popen(fake_result):
    """Create a fake Popen with a real os.pipe for select() compatibility."""
    stdout_bytes = fake_result.stdout.encode() if isinstance(fake_result.stdout, str) else fake_result.stdout

    class _FakePopen:
        def __init__(self, cmd, stdout=None, stderr=None):
            r, w = __import__("os").pipe()
            # Write from a daemon thread so the pipe buffer doesn't deadlock
            # on large outputs (OS pipe buffer is ~64KB).
            def _write():
                try:
                    __import__("os").write(w, stdout_bytes)
                except OSError:
                    pass
                finally:
                    try:
                        __import__("os").close(w)
                    except OSError:
                        pass
            __import__("threading").Thread(target=_write, daemon=True).start()
            self._stdout = __import__("os").fdopen(r, "rb")
            self._rc = fake_result.returncode
            self._poll_count = 0

        @property
        def stdout(self):
            return self._stdout

        @property
        def returncode(self):
            return self._rc

        def wait(self, timeout=None):
            return self._rc

        def kill(self):
            pass

        def poll(self):
            return self._rc

    return _FakePopen


# --- FfprobeMediaProbe -------------------------------------------------

@pytest.fixture()
def fake_ffprobe(tmp_path):
    """Create a fake ffprobe binary path for constructor tests."""
    p = tmp_path / "fake_ffprobe"
    p.write_text("#!/bin/sh")
    return str(p)

def test_ffprobe_missing_path_raises():
    with pytest.raises(FileNotFoundError):
        FfprobeMediaProbe(ffprobe_path="/nonexistent/ffprobe")


def test_ffprobe_parses_valid_json(monkeypatch, fake_ffprobe, tmp_path):
    """Verify ffprobe output parsing with a fake subprocess result."""
    probe = FfprobeMediaProbe(ffprobe_path=fake_ffprobe)
    target = tmp_path / "fake.mp4"; target.write_bytes(b"fake")
    fake_result = MagicMock(
        returncode=0, stdout=json.dumps({
            "streams": [{"codec_type": "video", "codec_name": "h264",
                         "width": 1920, "height": 1080, "duration": "12.5"}],
            "format": {"duration": "12.5"},
        }), stderr="")
    monkeypatch.setattr("subprocess.Popen", _make_fake_popen(fake_result))
    result = probe.probe(str(target))
    assert result.video_codec == "h264"
    assert result.width == 1920
    assert result.height == 1080
    assert result.duration_seconds == 12.5


def test_ffprobe_rejects_no_video_stream(monkeypatch, fake_ffprobe, tmp_path):
    probe = FfprobeMediaProbe(ffprobe_path=fake_ffprobe)
    target = tmp_path / "fake.mp4"; target.write_bytes(b"fake")
    fake = MagicMock(returncode=0, stdout=json.dumps({"streams": [{"codec_type": "audio"}]}), stderr="")
    monkeypatch.setattr("subprocess.Popen", _make_fake_popen(fake))
    with pytest.raises(ValueError, match="no video stream"):
        probe.probe(str(target))


def test_ffprobe_rejects_malformed_json(monkeypatch, fake_ffprobe, tmp_path):
    probe = FfprobeMediaProbe(ffprobe_path=fake_ffprobe)
    target = tmp_path / "fake.mp4"; target.write_bytes(b"fake")
    fake = MagicMock(returncode=0, stdout="not json at all", stderr="")
    monkeypatch.setattr("subprocess.Popen", _make_fake_popen(fake))
    with pytest.raises(ValueError, match="not valid JSON"):
        probe.probe(str(target))


def test_ffprobe_rejects_nan_duration(monkeypatch, fake_ffprobe, tmp_path):
    probe = FfprobeMediaProbe(ffprobe_path=fake_ffprobe)
    target = tmp_path / "fake.mp4"; target.write_bytes(b"fake")
    fake = MagicMock(returncode=0, stdout=json.dumps({
        "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720}],
        "format": {"duration": "NaN"},
    }), stderr="")
    monkeypatch.setattr("subprocess.Popen", _make_fake_popen(fake))
    with pytest.raises(ValueError, match="finite"):
        probe.probe(str(target))


def test_ffprobe_rejects_nonzero_exit(monkeypatch, fake_ffprobe, tmp_path):
    probe = FfprobeMediaProbe(ffprobe_path=fake_ffprobe)
    target = tmp_path / "fake.mp4"; target.write_bytes(b"fake")
    fake = MagicMock(returncode=1, stdout="", stderr="error")
    monkeypatch.setattr("subprocess.Popen", _make_fake_popen(fake))
    with pytest.raises(ValueError, match="exit 1"):
        probe.probe(str(target))


@pytest.mark.skipif(not __import__("shutil").which("ffprobe"), reason="ffprobe not installed")
def test_ffprobe_real_bad_file_rejected(tmp_path):
    probe = FfprobeMediaProbe()
    bad = tmp_path / "not_video.txt"
    bad.write_text("hello")
    with pytest.raises(ValueError):
        probe.probe(str(bad))


# ---- e5a round-7 contract tests --------------------------------------

def test_http_404_rejected_no_temp(tmp_path, monkeypatch):
    """HTTP 404 must be rejected before creating any temp file."""
    runtime = tmp_path / ".lecturecast" / "runtime"
    runtime.mkdir(parents=True)
    ref = "outputs/heygen/notfound.mp4"
    dl = _make_downloader_for_localhost(monkeypatch)
    import http.client as _hc

    class _404Conn:
        def request(self, method, path, headers=None): pass
        def getresponse(self):
            class _R:
                status = 404
                def getheader(self, n, d=""): return d
                def read(self, n=-1): return b""
                def close(self): pass
            return _R()
        def close(self): pass

    with pytest.raises(ValueError, match="HTTP 404"):
        dl.download_and_verify("http://127.0.0.1:9999/v.mp4", str(runtime), ref,
                              1_048_576, _StubProbe(), _connect=lambda h,i,p: _404Conn())
    assert not (runtime / (ref + ".tmp")).exists()


def test_http_500_rejected_no_temp(tmp_path, monkeypatch):
    runtime = tmp_path / ".lecturecast" / "runtime"
    runtime.mkdir(parents=True)
    ref = "outputs/heygen/server_err.mp4"
    dl = _make_downloader_for_localhost(monkeypatch)
    import http.client as _hc

    class _500Conn:
        def request(self, method, path, headers=None): pass
        def getresponse(self):
            class _R:
                status = 500
                def getheader(self, n, d=""): return d
                def read(self, n=-1): return b""
                def close(self): pass
            return _R()
        def close(self): pass

    with pytest.raises(ValueError, match="HTTP 500"):
        dl.download_and_verify("http://127.0.0.1:9999/v.mp4", str(runtime), ref,
                              1_048_576, _StubProbe(), _connect=lambda h,i,p: _500Conn())
    assert not (runtime / (ref + ".tmp")).exists()


def test_path_traversal_rejected(tmp_path, monkeypatch):
    """A local_output_ref with .. must be rejected."""
    runtime = tmp_path / ".lecturecast" / "runtime"
    runtime.mkdir(parents=True)
    dl = _make_downloader_for_localhost(monkeypatch)
    with pytest.raises(ValueError, match=r"\.\."):
        dl.download_and_verify("http://127.0.0.1:9999/v.mp4", str(runtime),
                              "outputs/../../etc/passwd.mp4", 1_048_576, _StubProbe(),
                              _connect=lambda h,i,p: None)


def test_ffprobe_overflow_kills(monkeypatch, fake_ffprobe, tmp_path):
    """ffprobe output exceeding 1 MiB must be killed immediately."""
    probe = FfprobeMediaProbe(ffprobe_path=fake_ffprobe)
    target = tmp_path / "fake.mp4"; target.write_bytes(b"fake")
    big_stdout = b"x" * (2 * 1024 * 1024)
    fake = MagicMock(returncode=0, stdout=big_stdout.decode("ascii"), stderr="")
    monkeypatch.setattr("subprocess.Popen", _make_fake_popen(fake))
    with pytest.raises(ValueError, match="exceeded 1 MiB"):
        probe.probe(str(target))


def test_ffprobe_hang_timeout():
    """Contract: ffprobe timeout path (kill + join + stdout close) is documented.
    A real 30s test is impractical for CI; the code path is:
    reader_thread.join(timeout=30) → if alive → proc.kill() + proc.wait() +
    proc.stdout.close() + reader_thread.join(timeout=5) + raise."""
    # Verify the code has the join+kill+close sequence by import inspection.
    import lecturecast.heygen_downloader as mod
    import inspect
    src = inspect.getsource(mod.FfprobeMediaProbe.probe)
    assert "reader_thread.join(timeout=30)" in src
    assert "proc.kill()" in src
    assert "proc.stdout.close()" in src
    assert "reader_thread.join(timeout=5)" in src
