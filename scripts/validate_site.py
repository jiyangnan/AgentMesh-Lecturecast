from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


COMMUNITY_DIRECTOR_PAGES = (
    Path("index.html"),
    Path("en/index.html"),
    Path("ja/index.html"),
    Path("ko/index.html"),
)
COMMUNITY_DIRECTOR_CONTRACT = "community-director-v1"


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


def _validate_community_director_contract(
    root: Path,
    documents: dict[Path, _SiteParser],
    errors: list[str],
) -> None:
    for relative_path in COMMUNITY_DIRECTOR_PAGES:
        page = (root / relative_path).resolve()
        label = relative_path.as_posix()
        document = documents.get(page)
        if document is None:
            errors.append(f"{label}: required localized page is missing")
            continue
        if COMMUNITY_DIRECTOR_CONTRACT not in document.product_contracts:
            errors.append(
                f"{label}: missing data-product-contract={COMMUNITY_DIRECTOR_CONTRACT}"
            )
        routes = {
            item.get("data-route", ""): item for item in document.route_contracts
        }
        community = routes.get("community")
        director = routes.get("director")
        if community is None:
            errors.append(f"{label}: missing community route contract")
        elif community.get("data-access") != "available":
            errors.append(f"{label}: community route must be available")
        if director is None:
            errors.append(f"{label}: missing director route contract")
        elif director.get("data-access") != "staged":
            errors.append(f"{label}: director route must remain staged")
        for route_name, route in (("community", community), ("director", director)):
            if route is not None and route.get("data-media") != "local":
                errors.append(f"{label}: {route_name} route must keep media local")

    llms_path = root / "llms.txt"
    if not llms_path.is_file():
        errors.append("llms.txt: required machine-readable product boundary is missing")
        return
    llms_text = llms_path.read_text(encoding="utf-8")
    for token in ("Community", "Director", "ProductionManifest", "not yet generally open"):
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

    if contract == "community-director":
        _validate_community_director_contract(root, documents, errors)

    return ValidationResult(len(pages), tuple(sorted(set(errors))))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the static LectureCast site without network access"
    )
    parser.add_argument("root", nargs="?", type=Path, default=Path("site"))
    parser.add_argument(
        "--contract",
        choices=("community-director",),
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
