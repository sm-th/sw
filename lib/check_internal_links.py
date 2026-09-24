#!/usr/bin/env python3
"""Validate internal navigation against a generated static site."""

from __future__ import annotations

import argparse
import sys
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value is not None:
                self.links.append(value.strip())
                return


def page_url(site_root: Path, page: Path, prefix: str) -> str:
    relative = page.relative_to(site_root).as_posix()
    if relative == "index.html":
        return prefix
    if relative.endswith("/index.html"):
        return f"{prefix}{relative[:-10]}"
    return f"{prefix}{relative}"


def target_candidates(site_root: Path, url_path: str, prefix: str) -> tuple[Path, ...]:
    if not url_path.startswith(prefix):
        return ()
    relative = PurePosixPath(unquote(url_path[len(prefix):]))
    target = site_root.joinpath(*relative.parts)
    if url_path.endswith("/"):
        return (target / "index.html",)
    if target.suffix:
        return (target,)
    return (target, target.with_suffix(".html"), target / "index.html")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site", nargs="?", default="_site", type=Path)
    parser.add_argument(
        "--prefix",
        default="/",
        help="URL path the site is served under, such as /wiki/ (default: /)",
    )
    args = parser.parse_args()
    prefix = "/" + args.prefix.strip("/") + "/" if args.prefix.strip("/") else "/"
    site_root = args.site.resolve()
    if not site_root.is_dir():
        print(f"Generated site directory does not exist: {site_root}", file=sys.stderr)
        return 2

    pages = sorted(site_root.rglob("*.html"))
    if not pages:
        print(f"Generated site contains no HTML pages: {site_root}", file=sys.stderr)
        return 2

    failures: list[str] = []
    checked = 0

    for page in pages:
        parser = LinkParser()
        parser.feed(page.read_text(encoding="utf-8"))
        source_url = page_url(site_root, page, prefix)

        for href in parser.links:
            if not href or href.startswith("#") or href.startswith("//"):
                continue
            parsed_href = urlsplit(href)
            if parsed_href.scheme:
                continue

            resolved = urlsplit(urljoin(source_url, href))
            candidates = target_candidates(site_root, resolved.path, prefix)
            checked += 1
            if not any(candidate.exists() for candidate in candidates):
                expected = ", ".join(
                    candidate.relative_to(site_root).as_posix()
                    for candidate in candidates
                ) or f"a path under {prefix}"
                failures.append(
                    f"{source_url}: {href!r} resolves to missing {resolved.path!r} "
                    f"(expected {expected})"
                )

    if failures:
        print("Broken internal links:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(f"Checked {checked} internal links across {len(pages)} generated pages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
