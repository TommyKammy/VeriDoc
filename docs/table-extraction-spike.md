# PDF table extraction spike

Issue #3 compares Camelot lattice/stream and pdfplumber table extraction on
synthetic or sanitized PDF samples only. Real confidential documents and real
batch records must not be committed to this repository.

## Minimal comparison contract

The spike report records each candidate's status, detected table count, first
table row count, first table column count, and whether every extracted cell has
a boundary box. A candidate fails closed when it cannot provide the expected
ruled-table shape or cell boundaries.

## Provisional selection

Camelot lattice is the provisional candidate for ruled tables because it is
designed for line-bounded table structure and exposes cell boundaries. Camelot
stream remains a comparison candidate for whitespace-delimited tables, and
pdfplumber remains a secondary comparison candidate.

## Unresolved risks

- Camelot lattice depends on optional native/image-processing components, so CI
  and local evaluation must install `requirements-pdf-eval.txt` before claiming
  extractor-level results.
- Stream extraction can over-split columns on ruled-table samples; the report
  treats lattice/stream shape drift as an explicit mismatch.
- This spike does not claim GMP suitability or production readiness.
