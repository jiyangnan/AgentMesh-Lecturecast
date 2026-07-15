from __future__ import annotations

import base64
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from jsonschema import Draft202012Validator

from lecturecast.assets import materialize_manifest_assets
from lecturecast.capabilities import capture_capabilities, load_component_catalog
from lecturecast.errors import LectureCastError
from lecturecast.manifest import PublicKeyRing, SigningKey
from lecturecast.preflight import run_preflight
from lecturecast.protocol import manifest_signing_bytes


ROOT = Path(__file__).parents[1]
FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _signed(payload: dict) -> tuple[dict, PublicKeyRing]:
    private_key = Ed25519PrivateKey.generate()
    public_key = base64.b64encode(
        private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    document = copy.deepcopy(payload)
    document["signature"]["key_id"] = "component_test_key_v1"
    document["signature"]["value"] = ""
    document["signature"]["value"] = base64.b64encode(
        private_key.sign(manifest_signing_bytes(document))
    ).decode()
    created = datetime.fromisoformat(document["created_at"].replace("Z", "+00:00"))
    keyring = PublicKeyRing(
        [
            SigningKey(
                key_id="component_test_key_v1",
                algorithm="Ed25519",
                public_key=public_key,
                status="current",
                not_before=(created - timedelta(days=1)).astimezone(UTC).isoformat(),
                not_after=(created + timedelta(days=1)).astimezone(UTC).isoformat(),
            )
        ]
    )
    return document, keyring


def test_catalog_is_locked_and_identical_for_python_and_remotion() -> None:
    catalog, digest = load_component_catalog()
    remotion_catalog = json.loads(
        (ROOT / "templates/remotion/src/director/component-catalog.json").read_text(
            encoding="utf-8"
        )
    )

    assert catalog == remotion_catalog
    assert len(catalog["components"]) == 11
    assert digest == "sha256:b492db83fd70a307520ef0bb123bb455682620b1a556e05d303850f8ffd11a76"
    assert len({item["component_id"] for item in catalog["components"]}) == 11


def test_default_capabilities_publish_the_exact_registry() -> None:
    catalog, digest = load_component_catalog()
    capabilities = capture_capabilities(repo_root=ROOT)
    payload = capabilities.model_dump()

    assert payload["component_catalog_digest"] == digest
    assert payload["components"] == sorted(item["component_id"] for item in catalog["components"])


def test_every_component_has_a_valid_cross_aspect_preview_fixture() -> None:
    catalog, _ = load_component_catalog()
    for component in catalog["components"]:
        assert component["supported_aspect_ratios"] == ["16:9", "9:16"]
        Draft202012Validator(component["props_schema"]).validate(
            component["preview_fixture"]
        )


@pytest.mark.parametrize("failure", ["props", "asset"])
def test_preflight_enforces_component_props_and_asset_contracts(failure: str) -> None:
    manifest = _fixture("production-manifest-v1.json")
    if failure == "props":
        manifest["scenes"][0]["props"]["headline"] = "x" * 65
    else:
        manifest["scenes"][1]["assets"] = []
    signed, keyring = _signed(manifest)

    with pytest.raises(LectureCastError) as captured:
        run_preflight(signed, _fixture("client-capabilities-v1.json"), keyring=keyring)

    assert captured.value.code == "manifest_incompatible"
    assert "component_contracts" in captured.value.message


def test_renderer_default_manifest_tracks_protocol_fixture_exactly() -> None:
    renderer_fixture = (
        ROOT / "templates/remotion/src/director/fixtures/production-manifest-v1.json"
    ).read_bytes()
    assert renderer_fixture == (FIXTURE_DIR / "production-manifest-v1.json").read_bytes()


def test_required_local_asset_must_exist_and_is_materialized(tmp_path: Path) -> None:
    manifest = _fixture("production-manifest-v1.json")
    signed, keyring = _signed(manifest)
    capabilities = _fixture("client-capabilities-v1.json")

    with pytest.raises(LectureCastError, match="local_assets"):
        run_preflight(signed, capabilities, keyring=keyring, project_root=tmp_path)

    source = tmp_path / ".lecturecast/assets/screen/home.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"local-image")
    result = run_preflight(signed, capabilities, keyring=keyring, project_root=tmp_path)
    assert result.passed

    public_root = tmp_path / "remotion-public"
    rendered = materialize_manifest_assets(
        manifest, project_root=tmp_path, public_root=public_root
    )
    rendered_uri = rendered["scenes"][1]["assets"][0]["uri"]
    assert rendered_uri == f"director/assets/{manifest['manifest_id']}/screen/home.png"
    assert (public_root / rendered_uri).read_bytes() == b"local-image"
