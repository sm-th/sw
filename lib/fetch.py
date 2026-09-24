#!/usr/bin/env python3
"""Capture a source: store the original and a Markdown reading copy by SHA-256.

Usage:
  fetch.py URL [SOURCE] [--file FILE] [--recapture]   capture URL, or return its current capture
  fetch.py --hash SHA                                 restore a stored capture into the local cache

A source is one URL with one current capture per research. Originals go to a
private Cloudflare R2 bucket under <sha256>/, shared by every research because
equal bytes are one capture; research/<research>/urls/<sha256 of the URL>.json
points at the current capture of each URL in one research. A local cache under
~/.cache/sw/captures/ keeps copies for reading. A URL that is already captured
in this research is not fetched again unless --recapture is given; the
replaced capture stays in R2 as an archive. With SOURCE, the capture is
recorded in sources/SOURCE.md, which is created with generated front matter
when it does not exist yet.

Run it through `sw fetch`, which supplies the credentials declared in the sw
module's secretspec.toml to this process only.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

from tidy import FOLD

EXTENSIONS = {
    "text/html": "html",
    "application/xhtml+xml": "html",
    "application/pdf": "pdf",
    "text/markdown": "md",
    "text/plain": "txt",
    "application/json": "json",
    "application/xml": "xml",
    "text/xml": "xml",
}
TYPES_BY_SUFFIX = {
    ".html": "text/html",
    ".htm": "text/html",
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
    ".xml": "application/xml",
}


class FetchError(Exception):
    """A failure to report to the user; KIND names a failing backend, DETAIL describes it without URLs."""

    def __init__(self, message: str, kind: str | None = None, detail: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail


@dataclass
class Capture:
    url: str
    retrieved: str
    sha256: str
    type: str
    meta: dict[str, str] = field(default_factory=dict)


# Paths and configuration


def cache_root() -> Path:
    """User-level cache shared by every research: captures are addressed by content, so they never collide."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "sw"


def research() -> str:
    """The research this worktree belongs to: the name of its origin repository."""
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    name = re.sub(r"\.git$", "", url.rstrip("/")).rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
        raise FetchError(f"origin names {name!r}, which is not a research slug")
    return name


CREDENTIALS = ("FIRECRAWL_API_KEY", "R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")


def media_type(value: str | None) -> str:
    return (value or "application/octet-stream").split(";")[0].strip().lower()


def extension(mime: str) -> str:
    return EXTENSIONS.get(mime, "bin")


# HTML reading copy and metadata

SKIP = {"script", "style", "svg", "noscript", "template", "iframe", "canvas", "head", "button", "form"}
BLOCK = {
    "p", "div", "section", "article", "header", "footer", "main", "aside", "nav",
    "ul", "ol", "li", "table", "tr", "blockquote", "pre", "dl", "dt", "dd",
    "figure", "figcaption", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
}


class TextReader(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.skip_depth = 0
        self.lines: list[str] = []
        self.current: list[str] = []
        self.href: str | None = None
        self.link_start = 0

    def flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self.current)).strip()
        if text and text not in {"-", "#"}:
            self.lines.append(text)
        self.current = []
        self.href = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in BLOCK:
            self.flush()
        if re.fullmatch(r"h[1-6]", tag):
            self.current.append("#" * int(tag[1]) + " ")
        elif tag == "li":
            self.current.append("- ")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            if href and not href.startswith(("#", "javascript:", "data:", "mailto:")):
                self.href = urljoin(self.base_url, href)
                self.link_start = len(self.current)

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag == "a" and self.href:
            if "".join(self.current[self.link_start:]).strip():
                self.current.insert(self.link_start, "[")
                self.current.append(f"]({self.href})")
            self.href = None
        if tag in BLOCK:
            self.flush()

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.current.append(data)


def html_to_markdown(original: bytes, base_url: str) -> str:
    reader = TextReader(base_url)
    reader.feed(original.decode("utf-8", errors="replace"))
    reader.close()
    reader.flush()
    return "\n".join(reader.lines) + "\n"


