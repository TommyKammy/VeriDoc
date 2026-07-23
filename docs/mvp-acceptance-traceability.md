# MVP Acceptance Traceability

This document is the repository-owned traceability baseline for every item in
the Obsidian source `15.3_MVP受入基準`. It records the current Phase10–12 issue,
test, and evidence boundary without treating an issue link or an isolated unit
test as proof of MVP acceptance.

Baseline owner: [#275](https://github.com/TommyKammy/VeriDoc/issues/275).

The reproducible report revision is the reachable commit that contains the
corresponding version of `docs/mvp-acceptance-gap-register.md`; resolve and
check out that revision with the repo-relative command recorded in the
register. The register also pins this criteria file and the evaluator by Git
blob ID so a later revision cannot silently substitute different report
inputs. Commit
`9981ffb9f3e633faedf5bc5c2bd3d5a4845424b7` is the product/harness baseline
being reconciled, not the report checkout target; it predates the gap register
and has older criteria statuses. The recorded report contains all 20 unique
items with `overall_decision=fail`, and its five-case harness contains one
`fail`, zero `unknown`, and four `pass` results.

## Status Rules

- `達成`: the acceptance boundary has implementation plus directly applicable
  automated or recorded evidence.
- `一部達成`: relevant implementation or evidence exists, but the complete
  15.3 boundary has not been demonstrated.
- `未達`: a required implementation, measurement, or acceptance record is not
  available. Missing prerequisites fail closed; they are not inferred from
  nearby functionality.
- `非対応`: explicitly excluded from the MVP, with the blocking behavior and
  user-facing statement both verified.
- `Phase13以降`: intentionally deferred beyond Phase12 and not required to
  claim the Phase12 MVP gate. A deferred item cannot be counted as `達成`.

Issue state is only planning evidence. The gate status must be recalculated
from committed implementation, tests, and the latest acceptance record.

## Acceptance Criteria

| ID | 15.3 acceptance item | Linked issue(s) | Test / evidence | Current status and remaining boundary |
| --- | --- | --- | --- | --- |
| AC-UI | UI: 投入→設定→ジョブ→プレビュー→レビュー→DLが通る | [#245](https://github.com/TommyKammy/VeriDoc/issues/245), [#247](https://github.com/TommyKammy/VeriDoc/issues/247), [#249](https://github.com/TommyKammy/VeriDoc/issues/249), [#259](https://github.com/TommyKammy/VeriDoc/issues/259), [#260](https://github.com/TommyKammy/VeriDoc/issues/260), [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#309](https://github.com/TommyKammy/VeriDoc/issues/309), [#314](https://github.com/TommyKammy/VeriDoc/issues/314) | `tests/test_mvp_browser_e2e.py`, `scripts/ci/mvp_browser_e2e.py`, `artifacts/mvp-browser-e2e/<run-id>/evidence.json`; current gap: P12G-12 accepted-scope rollup | **一部達成** — one repo-owned Playwright run binds upload, settings recovery, job, preview, primary download hash, audit artifact, screenshots, and trace to one correlation ID, then records keyboard-only warning-to-bbox, visible focus, structured warning fields, and edit/approve/reject/needs-fix transitions for a committed high-risk fixture. |
| AC-TEMPLATE | テンプレート: 代表3〜5種で変換が成立 | [#283](https://github.com/TommyKammy/VeriDoc/issues/283), [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#308](https://github.com/TommyKammy/VeriDoc/issues/308) | `datasets/mvp_evaluation_manifest_v1.json`, `docs/mvp-scope-decisions.md`, `tests/test_dataset_fixtures.py`, current five-case report; current gap: P12G-06/P12G-12 | **一部達成** — decision revision `p12g-02-v1` adopts the five-category manifest as the representative MVP scope and Word, Excel, text PDF, and record PDF pass, but scanned PDF still fails. |
| AC-QUALITY | 品質: 主要帳票でセル一致率80%+の目安 | [#283](https://github.com/TommyKammy/VeriDoc/issues/283), [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | `tests/test_evaluate_dataset.py`, current five-case report; current gap: P12G-06/P12G-12 | **未達** — Word, Excel, text PDF, and record PDF now pass artifact, review, audit, and performance evaluation; scanned PDF still fails, and no current per-template 80%+ result exists. |
| AC-PROVENANCE | 追跡性: 出典紐づけ率95%+目安、要確認から原本へ辿れる | [#278](https://github.com/TommyKammy/VeriDoc/issues/278), [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#289](https://github.com/TommyKammy/VeriDoc/issues/289), [#315](https://github.com/TommyKammy/VeriDoc/issues/315) | `tests/test_document_ir_v1.py`, `tests/test_web_pdf_preview.py`, `tests/test_evaluate_dataset.py`, `tests/test_mvp_browser_e2e.py`; current gap: P12G-12 | **一部達成** — browser evidence now traces the reviewed source page/bbox through one correlated artifact/audit flow; the current five-case run still has no MVP-wide 95%+ source-link measurement. |
| AC-REVIEW | レビュー: 高リスク項目の自動確定ゼロ・見逃し率0 | [#278](https://github.com/TommyKammy/VeriDoc/issues/278), [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#289](https://github.com/TommyKammy/VeriDoc/issues/289), [#314](https://github.com/TommyKammy/VeriDoc/issues/314) | `tests/test_automatic_validation.py::test_high_risk_item_cannot_be_auto_confirmed_even_when_value_matches`, `tests/test_template_fingerprint.py::TemplateFingerprintTest::test_high_risk_field_requires_review_when_matrix_omits_review_level`, `tests/test_poc_web_api.py::test_convert_uploaded_document_requires_review_for_high_risk_template_field`, `tests/test_mvp_browser_e2e.py`, `tests/test_evaluate_dataset.py`, `tests/test_persistence_audit_evidence.py`; current gap: P12G-12 dataset-wide rollup | **一部達成** — Word/Excel decisions pass through the authorized persistence boundary and share decision/item versions across artifact, audit, and harness snapshots; missing/forbidden decisions and unresolved high-risk items fail closed. Browser evidence now proves zero auto-confirms for its committed high-risk fixture and records keyboard edit/approve/reject/needs-fix transitions, while dataset-wide zero misses remain unproven. |
| AC-EFFICIENCY | 効率: 人手修正時間の削減を測定し効果を確認（30%+目安） | [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#285](https://github.com/TommyKammy/VeriDoc/issues/285), [#289](https://github.com/TommyKammy/VeriDoc/issues/289), [#308](https://github.com/TommyKammy/VeriDoc/issues/308) | `docs/mvp-scope-decisions.md`; current gap: P12G-13 protocol/schema followed by a real comparison | **未達** — the baseline task, minimum cohort, training, timing, paired comparison, and rejection conditions are approved, but no versioned protocol/schema or human 30%+ result exists. |
| AC-PERFORMANCE | 性能: 代表文書の処理時間・サイズ・timeout基準を守る | [#285](https://github.com/TommyKammy/VeriDoc/issues/285), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | `datasets/mvp_evaluation_manifest_v1.json`, `docs/mvp-performance-limits.md`, current five-case report; current gap: P12G-12 | **一部達成** — all five current results pass the 10-second processing, 2 MiB input, and 30-second timeout evaluations, but an accepted five-case metrics rollup does not yet exist. |
| AC-AUDIT | 監査: 変換/レビュー/承認・ハッシュ・版数が記録 | [#258](https://github.com/TommyKammy/VeriDoc/issues/258), [#282](https://github.com/TommyKammy/VeriDoc/issues/282), [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#315](https://github.com/TommyKammy/VeriDoc/issues/315) | `tests/test_persistence_audit_contracts.py`, `tests/test_persistence_audit_evidence.py`, `tests/test_llm_audit_parameters.py`, `tests/test_mvp_browser_e2e.py`, current five-case report | **達成** — browser run, harness result, download artifact, actor/decision, hashes, version lineage, timestamp, and complete audit chains share one correlation ID and a fail-closed acceptance snapshot. |
| AC-AUTH | 認証: 簡易認証＋ロールが機能 | [#276](https://github.com/TommyKammy/VeriDoc/issues/276), [#277](https://github.com/TommyKammy/VeriDoc/issues/277), [#308](https://github.com/TommyKammy/VeriDoc/issues/308), [#316](https://github.com/TommyKammy/VeriDoc/issues/316) | `docs/mvp-scope-decisions.md`, `services/api/poc_web.py`, `scripts/ci/mvp_browser_e2e.py`, `tests/test_desktop_api_auth.py`, `tests/test_poc_web_api.py`, `tests/test_mvp_browser_e2e.py` | **達成** — the product server requires configured local authentication, the six-role read/sensitive API matrix is probed, the UI exposes missing/rejected/forbidden/cleared/re-authenticated states, and approval is audit-bound to a preceding edit by a distinct reviewer under decision revision `p12g-02-v1`. |
| AC-SECURITY | セキュリティ: 外部送信なし | [#281](https://github.com/TommyKammy/VeriDoc/issues/281), [#288](https://github.com/TommyKammy/VeriDoc/issues/288), [#289](https://github.com/TommyKammy/VeriDoc/issues/289), [#317](https://github.com/TommyKammy/VeriDoc/issues/317) | `scripts/ci/mvp_browser_e2e.py`, `tests/test_mvp_browser_e2e.py`, `artifacts/mvp-browser-e2e/<run-id>/evidence.json` | **一部達成** — the harness and negative tests enforce the local-only boundary, but the report must not count this criterion as passed until a concrete retained `evidence.json` path has been validated for the reported run. |

## Failure Conditions

| ID | 15.3 failure condition | Linked issue(s) | Test / evidence | Current status and required guard |
| --- | --- | --- | --- | --- |
| FC-HIGH-RISK | 高リスク項目が自動確定される、または要確認に乗らない | [#278](https://github.com/TommyKammy/VeriDoc/issues/278), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | `tests/test_automatic_validation.py::test_high_risk_item_cannot_be_auto_confirmed_even_when_value_matches`, `tests/test_template_fingerprint.py::TemplateFingerprintTest::test_high_risk_field_requires_review_when_matrix_omits_review_level`, `tests/test_poc_web_api.py::test_convert_uploaded_document_requires_review_for_high_risk_template_field`, `tests/test_poc_web_api.py::test_convert_uploaded_document_emits_template_mapping_warning_review_item`, `tests/test_evaluate_dataset.py` | **一部達成** — #278 の実境界では risk matrix の設定漏れがあっても high-risk テンプレート項目を変換成功時・再実行時とも `requires_review` に固定し、mapping-level warningにもreview targetを生成する。代表データセットで一件でも見逃せば #289 の gate を失敗させる。 |
| FC-EVIDENCE | 出典・監査ログが欠落する | [#278](https://github.com/TommyKammy/VeriDoc/issues/278), [#282](https://github.com/TommyKammy/VeriDoc/issues/282), [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#315](https://github.com/TommyKammy/VeriDoc/issues/315) | `tests/test_document_ir_v1.py`, `tests/test_persistence_audit_evidence.py`, `tests/test_mvp_browser_e2e.py` | **達成** — real browser evidence mutations for missing provenance, missing audit events, changed hashes/versions, and mixed correlation IDs stay fail-closed with machine-readable boundary codes and operator explanations. |
| FC-EXTERNAL-SEND | 外部送信が発生する | [#281](https://github.com/TommyKammy/VeriDoc/issues/281), [#289](https://github.com/TommyKammy/VeriDoc/issues/289), [#317](https://github.com/TommyKammy/VeriDoc/issues/317) | `scripts/ci/mvp_browser_e2e.py`, `tests/test_mvp_browser_e2e.py`, `artifacts/mvp-browser-e2e/<run-id>/evidence.json` | **一部達成** — external HTTP, DNS, socket, redirect, and configured endpoint cases fail closed in the harness, but a concrete retained `evidence.json` is still required for a report-level pass. |
| FC-REVIEW-UI | レビューUIで要確認箇所が把握できない | [#278](https://github.com/TommyKammy/VeriDoc/issues/278), [#279](https://github.com/TommyKammy/VeriDoc/issues/279), [#286](https://github.com/TommyKammy/VeriDoc/issues/286), [#314](https://github.com/TommyKammy/VeriDoc/issues/314) | `tests/test_mvp_browser_e2e.py`, `tests/test_web_pdf_preview.py`, `scripts/ci/mvp_browser_e2e.py`, `artifacts/mvp-browser-e2e/<run-id>/evidence.json`; current gap: P12G-12 accepted-scope rollup | **一部達成** — one committed high-risk fixture records structured warning/remediation fields, keyboard-only bbox jump, visible focus, edit/needs-fix/reject transitions, and a fail-closed same-actor approval attempt; the final accepted-scope rollup remains open. |
| FC-REPRODUCIBILITY | 同一条件の再現性が説明できない | [#282](https://github.com/TommyKammy/VeriDoc/issues/282), [#283](https://github.com/TommyKammy/VeriDoc/issues/283), [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#317](https://github.com/TommyKammy/VeriDoc/issues/317) | `scripts/ci/mvp_browser_e2e.py`, `tests/test_mvp_browser_e2e.py`, `artifacts/mvp-browser-e2e/<run-id>/rerun-package.json` | **一部達成** — the sealed package and tests pin and validate the rerun boundary, but a concrete retained `rerun-package.json` and successful equivalence result are still required for a report-level pass. |

## Evaluation Methods

| ID | 15.3 evaluation method | Linked issue(s) | Required evidence | Current status |
| --- | --- | --- | --- | --- |
| EM-USER-REVIEW | 実務担当者によるレビュー試行（時間・見逃し・過検出） | [#283](https://github.com/TommyKammy/VeriDoc/issues/283), [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#289](https://github.com/TommyKammy/VeriDoc/issues/289), [#308](https://github.com/TommyKammy/VeriDoc/issues/308) | `docs/mvp-scope-decisions.md`; current gap: P12G-13 protocol/schema, followed by pseudonymous cohort, timing, miss/over-detection, and reviewer records | **未達** — task, cohort, training, timing, comparison, and invalidation scope is approved, but no execution protocol/schema or human user-review record exists. |
| EM-E2E | 代表テンプレートでのE2E評価 | [#283](https://github.com/TommyKammy/VeriDoc/issues/283), [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#289](https://github.com/TommyKammy/VeriDoc/issues/289), [#309](https://github.com/TommyKammy/VeriDoc/issues/309), [#315](https://github.com/TommyKammy/VeriDoc/issues/315), [#316](https://github.com/TommyKammy/VeriDoc/issues/316) | Versioned five-case manifest and current report plus `artifacts/mvp-browser-e2e/<run-id>/evidence.json`; current gap: P12G-06/P12G-11/P12G-12 | **一部達成** — one repo-owned browser run packages screenshots, trace, API result, downloaded artifact, audit artifact, complete audit chains, role/token/segregation evidence, and a correlated fail-closed acceptance snapshot; the dataset/harness/report chain has one `fail`, zero `unknown`, and four `pass`, with Word, Excel, text PDF, and record PDF evidence present. |

## Open Decisions

| ID | 15.3 open item | Owner issue / phase | Evidence needed to close | Current status |
| --- | --- | --- | --- | --- |
| OD-TEMPLATES | 対象業務・テンプレートの確定 | Historical owner [#283](https://github.com/TommyKammy/VeriDoc/issues/283); decision [#308](https://github.com/TommyKammy/VeriDoc/issues/308) | `datasets/mvp_evaluation_manifest_v1.json`, `docs/mvp-scope-decisions.md`; evaluator validates the approved manifest contract hash | **達成** — `TommyKammy` approved all five cases in manifest revision `phase12-mvp-v1` on `2026-07-22`; any case, fixture, source-policy, or expectation drift fails this item until renewed approval. |
| OD-EFFICIENCY-SCOPE | 効率指標の測定対象業務 | Historical owners [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#285](https://github.com/TommyKammy/VeriDoc/issues/285); decision [#308](https://github.com/TommyKammy/VeriDoc/issues/308) | `docs/mvp-scope-decisions.md` baseline task, cohort, training, timing, comparison, and rejection conditions; evaluator validates the revision-bound canonical section hash | **達成** — decision revision `p12g-02-v1` fixes a reproducible paired comparison and retains the 30% target without claiming a human result; any protocol-scope drift fails this item until renewed approval. |
| OD-SEGREGATION | 職務分掌をMVPでどこまで満たすか | Historical owner [#276](https://github.com/TommyKammy/VeriDoc/issues/276); decision [#308](https://github.com/TommyKammy/VeriDoc/issues/308); implementation [#316](https://github.com/TommyKammy/VeriDoc/issues/316) | `docs/mvp-scope-decisions.md` approved matrix hash, deny paths, known implementation gaps, and **Phase13以降** carryover; evaluator validates the revision-bound canonical section hash; browser evidence validates the implemented MVP boundary | **達成** — decision revision `p12g-02-v1` fixes the six-role target boundary, and P12G-10 proves mandatory authentication, API/UI deny paths, token lifecycle, and preceding distinct-actor review; any deny-path or carryover drift fails this item until renewed approval. |

## Stable MVP Gate

The Phase12 MVP gate is fail-closed and is evaluated from the table above:

1. Every `AC-*`, `FC-*`, `EM-*`, and `OD-*` row remains present and has an
   issue, test/evidence boundary, and status or explicit unmet reason.
2. Any `未達` row blocks an overall pass unless the row is explicitly accepted
   as `非対応` or `Phase13以降` by the authoritative acceptance decision.
3. `一部達成`, an open/closed issue state, or a component test never counts as
   a final pass by itself.
4. Every current or later acceptance report must use one consistent evidence
   set; it must not combine partial results from different runs.
5. The historical Phase9 decision in `docs/mvp-transition-decision.md` is
   context, not current Phase12 acceptance evidence.
6. The evaluator must recompute the approved manifest, efficiency-scope,
   segregation-scope, and permission-matrix input contracts. Any drift fails the
   affected `OD-*` item until a renewed human-approved decision revision updates
   the recorded and evaluator pins.

Minimum verification for changes to this baseline:

```bash
python3 -m pip install -r requirements-pdf-eval.txt
python3 -m unittest tests.test_mvp_acceptance_traceability
python3 scripts/evaluate_dataset.py --mvp-acceptance-report
python3 scripts/ci/repo_hygiene.py
```
