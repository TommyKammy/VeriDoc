# GMP-08 Acceptance Evaluation

This note defines the public synthetic GMP-08 acceptance report shape for
record-PDF-equivalent evaluation evidence. It does not claim production GMP
fitness, complete formal QA validation, or authorize use with confidential
source records.

Run the report with:

```bash
python3 scripts/evaluate_dataset.py --gmp-acceptance datasets/gold/gmp_acceptance_v1.json
```

The report is anchored to `datasets/gold/poc_mode_comparison_v1.json` and
recomputes the high-risk false auto-confirmation count instead of trusting the
GMP acceptance JSON alone. Any high-risk auto-confirmed item keeps the report
from meeting the target.

The eight 15.7 criteria reported by `datasets/gold/gmp_acceptance_v1.json` are:

- `high_risk_review`
- `missed_detection_zero`
- `source_traceability`
- `originality`
- `audit_trail`
- `completeness`
- `reproducibility`
- `segregation_of_duties`

Evidence is intentionally limited to public synthetic fixtures, repo tests, and
repo documentation. Real confidential records, formal QA validation execution,
and external audit deliverables remain out of scope.
