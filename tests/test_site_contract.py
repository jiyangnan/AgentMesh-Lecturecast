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


def test_production_site_links_and_indexes_course_video_guides() -> None:
    routes = (
        "/guides/ai-course-video-generator/",
        "/guides/course-video-for-bilibili-xiaohongshu/",
    )
    landing = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    sitemap = (ROOT / "site" / "sitemap.xml").read_text(encoding="utf-8")

    for route in routes:
        source = (
            ROOT / "site" / route.strip("/") / "index.html"
        ).read_text(encoding="utf-8")
        canonical = f"https://lecturecast.agentmesh360.com{route}"
        assert f'href="{route}"' in landing
        assert f'rel="canonical" href="{canonical}"' in source
        assert 'type="application/ld+json"' in source
        assert f"<loc>{canonical}</loc>" in sitemap


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


def _contract_page(
    *,
    director_access: str = "paid",
    extra_route: bool = False,
    localized_tokens: tuple[str, ...] = (),
) -> str:
    legacy = (
        '<article data-route="legacy" data-access="available">Legacy</article>'
        if extra_route
        else ""
    )
    return _page(
        body=(
            '<section data-product-contract="commercial-only-v1">'
            f'<article data-route="director" data-access="{director_access}" '
            'data-media="local">Director ProductionManifest</article>'
            f"{legacy}</section>"
            '<div class="routes" style="grid-template-columns:1fr"></div>'
            "<p>10 credits; Microsoft Edge; MiniMax; 1920×1080 MP4 and PNG; "
            "1080×1920 MP4 and PNG; complete signed narration; "
            "three independent human gates</p>"
            '<pre class="code">lecturecast onboard --adapter codex '
            "--host-contract 1.0.0 --json\n"
            "workflow.next_action\n"
            "lecturecast auth login</pre>"
            f"<p>{' '.join(localized_tokens)}</p>"
        )
    )


def _write_contract_site(
    root: Path,
    *,
    director_access: str = "paid",
    extra_route: bool = False,
) -> None:
    localized_tokens = {
        "index.html": ("有效月卡", "完整签名讲稿", "三次独立人工"),
        "en/index.html": (
            "active monthly pass",
            "complete signed narration",
            "three independent human",
        ),
        "ja/index.html": ("有効な月間パス", "署名脚本全文", "3 つの独立"),
        "ko/index.html": ("유효한 월간 패스", "전체 서명 대본", "세 번의 사람"),
    }
    for relative in ("index.html", "en/index.html", "ja/index.html", "ko/index.html"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _contract_page(
                director_access=director_access,
                extra_route=extra_route,
                localized_tokens=localized_tokens[relative],
            ),
            encoding="utf-8",
        )
    (root / "llms.txt").write_text(
        "Commercial Director ProductionManifest paid AgentMesh360 accounts with an "
        "active AgentMesh360 monthly pass; 10 credits; workflow.next_action; "
        "immutable signed narration; four files; Edge TTS; "
        "Linux and WSL are not supported; no account-free route",
        encoding="utf-8",
    )


