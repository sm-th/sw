#!/usr/bin/env python3
"""Index the current captures and the pages of one research in Qdrant, and search them.

Usage:
  search.py QUERY [--in sources|wiki] [--limit N]   hybrid search over sources, the research's pages, or both
  search.py --sync                                  index every current capture and origin/main page,
                                                    and drop everything else from the index

Each research has its own collection behind the alias sw-<research>. `sw fetch`
indexes each new current capture itself; `sw search` brings the page index up
to origin/main before it searches. A capture that failed to index is retried at
most once an hour by later `sw fetch` or `sw search` runs.

Run it through `sw search`, which supplies the credentials declared in the sw
module's secretspec.toml to this process only.
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from chonkie import RecursiveChunker, RecursiveLevel, RecursiveRules
from qdrant_client import QdrantClient, models

from fetch import FetchError, Store, cache_root, research

# The pipeline: how text becomes chunks and vectors. Changing any of it needs a
# new PIPELINE name and `search.py --sync`, which builds the new collection and
# moves the alias to it. search_eval.py chose it: on 196 Key point queries every
# chunker and model scored within the margin of error once fused with BM25
# (MRR 0.81 to 0.85, +/- 0.04), fusion beat dense search alone, and LLM chunking
# (Slumber) was no better while costing minutes and model calls per capture.
MODEL = "voyageai/voyage-4"
DIMENSION = 1024
QUERY_PREFIX = "Instruct: Given a research question, retrieve passages that answer it\nQuery: "  # Qwen3 embeddings
CHUNK_SIZE = 1600
PIPELINE = "v1-voyage-4-recursive-md-1600"


def alias(name: str) -> str:
    """The Qdrant alias of a research's index; it points at the current pipeline's collection."""
    return f"sw-{name}"


BM25 = "qdrant/bm25"

CREDENTIALS = ("OPENROUTER_API_KEY", "QDRANT_URL", "QDRANT_API_KEY")
# The research's own pages: the card at the root and the sections beside it.
PAGE_PATHS = ("index.md", "sources/", "entities/", "notes/")
RETRY_AFTER = 3600
REPORT_EVERY = 24 * 3600


class BackendError(Exception):
    """A backend failure while indexing or searching; `kind` names the backend."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# Chunking

HEADING = re.compile(r"^(#{1,6})[ \t]+(.{1,120}?)[ \t#]*$", re.M)  # longer lines are code comments, not headings
LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
FENCE = re.compile(r"^[ \t]*(```|~~~)", re.M)


def markdown_chunker(size: int = CHUNK_SIZE) -> RecursiveChunker:
    """Split on headings first, then paragraphs, lines, sentences, and words."""
    rules = RecursiveRules(levels=[
        RecursiveLevel(delimiters=["\n# ", "\n## ", "\n### ", "\n#### "], include_delim="next"),
        RecursiveLevel(delimiters=["\n\n"]),
        RecursiveLevel(delimiters=["\n"]),
        RecursiveLevel(delimiters=[". ", "! ", "? "]),
        RecursiveLevel(whitespace=True),
    ])
    return RecursiveChunker(tokenizer="character", chunk_size=size, rules=rules, min_characters_per_chunk=24)


@dataclass
class Piece:
    text: str
    start: int
    end: int
    headings: list[str]
    lines: tuple[int, int]


def headings(text: str) -> list[tuple[int, int, str]]:
    """(offset, level, title) of every Markdown heading outside code fences, with links reduced to their text."""
    fences = [match.start() for match in FENCE.finditer(text)]
    found = []
    for match in HEADING.finditer(text):
        if bisect.bisect_left(fences, match.start()) % 2:
            continue
        title = re.sub(r"\s+", " ", LINK.sub(r"\1", match.group(2)).replace("\U0001f517", "")).strip()
        found.append((match.start(), len(match.group(1)), title))
    return found


def first_heading(text: str) -> str:
    return next((title for _, _, title in headings(text)), "")


