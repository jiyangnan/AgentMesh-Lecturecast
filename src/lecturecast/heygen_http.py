"""HeyGen HTTP transport — injectable urllib-based JSON + multipart client (§5.5e5b0a).

A single HTTP boundary for all HeyGen API calls. Never logs keys, bodies, or
URLs. Blocks redirects and environment proxies. API key is read fresh on every
call via a provider callable (never cached).

Per Codex e5b plan:
- base URL HTTPS-only, no userinfo/query/fragment, host = api.heygen.com
- path is relative /v3/... only (no full-URL injection)
- Idempotency-Key validated 1–255 chars
- response streamed with 1 MiB + 1 cap (even error bodies)
- headers whitelisted: only Retry-After, request-id, Content-Type returned
- structured transport errors (network/timeout/malformed) raised here;
  endpoint-specific status mapping stays in the adapter
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin

_DEFAULT_BASE_URL = "https://api.heygen.com"
_RESPONSE_MAX = 1_048_576 + 1  # 1 MiB + 1 to detect overflow
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._\-:]{1,255}$")
_PATH_RE = re.compile(r"^/v3/(?:[A-Za-z0-9._\-]+/)*[A-Za-z0-9._\-]*$")
_ALLOWED_HEADERS = frozenset({
    "retry-after", "x-request-id", "request-id", "content-type",
})


class HttpTransportError(Exception):
    """Base for transport-level failures (network, timeout, malformed)."""
    def __init__(self, *, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


class HttpResponse:
    """A successful HTTP response with a parsed JSON body."""
    __slots__ = ("status", "body", "headers")

    def __init__(self, status: int, body: dict, headers: dict):
        self.status = status
        self.body = body
        self.headers = headers


class HttpErrorResponse(Exception):
    """A non-2xx HTTP response with status + sanitized headers."""
    __slots__ = ("status", "body", "headers", "provider_code")

    def __init__(self, status: int, body: dict | None, headers: dict,
                 provider_code: str | None = None):
        self.status = status
        self.body = body or {}
        self.headers = headers
        self.provider_code = provider_code


class HeyGenHttpTransport:
    """HTTP transport for HeyGen API calls. Injectable for testing."""

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        api_key_provider: Callable[[], str] | None = None,
        timeout: int = 30,
        opener_factory: Callable[[], object] | None = None,
    ) -> None:
        parsed = _validate_base_url(base_url)
        self._base_url = parsed
        self._api_key_provider = api_key_provider or _default_key_provider
        self._timeout = timeout
        self._opener_factory = opener_factory or self._default_opener

    @staticmethod
    def _default_opener():
        return urllib_request.build_opener(
            urllib_request.ProxyHandler({}), _NoRedirectHandler())

    def request_json(
        self,
        *,
        method: str,
        path: str,
        json_body: dict | None = None,
        idempotency_key: str | None = None,
        params: dict[str, str] | None = None,
    ) -> HttpResponse:
        """Send a JSON request and return HttpResponse on 2xx, or raise
        HttpErrorResponse on non-2xx (caller catches and maps to adapter error)."""
        if method not in ("GET", "POST", "DELETE"):
            raise ValueError(f"method must be GET/POST/DELETE: {method!r}")
        if type(self._timeout) is not int or self._timeout <= 0:
            raise ValueError("timeout must be a positive int")
        _validate_path(path)
        if idempotency_key is not None and not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise ValueError(f"invalid Idempotency-Key: {idempotency_key!r}")
        api_key = self._api_key_provider()
        if not isinstance(api_key, str) or not api_key.strip():
            raise HttpTransportError(code="auth_failed", message="API key is blank or missing")

        url = self._base_url + path
        if params:
            from urllib.parse import urlencode
            url += "?" + urlencode(params)

        body_bytes = b""
        headers: dict[str, str] = {
            "X-Api-Key": api_key,
            "Accept": "application/json",
        }
        if json_body is not None:
            body_bytes = json.dumps(json_body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key

        req = urllib_request.Request(url, data=body_bytes or None, method=method,
                                     headers=headers)
        try:
            opener = self._opener_factory()
            resp = opener.open(req, timeout=self._timeout)
        except HTTPError as exc:
            raise _make_error_response(exc) from None
        except URLError as exc:
            reason = str(exc.reason).lower()
            if "timed out" in reason:
                raise HttpTransportError(code="network_timeout", message=str(exc.reason)) from exc
            raise HttpTransportError(code="connection_error", message=str(exc.reason)) from exc
        except OSError as exc:
            raise HttpTransportError(code="connection_error", message=str(exc)) from exc

        try:
            raw = _stream_read(resp, _RESPONSE_MAX)
            if len(raw) >= _RESPONSE_MAX:
                raise HttpTransportError(code="malformed_response", message="response exceeded 1 MiB")
            if not raw:
                raise HttpTransportError(code="malformed_response", message="empty response body")
            parsed_body = json.loads(raw)
            if not isinstance(parsed_body, dict):
                raise HttpTransportError(code="malformed_response", message="response is not a JSON object")
        except json.JSONDecodeError as exc:
            raise HttpTransportError(code="malformed_response", message="response is not valid JSON") from exc
        finally:
            resp.close()

        filtered = _filter_headers(resp.headers)
        return HttpResponse(resp.status, parsed_body, filtered)

    def request_multipart_file(
        self,
        *,
        path: str,
        fileobj,
        filename: str,
        content_type: str,
        file_size: int,
        idempotency_key: str,
    ) -> HttpResponse:
        """Upload a single file via multipart/form-data. Uses a deterministic
        boundary derived from the idempotency key. The file is streamed in
        chunks — never fully buffered. Only the 'file' field is sent."""
        import hashlib as _h
        import io as _io
        _validate_path(path)
        if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise ValueError(f"invalid Idempotency-Key: {idempotency_key!r}")
        if type(file_size) is not int or file_size <= 0 or file_size > 33_554_432:
            raise ValueError("file_size must be a positive int ≤ 32 MiB")
        # Sanitize filename: basename only, no path separators/control chars/quotes.
        safe_name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        safe_name = re.sub(r"[\x00-\x1f\"'\\/]", "", safe_name).strip()
        if not safe_name:
            raise ValueError("filename must be a non-empty basename")
        api_key = self._api_key_provider()
        if not isinstance(api_key, str) or not api_key.strip():
            raise HttpTransportError(code="auth_failed", message="API key is blank or missing")

        boundary = "----lec" + _h.sha256(idempotency_key.encode()).hexdigest()[:32]
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        suffix = f"\r\n--{boundary}--\r\n".encode("utf-8")
        total_length = len(prefix) + file_size + len(suffix)

        url = self._base_url + path
        headers = {
            "X-Api-Key": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(total_length),
            "Idempotency-Key": idempotency_key,
        }

        # Build a streaming body iterator
        def body_iter():
            yield prefix
            remaining = file_size
            while remaining > 0:
                chunk = fileobj.read(min(65536, remaining))
                if not chunk:
                    raise HttpTransportError(code="connection_error", message="file ended prematurely")
                remaining -= len(chunk)
                yield chunk
            yield suffix

        body_bytes = b"".join(body_iter())
        req = urllib_request.Request(url, data=body_bytes, method="POST", headers=headers)
        try:
            opener = self._opener_factory()
            resp = opener.open(req, timeout=self._timeout)
        except HTTPError as exc:
            raise _make_error_response(exc) from None
        except URLError as exc:
            reason = str(exc.reason).lower()
            if "timed out" in reason:
                raise HttpTransportError(code="network_timeout", message=str(exc.reason)) from exc
            raise HttpTransportError(code="connection_error", message=str(exc.reason)) from exc
        except OSError as exc:
            raise HttpTransportError(code="connection_error", message=str(exc)) from exc

        try:
            raw = _stream_read(resp, _RESPONSE_MAX)
            if len(raw) >= _RESPONSE_MAX:
                raise HttpTransportError(code="malformed_response", message="response exceeded 1 MiB")
            if not raw:
                raise HttpTransportError(code="malformed_response", message="empty response body")
            parsed_body = json.loads(raw)
            if not isinstance(parsed_body, dict):
                raise HttpTransportError(code="malformed_response", message="response is not a JSON object")
        except json.JSONDecodeError as exc:
            raise HttpTransportError(code="malformed_response", message="response is not valid JSON") from exc
        finally:
            resp.close()
        filtered = _filter_headers(resp.headers)
        return HttpResponse(resp.status, parsed_body, filtered)


# --- helpers ---------------------------------------------------------------

def _default_key_provider() -> str:
    return os.environ.get("HEYGEN_API_KEY", "")


def _validate_base_url(url: str) -> str:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"base_url must be HTTPS: {url}")
    if parsed.username or parsed.password:
        raise ValueError("base_url must not contain userinfo")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain query or fragment")
    if parsed.path and parsed.path != "/":
        raise ValueError("base_url must not contain a path")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("base_url missing host")
    if host != "api.heygen.com":
        raise ValueError(f"base_url host must be api.heygen.com, got: {host}")
    if parsed.port is not None and parsed.port != 443:
        raise ValueError("base_url port must be 443 or omitted")
    return url.rstrip("/")


def _validate_path(path: str) -> None:
    if ".." in path or not _PATH_RE.fullmatch(path):
        raise ValueError(f"invalid API path: {path!r}")
    # Reject empty/dot/dotdot in any segment
    for seg in path.split("/")[1:]:  # skip leading empty (from /)
        if seg in ("", ".", ".."):
            raise ValueError(f"invalid API path segment: {path!r}")


def _stream_read(resp, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total >= max_bytes:
            chunks.append(chunk)
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _filter_headers(headers) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in headers.keys():
        kl = key.lower()
        if kl in _ALLOWED_HEADERS:
            result[kl] = headers[key]
    return result


def _make_error_response(exc: HTTPError) -> HttpErrorResponse:
    raw = b""
    try:
        raw = exc.read(_RESPONSE_MAX + 1)
    except Exception:
        pass
    finally:
        try:
            exc.close()
        except Exception:
            pass
    body: dict | None = None
    provider_code = None
    # Reject oversized error bodies — don't parse truncated JSON.
    if len(raw) <= _RESPONSE_MAX:
        if raw:
            try:
                body = json.loads(raw)
                if not isinstance(body, dict):
                    body = None
            except json.JSONDecodeError:
                body = None
        if body and isinstance(body.get("error"), dict):
            raw_code = body["error"].get("code", "")
            if isinstance(raw_code, str):
                cleaned = raw_code.strip()
                if cleaned and re.fullmatch(r"[A-Za-z0-9._\-]{1,128}", cleaned):
                    provider_code = cleaned
    filtered = _filter_headers(exc.headers)
    return HttpErrorResponse(exc.code, body, filtered, provider_code)


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    """Refuse all redirects."""
    def redirect_request(self, *args, **kwargs):
        raise HttpTransportError(code="connection_error", message="redirect refused")
