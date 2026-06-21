# Evaluation Dataset Policy

`datasets/` contains only public, synthetic, or anonymized fixtures that are safe
to review in pull requests. Real confidential records, customer documents,
operator exports, OCR byproducts, and generated outputs must not be committed.

Allowed tracked areas:

- `datasets/fixtures/`: curated synthetic or anonymized source fixtures.
- `datasets/fixtures/manifest.json`: fixture inventory and placement rules.
- `datasets/gold/`: hand-authored answer data for evaluation.

Blocked areas are enforced by repository hygiene and `.gitignore`:

- `datasets/raw/`
- `datasets/private/`
- `datasets/confidential/`
- `datasets/incoming/`
- `datasets/output/`
- `datasets/cache/`

Fixture placement rules:

- Text PDFs, scanned PDFs, Word files, Excel files, and record excerpts must be
  listed in `datasets/fixtures/manifest.json` before use in evaluation.
- The initial public dataset may reference existing synthetic Document IR JSON
  samples instead of binary documents until converter fixtures are added.
- Each fixture entry must declare its source type, anonymization status,
  confidentiality class, and whether it is safe for public repository review.
- High-risk gold labels live under `datasets/gold/` and must point to explicit
  fixture records and block ids. Missing fixture linkage is treated as invalid.

This dataset is only a Phase 0 evaluation fixture set. It does not claim GMP
fitness, production readiness, or suitability for business use.