def split(text: str, chunker=None, prefix: str = "") -> list[Piece]:
    """Chunks of TEXT with the headings in force at each; offsets and lines count from the start of PREFIX + TEXT."""
    marks, path, mark = headings(text), [], 0
    base, base_line = len(prefix), prefix.count("\n")
    pieces = []
    for chunk in (chunker or markdown_chunker()).chunk(text):
        start = chunk.start_index + len(chunk.text) - len(chunk.text.lstrip())
        end = chunk.end_index
        if start >= end:
            continue
        while mark < len(marks) and marks[mark][0] <= start:
            _, level, title = marks[mark]
            path = [item for item in path if item[0] < level] + [(level, title)]
            mark += 1
        lines = (base_line + text.count("\n", 0, start) + 1, base_line + text.count("\n", 0, end - 1) + 1)
        pieces.append(Piece(chunk.text, base + start, base + end, [title for _, title in path], lines))
    return pieces


def embedding_text(title: str, piece: Piece) -> str:
    context = " > ".join([title, *piece.headings]) if title else " > ".join(piece.headings)
    return f"{context}\n\n{piece.text}" if context else piece.text


# Embeddings

def error_class(error: Exception) -> str:
    """A short description of a failure that names no URL."""
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, (urllib.error.URLError, TimeoutError)):
        return "unreachable"
    status = getattr(error, "status_code", None)
    return f"HTTP {status}" if status else type(error).__name__


def embed(texts: list[str], model: str = MODEL, query: bool = False, batch: int = 32) -> list[list[float]]:
    """Embed TEXTS through OpenRouter, retrying transient failures."""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch):
        inputs = texts[start:start + batch]
        if query and model.startswith("qwen/"):
            inputs = [QUERY_PREFIX + text for text in inputs]
        body = {"model": model, "input": inputs}
        if model.startswith("voyageai/"):
            body["input_type"] = "query" if query else "document"
        request = urllib.request.Request(
            "https://openrouter.ai/api/v1/embeddings",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}", "Content-Type": "application/json"},
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    data = json.load(response)["data"]
                break
            except (urllib.error.URLError, TimeoutError, KeyError) as error:
                retryable = not isinstance(error, urllib.error.HTTPError) or error.code in (408, 429) or error.code >= 500
                if attempt == 3 or not retryable:
                    raise BackendError("embeddings", error_class(error)) from error
                time.sleep(2 ** attempt * 2)
        vectors += [item["embedding"] for item in sorted(data, key=lambda item: item["index"])]
    return vectors


# Qdrant

