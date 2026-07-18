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
  `f3c387300d1760d534508835a2ad463caa9fdac9`
- Evaluator Git blob:
  `113797aa7ffd7a615b6fdd5eb9c51d8e18536585`
- Generated at: `2026-07-18` (Asia/Tokyo)
- PDF evaluation prerequisite:
  `python3 -m pip install -r requirements-pdf-eval.txt`
- Command: `python3 scripts/evaluate_dataset.py --mvp-acceptance-report`
- Dataset manifest: `datasets/mvp_evaluation_manifest_v1.json`
- Dataset manifest SHA-256:
  `a9374c81d4ff83cfce405582affd1c001216680ee8c53fa4e6acd0ade04abbd4`
- Criteria source: `docs/mvp-acceptance-traceability.md`
- Report result: `item_count=20`, unique item IDs `20`,
  `overall_decision=fail` (`pass=0`, `fail=20`)
- Harness result: `case_count=5`, `acceptance_status=fail`
  (`pass=3`, `fail=2`, `unknown=0`)

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
| AC-UI | 一部達成 / fail | `tests/test_mvp_browser_e2e.py` now runs one real-browser upload, settings failure/retry, job, preview, approval, primary download, hash, and audit scenario under one `p12g03-...` correlation ID; keyboard/high-risk breadth remains open. | `e2e_gap` | codex | Extend the fixed browser evidence to the keyboard and complete high-risk review matrix. | P12G-08 |
| AC-TEMPLATE | 一部達成 / fail | The versioned manifest fixes five categories and Word, Excel, and record PDF pass, but the remaining two cases fail and the representative scope is not authoritatively approved. | `e2e_gap`, `decision_gap` | manual/hybrid then codex | Approved 3–5-template scope plus passing results from that manifest revision. | P12G-02, P12G-12 |
| AC-QUALITY | 未達 / fail | Word, Excel, and record PDF pass artifact/review/audit evaluation; text and scanned PDF remain `fail`, so no five-template 80%+ metric can be claimed. | `implementation_gap`, `e2e_gap` | codex/hybrid | Fix or explicitly validate the remaining text and scanned PDF boundaries, then publish per-template cell/content agreement. | P12G-05, P12G-06, P12G-12 |
| AC-PROVENANCE | 一部達成 / fail | Source-coordinate components exist, but no five-case source-link coverage result proves 95%+ or the original-document jump. | `e2e_gap` | codex | Snapshot-consistent source-link metric and browser-to-artifact provenance trace. | P12G-09, P12G-12 |
| AC-REVIEW | 一部達成 / fail | Word/Excel use persisted approver decisions and share decision/item versions across artifact, audit, and harness snapshots; missing/forbidden decisions and unresolved high-risk items are rejected. Browser evidence and dataset-wide zero misses remain absent. | `e2e_gap`, `human_evidence_gap` | codex then manual | Keyboard review flow and zero high-risk misses across the accepted dataset. | P12G-08, P12G-12 |
| AC-EFFICIENCY | 未達 / fail | No approved baseline task/cohort/timing boundary and no measured 30%+ result exist. | `human_evidence_gap`, `decision_gap` | manual/hybrid | Approved protocol/schema followed by a real, reproducible manual-versus-VeriDoc comparison. | P12G-02, P12G-13 |
| AC-PERFORMANCE | 一部達成 / fail | All five live results pass input-size, processing-time, and timeout evaluations, but no accepted five-case metrics rollup exists. | `e2e_gap` | codex | One committed rollup retaining the 10 s, 2 MiB, and 30 s limits for all accepted cases. | P12G-12 |
| AC-AUDIT | 一部達成 / fail | `audit=pass` for all five harness results, and Word/Excel bind persisted approver identity, decision/item versions, hash-chain audit event, and artifact/acceptance snapshots. Browser correlation and equivalent proof for the remaining cases are incomplete. | `e2e_gap` | codex | Fail-closed full-flow audit assertions tied to browser run, harness result, and artifact. | P12G-09 |
| AC-AUTH | 一部達成 / fail | Authenticated API component behavior exists; role deny paths, token lifecycle UX, and the approved segregation boundary lack E2E proof. | `e2e_gap`, `decision_gap` | manual/hybrid then codex | Approved role boundary plus allow/deny and token lifecycle E2E evidence. | P12G-02, P12G-10 |
| AC-SECURITY | 一部達成 / fail | Local-only configuration is documented and tested, but the acceptance run has no network-boundary evidence proving zero external sends. | `e2e_gap` | codex | Acceptance-time network observation with zero external AI/API sends. | P12G-11 |
| FC-HIGH-RISK | 一部達成 / fail | Component guards prevent auto-confirmation, but the five-case snapshot does not prove zero high-risk misses through the final review UI and gate. | `e2e_gap` | codex | Dataset-wide fail-closed gate plus browser review evidence for every high-risk item. | P12G-08, P12G-12 |
| FC-EVIDENCE | 一部達成 / fail | Provenance/audit component tests exist; no E2E mutation proves missing or altered evidence rejects acceptance while leaving no accepted partial result. | `e2e_gap` | codex | Negative E2E cases for missing/tampered provenance and audit records. | P12G-09 |
| FC-EXTERNAL-SEND | 一部達成 / fail | Policy/config checks exist; no acceptance-time boundary assertion observes and rejects an external send. | `e2e_gap` | codex | Network-boundary zero-send evidence and a fail-closed negative case. | P12G-11 |
| FC-REVIEW-UI | 未達 / fail | No recorded keyboard-only warning, original jump, edit, approve/reject/needs-fix flow exists. | `e2e_gap` | codex | Browser trace covering warnings, remediation, keyboard flow, and API agreement. | P12G-08 |
| FC-REPRODUCIBILITY | 一部達成 / fail | Commit and manifest are fixed, but no packaged rerun pins fixture/config/model/prompt/schema versions and demonstrates an equivalent result. | `e2e_gap` | codex | A pinned run package and an equivalence-checked rerun. | P12G-11 |
| EM-USER-REVIEW | 未達 / fail | No representative cohort, task protocol, timing, miss/over-detection results, or reviewer record exists. | `human_evidence_gap`, `decision_gap` | manual/hybrid | Approved protocol and evidence schema ready for the later human execution. | P12G-02, P12G-13 |
| EM-E2E | 一部達成 / fail | A repo-owned browser run emits screenshots, trace, API result, downloaded artifact, audit artifact, and correlation metadata for one representative case; the versioned five-case report is two `fail`, zero `unknown`, and three `pass`, with persisted review decisions for Word and Excel and structured record-PDF content evidence. | `implementation_gap`, `e2e_gap` | codex/hybrid | P12G-05 through P12G-12 evidence from one committed snapshot with all intended cases passing. | P12G-05–P12G-12 |
| OD-TEMPLATES | 未達 / fail | The manifest is `fixed_for_mvp` with five categories, but no authoritative approval adopts those cases as the representative MVP scope. | `decision_gap` | manual/hybrid | Approval record naming the 3–5 templates and manifest revision. | P12G-02 |
| OD-EFFICIENCY-SCOPE | 未達 / fail | Baseline task, cohort, timing boundaries, and comparison method remain unapproved. | `decision_gap` | manual/hybrid | Authoritative efficiency-scope decision. | P12G-02 |
| OD-SEGREGATION | 未達 / fail | API role checks do not decide which segregation controls are mandatory for MVP versus explicitly deferred to Phase13. | `decision_gap`, `e2e_gap` | manual/hybrid then codex | Approved role matrix/carryover plus deny-path E2E proof. | P12G-02, P12G-10 |

