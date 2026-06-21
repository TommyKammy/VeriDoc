#!/usr/bin/env python3
"""Minimal repository hygiene checks for VeriDoc.

This intentionally avoids project-specific build/test assumptions so it can run
before the Python, Node, and document-conversion stacks are bootstrapped.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_FILES = (
    "README.md",
    "LICENSE",
    ".gitignore",
)

FORBIDDEN_TRACKED_PREFIXES = (
    ".env",
    ".hermes/",
    ".local/",
    ".codex-supervisor/",
    "datasets/raw/",
    "datasets/private/",
    "datasets/confidential/",
    "datasets/incoming/",
    "datasets/output/",
    "datasets/cache/",
    "models/",
    "outputs/",
    "exports/",
    "artifacts/",
    "converted/",
    "rendered/",
    "logs/",
    "tmp/",
    "temp/",
)

FORBIDDEN_TRACKED_SUFFIXES = (
    ".gguf",
    ".safetensors",
    ".onnx",
    ".pt",
    ".pth",
    ".ckpt",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".sqlite",
    ".sqlite3",
    ".db",
)


def run_git(*args: str) -> list[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        print(proc.stderr.strip() or proc.stdout.strip(), file=sys.stderr)
        raise SystemExit(proc.returncode)
    return [line for line in proc.stdout.splitlines() if line]


def is_forbidden(path: str) -> bool:
    lowered = path.lower()
    return (
        any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in FORBIDDEN_TRACKED_PREFIXES)
        or any(lowered.endswith(suffix) for suffix in FORBIDDEN_TRACKED_SUFFIXES)
    )


def main() -> int:
    failures: list[str] = []

    for relpath in REQUIRED_FILES:
        if not (REPO_ROOT / relpath).is_file():
            failures.append(f"missing required file: {relpath}")

    tracked = run_git("ls-files")
    forbidden = [path for path in tracked if is_forbidden(path)]
    if forbidden:
        failures.append("forbidden tracked file(s): " + ", ".join(forbidden))

    for markdown in (REPO_ROOT / "README.md",):
        if markdown.exists() and markdown.read_text(encoding="utf-8").strip() == "":
            failures.append(f"empty markdown file: {markdown.relative_to(REPO_ROOT)}")

    if failures:
        print("Repository hygiene check failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("Repository hygiene check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
