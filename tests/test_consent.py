"""HeyGen consent disclosure + identity model tests (§5.5e2a)."""

from __future__ import annotations

import hashlib

import pytest


from lecturecast.consent import (
    DisclosedAsset,
    HeyGenOperationIdentity,
    OPERATION_IDENTITY_NAMESPACE,
    PreparedOperation,
    ThirdPartyTransferDisclosure,
    canonical_json,
    is_digest,
    prepare_operation,
    sha256_digest,
)

D = "sha256:" + "a" * 64
BRIEF = "sha256:" + "b" * 64


def _identity(**over) -> HeyGenOperationIdentity:
    base = dict(
        operation_kind="video",
        endpoint="/v3/videos",
        generation_id="gen_1",
        manifest_digest=D,
        request_digest="sha256:" + "c" * 64,
        credential_profile_id="cp_1",
    )
    base.update(over)
    return HeyGenOperationIdentity(**base)


def _disclosure(**over) -> ThirdPartyTransferDisclosure:
    base = dict(
        provider="heygen",
        operation_kind="video",
        disclosure_version="heygen-transfer-2026-07-27",
        disclosed_assets=[DisclosedAsset(asset_kind="portrait_photo",
                                         display_filename="face.png", asset_digest=D)],
        data_categories=["portrait_image", "facial_biometric_template"],
        provider_cost_disclosure="HeyGen charges your own BYO account; AgentMesh360 does not pay HeyGen.",
        agentmesh_non_processor_disclosure="AgentMesh360 does not proxy, record, host, or bill HeyGen.",
    )
    base.update(over)
    return ThirdPartyTransferDisclosure(**base)


# --- canonical / digest helpers ---

def test_canonical_json_is_key_sorted_and_minimal():
    a = canonical_json({"b": 1, "a": 2})
    b = canonical_json({"a": 2, "b": 1})
    assert a == b == '{"a":2,"b":1}'


def test_is_digest_shape():
    assert is_digest(D)
    assert not is_digest("sha256:deadbeef")
    assert not is_digest("")
    assert not is_digest("md5:" + "a" * 32)


# --- DisclosedAsset ---

def test_asset_filename_strips_to_basename():
    a = DisclosedAsset(asset_kind="portrait_photo",
                      display_filename="/secret/path/face.png", asset_digest=D)
    assert a.display_filename == "face.png"


def test_asset_filename_rejects_empty_after_strip():
    with pytest.raises(ValueError):
        DisclosedAsset(asset_kind="portrait_photo", display_filename="///", asset_digest=D)


def test_asset_filename_strips_control_chars():
    a = DisclosedAsset(asset_kind="synthetic_narration_audio",
                      display_filename="voice\x00.wav", asset_digest=D)
    assert a.display_filename == "voice.wav"


def test_asset_digest_must_be_sha256():
    with pytest.raises(ValueError):
        DisclosedAsset(asset_kind="portrait_photo", display_filename="f.png",
                       asset_digest="not-a-digest")


def test_asset_rejects_extra_fields():
    # dataclasses reject unexpected fields (no real_path can sneak in).
    with pytest.raises(TypeError):
        DisclosedAsset(asset_kind="portrait_photo", display_filename="f.png",
                       asset_digest=D, real_path="/etc/passwd")


# --- ThirdPartyTransferDisclosure ---

def test_disclosure_accepts_valid():
    d = _disclosure()
    assert d.provider == "heygen"
    assert d.disclosure_version == "heygen-transfer-2026-07-27"


def test_disclosure_rejects_empty_assets():
    with pytest.raises(ValueError):
        _disclosure(disclosed_assets=[])


def test_disclosure_rejects_duplicate_assets():
    asset = DisclosedAsset(asset_kind="portrait_photo", display_filename="f.png", asset_digest=D)
    with pytest.raises(ValueError):
        _disclosure(disclosed_assets=[asset, asset])


def test_disclosure_rejects_empty_and_duplicate_categories():
    with pytest.raises(ValueError):
        _disclosure(data_categories=[])
    with pytest.raises(ValueError):
        _disclosure(data_categories=["portrait_image", "portrait_image"])


def test_disclosure_requires_cost_and_non_processor_text():
    with pytest.raises(ValueError):
        _disclosure(provider_cost_disclosure="   ")
    with pytest.raises(ValueError):
        _disclosure(agentmesh_non_processor_disclosure="")


def test_disclosure_rejects_unknown_data_category():
    with pytest.raises(ValueError):
        _disclosure(data_categories=["passport_number"])


