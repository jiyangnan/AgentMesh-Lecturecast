"""HeyGen asset GET / DELETE adapter tests (§5.5e5b0c3a).

Covers get_asset (existence + id/type, 404→absent, no digest) and delete_asset
(404→already_absent, 200 verifies data.id, separate parser from video delete),
plus the per-operation error matrix.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from lecturecast.heygen_asset_adapter import (
    HeyGenAssetAdapter, AssetProbeResult, AssetDeleteResult, AssetReadError,
)
from lecturecast.heygen_http import HeyGenHttpTransport, HttpErrorResponse, HttpTransportError


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
    def __init__(self, responder):
        self._responder = responder
        self.calls: list[dict] = []
    def open(self, req, timeout=None):
        parsed = urlparse(req.full_url)
        self.calls.append({"path": parsed.path, "method": req.get_method()})
        result = self._responder()
        if isinstance(result, Exception):
            raise result
        status, raw = result
        return _FakeResp(status, raw)


def _transport(responder):
    opener = _FakeOpener(responder)
    t = HeyGenHttpTransport(api_key_provider=lambda: "k", opener_factory=lambda: opener)
    return t, opener


def _single(body=None, status=200):
    raw = json.dumps(body or {}).encode() if body is not None else b""
    return lambda: (status, raw)


# === get_asset =============================================================

def test_get_asset_exists():
    t, opener = _transport(_single({"data": {"id": "asset_1", "type": "image"}}))
    r = HeyGenAssetAdapter(t).get_asset("asset_1")
    assert r.exists is True
    assert r.asset_id == "asset_1"
    assert r.asset_type == "image"
    assert opener.calls[0]["method"] == "GET"


def test_get_asset_404_is_absent():
    t, _ = _transport(lambda: HttpErrorResponse(404, {}, {}, "not_found"))
    r = HeyGenAssetAdapter(t).get_asset("asset_1")
    assert r.exists is False


def test_get_asset_id_mismatch_malformed():
    t, _ = _transport(_single({"data": {"id": "asset_other", "type": "image"}}))
    with pytest.raises(AssetReadError, match="does not match"):
        HeyGenAssetAdapter(t).get_asset("asset_1")


def test_get_asset_missing_type_malformed():
    t, _ = _transport(_single({"data": {"id": "asset_1"}}))
    with pytest.raises(AssetReadError, match="missing type"):
        HeyGenAssetAdapter(t).get_asset("asset_1")


def test_get_asset_rejects_bad_id():
    t, _ = _transport(_single({"data": {"id": "x", "type": "image"}}))
    with pytest.raises(ValueError):
        HeyGenAssetAdapter(t).get_asset("../etc/passwd")


def test_get_asset_5xx_retryable():
    t, _ = _transport(lambda: HttpErrorResponse(500, {}, {}, "boom"))
    with pytest.raises(AssetReadError) as exc:
        HeyGenAssetAdapter(t).get_asset("asset_1")
    assert exc.value.retryable is True


# === delete_asset ==========================================================

def test_delete_asset_success_verifies_echoed_id():
    t, opener = _transport(_single({"data": {"id": "asset_1"}}))
    r = HeyGenAssetAdapter(t).delete_asset("asset_1")
    assert r.status == "deleted"
    assert opener.calls[0]["method"] == "DELETE"


def test_delete_asset_404_already_absent():
    t, _ = _transport(lambda: HttpErrorResponse(404, {}, {}, "not_found"))
    assert HeyGenAssetAdapter(t).delete_asset("asset_1").status == "already_absent"


def test_delete_asset_id_mismatch_malformed():
    # NOT the video 'data.deleted is True' parser — asset delete echoes data.id.
    t, _ = _transport(_single({"data": {"id": "asset_other"}}))
    with pytest.raises(AssetReadError, match="does not match"):
        HeyGenAssetAdapter(t).delete_asset("asset_1")


def test_delete_asset_no_data_malformed():
    t, _ = _transport(_single({"data": [1, 2]}))
    with pytest.raises(AssetReadError, match="not a dict"):
        HeyGenAssetAdapter(t).delete_asset("asset_1")


def test_delete_asset_rejects_bad_id():
    t, _ = _transport(_single({"data": {"id": "x"}}))
    with pytest.raises(ValueError):
        HeyGenAssetAdapter(t).delete_asset("../x")


@pytest.mark.parametrize("status,code,retryable", [
    (401, "auth_failed", False),
    (403, "auth_failed", False),
    (429, "rate_limited", True),
    (500, "provider_server_error", True),
    (503, "provider_server_error", True),
    (400, "validation_error", False),
])
def test_delete_asset_error_matrix(status, code, retryable):
    t, _ = _transport(lambda s=status: HttpErrorResponse(s, {}, {}, "p"))
    with pytest.raises(AssetReadError) as exc:
        HeyGenAssetAdapter(t).delete_asset("asset_1")
    assert exc.value.code == code
    assert exc.value.retryable is retryable


def test_delete_asset_transport_timeout_retryable():
    t, _ = _transport(lambda: HttpTransportError(code="network_timeout", message="x"))
    with pytest.raises(AssetReadError) as exc:
        HeyGenAssetAdapter(t).delete_asset("asset_1")
    assert exc.value.retryable is True


def test_delete_asset_transport_auth_not_retryable():
    t, _ = _transport(lambda: HttpTransportError(code="auth_failed", message="x"))
    with pytest.raises(AssetReadError) as exc:
        HeyGenAssetAdapter(t).delete_asset("asset_1")
    assert exc.value.code == "auth_failed"
    assert exc.value.retryable is False
