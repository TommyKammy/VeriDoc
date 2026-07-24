# MVP Acceptance Gap Register

This register freezes the Phase12.5 P12G-01 reconciliation. A closed issue,
component test, or nearby implementation is not acceptance evidence by itself.
Missing authoritative decisions or run evidence remain fail-closed.

## Evidence snapshot

- Reproduction revision:
  the reachable commit that contains this register version; resolve it with
  `git log -1 --format=%H -- docs/mvp-acceptance-gap-register.md`
- Product/harness base commit:
  `9981ffb9f3e633faedf5bc5c2bd3d5a4845424b7`
- Reproduction checkout:
  `git checkout --detach "$(git log -1 --format=%H -- docs/mvp-acceptance-gap-register.md)"`
- Criteria source Git blob:
  `7843bf248fd85c0957ecdb4ffae903980eecb001`
- Evaluator Git blob:
  `c54aac39de48eedbe4d2aabb0c0690fdfd7a8b6f`
- Generated at: `2026-07-24` (Asia/Tokyo)
- PDF evaluation prerequisite:
  `python3 -m pip install -r requirements-pdf-eval.txt`
- Command: `python3 scripts/evaluate_dataset.py --mvp-acceptance-report`
- Dataset manifest: `datasets/mvp_evaluation_manifest_v1.json`
- Dataset manifest SHA-256:
  `a9374c81d4ff83cfce405582affd1c001216680ee8c53fa4e6acd0ade04abbd4`
- Criteria source: `docs/mvp-acceptance-traceability.md`
- Report result: `item_count=20`, unique item IDs `20`,
  `overall_decision=fail` (`pass=12`, `fail=8`)
- Harness result: `case_count=5`, `acceptance_status=pass`
  (`pass=5`, `fail=0`, `unknown=0`)

The containing revision above was checked out, the criteria and evaluator blob
IDs were verified with `git rev-parse HEAD:<repo-relative-path>`, and the PDF
evaluation prerequisite was installed before the recorded command. Resolving
the containing revision from reachable history avoids relying on a PR-only
commit that can disappear after squash merge. The command emits JSON to
standard output. The facts below were read from that single invocation, so the
five cases share the containing revision, manifest, dependency set, limits, and
criteria snapshot above. The product/harness base commit is a comparison
anchor, not a checkout instruction. Without the prerequisite, the PDF cases
fail at the dependency boundary and their failure reasons are not comparable
to the extractor-level facts recorded below.

## Gap classes

- `implementation_gap`: a product, harness, validator, or durable-record
  boundary is missing or currently fails its contract.
- `e2e_gap`: component coverage exists, but the directly applicable,
  snapshot-consistent acceptance path or metric is absent or failing.
- `human_evidence_gap`: a real reviewer, participant, or comparison record is
  required and has not been produced.
- `decision_gap`: an authoritative scope or acceptance decision is absent.

## Twenty-item reconciliation

