# MVP Acceptance Report Sample

Generate the current machine-readable Phase12 acceptance report from the
repository root:

```bash
python3 scripts/evaluate_dataset.py --mvp-acceptance-report
```

The command evaluates `datasets/mvp_evaluation_manifest_v1.json`, reads every
15.3 row from `docs/mvp-acceptance-traceability.md`, and emits
`veridoc-mvp-acceptance-report/v1` JSON. The report contains the complete
harness result set under `evidence_snapshot`, and its SHA-256 binds that result
set to the exact traceability text used for the decision. Processing-time
measurements are live evidence, so the snapshot hash is intentionally not
pinned in this sample.

## Current Sample Outcome

With the committed default inputs, the report contains 20 acceptance items and
fails closed: all 20 are `fail` because the traceability baseline still marks
each item `一部達成` or `未達`. A passing harness case does not promote a broader
15.3 item whose required evidence remains incomplete. The overall decision also
remains `fail` whenever the live harness overall status is `fail` or `unknown`.

```json
{
  "schema_version": "veridoc-mvp-acceptance-report/v1",
  "criteria_source": "docs/mvp-acceptance-traceability.md",
  "summary": {
    "overall_decision": "fail",
    "item_count": 20,
    "decision_counts": {"pass": 0, "fail": 20}
  },
  "carryovers": {
    "phase13": ["OD-SEGREGATION"],
    "phase14": []
  }
}
```

`phase14` is explicit and empty because the current authoritative table does
not assign a 15.3 item to Phase14. It must not be populated by inference.

## Item Contract

Each object in `items` records:

- `decision`: binary `pass` or `fail` for the current 15.3 gate.
- `evidence.traceability`: the evidence boundary copied from the authoritative
  traceability row.
- `evidence.harness_refs`: directly applicable paths into the single harness
  snapshot; an empty list means the current harness does not prove that item.
- `unmet`: the explicit incomplete boundary for every failed item.
- `carryover_phases`: only phases explicitly named by that row.

Missing, duplicate, malformed, or unrecognized traceability rows stop report
generation instead of silently reducing the read set or guessing a decision.
