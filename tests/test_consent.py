"""HeyGen consent disclosure + identity model tests (§5.5e2a)."""

from __future__ import annotations

import unicodedata

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
REQ = "sha256:" + "c" * 64


def _identity(**over) -> HeyGenOperationIdentity:
    base = dict(
        operation_kind="video",
        generation_id="gen_1",
        manifest_digest=D,
        request_digest=REQ,
        credential_profile_id="heygen_env_default",
        orchestration_plan_digest=BRIEF,  # video operations must bind the M3 plan
        endpoint="/v3/videos",
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


def _payload_kwargs(**over):
    base = dict(
        operation_id="lc_hg_op1",
        generation_id="gen_1",
        request_digest=REQ,
        creative_brief_digest=BRIEF,
        decision="granted",
        decision_at="2026-07-29T00:00:00Z",
    )
    base.update(over)
    return base


# --- canonical / digest helpers ---

def test_canonical_json_is_key_sorted_and_minimal():
    a = canonical_json({"b": 1, "a": 2})
    b = canonical_json({"a": 2, "b": 1})
    assert a == b == '{"a":2,"b":1}'


def test_canonical_json_nfc_normalizes_strings():
    nfd = unicodedata.normalize("NFD", "café.png")
    nfc = unicodedata.normalize("NFC", "café.png")
    assert canonical_json({"f": nfd}) == canonical_json({"f": nfc})


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


def test_asset_filename_nfc_normalizes():
    nfd = unicodedata.normalize("NFD", "café.png")
    a = DisclosedAsset(asset_kind="portrait_photo", display_filename=nfd, asset_digest=D)
    assert a.display_filename == unicodedata.normalize("NFC", "café.png")


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
    assert d.disclosed_assets == tuple(d.disclosed_assets)  # frozen to tuple


def test_disclosure_rejects_empty_assets():
    with pytest.raises(ValueError):
        _disclosure(disclosed_assets=[])


def test_disclosure_rejects_duplicate_assets():
    asset = DisclosedAsset(asset_kind="portrait_photo", display_filename="f.png", asset_digest=D)
    with pytest.raises(ValueError):
        _disclosure(disclosed_assets=[asset, asset])


def test_disclosure_categories_must_match_assets():
    # portrait_photo without facial_biometric_template → reject.
    with pytest.raises(ValueError):
        _disclosure(data_categories=["portrait_image"])
    # over-report a category with no matching asset → reject.
    with pytest.raises(ValueError):
        _disclosure(data_categories=["portrait_image", "facial_biometric_template",
                                     "synthetic_narration_audio"])


def test_disclosure_synthetic_audio_requires_its_category():
    asset = DisclosedAsset(asset_kind="synthetic_narration_audio", display_filename="v.wav", asset_digest=D)
    with pytest.raises(ValueError):
        _disclosure(disclosed_assets=[asset], data_categories=["portrait_image"])
    d = _disclosure(disclosed_assets=[asset], data_categories=["synthetic_narration_audio"])
    assert set(d.data_categories) == {"synthetic_narration_audio"}


def test_disclosure_rejects_duplicate_categories():
    # Use the full asset-determined set WITH a duplicate so this fails on the
    # uniqueness guard specifically — without it, the cross-field set==allowed
    # check would still pass and the regression would slip through.
    with pytest.raises(ValueError, match="unique"):
        _disclosure(data_categories=["portrait_image", "portrait_image",
                                     "facial_biometric_template"])


def test_disclosure_requires_cost_and_non_processor_text():
    with pytest.raises(ValueError):
        _disclosure(provider_cost_disclosure="   ")
    with pytest.raises(ValueError):
        _disclosure(agentmesh_non_processor_disclosure="")


def test_disclosure_closes_operation_kind():
    with pytest.raises(ValueError):
        _disclosure(operation_kind="bogus")


# --- canonical_payload: deterministic + decision-bound + validated ---

def test_payload_is_deterministic_regardless_of_input_order():
    a1 = DisclosedAsset(asset_kind="portrait_photo", display_filename="b.png", asset_digest=D)
    a2 = DisclosedAsset(asset_kind="synthetic_narration_audio", display_filename="a.wav", asset_digest=D)
    cats = ["portrait_image", "facial_biometric_template", "synthetic_narration_audio"]
    d_rev = _disclosure(disclosed_assets=[a1, a2], data_categories=cats)
    d_rev2 = _disclosure(disclosed_assets=[a2, a1], data_categories=cats)
    assert d_rev.canonical_payload(**_payload_kwargs()) == d_rev2.canonical_payload(**_payload_kwargs())


def test_payload_decision_changes_digest():
    d = _disclosure()
    kw = _payload_kwargs()
    kw.pop("decision")
    g = sha256_digest(d.canonical_payload(decision="granted", **kw))
    dec = sha256_digest(d.canonical_payload(decision="declined", **kw))
    assert g != dec


def test_payload_decision_at_and_request_participate_in_digest():
    d = _disclosure()
    base = dict(operation_id="op", generation_id="g", creative_brief_digest=BRIEF, decision="granted")
    d1 = sha256_digest(d.canonical_payload(decision_at="2026-07-29T00:00:00Z", request_digest=REQ, **base))
    d2 = sha256_digest(d.canonical_payload(decision_at="2026-07-29T00:00:01Z", request_digest=REQ, **base))
    d3 = sha256_digest(d.canonical_payload(decision_at="2026-07-29T00:00:00Z",
                       request_digest="sha256:" + "d" * 64, **base))
    assert len({d1, d2, d3}) == 3


def test_payload_rejects_bad_inputs():
    d = _disclosure()
    with pytest.raises(ValueError):
        d.canonical_payload(**_payload_kwargs(decision="maybe"))
    with pytest.raises(ValueError):
        d.canonical_payload(**_payload_kwargs(operation_id="  "))
    with pytest.raises(ValueError):
        d.canonical_payload(**_payload_kwargs(request_digest="not-a-digest"))
    with pytest.raises(ValueError):
        d.canonical_payload(**_payload_kwargs(creative_brief_digest="not-a-digest"))
    with pytest.raises(ValueError):
        d.canonical_payload(**_payload_kwargs(decision_at="2026-07-29T00:00:00"))  # naive
    with pytest.raises(ValueError):
        d.canonical_payload(**_payload_kwargs(decision_at="not-a-time"))


def test_payload_canonicalizes_decision_at_to_utc_seconds():
    d = _disclosure()
    # +08:00 offset → 00:00:00Z ; fractional seconds dropped.
    p = d.canonical_payload(**_payload_kwargs(decision_at="2026-07-29T08:00:00.250+08:00"))
    assert p["decision_at"] == "2026-07-29T00:00:00Z"


# --- HeyGenOperationIdentity + prepare_operation ---

def test_identity_rejects_bad_digests_and_empty_fields():
    with pytest.raises(ValueError):
        _identity(manifest_digest="not-a-digest")
    with pytest.raises(ValueError):
        _identity(request_digest="not-a-digest")
    with pytest.raises(ValueError):
        _identity(generation_id="")


def test_identity_rejects_arbitrary_credential_profile():
    # An API key or account name cannot sneak into credential_profile_id and
    # reach the DB — only the closed internal identifier is accepted.
    with pytest.raises(ValueError):
        _identity(credential_profile_id="sk_live_XYc8RpGp9mW6WtwQZFn0DyoOsNqoaV5Y")
    with pytest.raises(ValueError):
        _identity(credential_profile_id="cp_1")
    assert _identity().credential_profile_id == "heygen_env_default"


def test_video_operation_requires_orchestration_plan():
    with pytest.raises(ValueError):
        _identity(orchestration_plan_digest=None)


def test_identity_closes_operation_kind_and_endpoint():
    with pytest.raises(ValueError):
        _identity(operation_kind="bogus")
    with pytest.raises(ValueError):
        _identity(endpoint="/v3/widgets")  # not in closed set
    with pytest.raises(ValueError):
        _identity(endpoint="/v3/videos?x=1")  # query rejected
    with pytest.raises(ValueError):
        _identity(endpoint="/v3/videos#frag")  # fragment rejected
    with pytest.raises(ValueError):
        _identity(operation_kind="video", endpoint="/v3/assets")  # inconsistent with kind


def test_identity_validates_optional_fields():
    with pytest.raises(ValueError):
        _identity(orchestration_plan_digest="not-a-digest")
    with pytest.raises(ValueError):
        _identity(segment_id="bad space")
    with pytest.raises(ValueError):
        _identity(segment_id="   ")
    # valid optional fields accepted.
    ident = _identity(orchestration_plan_digest=BRIEF, segment_id="seg_01")
    assert ident.orchestration_plan_digest == BRIEF
    assert ident.segment_id == "seg_01"


def test_identity_payload_namespace_and_no_secret_keys():
    payload = _identity().identity_payload()
    assert payload["namespace"] == OPERATION_IDENTITY_NAMESPACE
    assert "api_key" not in payload
    assert "timestamp" not in payload


def test_prepare_operation_is_deterministic():
    ident = _identity()
    p1 = prepare_operation(ident)
    p2 = prepare_operation(ident)
    assert p1 == p2
    assert p1.operation_id.startswith("lc_hg_")
    # 128-bit (32 hex) after the prefix — no truncation-collision risk.
    assert len(p1.operation_id) == len("lc_hg_") + 32
    assert p1.idempotency_key.startswith("lc-hg-")
    assert p1.heygen_title == f"lecturecast:{p1.operation_id}"


def test_prepare_operation_changes_when_request_digest_changes():
    p1 = prepare_operation(_identity(request_digest=REQ))
    p2 = prepare_operation(_identity(request_digest="sha256:" + "d" * 64))
    assert p1.operation_id != p2.operation_id
    assert p1.idempotency_key != p2.idempotency_key


def test_prepare_operation_changes_when_credential_profile_changes():
    # credential_profile_id is closed (single v1 value), so it is NOT a
    # distinguishing dimension — operations are distinguished by request/plan.
    assert _identity().credential_profile_id == "heygen_env_default"


def test_prepare_operation_independent_of_optional_fields_when_none():
    p1 = prepare_operation(_identity())
    p2 = prepare_operation(_identity())
    assert p1 == p2
    p3 = prepare_operation(_identity(segment_id="seg_a"))
    assert p1.operation_id != p3.operation_id


def test_prepare_operation_returns_frozen_dataclass():
    p = prepare_operation(_identity())
    assert isinstance(p, PreparedOperation)
    with pytest.raises(Exception):
        p.operation_id = "tampered"  # type: ignore[misc]
