from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


COMMERCIAL_ONLY_PAGES = (
    Path("index.html"),
    Path("zh/index.html"),
    Path("en/index.html"),
    Path("ja/index.html"),
    Path("ko/index.html"),
)
COMMERCIAL_ONLY_CONTRACT = "commercial-only-v1"
COMMERCIAL_REQUIRED_PAGE_TOKENS = (
    "10 credits",
    "ProductionManifest",
    "lecturecast onboard --adapter codex --host-contract 1.0.0 --json",
    "lecturecast auth login",
    "workflow.next_action",
    "Microsoft Edge",
    "MiniMax",
    "1920×1080",
    "1080×1920",
    "MP4",
    "PNG",
)
COMMERCIAL_LOCALIZED_TOKENS = {
    Path("index.html"): (
        "active monthly pass",
        "complete signed narration",
        "three independent human",
    ),
    Path("zh/index.html"): ("有效月卡", "完整签名讲稿", "三次独立人工"),
    Path("en/index.html"): (
        "active monthly pass",
        "complete signed narration",
        "three independent human",
    ),
    Path("ja/index.html"): ("有効な月間パス", "署名脚本全文", "3 つの独立"),
    Path("ko/index.html"): ("유효한 월간 패스", "전체 서명 대본", "세 번의 사람"),
}
RETIRED_MARKETING_FRAGMENTS = (
    "开源本地工具",
    "open-source local tool",
    "オープンソースのローカル",
    "오픈소스 로컬",
)
UNSAFE_CREDENTIAL_CLAIMS = (
    "绝不写入磁盘",
    "never written to disk",
    "ディスクに書き込まれることはありません",
    "디스크에 기록되지 않습니다",
)


@dataclass(frozen=True)
class ValidationResult:
    files_checked: int
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "files_checked": self.files_checked,
            "errors": list(self.errors),
        }


class _SiteParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.targets: list[str] = []
        self.jsonld: list[str] = []
        self.route_contracts: list[dict[str, str]] = []
        self.product_contracts: list[str] = []
        self.html_lang: str | None = None
        self.title_count = 0
        self._jsonld_buffer: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = {name: value or "" for name, value in attrs}
        element_id = values.get("id")
        if element_id:
            self.ids.append(element_id)
        for name in ("href", "src"):
            target = values.get(name)
            if target:
                self.targets.append(target)
        if tag == "html":
            self.html_lang = values.get("lang") or None
        if tag == "title":
            self.title_count += 1
        if tag == "script" and values.get("type") == "application/ld+json":
            self._jsonld_buffer = []
        if "data-route" in values:
            self.route_contracts.append(values)
        product_contract = values.get("data-product-contract")
        if product_contract:
            self.product_contracts.append(product_contract)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._jsonld_buffer is not None:
            self._jsonld_buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._jsonld_buffer is not None:
            self.jsonld.append("".join(self._jsonld_buffer))
            self._jsonld_buffer = None


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _local_target(root: Path, page: Path, target: str) -> tuple[Path, str] | None:
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or target.startswith("//"):
        return None
    target_path = unquote(parsed.path)
    if not target_path:
        return page, parsed.fragment
    if target_path.startswith("/"):
        candidate = root / target_path.lstrip("/")
    else:
        candidate = page.parent / target_path
    candidate = candidate.resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("local target escapes site root")
    if candidate.is_dir():
        candidate /= "index.html"
    return candidate, parsed.fragment


def _parse_page(path: Path) -> _SiteParser:
    parser = _SiteParser()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    return parser


