# PDF text extraction spike

Issue #2 evaluates whether Phase0 can extract PDF text with page numbers and
coordinates from sanitized or synthetic samples only.

## Current candidate comparison

| Candidate | Result | bbox support | Notes |
| --- | --- | --- | --- |
| PyMuPDF | selected for the spike | yes | Provides page geometry and span-level text bboxes in PDF point coordinates. |
| pypdf | comparison candidate only | no | Useful for text-only fallback checks, but this spike does not treat it as satisfying bbox requirements. |

## Intermediate data contract

- Each extracted page is represented with a 1-based `page_number`, `width_pt`,
  and `height_pt`.
- Each text fragment carries its own 1-based `page_number` and `bbox`.
- `bbox` values are normalized as PDF points (`pt`) with a top-left origin:
  `x`, `y`, `width`, and `height`.
- Fragment order follows the extractor's page/block/line/span order and is not
  treated as a reading-order guarantee beyond the spike tests.

## Failure and missing-coordinate handling

- A missing source file raises `FileNotFoundError`.
- A PDF that cannot be opened raises `ValueError` and must not be silently
  converted into a successful extraction.
- Empty text spans and spans without bbox data are skipped because they cannot
  satisfy the downstream coordinate contract.
- If a candidate can extract text but cannot provide fragment-level bboxes, it
  remains a comparison result only and must not be promoted to a passing parser
  without an explicit coordinate source.