## Five representative cases

All evaluation cells below come from the same invocation recorded in
`Evidence snapshot`. `review_decision` is authoritative only when the
conversion/review boundary records it; it is not inferred from expected status,
warnings, or review-item count.

| Case | Category | `acceptance_status` | Evaluation results | Failure reason | Review decision |
| --- | --- | --- | --- | --- | --- |
| mvp-word-001 | Word | `pass` | artifact/pass; audit/pass; input_size/pass; processing_time/pass; timeout/pass; review/pass | none | persisted approver decisions for 2 review items; artifact/audit/result share each decision ID and item/decision version |
| mvp-excel-001 | Excel | `pass` | artifact/pass; audit/pass; input_size/pass; processing_time/pass; timeout/pass; review/pass | none | persisted approver decisions for 3 review items; artifact/audit/result share each decision ID and item/decision version |
| mvp-text-pdf-001 | text PDF | `fail` | artifact/fail; audit/pass; input_size/pass; processing_time/pass; timeout/pass; review/fail | Expected `A1:D17`, got `A1:D14`; rows `A15:D17`, the expected 3x4 table, and source comment at `A15` are missing. Expected `converted`, got `requires_review`; warnings mismatch and unexpected review items were emitted. | absent (`null`) |
| mvp-scanned-pdf-001 | scanned PDF | `fail` | artifact/pass; audit/pass; input_size/pass; processing_time/pass; timeout/pass; review/fail | Explicit-review block, review guard, and source linkage pass; authoritative review decision is required. | absent (`null`) |
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
