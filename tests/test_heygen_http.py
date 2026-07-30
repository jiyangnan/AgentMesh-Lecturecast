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
