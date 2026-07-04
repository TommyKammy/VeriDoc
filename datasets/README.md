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
- Evaluation cases may only reference manifest entries with an explicit
  repo-relative fixture path under `datasets/fixtures/`.
- Placeholder slots document required future source types, but they are not
  scorable fixtures until a synthetic or anonymized file path is added.
- Each fixture entry must declare its source type, anonymization status,
  confidentiality class, and whether it is safe for public repository review.
- High-risk gold labels live under `datasets/gold/` and must point to explicit
  fixture records and block ids. Missing fixture linkage is treated as invalid.

Evaluation harness:

```sh
python3 scripts/evaluate_dataset.py --cases datasets/gold/evaluation_cases_v0.json
```

The harness emits JSON metrics for table extraction rate, cell match rate,
source linkage rate, and false auto-confirmed count.

LLM stability spike harness:

```sh
python3 scripts/evaluate_dataset.py --llm-stability-runs datasets/gold/llm_stability_runs_v0.json
```

The stability harness emits JSON metrics for same-input N-run plan agreement,
confirmed-value agreement, distinct output counts, and anonymized representative
unstable examples. It also reports schema failure rate, repair success rate,
deterministic fallback rate, and external AI API guard violation count from
public synthetic run outcome records.

MVP acceptance comparison harness:

```sh
python3 scripts/evaluate_dataset.py --poc-comparison datasets/gold/poc_mode_comparison_v1.json
```

The comparison harness emits JSON metrics for required PoC modes, high-risk
false auto-confirmed count, review item and warning deltas, and manual
correction-time reduction for the public synthetic representative template.

Phase8 LLM stability evaluation handoff:

```sh
python3 scripts/evaluate_dataset.py --llm-stability-report
```

The report combines the stability and PoC mode comparison records into a
machine-readable `veridoc-llm-stability-evaluation/v0` JSON object that Phase9
can inherit without requiring real confidential records or external AI API
transmission.

This dataset is only a Phase 0 evaluation fixture set. It does not claim GMP
fitness, production readiness, or suitability for business use.