| ID | Current status | Raw fact at the evidence snapshot | Gap class | Owner boundary | Evidence required | Follow-up P12G Issue |
| --- | --- | --- | --- | --- | --- | --- |
| AC-UI | 一部達成 / fail | `tests/test_mvp_browser_e2e.py` runs one real-browser upload, settings failure/retry, job, preview, approval, primary download, hash, and audit scenario under one `p12g03-...` correlation ID, then records keyboard-only warning-to-bbox and high-risk review state transitions with visible-focus evidence. | `e2e_gap` | codex | Carry the fixed browser evidence into the final accepted-scope rollup. | P12G-12 |
| AC-TEMPLATE | 達成 / pass | The approved `phase12-mvp-v1` manifest and one report snapshot contain exactly five structured, passing case results across Word, Excel, text PDF, scanned PDF, and record PDF. The scanned result remains explicitly fail-closed without fabricated OCR text. | `none` | — | Preserve the fixed manifest, completeness gate, and per-case structured results. | — |
| AC-QUALITY | 達成 / pass | The snapshot reports 56/56 cell/content matches (100%, threshold 80%) with recomputable per-case numerators and denominators. The scanned contribution is one explicit-review block, not an OCR-accuracy claim. | `none` | — | Preserve the per-case cell/block validators and fail-closed unknown handling. | — |
| AC-PROVENANCE | 達成 / pass | The snapshot reports 12/12 source bindings (100%, threshold 95%): five audit source-hash bindings and seven direct source links across text/scanned/record PDF outputs. | `none` | — | Preserve direct source-link validation, audit source hashes, and per-case denominators. | — |
| AC-REVIEW | 達成 / pass | Component/browser evidence forces actual high-risk targets through review, and the five accepted cases explicitly report `no_targets`, zero misses, and zero auto-confirms. Outside the accepted OCR block, `authoritative review decision is required`; a missing/unknown per-case metric or any miss/auto-confirm rejects the rollup. | `none` | — | Preserve the high-risk guards, browser target, and fail-closed rollup. | — |
| AC-EFFICIENCY | 未達 / fail | `docs/mvp-scope-decisions.md` approves the baseline task, cohort, training, timing boundary, comparison method, and rejection conditions; no protocol/schema or measured 30%+ human result exists. | `human_evidence_gap` | manual/hybrid | Versioned protocol/schema followed by a real, reproducible manual-versus-VeriDoc comparison. | P12G-13 |
| AC-PERFORMANCE | 達成 / pass | One snapshot retains 15 passing case/dimension observations and the existing 10 s processing, 2 MiB input, and 30 s timeout limits. Missing, unknown, or inconsistent values reject the rollup. | `none` | — | Preserve the limits, maxima, and per-case observations. | — |
| AC-AUDIT | 達成 / pass | `tests/test_mvp_browser_e2e.py` binds browser run, harness result, artifact hashes, actor/decision, version lineage, timestamp, and complete job/review hash chains to one correlation ID and emits a fail-closed acceptance snapshot. | `none` | — | Preserve the correlated E2E and persistence audit contract tests. | — |
| AC-AUTH | 達成 / pass | The product server fails closed when `VERIDOC_LOCAL_AUTH_TOKENS` is unset, approval requires a preceding distinct-reviewer edit, and `scripts/ci/mvp_browser_e2e.py` retains six-role read/sensitive allow-deny probes plus missing, rejected, forbidden, cleared, and re-authenticated UI states against decision revision `p12g-02-v1`. | `none` | — | Preserve the fail-closed product, role-matrix, token-lifecycle, and segregation evidence tests. | — |
| AC-SECURITY | 一部達成 / fail | The harness and negative tests enforce the local-only boundary, but the clean checkout does not retain a concrete run `evidence.json` that the report can validate before claiming `external_ai_api_send_count=0`. | `e2e_gap` | codex | Retain and validate the machine-readable network observation for the reported acceptance run. | P12G-11 |
| FC-HIGH-RISK | 達成 / pass | Component/browser guards cover actual high-risk targets; the accepted five-case snapshot explicitly records zero targets, zero misses, and zero auto-confirms, and refuses missing/unknown case metrics. | `none` | — | Preserve the high-risk target fixture and fail-closed zero-count rollup. | — |
| FC-EVIDENCE | 達成 / pass | The browser E2E mutates provenance, audit-event, hash/version, and correlation boundaries and requires structured failure codes; an artifact alone cannot produce an acceptance pass. | `none` | — | Preserve the fail-closed negative scenarios in `tests/test_mvp_browser_e2e.py`. | — |
| FC-EXTERNAL-SEND | 一部達成 / fail | External endpoint configuration plus HTTP, DNS, socket, and redirect cases fail closed in the harness, but no concrete retained run evidence is bound to this report snapshot. | `e2e_gap` | codex | Retain and validate the zero-attempt network observation together with the focused negative cases. | P12G-11 |
| FC-REVIEW-UI | 一部達成 / fail | A browser trace records keyboard-only warning/remediation review, original bbox jump, visible focus, edit/needs-fix/reject transitions, and a fail-closed approval attempt for the committed high-risk fixture; the final accepted-scope rollup remains absent. | `e2e_gap` | codex | Carry the fixed keyboard/review-UI evidence into the final accepted-scope rollup. | P12G-12 |
| FC-REPRODUCIBILITY | 一部達成 / fail | The harness can seal and validate a package that pins commit, inputs, inference/browser configuration, dependencies, versions, and commands, but this report snapshot does not retain a concrete package plus successful equivalence result. | `e2e_gap` | codex | Retain and validate the sealed package and successful equivalence result for the reported run. | P12G-11 |
| EM-USER-REVIEW | 未達 / fail | The task, cohort, training, timing, and comparison scope is approved in `docs/mvp-scope-decisions.md`; no versioned execution protocol/schema, miss/over-detection result, or reviewer record exists. | `human_evidence_gap` | manual/hybrid | Versioned protocol and evidence schema ready for the later human execution. | P12G-13 |
| EM-E2E | 一部達成 / fail | A repo-owned browser run emits screenshots, trace, API result, downloaded artifact, audit artifact, and correlation metadata for one representative case; the versioned five-case report is zero `fail`, zero `unknown`, and five `pass`, with persisted review decisions for Word and Excel plus structured text-PDF/scanned-PDF/record-PDF evidence. | `e2e_gap` | codex | Retain the rerun package and aggregate all accepted evidence from one committed snapshot. | P12G-11, P12G-12 |
| OD-TEMPLATES | 達成 / pass | `TommyKammy` approved all five manifest cases at product commit `584ef2db12a6676abb65f75de1ec38145e06b487` and manifest revision `phase12-mvp-v1`; the report recomputes the approved case, fixture, source-policy, and expectation contract hash. | `none` | — | Preserve `docs/mvp-scope-decisions.md`; contract drift fails this item until renewed approval. | — |
| OD-EFFICIENCY-SCOPE | 達成 / pass | Decision revision `p12g-02-v1` fixes the baseline task, minimum three-person pseudonymous cohort, training, timing boundaries, paired median comparison, 30% target, and invalidation conditions; the report validates the revision-bound canonical section hash. | `none` | — | Preserve the approved scope; protocol-scope drift fails this item until renewed approval, while P12G-13 defines its versioned protocol and evidence schema. | — |
| OD-SEGREGATION | 達成 / pass | Decision revision `p12g-02-v1` fixes the six-role `ROLE_PERMISSIONS` target matrix, mandatory authentication and preceding distinct-actor review boundaries, known implementation gaps, and explicit Phase13 carryover; the report validates the revision-bound canonical section and matrix hashes, while P12G-10 implements and proves the MVP deny paths. | `none` | — | Preserve the approved scope; any deny-path, carryover, or matrix drift fails this item. | — |

