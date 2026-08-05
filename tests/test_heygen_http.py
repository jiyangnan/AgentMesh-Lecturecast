"""HeyGen HTTP transport tests (§5.5e5b0a)."""

from __future__ import annotations

import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

import pytest

from lecturecast.heygen_http import (
    HeyGenHttpTransport, HttpResponse, HttpErrorResponse, HttpTransportError,
    _validate_base_url, _validate_path,
)


# --- validation --------------------------------------------------------

def test_validate_base_url_rejects_http():
    with pytest.raises(ValueError, match="HTTPS"):
        _validate_base_url("http://api.heygen.com")


def test_validate_base_url_rejects_userinfo():
    with pytest.raises(ValueError, match="userinfo"):
        _validate_base_url("https://user:pass@api.heygen.com")


def test_validate_base_url_rejects_path():
    with pytest.raises(ValueError, match="path"):
        _validate_base_url("https://api.heygen.com/extra")


def test_validate_base_url_accepts_clean():
    assert _validate_base_url("https://api.heygen.com") == "https://api.heygen.com"


def test_validate_path_rejects_full_url():
    with pytest.raises(ValueError, match="path"):
        _validate_path("https://evil.com/v3/videos")


def test_validate_path_rejects_traversal():
    with pytest.raises(ValueError, match="path"):
        _validate_path("/v3/../etc/passwd")


def test_validate_path_accepts_valid():
    _validate_path("/v3/videos")
    _validate_path("/v3/videos/abc-123")


def test_transport_rejects_blank_api_key():
    transport = HeyGenHttpTransport(api_key_provider=lambda: "")
    with pytest.raises(HttpTransportError, match="API key"):
        transport.request_json(method="GET", path="/v3/Health")


def test_transport_rejects_non_string_api_key():
    transport = HeyGenHttpTransport(api_key_provider=lambda: 12345)  # type: ignore
    with pytest.raises(HttpTransportError, match="API key"):
        transport.request_json(method="GET", path="/v3/Health")


def test_transport_rejects_bad_idempotency_key():
    transport = HeyGenHttpTransport(api_key_provider=lambda: "test-key")
    with pytest.raises(ValueError, match="Idempotency-Key"):
        transport.request_json(method="POST", path="/v3/videos",
                               json_body={"x": 1}, idempotency_key="bad key with spaces!")


# --- local HTTP server tests -------------------------------------------

def _start_server(handler_cls):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _make_transport(monkeypatch, port):
    """Create a transport for local HTTP testing (bypasses HTTPS validation)."""
    monkeypatch.setattr("lecturecast.heygen_http._validate_base_url",
                        lambda u: f"http://127.0.0.1:{port}")
    t = HeyGenHttpTransport(
        base_url=f"http://127.0.0.1:{port}",
        api_key_provider=lambda: "test-key")
    return t


def test_request_json_success(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.dumps({"data": {"video_id": "vid_123", "status": "pending"}})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Request-Id", "req-abc")
            self.end_headers()
            self.wfile.write(body.encode())
        def log_message(self, *a): pass
    server = _start_server(Handler)
    port = server.server_address[1]
    t = _make_transport(monkeypatch, port)
    try:
        resp = t.request_json(method="POST", path="/v3/videos",
                              json_body={"test": True}, idempotency_key="idem-001")
        assert resp.status == 200
        assert resp.body["data"]["video_id"] == "vid_123"
        assert resp.headers.get("x-request-id") == "req-abc"
    finally:
        server.shutdown()


def test_request_json_error_response(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.dumps({"error": {"code": "invalid_request", "message": "bad"}})
            self.send_response(422)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())
        def log_message(self, *a): pass
    server = _start_server(Handler)
    port = server.server_address[1]
    t = _make_transport(monkeypatch, port)
    try:
        with pytest.raises(HttpErrorResponse) as exc_info:
            t.request_json(method="POST", path="/v3/videos", json_body={"bad": True})
        assert exc_info.value.status == 422
        assert exc_info.value.provider_code == "invalid_request"
    finally:
        server.shutdown()


def test_request_json_malformed_2xx(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "5")
            self.end_headers()
            self.wfile.write(b"hello")
        def log_message(self, *a): pass
    server = _start_server(Handler)
    port = server.server_address[1]
    t = _make_transport(monkeypatch, port)
    try:
        with pytest.raises(HttpTransportError, match="not valid JSON"):
            t.request_json(method="GET", path="/v3/videos/abc")
    finally:
        server.shutdown()


def test_request_json_non_object_json(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"[1, 2, 3]"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a): pass
    server = _start_server(Handler)
    port = server.server_address[1]
    t = _make_transport(monkeypatch, port)
    try:
        with pytest.raises(HttpTransportError, match="not a JSON object"):
            t.request_json(method="GET", path="/v3/videos/abc")
    finally:
        server.shutdown()


def test_request_json_response_too_large(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"x" * (2 * 1024 * 1024)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a): pass
    server = _start_server(Handler)
    port = server.server_address[1]
    t = _make_transport(monkeypatch, port)
    try:
        with pytest.raises(HttpTransportError, match="exceeded 1 MiB"):
            t.request_json(method="GET", path="/v3/videos/abc")
    finally:
        server.shutdown()


def test_header_whitelist(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"ok": True})
            self.send_response(200)
            self.send_header("X-Request-Id", "req-1")
            self.send_header("X-Secret-Internal", "leaked")
            self.send_header("Retry-After", "5")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())
        def log_message(self, *a): pass
    server = _start_server(Handler)
    port = server.server_address[1]
    t = _make_transport(monkeypatch, port)
    try:
        resp = t.request_json(method="GET", path="/v3/videos/abc")
        assert "x-request-id" in resp.headers
        assert "retry-after" in resp.headers
        assert "x-secret-internal" not in resp.headers
    finally:
        server.shutdown()


