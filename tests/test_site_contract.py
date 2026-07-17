from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "validate_site.py"


def _validate(
    site: Path,
    *,
    contract: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    command = [sys.executable, str(SCRIPT), str(site), "--json"]
    if contract is not None:
        command.extend(("--contract", contract))
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    return result, json.loads(result.stdout)


def _page(
    *,
    body: str = '<main id="main"></main><a href="#main">Main</a>',
    jsonld: str | None = None,
) -> str:
    payload = jsonld or json.dumps({"@context": "https://schema.org"})
    return (
        '<!doctype html><html lang="en"><head><title>LectureCast</title>'
        f'<script type="application/ld+json">{payload}</script>'
        f"</head><body>{body}</body></html>"
    )


def _write_base_site(root: Path, *, body: str | None = None) -> None:
    root.mkdir()
    (root / "index.html").write_text(_page(body=body or '<main id="main"></main>'))
    (root / "llms.txt").write_text("LectureCast")


def test_validate_site_accepts_offline_static_structure(tmp_path: Path) -> None:
    site = tmp_path / "site"
    _write_base_site(
        site,
        body=(
            '<main id="main"></main><a href="#main">Main</a>'
            '<a href="/llms.txt">LLMs</a>'
        ),
    )

    process, result = _validate(site)

    assert process.returncode == 0
    assert result["ok"] is True
    assert result["files_checked"] == 1
    assert result["errors"] == []


def test_validate_site_rejects_invalid_jsonld_duplicate_ids_and_missing_targets(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    _write_base_site(
        site,
        body=(
            '<main id="same"></main><aside id="same"></aside>'
            '<a href="#missing">Missing anchor</a>'
            '<img src="/missing.png">'
        ),
    )
    (site / "index.html").write_text(
        _page(
            body=(
                '<main id="same"></main><aside id="same"></aside>'
                '<a href="#missing">Missing anchor</a>'
                '<img src="/missing.png">'
            ),
            jsonld="{broken",
        )
    )

    process, result = _validate(site)

    assert process.returncode == 1
    errors = result["errors"]
    assert isinstance(errors, list)
    assert any("duplicate id" in error for error in errors)
    assert any("anchor" in error for error in errors)
    assert any("does not exist" in error for error in errors)
    assert any("JSON-LD" in error for error in errors)


def test_validate_site_rejects_path_escape(tmp_path: Path) -> None:
    site = tmp_path / "site"
    _write_base_site(site, body='<a href="../outside.txt">Outside</a>')

    process, result = _validate(site)

    assert process.returncode == 1
    errors = result["errors"]
    assert isinstance(errors, list)
    assert any("escapes site root" in error for error in errors)


def _contract_page(*, director_access: str = "staged") -> str:
    return _page(
        body=(
            '<section data-product-contract="community-director-v1">'
            '<article data-route="community" data-access="available" '
            'data-media="local">Community</article>'
            f'<article data-route="director" data-access="{director_access}" '
            'data-media="local">Director ProductionManifest</article>'
            "</section>"
        )
    )


def _write_contract_site(root: Path, *, director_access: str = "staged") -> None:
    for relative in ("index.html", "en/index.html", "ja/index.html", "ko/index.html"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_contract_page(director_access=director_access))
    (root / "llms.txt").write_text(
        "Community Director ProductionManifest not yet generally open"
    )


def test_community_director_contract_accepts_staged_local_media_routes(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    _write_contract_site(site)

    process, result = _validate(site, contract="community-director")

    assert process.returncode == 0
    assert result["ok"] is True
    assert result["files_checked"] == 4


def test_community_director_contract_rejects_director_marked_available(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    _write_contract_site(site, director_access="available")

    process, result = _validate(site, contract="community-director")

    assert process.returncode == 1
    errors = result["errors"]
    assert isinstance(errors, list)
    assert sum("director route must remain staged" in error for error in errors) == 4


def test_community_director_contract_requires_machine_readable_boundary(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    _write_contract_site(site)
    (site / "llms.txt").write_text("Community only")

    process, result = _validate(site, contract="community-director")

    assert process.returncode == 1
    errors = result["errors"]
    assert isinstance(errors, list)
    assert any("llms.txt" in error for error in errors)


def test_production_hosting_stays_behind_agentmesh_caddy() -> None:
    boundary = (ROOT / "docs/LECTURECAST-SYSTEM-BOUNDARY.md").read_text()

    assert not (ROOT / ".github/workflows/pages.yml").exists()
    assert not (ROOT / "site/CNAME").exists()
    assert "jobagent-caddy" in boundary
    assert "agentmesh-core" in boundary
    assert "GitHub Pages is not a production origin" in boundary
    assert (ROOT / ".github/workflows/site-contract.yml").exists()