## Five representative cases

All evaluation cells below come from the same invocation recorded in
`Evidence snapshot`. `review_decision` is authoritative only when the
conversion/review boundary records it; it is not inferred from expected status,
warnings, or review-item count.

| Case | Category | `acceptance_status` | Evaluation results | Failure reason | Review decision |
| --- | --- | --- | --- | --- | --- |
| mvp-word-001 | Word | `pass` | artifact/pass; audit/pass; input_size/pass; processing_time/pass; timeout/pass; review/pass | none | persisted approver decisions for 2 review items; artifact/audit/result share each decision ID and item/decision version |
| mvp-excel-001 | Excel | `pass` | artifact/pass; audit/pass; input_size/pass; processing_time/pass; timeout/pass; review/pass | none | persisted approver decisions for 3 review items; artifact/audit/result share each decision ID and item/decision version |
| mvp-text-pdf-001 | text PDF | `pass` | artifact/pass; audit/pass; input_size/pass; processing_time/pass; timeout/pass; review/pass | none; expected table boundaries, blank-cell preservation, editable cells, and source evidence pass. | not required because the conversion emitted no review item |
| mvp-scanned-pdf-001 | scanned PDF | `pass` | artifact/pass; audit/pass; input_size/pass; processing_time/pass; timeout/pass; review/pass | none; dedicated validator proves the explicit placeholder, review guard, structured warning/reason/remediation, and per-block source linkage without invented OCR text. | not applicable; accepted fail-closed OCR boundary remains `requires_review` and does not fabricate human approval |
| mvp-record-pdf-001 | record PDF | `pass` | artifact/pass; audit/pass; input_size/pass; processing_time/pass; timeout/pass; review/pass | none; section order, body completeness, expected content, and per-block source linkage all pass. | not required because the conversion emitted no review item |

## P12G-02 through P12G-13 handoff

| Issue | Execution boundary | Depends on | Fixed scope from this register |
| --- | --- | --- | --- |
| P12G-02 | manual/hybrid | P12G-01 | Approve template scope, efficiency protocol scope, and MVP/Phase13 segregation boundary; Codex may prepare records but cannot supply approval. |
| P12G-03 | codex | P12G-01 | Record one browser upload-to-download scenario plus recovery path, using one run/correlation ID. |
| P12G-04 | codex | P12G-01 | Connect authoritative reviewer/approver decisions to Word/Excel harness, artifact, audit, and snapshot; missing/forbidden decisions stay blocked. |
| P12G-05 | codex | P12G-01 | Fix text PDF table extraction at the general extraction boundary and satisfy the declared artifact/review contract. |
| P12G-06 | hybrid | P12G-02 | Validate scanned-PDF OCR as an explicit accepted block, not fabricated extraction or an unavailable validator. |
| P12G-07 | codex | P12G-01 | Implement record-PDF-to-Word content validation for order, completeness, and source linkage. |
| P12G-08 | codex | P12G-03, P12G-04 | Add browser E2E for warning/review/high-risk/keyboard behavior and keep high-risk fail-closed. |
| P12G-09 | codex | P12G-03, P12G-04 | Prove provenance/audit completeness and rejection of missing or tampered evidence across one correlation ID. |
| P12G-10 | codex | P12G-02, P12G-03 | Prove role allow/deny, token lifecycle, and the approved segregation boundary in API and UI. |
| P12G-11 | codex | P12G-03 | Capture zero external sends and a pinned, equivalence-checked rerun package. |
| P12G-12 | codex | P12G-04–P12G-07, P12G-09, P12G-11 | Aggregate five-case quality, provenance, high-risk, performance, size, and timeout metrics without mixed snapshots. |
| P12G-13 | hybrid | P12G-02 | Define the human-review protocol and recomputable, privacy-safe evidence schema; real participant execution remains manual. |

No row in this register grants an acceptance pass. A later report must derive
status again from authoritative decisions and one committed evidence snapshot.
