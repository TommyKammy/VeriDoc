# MVP Acceptance Report Sample

Generate the current machine-readable Phase12 acceptance report from the
repository root:

```bash
python3 scripts/evaluate_dataset.py --mvp-acceptance-report
```

The command evaluates `datasets/mvp_evaluation_manifest_v1.json`, reads every
15.3 row from `docs/mvp-acceptance-traceability.md`, and emits
`veridoc-mvp-acceptance-report/v1` JSON. The report contains the complete
harness result set and `metrics_rollup` under `evidence_snapshot`, and its
SHA-256 binds those results to the exact traceability text, scope-decision
record, commit, manifest revision/hash, dependency-set hashes, runtime,
validated `OD-*` inputs, and item decisions used for the report. Processing-time
measurements are live evidence, so the snapshot hash is intentionally not
pinned in this sample.

## Current Sample Outcome

With the committed default inputs, the report contains 20 acceptance items and
fails closed with twelve `pass` and eight `fail`. The five-case metrics rollup
passes with 56/56 quality matches, 12/12 provenance bindings, zero high-risk
misses/auto-confirms, zero audit-level external-send guard violations, and all
15 performance/size/timeout observations within the existing limits. The three approved `OD-*`
items pass only while the live manifest, canonical efficiency- and
segregation-scope sections, and `ROLE_PERMISSIONS` contracts match the
revision-bound decision pins; scope drift fails the affected item until renewed
approval. A passing harness case does not promote a broader 15.3 item whose
required evidence remains incomplete. Missing/unknown metrics and case-level
failures cannot be hidden by the aggregate rate and reject the rollup. The
six metric-backed rows are also revalidated from their live rollup dimensions,
so a non-passing dimension downgrades that item instead of leaving a static
traceability `pass` in the emitted snapshot. The overall decision also remains
`fail` whenever the live harness or metrics
rollup is not `pass`; a dirty worktree or missing commit identity makes the
commit-bound snapshot integrity dimension non-passing.

```json
{
  "schema_version": "veridoc-mvp-acceptance-report/v1",
  "criteria_source": "docs/mvp-acceptance-traceability.md",
  "summary": {
    "overall_decision": "fail",
    "item_count": 20,
    "decision_counts": {"pass": 12, "fail": 8}
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
- `evidence.decision_input_validation`: for `OD-*` rows, the result of checking
  approval metadata and the applicable manifest, canonical decision-section,
  or permission-matrix pin.
- `unmet`: the explicit incomplete boundary for every failed item.
- `carryover_phases`: only phases explicitly named by that row.

Missing, duplicate, malformed, or unrecognized traceability rows stop report
generation instead of silently reducing the read set or guessing a decision.