def test_commercial_only_contract_accepts_one_paid_local_media_route(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    _write_contract_site(site)

    process, result = _validate(site, contract="commercial-only")

    assert process.returncode == 0
    assert result["ok"] is True
    assert result["files_checked"] == 4


def test_current_site_publishes_ten_credit_manifest_price() -> None:
    for relative in ("index.html", "en/index.html", "ja/index.html", "ko/index.html"):
        page = (ROOT / "site" / relative).read_text(encoding="utf-8")
        assert "10 credits" in page


def test_current_site_publishes_real_dual_format_customer_case() -> None:
    media = (
        "assets/showcase/difficult-task-bilibili.mp4",
        "assets/showcase/difficult-task-xiaohongshu.mp4",
    )
    posters = (
        "assets/showcase/difficult-task-bilibili-poster.jpg",
        "assets/showcase/difficult-task-xiaohongshu-poster.jpg",
    )

    for relative in ("index.html", "en/index.html", "ja/index.html", "ko/index.html"):
        page = (ROOT / "site" / relative).read_text(encoding="utf-8")
        case = page.split('data-case-study="real-customer-canary-v1"', 1)[1].split(
            "</section>", 1
        )[0]

        assert case.count("<video ") == 2
        assert case.count(" controls") == 2
        assert case.count(" playsinline") == 2
        assert case.count('preload="none"') == 2
        assert "autoplay" not in case
        assert "1920 × 1080" in case
        assert "1080 × 1920" in case
        for target in (*media, *posters):
            assert f"/{target}" in case

    for target in (*media, *posters):
        path = ROOT / "site" / target
        assert path.is_file()
        assert path.stat().st_size > 1024

    for target in media:
        header = (ROOT / "site" / target).read_bytes()[:12]
        assert header[4:8] == b"ftyp"


def test_current_site_installs_then_onboards_before_conditional_login() -> None:
    for relative in ("index.html", "en/index.html", "ja/index.html", "ko/index.html"):
        page = (ROOT / "site" / relative).read_text(encoding="utf-8")
        terminal = page.split('<pre class="code">', 1)[1].split("</pre>", 1)[0]

        assert terminal.index("lecturecast onboard") < terminal.index(
            "lecturecast auth login"
        )
        assert "requires_user_action" in terminal
        assert "workflow.next_action" in terminal
        assert "--host-contract 1.0.0" in terminal


def test_current_site_publishes_three_human_gates_in_every_language() -> None:
    required = {
        "index.html": ("Creative Brief", "10 credits", "完整签名讲稿"),
        "en/index.html": ("Creative Brief", "10 credits", "complete signed narration"),
        "ja/index.html": ("Creative Brief", "10 credits", "署名脚本全文"),
        "ko/index.html": ("Creative Brief", "10 credits", "전체 서명 대본"),
    }
    for relative, tokens in required.items():
        page = (ROOT / "site" / relative).read_text(encoding="utf-8")
        for token in tokens:
            assert token in page


def test_current_site_publishes_tts_network_and_credential_boundaries() -> None:
    unsafe_claims = (
        "绝不写入磁盘",
        "never written to disk",
        "ディスクに書き込まれることはありません",
        "디스크에 기록되지 않습니다",
    )
    for relative in ("index.html", "en/index.html", "ja/index.html", "ko/index.html"):
        page = (ROOT / "site" / relative).read_text(encoding="utf-8")
        assert "Microsoft Edge" in page
        assert "MiniMax" in page
        assert "MINIMAX_API_KEY" in page
        assert "shell" in page
        assert all(claim not in page for claim in unsafe_claims)


def test_current_site_declares_four_standard_outputs_and_one_product_route() -> None:
    retired_route_labels = ("两种路线", "2 つの経路", "두 경로")
    for relative in ("index.html", "en/index.html", "ja/index.html", "ko/index.html"):
        page = (ROOT / "site" / relative).read_text(encoding="utf-8")
        assert "1920×1080" in page
        assert "1080×1920" in page
        assert "MP4" in page
        assert "PNG" in page
        assert (
            ".routes{margin-top:40px;display:grid;"
            "grid-template-columns:1fr 1fr"
        ) not in page
        assert page.count('data-route="director"') == 1
        assert all(label not in page for label in retired_route_labels)


def test_current_site_metadata_describes_the_commercial_product() -> None:
    retired_headlines = (
        "开源本地工具",
        "open-source local tool",
        "オープンソースのローカル",
        "오픈소스 로컬",
    )
    for relative in ("index.html", "en/index.html", "ja/index.html", "ko/index.html"):
        page = (ROOT / "site" / relative).read_text(encoding="utf-8")
        head = page.split("</head>", 1)[0]
        assert "commercial agentic video production" in head
        assert all(headline not in head for headline in retired_headlines)


def test_current_site_links_every_language_directly_to_pass_purchase() -> None:
    purchase_url = "https://agentmesh360.com/app/#pricing"
    for relative in ("index.html", "en/index.html", "ja/index.html", "ko/index.html"):
        page = (ROOT / "site" / relative).read_text(encoding="utf-8")
        assert page.count("data-purchase-cta") == 1
        assert f'href="{purchase_url}"' in page


def test_current_site_supports_macos_and_windows_without_linux() -> None:
    for relative in ("index.html", "en/index.html", "ja/index.html", "ko/index.html"):
        page = (ROOT / "site" / relative).read_text(encoding="utf-8")
        assert '"operatingSystem": "macOS, Windows"' in page
        assert '"operatingSystem": "macOS, Windows, Linux"' not in page
        assert "install.ps1" in page


def test_commercial_only_contract_rejects_unmetered_director_access(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    _write_contract_site(site, director_access="available")

    process, result = _validate(site, contract="commercial-only")

    assert process.returncode == 1
    errors = result["errors"]
    assert isinstance(errors, list)
    assert sum("director route must be paid" in error for error in errors) == 4


def test_commercial_only_contract_requires_machine_readable_boundary(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    _write_contract_site(site)
    (site / "llms.txt").write_text("Director only")

    process, result = _validate(site, contract="commercial-only")

    assert process.returncode == 1
    errors = result["errors"]
    assert isinstance(errors, list)
    assert any("llms.txt" in error for error in errors)


def test_commercial_only_contract_rejects_a_second_product_route(tmp_path: Path) -> None:
    site = tmp_path / "site"
    _write_contract_site(site, extra_route=True)

    process, result = _validate(site, contract="commercial-only")

    assert process.returncode == 1
    assert any("unexpected product route" in error for error in result["errors"])


def test_current_site_does_not_publish_retired_product_tier() -> None:
    retired_tier = "".join(("comm", "unity"))
    for relative in ("index.html", "en/index.html", "ja/index.html", "ko/index.html"):
        page = (ROOT / "site" / relative).read_text(encoding="utf-8")
        assert retired_tier not in page.lower()
    assert retired_tier not in (ROOT / "site" / "llms.txt").read_text(
        encoding="utf-8"
    ).lower()


def test_production_hosting_stays_behind_agentmesh_caddy() -> None:
    boundary = (ROOT / "docs/LECTURECAST-SYSTEM-BOUNDARY.md").read_text(
        encoding="utf-8"
    )

    assert not (ROOT / ".github/workflows/pages.yml").exists()
    assert not (ROOT / "site/CNAME").exists()
    assert "jobagent-caddy" in boundary
    assert "agentmesh-core" in boundary
    assert "GitHub Pages is not a production origin" in boundary
    assert (ROOT / ".github/workflows/site-contract.yml").exists()


def test_localized_pages_use_the_lecturecast_product_mark_as_favicon() -> None:
    favicon = (ROOT / "site/favicon.svg").read_text(encoding="utf-8")

    assert "#D1493F" in favicon
    assert "Lecturecast" in favicon
    for relative in ("index.html", "en/index.html", "ja/index.html", "ko/index.html"):
        page = (ROOT / "site" / relative).read_text(encoding="utf-8")
        assert (
            '<link rel="icon" type="image/svg+xml" '
            'href="/favicon.svg?v=product-mark-v1" />'
        ) in page
        assert "data:image/svg+xml" not in page