def _validate_commercial_only_contract(
    root: Path,
    documents: dict[Path, _SiteParser],
    errors: list[str],
) -> None:
    retired_tier = "".join(("comm", "unity"))
    for relative_path in COMMERCIAL_ONLY_PAGES:
        page = (root / relative_path).resolve()
        label = relative_path.as_posix()
        document = documents.get(page)
        if document is None:
            errors.append(f"{label}: required localized page is missing")
            continue
        if COMMERCIAL_ONLY_CONTRACT not in document.product_contracts:
            errors.append(
                f"{label}: missing data-product-contract={COMMERCIAL_ONLY_CONTRACT}"
            )
        directors = [
            item for item in document.route_contracts
            if item.get("data-route") == "director"
        ]
        unexpected = [
            item.get("data-route", "") for item in document.route_contracts
            if item.get("data-route") != "director"
        ]
        if len(directors) != 1:
            errors.append(f"{label}: missing director route contract")
        else:
            director = directors[0]
            if director.get("data-access") != "paid":
                errors.append(f"{label}: director route must be paid")
            if director.get("data-media") != "local":
                errors.append(f"{label}: director route must keep media local")
        if unexpected:
            errors.append(f"{label}: unexpected product route(s): {', '.join(unexpected)}")
        page_text = page.read_text(encoding="utf-8")
        lowered_page = page_text.lower()
        if retired_tier in lowered_page:
            errors.append(f"{label}: retired product tier must not be published")
        for token in COMMERCIAL_REQUIRED_PAGE_TOKENS:
            if token not in page_text:
                errors.append(f"{label}: missing commercial-workflow token {token!r}")
        for token in COMMERCIAL_LOCALIZED_TOKENS[relative_path]:
            if token not in page_text:
                errors.append(f"{label}: missing localized product-contract token {token!r}")
        onboard_index = page_text.find("lecturecast onboard")
        auth_index = page_text.find("lecturecast auth login")
        if onboard_index == -1 or auth_index == -1 or onboard_index >= auth_index:
            errors.append(
                f"{label}: install guidance must onboard before conditional auth login"
            )
        head_text = page_text.partition("</head>")[0].lower()
        for fragment in RETIRED_MARKETING_FRAGMENTS:
            if fragment.lower() in head_text:
                errors.append(
                    f"{label}: retired open-source-local metadata must not be published"
                )
        for fragment in UNSAFE_CREDENTIAL_CLAIMS:
            if fragment.lower() in lowered_page:
                errors.append(
                    f"{label}: unsafe absolute credential-storage claim {fragment!r}"
                )
        if (
            ".routes{margin-top:40px;display:grid;"
            "grid-template-columns:1fr 1fr"
        ) in page_text:
            errors.append(f"{label}: single commercial route must not use a two-column grid")

    llms_path = root / "llms.txt"
    if not llms_path.is_file():
        errors.append("llms.txt: required machine-readable product boundary is missing")
        return
    llms_text = llms_path.read_text(encoding="utf-8")
    if retired_tier in llms_text.lower():
        errors.append("llms.txt: retired product tier must not be published")
    for token in (
        "Commercial",
        "Director",
        "ProductionManifest",
        "paid AgentMesh360 account",
        "active AgentMesh360 monthly pass",
        "10 credits",
        "workflow.next_action",
        "immutable signed",
        "four files",
        "Edge TTS",
        "Linux and WSL are not supported",
        "no account-free route",
    ):
        if token not in llms_text:
            errors.append(f"llms.txt: missing product-boundary token {token!r}")


def validate_site(
    root: Path,
    *,
    contract: str | None = None,
) -> ValidationResult:
    root = root.resolve()
    errors: list[str] = []
    if not root.is_dir():
        return ValidationResult(0, (f"{root}: site root is not a directory",))

    pages = sorted(path.resolve() for path in root.rglob("*.html"))
    if not pages:
        return ValidationResult(0, ("site root contains no HTML pages",))

    documents: dict[Path, _SiteParser] = {}
    for page in pages:
        label = _relative(root, page)
        try:
            document = _parse_page(page)
        except (OSError, UnicodeError) as exc:
            errors.append(f"{label}: cannot read UTF-8 HTML: {exc}")
            continue
        documents[page] = document
        if document.html_lang is None:
            errors.append(f"{label}: html element must declare lang")
        if document.title_count != 1:
            errors.append(f"{label}: expected exactly one title element")
        duplicate_ids = sorted(
            element_id
            for element_id in set(document.ids)
            if document.ids.count(element_id) > 1
        )
        for element_id in duplicate_ids:
            errors.append(f"{label}: duplicate id {element_id!r}")
        if not document.jsonld:
            errors.append(f"{label}: missing application/ld+json")
        for index, payload in enumerate(document.jsonld, start=1):
            try:
                json.loads(payload)
            except json.JSONDecodeError as exc:
                errors.append(f"{label}: JSON-LD #{index} is invalid: {exc.msg}")

    for page, document in documents.items():
        label = _relative(root, page)
        for target in document.targets:
            try:
                resolved = _local_target(root, page, target)
            except ValueError as exc:
                errors.append(f"{label}: {target!r} {exc}")
                continue
            if resolved is None:
                continue
            target_path, fragment = resolved
            if not target_path.exists():
                errors.append(f"{label}: local target {target!r} does not exist")
                continue
            target_document = documents.get(target_path)
            if fragment and target_document is not None and fragment not in target_document.ids:
                errors.append(f"{label}: anchor {target!r} does not exist")

    if contract == "commercial-only":
        _validate_commercial_only_contract(root, documents, errors)

    return ValidationResult(len(pages), tuple(sorted(set(errors))))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the static LectureCast site without network access"
    )
    parser.add_argument("root", nargs="?", type=Path, default=Path("site"))
    parser.add_argument(
        "--contract",
        choices=("commercial-only",),
        help="enforce an optional product-boundary contract",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = validate_site(args.root, contract=args.contract)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    elif result.ok:
        print(f"site validation passed ({result.files_checked} HTML files)")
    else:
        for error in result.errors:
            print(f"ERROR: {error}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