def point_id(doc: str, number: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{PIPELINE}/{doc}#{number}"))


def match(scope: str | None = None, **fields) -> models.Filter | None:
    """A filter requiring each given payload field, and SCOPE when given, to equal its value."""
    if scope:
        fields["scope"] = scope
    must = [models.FieldCondition(key=key, match=models.MatchValue(value=value)) for key, value in fields.items()]
    return models.Filter(must=must) if must else None


class Index:
    def __init__(self, name: str) -> None:
        self.client = QdrantClient(url=os.environ["QDRANT_URL"], api_key=os.environ["QDRANT_API_KEY"],
                                   cloud_inference=True, timeout=120)
        self.alias = alias(name)
        # Research slugs never contain "--", so this prefix belongs to one research only.
        self.pipeline_collection = f"{self.alias}--{PIPELINE}"
        self.collection = self.alias

    def call(self, method: str, *args, **kwargs):
        try:
            return getattr(self.client, method)(*args, **kwargs)
        except Exception as error:  # qdrant-client raises several transport and response types
            raise BackendError("qdrant", error_class(error)) from error

    def ensure(self) -> None:
        """Create the pipeline's collection and point the alias at it when neither exists yet."""
        if not self.call("collection_exists", self.pipeline_collection):
            self.call("create_collection", self.pipeline_collection,
                      vectors_config={"dense": models.VectorParams(size=DIMENSION, distance=models.Distance.COSINE)},
                      sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)})
            for field, schema in (("scope", "keyword"), ("doc", "keyword"), ("chunk", "integer")):
                self.call("create_payload_index", self.pipeline_collection, field, field_schema=schema)
        aliases = {alias.alias_name: alias.collection_name for alias in self.call("get_aliases").aliases}
        if self.alias not in aliases:
            self.move_alias()

    def move_alias(self) -> None:
        """Point the alias at this pipeline's collection and delete earlier pipelines' collections."""
        operations = []
        if self.alias in {alias.alias_name for alias in self.call("get_aliases").aliases}:
            operations.append(models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=self.alias)))
        operations.append(models.CreateAliasOperation(
            create_alias=models.CreateAlias(collection_name=self.pipeline_collection, alias_name=self.alias)))
        self.call("update_collection_aliases", change_aliases_operations=operations)
        for collection in self.call("get_collections").collections:
            if collection.name.startswith(f"{self.alias}--") and collection.name != self.pipeline_collection:
                self.call("delete_collection", collection.name)

    def drop(self, doc: str) -> None:
        self.call("delete", self.collection, points_selector=models.FilterSelector(filter=match(doc=doc)))

    def put(self, scope: str, doc: str, title: str, pieces: list[Piece], extra: dict) -> None:
        texts = [embedding_text(title, piece) for piece in pieces]
        vectors = embed(texts) if texts else []
        self.drop(doc)
        points = [
            models.PointStruct(
                id=point_id(doc, number),
                vector={"dense": vector, "bm25": models.Document(text=text, model=BM25)},
                payload={"scope": scope, "doc": doc, "chunk": number, "title": title, "text": piece.text,
                         "headings": piece.headings, "start": piece.start, "end": piece.end,
                         "lines": list(piece.lines), "pipeline": PIPELINE, **extra},
            )
            for number, (piece, text, vector) in enumerate(zip(pieces, texts, vectors))
        ]
        for start in range(0, len(points), 64):
            self.call("upsert", self.collection, points=points[start:start + 64], wait=True)

    def docs(self, scope: str) -> dict[str, dict]:
        """Indexed documents of SCOPE and the payload of their first chunk."""
        found, offset = {}, None
        while True:
            points, offset = self.call("scroll", self.collection, scroll_filter=match(scope, chunk=0),
                                       with_payload=True, with_vectors=False, limit=256, offset=offset)
            found.update({point.payload["doc"]: point.payload for point in points})
            if offset is None:
                return found

    def search(self, query: str, scope: str | None, limit: int):
        vector = embed([query], query=True)[0]
        where = match(scope)
        return self.call(
            "query_points_groups", self.collection,
            prefetch=[
                models.Prefetch(query=vector, using="dense", limit=50, filter=where),
                models.Prefetch(query=models.Document(text=query, model=BM25), using="bm25", limit=50, filter=where),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            group_by="doc", group_size=1, limit=limit, with_payload=True,
        ).groups


# Sources and wiki pages

def index_capture(index: Index, store: Store, sha: str, replaces: str | None = None) -> str:
    """Index the current capture SHA, dropping the capture it replaces; return what happened."""
    try:
        capture, original = store.restore(sha)
    except FetchError as error:
        raise BackendError("r2", "download failed") from error
    markdown = original.with_name("content.md")
    if replaces and replaces != sha:
        index.drop(replaces)
    if not markdown.exists():
        return "not indexed: the capture has no Markdown copy"
    text = markdown.read_text()
    pieces = split(text)
    title = capture.meta.get("title") or first_heading(text) or capture.url
    index.put("sources", sha, title, pieces, {"url": capture.url})
    return f"indexed {len(pieces)} chunks"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def front(text: str) -> tuple[dict[str, str], str]:
    """Scalar front matter fields and the body of a wiki page."""
    if not text.startswith("---\n"):
        return {}, text
    head, _, body = text[4:].partition("\n---\n")
    fields = {}
    for line in head.split("\n"):
        key, sep, value = line.partition(":")
        if sep and not line.startswith(" "):
            fields[key.strip()] = value.strip().strip('"')
    return fields, body


def wiki_pages() -> dict[str, str]:
    """Path -> blob of every page of the research on origin/main, fetching main first when the network allows."""
    subprocess.run(["git", "fetch", "--quiet", "origin", "main"], capture_output=True)
    pages = {}
    for line in git("ls-tree", "-r", "origin/main", "--", *PAGE_PATHS).splitlines():
        meta, path = line.split("\t", 1)
        if path.endswith(".md"):
            pages[path] = meta.split()[2]
    return pages


def sync_wiki(index: Index) -> str:
    wanted, indexed = wiki_pages(), index.docs("wiki")
    for path in indexed.keys() - wanted.keys():
        index.drop(path)
    changed = [path for path, blob in wanted.items() if indexed.get(path, {}).get("blob") != blob]
    for path in changed:
        page = git("cat-file", "-p", wanted[path])
        fields, body = front(page)
        pieces = split(body, prefix=page[:len(page) - len(body)])
        index.put("wiki", path, fields.get("title", path), pieces, {"blob": wanted[path]})
    removed = len(indexed.keys() - wanted.keys())
    return f"wiki: {len(changed)} pages indexed, {removed} removed" if changed or removed else ""


def sync_sources(index: Index, store: Store) -> str:
    current = set(store.pointers().values())
    indexed = set(index.docs("sources"))
    for sha in indexed - current:
        index.drop(sha)
    notes = [f"{sha[:12]}: {index_capture(index, store, sha)}" for sha in sorted(current - indexed)]
    return "\n".join([*notes, f"sources: {len(current - indexed)} indexed, {len(indexed - current)} removed"])


# Pending captures and failure reports

class Health:
    """Captures that failed to index, and hourly retries and daily reports of backend failures."""

    def __init__(self, name: str) -> None:
        self.folder = cache_root() / "index" / name
        self.pending_path = self.folder / "pending.json"
        self.reports_path = self.folder / "reports.json"

    @staticmethod
    def load(path: Path) -> dict:
        return json.loads(path.read_text()) if path.exists() else {}

    def save(self, path: Path, data: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")

    def defer(self, sha: str, replaces: str | None, error: BackendError) -> None:
        pending = self.load(self.pending_path)
        pending.setdefault("captures", {}).setdefault(sha, {"since": now(), "replaces": replaces})
        pending.update(last_attempt=time.time(), error=f"{error.kind}: {error}")
        self.save(self.pending_path, pending)
        self.report(error.kind, str(error))

    def retry(self, index: Index, store: Store) -> None:
        """Retry pending captures once an hour; print one status line while any remain."""
        pending = self.load(self.pending_path)
        captures = pending.get("captures", {})
        if captures and time.time() - pending.get("last_attempt", 0) >= RETRY_AFTER:
            pending["last_attempt"] = time.time()
            for sha, entry in list(captures.items()):
                try:
                    index_capture(index, store, sha, entry.get("replaces"))
                    del captures[sha]
                except BackendError as error:
                    pending["error"] = f"{error.kind}: {error}"
                    self.report(error.kind, str(error))
                    break
            self.save(self.pending_path, pending)
        if captures:
            since = min(entry["since"] for entry in captures.values())
            print(f"index: {len(captures)} captures await indexing since {since}; last error {pending.get('error')}; "
                  "retried hourly by sw fetch and sw search", file=sys.stderr)

    def report(self, kind: str, detail: str) -> None:
        """Count a failure of backend KIND; at most once a day, record it in that backend's sw-health Issue."""
        reports = self.load(self.reports_path)
        entry = reports.setdefault(kind, {"count": 0, "since": now(), "reported": 0})
        entry["count"] += 1
        entry["last"] = detail
        if time.time() - entry["reported"] >= REPORT_EVERY:
            text = f"{entry['count']} failures of {kind} since {entry['since']}; last: {detail}."
            if report_issue(kind, text):
                entry.update(count=0, since=now(), reported=time.time())
        self.save(self.reports_path, reports)


def report_issue(kind: str, text: str) -> bool:
    """Comment on the open sw-health Issue for KIND, or open one; False when GitHub is unreachable."""
    title = f"sw health: {kind} failures"
    try:
        found = json.loads(subprocess.run(
            ["gh", "issue", "list", "--label", "sw-health", "--state", "open", "--json", "number,title"],
            capture_output=True, text=True, check=True).stdout)
        number = next((issue["number"] for issue in found if issue["title"] == title), None)
        if number:
            subprocess.run(["gh", "issue", "comment", str(number), "--body", text], capture_output=True, check=True)
        else:
            subprocess.run(["gh", "label", "create", "sw-health", "--color", "B60205",
                            "--description", "Failures that sw reports on its own"], capture_output=True)
            subprocess.run(["gh", "issue", "create", "--title", title, "--label", "sw-health", "--body", text],
                           capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return False


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def index_new_capture(store: Store, name: str, sha: str, replaces: str | None) -> str:
    """Index a capture that sw fetch just made current; on failure keep it pending instead of failing the fetch."""
    health = Health(name)
    try:
        index = Index(name)
        index.ensure()
        health.retry(index, store)
        return index_capture(index, store, sha, replaces)
    except BackendError as error:
        health.defer(sha, replaces, error)
        return f"not indexed yet ({error.kind}: {error}); sw fetch and sw search retry it hourly"


# Output

def source_pages(worktree: Path) -> dict[str, str]:
    """sha256 of each source page's current capture -> page slug, in this worktree."""
    pages = {}
    for path in (worktree / "sources").glob("*.md"):
        match = re.search(r'^sha256:\s*"?([0-9a-f]{64})', path.read_text(), re.M)
        if match:
            pages[match.group(1)] = path.stem
    return pages


def snippet(text: str, width: int = 320) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= width else flat[:width].rsplit(" ", 1)[0] + " ..."


def show(groups, store: Store, worktree: Path) -> None:
    pages = source_pages(worktree)
    for rank, group in enumerate(groups, 1):
        hit = group.hits[0].payload
        where = " > ".join(hit["headings"]) or hit["title"]
        first, last = hit["lines"]
        if hit["scope"] == "sources":
            page = pages.get(hit["doc"], "(no source page)")
            print(f"{rank}. [sources] {page} | {hit['title']}")
            print(f"   {hit['url']}")
            print(f"   sha256 {hit['doc']} | {where}")
            copy = store.cache / hit["doc"] / "content.md"
            if not copy.exists():
                store.restore(hit["doc"])
            print(f"   {copy}:{first}-{last}")
        else:
            print(f"{rank}. [wiki] {hit['title']}")
            print(f"   {hit['doc']}:{first}-{last} | {where}")
        print(f"   {snippet(hit['text'])}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("query", nargs="?")
    parser.add_argument("--in", dest="scope", choices=("sources", "wiki"), help="search only this scope")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--sync", action="store_true", help="index every current capture and page")
    args = parser.parse_args()
    missing = [name for name in CREDENTIALS if not os.environ.get(name)]
    if missing:
        print(f"sw search: missing {', '.join(missing)}; run it as sw search so secretspec supplies them", file=sys.stderr)
        return 1
    if bool(args.query) == args.sync:
        parser.error("give a QUERY or --sync")

    try:
        name = research()
        store = Store(name)
        health = Health(name)
    except FetchError as error:
        print(f"sw search: {error}", file=sys.stderr)
        return 1
    try:
        index = Index(name)
        index.ensure()
        if args.sync:
            index.collection = index.pipeline_collection
            print(sync_sources(index, store))
            print(sync_wiki(index) or "wiki: up to date")
            index.move_alias()
            return 0
        health.retry(index, store)
        note = sync_wiki(index)
        if note:
            print(note, file=sys.stderr)
        show(index.search(args.query, args.scope, args.limit), store, Path(git("rev-parse", "--show-toplevel").strip()))
        return 0
    except BackendError as error:
        health.report(error.kind, str(error))
        print(f"sw search: {error.kind} failed: {error}", file=sys.stderr)
    except FetchError as error:
        if error.kind:
            health.report(error.kind, error.detail or "failed")
        print(f"sw search: {error}", file=sys.stderr)
    return 1

if __name__ == "__main__":
    raise SystemExit(main())
