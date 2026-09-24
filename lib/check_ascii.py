#!/usr/bin/env python3
"""Reject non-ASCII Git metadata, added content, and binary files."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


def git(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout


def describe_bytes(data: bytes) -> str:
    return " ".join(f"0x{byte:02x}" for byte in data if byte > 0x7F)


def check_value(label: str, data: bytes) -> list[str]:
    if data.isascii():
        return []
    return [f"{label}: non-ASCII bytes {describe_bytes(data)}"]


def diff_args(staged: bool, revision_range: str | None) -> list[str]:
    if staged:
        return ["diff", "--cached"]
    if revision_range is None or revision_range.startswith("-"):
        raise ValueError("a safe revision range is required")
    return ["diff", revision_range]


def check_diff(staged: bool, revision_range: str | None) -> list[str]:
    base = diff_args(staged, revision_range)
    errors: list[str] = []

    names = git(*base, "--name-only", "-z", "--diff-filter=ACMR", "--")
    for name in names.rstrip(b"\0").split(b"\0") if names else []:
        errors.extend(check_value(f"path {name!r}", name))

    numstat = git(*base, "--numstat", "--diff-filter=ACMR", "--")
    for line in numstat.splitlines():
        added, deleted, path = line.split(b"\t", 2)
        if added == b"-" and deleted == b"-":
            display_path = path.decode("ascii", "backslashreplace")
            errors.append(f"{display_path}: binary files are not allowed")

    patch = git(
        *base,
        "--no-ext-diff",
        "--no-color",
        "--unified=0",
        "--diff-filter=ACMR",
        "--",
    )
    current_path = "unknown"
    new_line = 0
    for line in patch.splitlines():
        if line.startswith(b"+++ "):
            current_path = line[4:].decode("ascii", "backslashreplace")
            continue
        match = re.match(rb"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if match:
            new_line = int(match.group(1))
            continue
        if line.startswith(b"+"):
            content = line[1:]
            if not content.isascii():
                errors.append(
                    f"{current_path}:{new_line}: non-ASCII bytes {describe_bytes(content)}"
                )
            new_line += 1
        elif line.startswith(b" "):
            new_line += 1
    return errors


def check_commit_messages(revision_range: str) -> list[str]:
    if revision_range.startswith("-"):
        raise ValueError("a safe revision range is required")
    records = git("log", "--format=%H%x00%B%x00", revision_range, "--").split(b"\0")
    errors: list[str] = []
    for index in range(0, len(records) - 1, 2):
        commit_hash = records[index].decode("ascii")
        message = records[index + 1]
        errors.extend(check_value(f"commit {commit_hash} message", message))
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="check staged additions")
    mode.add_argument("--range", dest="revision_range", help="check additions in RANGE")
    mode.add_argument("--commits", metavar="RANGE", help="check commit messages in RANGE")
    mode.add_argument("--file", type=Path, help="check one file")
    mode.add_argument("--text", nargs=2, metavar=("LABEL", "VALUE"), help="check text")
    mode.add_argument("--env", action="append", metavar="NAME", help="check an environment value")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.staged or args.revision_range:
            errors = check_diff(args.staged, args.revision_range)
        elif args.commits:
            errors = check_commit_messages(args.commits)
        elif args.file:
            errors = check_value(str(args.file), args.file.read_bytes())
        elif args.text:
            errors = check_value(args.text[0], args.text[1].encode())
        else:
            errors = []
            for name in args.env:
                errors.extend(check_value(name, os.environ.get(name, "").encode()))
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"ASCII check failed: {error}", file=sys.stderr)
        return 2

    if errors:
        print("ASCII policy violation:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