class MetaReader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.title = ""
        self.in_title = False
        self.in_json_ld = False
        self.json_ld: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta":
            key = (values.get("name") or values.get("property") or "").lower()
            if key and values.get("content"):
                self.meta.setdefault(key, []).append(values["content"].strip())
        elif tag == "title":
            self.in_title = True
        elif tag == "script" and values.get("type") == "application/ld+json":
            self.in_json_ld = True
            self.json_ld.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        elif tag == "script":
            self.in_json_ld = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        elif self.in_json_ld:
            self.json_ld[-1] += data


def json_ld_objects(blocks: list[str]):
    for block in blocks:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                yield item
                stack.extend(item.get("@graph", []))


def person_names(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [value["name"]] if isinstance(value.get("name"), str) else []
    if isinstance(value, list):
        return [name for item in value for name in person_names(item)]
    return []


def iso_date(value: str | None) -> str | None:
    match = re.match(r"\d{4}-\d{2}-\d{2}", value.replace("/", "-") if isinstance(value, str) else "")
    return match.group(0) if match else None


def join_names(names: list[str]) -> str:
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + ", and " + names[-1]


def html_metadata(original: bytes) -> dict[str, str]:
    reader = MetaReader()
    reader.feed(original.decode("utf-8", errors="replace"))
    meta = reader.meta
    first = lambda *keys: next((meta[k][0] for k in keys if meta.get(k)), None)

    title = first("citation_title", "og:title", "twitter:title") or re.sub(r"\s+", " ", reader.title).strip()
    authors = meta.get("citation_author") or []
    published = iso_date(first("citation_publication_date", "citation_date", "article:published_time", "dc.date"))
    publisher = first("citation_journal_title", "citation_conference_title", "og:site_name")
    for item in json_ld_objects(reader.json_ld):
        authors = authors or person_names(item.get("author"))
        published = published or iso_date(item.get("datePublished"))
    if not authors:
        author = first("author", "dc.creator")
        authors = [author] if author and not author.startswith("http") else []

    result = {
        "title": title,
        "author": join_names(list(dict.fromkeys(authors))),
        "publisher": publisher or "",
        "published": published or "",
    }
    return {key: value for key, value in result.items() if value}


# Backends


def firecrawl_scrape(url: str, formats: list[str]) -> dict:
    """One uncached Firecrawl scrape of URL in the given formats."""
    api = os.environ.get("FIRECRAWL_API_URL", "https://api.firecrawl.dev/v2").rstrip("/")
    body = json.dumps({"url": url, "formats": formats, "maxAge": 0, "onlyMainContent": True}).encode()
    request = urllib.request.Request(
        f"{api}/scrape",
        data=body,
        headers={
            "Authorization": f"Bearer {os.environ['FIRECRAWL_API_KEY']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        service = error.code >= 500 or error.code in (401, 402, 429)
        raise FetchError(f"Firecrawl returned {error.code} for {url}: {detail}",
                         "firecrawl" if service else None, f"HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise FetchError(f"could not reach Firecrawl: {error}", "firecrawl", "unreachable") from error

    data = payload.get("data") or {}
    status = (data.get("metadata") or {}).get("statusCode")
    if not payload.get("success") or not any(data.get(name) for name in formats):
        raise FetchError(f"Firecrawl could not scrape {url}: {payload.get('error') or 'no content'}")
    if isinstance(status, int) and status >= 400:
        raise FetchError(f"{url} answered HTTP {status} to Firecrawl")
    return data


def firecrawl(url: str) -> tuple[bytes, str, str | None]:
    """Return (original bytes, media type, Markdown) scraped by Firecrawl.

    Firecrawl returns the original response body (rawBase64) only on its own,
    so the original and the Markdown reading copy take two requests.
    """
    raw = firecrawl_scrape(url, ["rawBase64"])
    original = base64.b64decode(raw["rawBase64"])
    mime = media_type((raw.get("metadata") or {}).get("contentType"))
    markdown = firecrawl_scrape(url, ["markdown"]).get("markdown")
    return original, mime, markdown


def reading_copy(original: bytes, mime: str, base_url: str) -> str | None:
    if extension(mime) == "html":
        return html_to_markdown(original, base_url)
    if mime.startswith("text/") or mime == "application/json":
        return original.decode("utf-8", errors="replace")
    return None


# Storage


class Store:
    def __init__(self, research: str) -> None:
        self.cache = cache_root() / "captures"
        self.prefix = f"research/{research}/urls/"
        self.bucket = os.environ["R2_BUCKET"]
        self.endpoint = f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
        self.env = os.environ | {
            "AWS_ACCESS_KEY_ID": os.environ["R2_ACCESS_KEY_ID"],
            "AWS_SECRET_ACCESS_KEY": os.environ["R2_SECRET_ACCESS_KEY"],
            "AWS_DEFAULT_REGION": "auto",
            "AWS_REQUEST_CHECKSUM_CALCULATION": "WHEN_REQUIRED",
            "AWS_RESPONSE_CHECKSUM_VALIDATION": "WHEN_REQUIRED",
            "AWS_PAGER": "",
        }

    def s3(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["aws", "s3api", *args, "--bucket", self.bucket, "--endpoint-url", self.endpoint],
            env=self.env, capture_output=True, text=True,
        )

    def names(self, capture: Capture) -> dict[str, str]:
        return {
            "original": f"original.{extension(capture.type)}",
            "markdown": "content.md",
            "record": "capture.json",
        }

    def save(self, capture: Capture, original: bytes, markdown: str | None) -> Path | None:
        folder = self.cache / capture.sha256
        folder.mkdir(parents=True, exist_ok=True)
        files = self.names(capture)
        (folder / files["original"]).write_bytes(original)
        record = folder / files["record"]
        if not record.exists():
            record.write_text(json.dumps(asdict(capture), indent=2) + "\n")
        if markdown is not None:
            (folder / files["markdown"]).write_text(markdown)
        for kind, name in files.items():
            path = folder / name
            if not path.exists():
                continue
            key = f"{capture.sha256}/{name}"
            if self.s3("head-object", "--key", key).returncode == 0:
                continue
            content_type = {"original": capture.type, "markdown": "text/markdown", "record": "application/json"}[kind]
            result = self.s3("put-object", "--key", key, "--body", str(path), "--content-type", content_type)
            if result.returncode != 0:
                raise FetchError(f"R2 upload failed for {key}: {result.stderr.strip()}", "r2", "upload failed")
        return folder / files["markdown"] if markdown is not None else None

    def restore(self, sha: str) -> tuple[Capture, Path]:
        folder = self.cache / sha
        record = folder / "capture.json"
        if not record.exists():
            folder.mkdir(parents=True, exist_ok=True)
            self.download(f"{sha}/capture.json", record)
        capture = Capture(**json.loads(record.read_text()))
        for name in self.names(capture).values():
            path = folder / name
            if not path.exists():
                self.download(f"{sha}/{name}", path, optional=name == "content.md")
        return capture, folder / f"original.{extension(capture.type)}"

    def download(self, key: str, path: Path, optional: bool = False) -> None:
        result = self.s3("get-object", "--key", key, str(path))
        if result.returncode != 0 and not optional:
            raise FetchError(f"R2 download failed for {key}: {result.stderr.strip()}", "r2", "download failed")

    def pointer_key(self, url: str) -> str:
        return f"{self.prefix}{hashlib.sha256(url.encode()).hexdigest()}.json"

    def current(self, url: str) -> str | None:
        """SHA-256 of the current capture of URL, or None when URL was never captured."""
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "pointer.json"
            result = self.s3("get-object", "--key", self.pointer_key(url), str(path))
            if result.returncode != 0:
                if "NoSuchKey" in result.stderr:
                    return None
                raise FetchError(f"R2 lookup failed for {url}: {result.stderr.strip()}", "r2", "lookup failed")
            return json.loads(path.read_text())["sha256"]

    def point(self, url: str, sha: str) -> None:
        """Make SHA the current capture of URL; the capture it replaces stays in R2."""
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "pointer.json"
            path.write_text(json.dumps({"url": url, "sha256": sha}) + "\n")
            key = self.pointer_key(url)
            result = self.s3("put-object", "--key", key, "--body", str(path), "--content-type", "application/json")
            if result.returncode != 0:
                raise FetchError(f"R2 upload failed for {key}: {result.stderr.strip()}", "r2", "upload failed")

    def pointers(self) -> dict[str, str]:
        """URL -> SHA-256 of its current capture, for every URL this research captured."""
        listing = self.s3("list-objects-v2", "--prefix", self.prefix, "--query", "Contents[].Key", "--output", "json")
        if listing.returncode != 0:
            raise FetchError(f"R2 listing failed: {listing.stderr.strip()}", "r2", "listing failed")
        found = {}
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "pointer.json"
            for key in json.loads(listing.stdout) or []:
                self.download(key, path)
                pointer = json.loads(path.read_text())
                found[pointer["url"]] = pointer["sha256"]
        return found


# Source pages

def scalar(value: str) -> str:
    return json.dumps(value.translate(FOLD))


def guess_kind(capture: Capture) -> str:
    if re.match(r"https?://((www\.)?(github|gitlab|codeberg)\.(com|org)|raw\.githubusercontent\.com)/", capture.url):
        return "repository"
    if capture.type == "application/pdf":
        return "paper"
    return "webpage"


def front_matter(path: Path) -> tuple[list[str], int]:
    """The page's lines and the index of the line that closes its front matter."""
    lines = path.read_text().split("\n")
    return lines, lines.index("---", 1)


def page_url(path: Path) -> str:
    lines, end = front_matter(path)
    return next((line.split(":", 1)[1].strip() for line in lines[1:end] if line.startswith("url:")), "")


def check_page(worktree: Path, slug: str, url: str) -> None:
    """A source is one URL: refuse to record URL on a page that describes another."""
    path = worktree / "sources" / f"{slug}.md"
    if path.exists() and page_url(path) != url:
        raise FetchError(f"{path.relative_to(worktree)} describes {page_url(path)}; a source is one URL")


def record_in_page(worktree: Path, slug: str, capture: Capture) -> str:
    """Create the source page, or make CAPTURE its current capture; return what happened."""
    path = worktree / "sources" / f"{slug}.md"
    stamp = [f"retrieved: {capture.retrieved}", f"sha256: {scalar(capture.sha256)}"]
    if not path.exists():
        meta = capture.meta
        lines = ["---", f"title: {scalar(meta.get('title', ''))}", 'summary: ""', f"url: {capture.url}"]
        lines.append(f"author: {scalar(meta.get('author', ''))}")
        if meta.get("publisher"):
            lines.append(f"publisher: {scalar(meta['publisher'])}")
        if meta.get("published"):
            lines.append(f"published: {meta['published']}")
        lines += [f"kind: {guess_kind(capture)}", *stamp, "---", ""]
        lines += ["## Overview", "", "", "## Key points", ""]
        path.write_text("\n".join(lines))
        missing = ["summary", *(field for field in ("title", "author") if not meta.get(field))]
        if not meta.get("published"):
            missing.append("published (the date the source states, often on its first page)")
        return f"created {path.relative_to(worktree)}; fill {', '.join(missing)}, Overview, and Key points"

    lines, end = front_matter(path)
    if any(capture.sha256 in line for line in lines[1:end]):
        return f"{path.relative_to(worktree)} already records this capture"
    kept = [line for line in lines[1:end] if not line.startswith(("retrieved:", "sha256:"))]
    path.write_text("\n".join(["---", *kept, *stamp, *lines[end:]]))
    return f"replaced the capture in {path.relative_to(worktree)}; recheck its Key points against the new capture"


# Commands


def capture_url(url: str, file: Path | None) -> tuple[Capture, bytes, str | None, str]:
    retrieved = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if file:
        original = file.read_bytes()
        mime = TYPES_BY_SUFFIX.get(file.suffix.lower(), "application/octet-stream")
        markdown, method = None, "file"
    else:
        original, mime, markdown = firecrawl(url)
        method = "firecrawl"
    markdown = markdown or reading_copy(original, mime, url)
    meta = html_metadata(original) if extension(mime) == "html" else {}
    capture = Capture(url, retrieved, hashlib.sha256(original).hexdigest(), mime, meta)
    return capture, original, markdown, method


def print_capture(capture: Capture, markdown: Path | None, stored: str, method: str | None = None) -> None:
    print("capture:")
    print(f"  url: {capture.url}")
    print(f"  retrieved: {capture.retrieved}")
    print(f"  sha256: {capture.sha256}")
    print(f"  type: {capture.type}")
    for key in ("title", "author", "publisher", "published"):
        if capture.meta.get(key):
            print(f"{key}: {json.dumps(capture.meta[key], ensure_ascii=False)}")
    if markdown:
        text = markdown.read_text()
        print(f"markdown: {markdown} ({len(text.splitlines())} lines, {len(text.split())} words)")
    else:
        print("markdown: none; read the original")
    if method:
        print(f"method: {method}")
    print(f"stored: {stored}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", nargs="?")
    parser.add_argument("source", nargs="?", help="source page slug under sources/")
    parser.add_argument("--file", type=Path, help="copy of URL saved from a browser")
    parser.add_argument("--hash", dest="sha", help="restore a stored capture")
    parser.add_argument("--recapture", action="store_true", help="replace the current capture of URL")
    args = parser.parse_args()

    missing = [name for name in CREDENTIALS if not os.environ.get(name)]
    if missing:
        print(f"sw fetch: missing {', '.join(missing)}; run it as sw fetch so secretspec supplies them", file=sys.stderr)
        return 1
    try:
        name = research()
        store = Store(name)
        stored = f"r2://{store.bucket}/<sha256>/"
        if args.sha:
            if args.url or args.file or args.source or args.recapture:
                parser.error("--hash takes no URL, SOURCE, --file, or --recapture")
            if not re.fullmatch(r"[0-9a-f]{64}", args.sha):
                parser.error("--hash needs a 64-character hex SHA-256")
            capture, original = store.restore(args.sha)
            markdown = original.with_name("content.md")
            print_capture(capture, markdown if markdown.exists() else None, stored.replace("<sha256>", capture.sha256))
            print(f"original: {original}")
            return 0
        if not args.url or not re.match(r"https?://", args.url):
            parser.error("give an http(s) URL or --hash SHA")
        if args.file and not args.file.is_file():
            parser.error(f"file not found: {args.file}")
        if args.source and not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", args.source):
            parser.error("SOURCE must use lowercase ASCII words and hyphens")
        if args.source and re.search(r"-\d{4}-\d{2}-\d{2}$", args.source):
            parser.error("SOURCE names the source, not the capture; drop the date")
        worktree = Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True,
        ).stdout.strip())
        if args.source:
            check_page(worktree, args.source, args.url)
        previous = store.current(args.url)
        current = None if args.recapture else previous
        if current:
            if args.file:
                raise FetchError(f"{args.url} is already captured; add --recapture to replace it with {args.file}")
            capture, original = store.restore(current)
            markdown = original.with_name("content.md")
            method = "current capture (add --recapture to replace it)"
        else:
            capture, original_bytes, markdown_text, method = capture_url(args.url, args.file)
            markdown = store.save(capture, original_bytes, markdown_text)
            store.point(capture.url, capture.sha256)
        print_capture(capture, markdown if markdown and markdown.exists() else None,
                      stored.replace("<sha256>", capture.sha256), method)
        if not current:
            from search import index_new_capture  # search imports this module
            print(f"index: {index_new_capture(store, name, capture.sha256, previous)}")
        if args.source:
            print(f"page: {record_in_page(worktree, args.source, capture)}")
        return 0
    except FetchError as error:
        print(f"sw fetch: {error}", file=sys.stderr)
        if error.kind:
            from search import Health
            Health(research()).report(error.kind, error.detail or "failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
