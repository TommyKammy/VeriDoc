#!/usr/bin/env python3
"""Validate the documented desktop package path without building installers."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ADR_PATH = REPO_ROOT / "adr" / "ADR-004-desktop-distribution-update.md"
DESKTOP_README_PATH = REPO_ROOT / "apps" / "desktop" / "README.md"
DESKTOP_ROOT = REPO_ROOT / "apps" / "desktop"

PACKAGE_COMMAND = "npm --prefix apps/desktop run tauri -- build --bundles nsis"
TAURI_BUILD_ARGUMENTS = "tauri build --bundles nsis"
REQUIRED_TERMS = {
    ADR_PATH: (
        "Tauri v2 NSIS installer",
        "Tauri updater",
        "TAURI_SIGNING_PRIVATE_KEY",
        "TAURI_SIGNING_PRIVATE_KEY_PASSWORD",
        "VERIDOC_DESKTOP_UPDATE_ENDPOINT",
        "tauri-plugin-updater",
        "lib.rs",
        "bundle.createUpdaterArtifacts",
        "bundle.windows.signCommand",
        "Windows installer code-signing certificate",
        "Windows 10 22H2 or later",
        "rollback",
        "managed endpoint distribution",
        PACKAGE_COMMAND,
    ),
    DESKTOP_README_PATH: (
        "Distribution and Update",
        "Tauri v2 NSIS installer",
        "Tauri updater",
        "scripts/desktop_package_dry_run.py --dry-run",
        "TAURI_SIGNING_PRIVATE_KEY",
        "TAURI_SIGNING_PRIVATE_KEY_PASSWORD",
        "VERIDOC_DESKTOP_UPDATE_ENDPOINT",
        "tauri-plugin-updater",
        "lib.rs",
        "bundle.createUpdaterArtifacts",
        "bundle.windows.signCommand",
        "Windows installer code-signing certificate",
    ),
}
FORBIDDEN_FRAGMENTS = ("/" + "Users" + "/", "C:" + "\\Users" + "\\")


def validate_document(path: Path, required_terms: tuple[str, ...]) -> list[str]:
    failures: list[str] = []
    if not path.is_file():
        return [f"missing file: {path.relative_to(REPO_ROOT)}"]

    text = path.read_text(encoding="utf-8")
    for term in required_terms:
        if term not in text:
            failures.append(f"{path.relative_to(REPO_ROOT)} missing term: {term}")
    for fragment in FORBIDDEN_FRAGMENTS:
        if fragment in text:
            failures.append(
                f"{path.relative_to(REPO_ROOT)} contains workstation-local path fragment"
            )
    return failures


def run_dry_run() -> int:
    failures: list[str] = []
    if not DESKTOP_ROOT.is_dir():
        failures.append(f"missing desktop root: {DESKTOP_ROOT.relative_to(REPO_ROOT)}")

    for path, required_terms in REQUIRED_TERMS.items():
        failures.extend(validate_document(path, required_terms))

    if failures:
        print("Desktop package dry-run failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("Desktop package dry-run passed.")
    print(f"Desktop root: {DESKTOP_ROOT.relative_to(REPO_ROOT)}")
    print(f"Selected installer: Tauri v2 NSIS installer")
    print(f"Selected updater: Tauri updater")
    print(f"Package command: {PACKAGE_COMMAND}")
    print(
        "Required updater signing secrets: "
        "TAURI_SIGNING_PRIVATE_KEY, TAURI_SIGNING_PRIVATE_KEY_PASSWORD"
    )
    print(
        "Required installer signing: Windows installer code-signing certificate "
        "and bundle.windows.signCommand or equivalent trusted signer config"
    )
    print("Required update endpoint: VERIDOC_DESKTOP_UPDATE_ENDPOINT")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run VeriDoc desktop package/update prerequisites."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate documented packaging prerequisites without building",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if not args.dry_run:
        print("Refusing to build installers without --dry-run.", file=sys.stderr)
        print("Use --dry-run until the Tauri scaffold and signing lane exist.", file=sys.stderr)
        return 2
    return run_dry_run()


if __name__ == "__main__":
    raise SystemExit(main())
