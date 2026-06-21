from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any

from core.parsers import pdf_text_extraction


def test_compare_pdf_text_extractors_reports_missing_pymupdf(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    original_import = builtins.__import__

    def import_without_fitz(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "fitz":
            raise ImportError("fitz unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_fitz)

    candidates = pdf_text_extraction.compare_pdf_text_extractors(pdf_path)

    assert candidates[0].name == "pymupdf"
    assert candidates[0].status == "not-installed"
    assert "requirements-pdf-eval.txt" in candidates[0].notes
