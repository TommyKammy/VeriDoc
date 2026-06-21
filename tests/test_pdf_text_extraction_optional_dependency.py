from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any

from core.parsers import pdf_text_extraction


REPO_ROOT = Path(__file__).resolve().parents[1]


def _requirement_names(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        requirement = line.strip()
        if not requirement or requirement.startswith("#") or requirement.startswith("-"):
            continue
        names.add(
            requirement.split(";", 1)[0]
            .split("[", 1)[0]
            .split("<", 1)[0]
            .split(">", 1)[0]
            .split("=", 1)[0]
            .strip()
            .lower()
        )
    return names


def _requirement_specs(path: Path) -> dict[str, str]:
    specs: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        requirement = line.strip()
        if not requirement or requirement.startswith("#") or requirement.startswith("-"):
            continue
        name = (
            requirement.split(";", 1)[0]
            .split("[", 1)[0]
            .split("=", 1)[0]
            .split("<", 1)[0]
            .split(">", 1)[0]
            .split("~", 1)[0]
            .split("!", 1)[0]
            .strip()
            .lower()
        )
        specs[name] = requirement
    return specs


def test_pdf_eval_requirements_include_all_comparison_candidates() -> None:
    default_requirements = _requirement_names(REPO_ROOT / "requirements.txt")
    eval_requirements = _requirement_names(REPO_ROOT / "requirements-pdf-eval.txt")
    eval_specs = _requirement_specs(REPO_ROOT / "requirements-pdf-eval.txt")
    eval_requirements_text = (REPO_ROOT / "requirements-pdf-eval.txt").read_text(encoding="utf-8")

    assert "pypdf" not in default_requirements
    assert "pymupdf" not in default_requirements
    assert {"camelot-py", "pdfplumber", "pypdf", "pymupdf"} <= eval_requirements
    assert eval_specs["camelot-py"].startswith("camelot-py")
    assert eval_specs["pdfplumber"].startswith("pdfplumber==")
    assert eval_specs["pypdf"].startswith("pypdf==")
    assert eval_specs["pymupdf"].startswith("pymupdf==")
    assert 'pypdf==3.17.4; python_version < "3.12"' in eval_requirements_text
    assert 'pypdf==5.9.0; python_version >= "3.12"' in eval_requirements_text


def test_compare_pdf_text_extractors_reports_missing_pymupdf(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    original_import = builtins.__import__

    def import_without_pymupdf(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pymupdf":
            raise ImportError("pymupdf unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_pymupdf)

    candidates = pdf_text_extraction.compare_pdf_text_extractors(pdf_path)

    assert candidates[0].name == "pymupdf"
    assert candidates[0].status == "not-installed"
    assert "requirements-pdf-eval.txt" in candidates[0].notes