# --- canonical_payload: deterministic + decision-bound ---

def test_payload_is_deterministic_regardless_of_input_order():
    d = _disclosure()
    a1 = DisclosedAsset(asset_kind="portrait_photo", display_filename="b.png", asset_digest=D)
    a2 = DisclosedAsset(asset_kind="synthetic_narration_audio", display_filename="a.wav", asset_digest=D)
    d_rev = _disclosure(disclosed_assets=[a1, a2])
    d_rev2 = _disclosure(disclosed_assets=[a2, a1])
    kw = dict(operation_id="op", generation_id="g", request_digest="sha256:" + "c" * 64,
              creative_brief_digest=BRIEF, decision="granted", decision_at="2026-07-29T00:00:00Z")
    assert d_rev.canonical_payload(**kw) == d_rev2.canonical_payload(**kw)
    # assets canonical-sorted alphabetically by kind (portrait_photo < synthetic_narration_audio)
    payload = d_rev.canonical_payload(**kw)
    assert payload["disclosed_assets"][0]["kind"] == "portrait_photo"
    assert payload["disclosed_assets"][1]["kind"] == "synthetic_narration_audio"


def test_payload_decision_changes_digest():
    d = _disclosure()
    kw = dict(operation_id="op", generation_id="g", request_digest="sha256:" + "c" * 64,
              creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z")
    g = sha256_digest(d.canonical_payload(decision="granted", **kw))
    dec = sha256_digest(d.canonical_payload(decision="declined", **kw))
    assert g != dec


def test_payload_decision_at_and_request_participate_in_digest():
    d = _disclosure()
    base = dict(operation_id="op", generation_id="g", creative_brief_digest=BRIEF,
                decision="granted")
    d1 = sha256_digest(d.canonical_payload(decision_at="2026-07-29T00:00:00Z",
                       request_digest="sha256:" + "c" * 64, **base))
    d2 = sha256_digest(d.canonical_payload(decision_at="2026-07-29T00:00:01Z",
                       request_digest="sha256:" + "c" * 64, **base))
    d3 = sha256_digest(d.canonical_payload(decision_at="2026-07-29T00:00:00Z",
                       request_digest="sha256:" + "d" * 64, **base))
    assert len({d1, d2, d3}) == 3


# --- HeyGenOperationIdentity + prepare_operation ---

def test_identity_rejects_bad_digests_and_empty_fields():
    with pytest.raises(ValueError):
        _identity(manifest_digest="not-a-digest")
    with pytest.raises(ValueError):
        _identity(request_digest="not-a-digest")
    with pytest.raises(ValueError):
        _identity(generation_id="")
    with pytest.raises(ValueError):
        _identity(credential_profile_id="  ")


def test_identity_payload_namespace_and_keys():
    ident = _identity()
    payload = ident.identity_payload()
    assert payload["namespace"] == OPERATION_IDENTITY_NAMESPACE
    assert "api_key" not in payload
    assert "timestamp" not in payload


def test_prepare_operation_is_deterministic():
    ident = _identity()
    p1 = prepare_operation(ident)
    p2 = prepare_operation(ident)
    assert p1 == p2
    assert p1.operation_id.startswith("lc_hg_")
    assert p1.idempotency_key.startswith("lc-hg-")
    assert p1.heygen_title == f"lecturecast:{p1.operation_id}"


def test_prepare_operation_changes_when_request_digest_changes():
    p1 = prepare_operation(_identity(request_digest="sha256:" + "c" * 64))
    p2 = prepare_operation(_identity(request_digest="sha256:" + "d" * 64))
    assert p1.operation_id != p2.operation_id
    assert p1.idempotency_key != p2.idempotency_key


def test_prepare_operation_changes_when_credential_profile_changes():
    p1 = prepare_operation(_identity(credential_profile_id="cp_1"))
    p2 = prepare_operation(_identity(credential_profile_id="cp_2"))
    assert p1.operation_id != p2.operation_id


def test_prepare_operation_independent_of_optional_fields_when_none():
    # orchestration_plan_digest / segment_id default None; both None ⇒ same id.
    p1 = prepare_operation(_identity())
    p2 = prepare_operation(_identity())
    assert p1 == p2
    # setting one diverges.
    p3 = prepare_operation(_identity(segment_id="seg_a"))
    assert p1.operation_id != p3.operation_id


def test_prepare_operation_returns_frozen_dataclass():
    p = prepare_operation(_identity())
    assert isinstance(p, PreparedOperation)
    with pytest.raises(Exception):
        p.operation_id = "tampered"  # type: ignore[misc]