# ---- e5b0a round-2 contract tests ------------------------------------

def test_validate_base_url_rejects_evil_host():
    with pytest.raises(ValueError, match="api.heygen.com"):
        _validate_base_url("https://evil.example")


def test_validate_base_url_rejects_non_443_port():
    with pytest.raises(ValueError, match="443"):
        _validate_base_url("https://api.heygen.com:8443")


def test_validate_path_rejects_v1():
    with pytest.raises(ValueError, match="path"):
        _validate_path("/v1/videos")


def test_validate_path_rejects_dot_segment():
    with pytest.raises(ValueError, match="path"):
        _validate_path("/v3/./videos")


def test_validate_path_rejects_double_slash():
    with pytest.raises(ValueError, match="path"):
        _validate_path("/v3//videos")


def test_transport_rejects_bad_method():
    t = HeyGenHttpTransport(api_key_provider=lambda: "key")
    with pytest.raises(ValueError, match="method"):
        t.request_json(method="PUT", path="/v3/videos")


def test_transport_rejects_bad_timeout():
    t = HeyGenHttpTransport(api_key_provider=lambda: "key", timeout=0)
    with pytest.raises(ValueError, match="timeout"):
        t.request_json(method="GET", path="/v3/videos")


def test_transport_rejects_empty_2xx(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        def log_message(self, *a): pass
    server = _start_server(Handler)
    port = server.server_address[1]
    t = _make_transport(monkeypatch, port)
    try:
        with pytest.raises(HttpTransportError, match="empty"):
            t.request_json(method="GET", path="/v3/videos/abc")
    finally:
        server.shutdown()


def test_opener_factory_injection():
    """Verify the opener factory is called and its opener is used."""
    called = []
    class _FakeOpener:
        def open(self, req, timeout=None):
            called.append(req)
            class _R:
                status = 200
                headers = {}
                def read(self, n=-1):
                    if not hasattr(self, "_sent"):
                        self._sent = True
                        return json.dumps({"ok": True}).encode()
                    return b""
                def close(self): pass
            return _R()
    t = HeyGenHttpTransport(
        api_key_provider=lambda: "key",
        opener_factory=lambda: _FakeOpener())
    resp = t.request_json(method="GET", path="/v3/videos/abc")
    assert resp.body == {"ok": True}
    assert len(called) == 1


def test_api_key_read_each_call():
    """API key provider is called on every request, not cached."""
    count = [0]
    def provider():
        count[0] += 1
        return f"key-{count[0]}"
    class _FakeOpener:
        def open(self, req, timeout=None):
            class _R:
                status = 200
                headers = {}
                def read(self, n=-1):
                    if not hasattr(self, "_sent"):
                        self._sent = True
                        return json.dumps({"ok": True}).encode()
                    return b""
                def close(self): pass
            return _R()
    t = HeyGenHttpTransport(
        api_key_provider=provider,
        opener_factory=lambda: _FakeOpener())
    t.request_json(method="GET", path="/v3/videos/a")
    t.request_json(method="GET", path="/v3/videos/b")
    assert count[0] == 2  # called twice, fresh each time


# ---- e5b0a round-3: error body, default opener, canonical body --------

def test_default_opener_has_redirect_handler():
    """Default opener must wire _NoRedirectHandler."""
    from lecturecast.heygen_http import HeyGenHttpTransport, _NoRedirectHandler
    from urllib.request import HTTPRedirectHandler
    t = HeyGenHttpTransport(api_key_provider=lambda: "key")
    opener = t._opener_factory()
    redirect_handlers = [h for h in opener.handlers if isinstance(h, HTTPRedirectHandler)]
    assert len(redirect_handlers) == 1
    assert isinstance(redirect_handlers[0], _NoRedirectHandler)

def test_default_opener_disables_proxy():
    """Default opener source includes ProxyHandler({}) to disable env proxies."""
    import inspect
    from lecturecast.heygen_http import HeyGenHttpTransport
    src = inspect.getsource(HeyGenHttpTransport._default_opener)
    assert "ProxyHandler({})" in src


def test_canonical_json_body_sorts_keys():
    """JSON body must use sort_keys + separators for deterministic replay."""
    class _CapturingOpener:
        def __init__(self): self.captured_body = None
        def open(self, req, timeout=None):
            self.captured_body = req.data
            class _R:
                status = 200
                headers = {}
                def read(self, n=-1):
                    if not hasattr(self, "_s"):
                        self._s = True
                        return json.dumps({"ok": True}).encode()
                    return b""
                def close(self): pass
            return _R()
    cap = _CapturingOpener()
    t = HeyGenHttpTransport(
        api_key_provider=lambda: "key",
        opener_factory=lambda: cap)
    t.request_json(method="POST", path="/v3/videos",
                   json_body={"b": 1, "a": 2})
    assert cap.captured_body == json.dumps({"a": 2, "b": 1}, sort_keys=True,
                                           separators=(",", ":")).encode()


def test_api_key_header_sent_each_call():
    """Each request gets a fresh API key in the X-Api-Key header."""
    keys_sent = []
    class _KeyCaptureOpener:
        def open(self, req, timeout=None):
            keys_sent.append(req.get_header("X-api-key") or req.headers.get("X-Api-Key") or "")
            class _R:
                status = 200
                headers = {}
                def read(self, n=-1):
                    if not hasattr(self, "_s"):
                        self._s = True
                        return json.dumps({"ok": True}).encode()
                    return b""
                def close(self): pass
            return _R()
    counter = [0]
    t = HeyGenHttpTransport(
        api_key_provider=lambda: (counter.__setitem__(0, counter[0]+1) or f"key-{counter[0]}"),
        opener_factory=lambda: _KeyCaptureOpener())
    t.request_json(method="GET", path="/v3/videos/a")
    t.request_json(method="GET", path="/v3/videos/b")
    assert keys_sent == ["key-1", "key-2"]


def test_idempotency_key_header_sent():
    class _Cap:
        def open(self, req, timeout=None):
            assert req.get_header("Idempotency-key") == "idem-abc"
            class _R:
                status = 200
                headers = {}
                def read(self, n=-1):
                    if not hasattr(self, "_s"):
                        self._s = True
                        return json.dumps({"ok": True}).encode()
                    return b""
                def close(self): pass
            return _R()
    t = HeyGenHttpTransport(
        api_key_provider=lambda: "key",
        opener_factory=lambda: _Cap())
    t.request_json(method="POST", path="/v3/videos",
                   json_body={"x": 1}, idempotency_key="idem-abc")


def test_oversized_error_body_not_parsed(monkeypatch):
    """Error body exceeding 1 MiB → body=None, provider_code=None."""
    import urllib.error
    big_body = b"x" * (2 * 1024 * 1024)
    fake_exc = urllib.error.HTTPError(
        "https://api.heygen.com/v3/videos", 500, "Server Error",
        {"Content-Type": "text/plain"},
        __import__("io").BytesIO(big_body))
    from lecturecast.heygen_http import _make_error_response
    err = _make_error_response(fake_exc)
    assert err.status == 500
    assert not err.body  # None or {}
    assert err.provider_code is None


def test_provider_code_rejects_control_chars(monkeypatch):
    """provider_code with control chars or >128 chars is rejected."""
    import urllib.error, io
    body = json.dumps({"error": {"code": "bad\x00code"}}).encode()
    fake_exc = urllib.error.HTTPError(
        "https://api.heygen.com/v3/videos", 422, "Bad",
        {}, io.BytesIO(body))
    from lecturecast.heygen_http import _make_error_response
    err = _make_error_response(fake_exc)
    assert err.provider_code is None
