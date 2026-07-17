# MVP Acceptance Traceability

This document is the repository-owned traceability baseline for every item in
the Obsidian source `15.3_MVP受入基準`. It records the current Phase10–12 issue,
test, and evidence boundary without treating an issue link or an isolated unit
test as proof of MVP acceptance.

Baseline owner: [#275](https://github.com/TommyKammy/VeriDoc/issues/275).

The current reconciliation is fixed at commit
`9981ffb9f3e633faedf5bc5c2bd3d5a4845424b7` in
`docs/mvp-acceptance-gap-register.md`. Its live report contains all 20 unique
items with `overall_decision=fail`, and its five-case harness contains three
`fail`, two `unknown`, and zero `pass` results.

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
| AC-UI | UI: 投入→設定→ジョブ→プレビュー→レビュー→DLが通る | [#245](https://github.com/TommyKammy/VeriDoc/issues/245), [#247](https://github.com/TommyKammy/VeriDoc/issues/247), [#249](https://github.com/TommyKammy/VeriDoc/issues/249), [#259](https://github.com/TommyKammy/VeriDoc/issues/259), [#260](https://github.com/TommyKammy/VeriDoc/issues/260), [#284](https://github.com/TommyKammy/VeriDoc/issues/284) | `tests/test_poc_web_api.py`, `tests/test_web_pdf_preview.py`; current gap: P12G-03/P12G-08 browser evidence | **一部達成** — screen, job, preview, and artifact APIs exist; no browser upload-to-download run currently binds review, approval, artifact, and audit evidence. |
| AC-TEMPLATE | テンプレート: 代表3〜5種で変換が成立 | [#283](https://github.com/TommyKammy/VeriDoc/issues/283), [#284](https://github.com/TommyKammy/VeriDoc/issues/284) | `datasets/mvp_evaluation_manifest_v1.json`, `tests/test_dataset_fixtures.py`, current five-case report; current gap: P12G-02/P12G-12 | **一部達成** — a versioned five-category manifest and run exist, but the run has zero passing cases and no authoritative decision adopts the manifest as the representative MVP scope. |
| AC-QUALITY | 品質: 主要帳票でセル一致率80%+の目安 | [#283](https://github.com/TommyKammy/VeriDoc/issues/283), [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | `tests/test_evaluate_dataset.py`, current five-case report; current gap: P12G-05/P12G-06/P12G-07/P12G-12 | **未達** — Word/Excel are `unknown`; text/scanned/record PDF are `fail`; no current per-template 80%+ result exists. |
| AC-PROVENANCE | 追跡性: 出典紐づけ率95%+目安、要確認から原本へ辿れる | [#278](https://github.com/TommyKammy/VeriDoc/issues/278), [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | `tests/test_document_ir_v1.py`, `tests/test_web_pdf_preview.py`, `tests/test_evaluate_dataset.py`; current gap: P12G-09/P12G-12 | **一部達成** — source coordinates and preview behavior have tests; the current five-case run has no MVP-wide 95%+ source-link measurement or browser-to-artifact trace. |
| AC-REVIEW | レビュー: 高リスク項目の自動確定ゼロ・見逃し率0 | [#278](https://github.com/TommyKammy/VeriDoc/issues/278), [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | `tests/test_automatic_validation.py::test_high_risk_item_cannot_be_auto_confirmed_even_when_value_matches`, `tests/test_template_fingerprint.py::TemplateFingerprintTest::test_high_risk_field_requires_review_when_matrix_omits_review_level`, `tests/test_poc_web_api.py::test_convert_uploaded_document_requires_review_for_high_risk_template_field`, `tests/test_poc_web_api.py::test_convert_uploaded_document_emits_template_mapping_warning_review_item`, `tests/test_poc_web_api.py::test_poc_http_api_rejects_review_approve_without_approver_role`, current five-case report; current gap: P12G-04/P12G-08/P12G-12 | **一部達成** — core/API guards remain verified, but all five current cases have `review_decision=null`; Word and Excel therefore remain `review=unknown`, and dataset-wide zero misses are unproven. |
| AC-EFFICIENCY | 効率: 人手修正時間の削減を測定し効果を確認（30%+目安） | [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#285](https://github.com/TommyKammy/VeriDoc/issues/285), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | Current gap: P12G-02/P12G-13 approved protocol and evidence schema, followed by a real comparison | **未達** — measurement scope remains an open decision and no human 30%+ result exists. |
| AC-PERFORMANCE | 性能: 代表文書の処理時間・サイズ・timeout基準を守る | [#285](https://github.com/TommyKammy/VeriDoc/issues/285), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | `datasets/mvp_evaluation_manifest_v1.json`, `docs/mvp-performance-limits.md`, current five-case report; current gap: P12G-12 | **一部達成** — all five current results pass the 10-second processing, 2 MiB input, and 30-second timeout evaluations, but an accepted five-case metrics rollup does not yet exist. |
| AC-AUDIT | 監査: 変換/レビュー/承認・ハッシュ・版数が記録 | [#258](https://github.com/TommyKammy/VeriDoc/issues/258), [#282](https://github.com/TommyKammy/VeriDoc/issues/282), [#284](https://github.com/TommyKammy/VeriDoc/issues/284) | `tests/test_persistence_audit_contracts.py`, `tests/test_persistence_audit_evidence.py`, `tests/test_llm_audit_parameters.py`, current five-case report; current gap: P12G-09 | **一部達成** — all five current harness results have `audit=pass`, but review/approval actor, decision, version, hash-chain, and cross-surface correlation are not proven together. |
| AC-AUTH | 認証: 簡易認証＋ロールが機能 | [#276](https://github.com/TommyKammy/VeriDoc/issues/276), [#277](https://github.com/TommyKammy/VeriDoc/issues/277) | `tests/test_desktop_api_auth.py`, `tests/test_poc_web_api.py`; current gap: P12G-02/P12G-10 | **一部達成** — authenticated API behavior exists; the authoritative role boundary, deny paths, and token lifecycle UI lack current E2E evidence. |
| AC-SECURITY | セキュリティ: 外部送信なし | [#281](https://github.com/TommyKammy/VeriDoc/issues/281), [#288](https://github.com/TommyKammy/VeriDoc/issues/288), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | `docs/local-inference-setup.md`, `tests/test_local_inference_setup.py`; current gap: P12G-11 acceptance-time boundary evidence | **一部達成** — local inference policy is documented and tested, but the current acceptance run does not prove zero external AI/API sends. |

## Failure Conditions

| ID | 15.3 failure condition | Linked issue(s) | Test / evidence | Current status and required guard |
| --- | --- | --- | --- | --- |
| FC-HIGH-RISK | 高リスク項目が自動確定される、または要確認に乗らない | [#278](https://github.com/TommyKammy/VeriDoc/issues/278), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | `tests/test_automatic_validation.py::test_high_risk_item_cannot_be_auto_confirmed_even_when_value_matches`, `tests/test_template_fingerprint.py::TemplateFingerprintTest::test_high_risk_field_requires_review_when_matrix_omits_review_level`, `tests/test_poc_web_api.py::test_convert_uploaded_document_requires_review_for_high_risk_template_field`, `tests/test_poc_web_api.py::test_convert_uploaded_document_emits_template_mapping_warning_review_item`, `tests/test_evaluate_dataset.py` | **一部達成** — #278 の実境界では risk matrix の設定漏れがあっても high-risk テンプレート項目を変換成功時・再実行時とも `requires_review` に固定し、mapping-level warningにもreview targetを生成する。代表データセットで一件でも見逃せば #289 の gate を失敗させる。 |
| FC-EVIDENCE | 出典・監査ログが欠落する | [#278](https://github.com/TommyKammy/VeriDoc/issues/278), [#282](https://github.com/TommyKammy/VeriDoc/issues/282), [#284](https://github.com/TommyKammy/VeriDoc/issues/284) | `tests/test_document_ir_v1.py`, `tests/test_persistence_audit_evidence.py` | **一部達成** — E2E must reject missing provenance or audit evidence instead of assembling a partial pass. |
| FC-EXTERNAL-SEND | 外部送信が発生する | [#281](https://github.com/TommyKammy/VeriDoc/issues/281), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | `tests/test_local_inference_setup.py`, `docs/local-inference-setup.md` | **一部達成** — configured local-only behavior exists; an acceptance-time network boundary check remains required. |
| FC-REVIEW-UI | レビューUIで要確認箇所が把握できない | [#278](https://github.com/TommyKammy/VeriDoc/issues/278), [#279](https://github.com/TommyKammy/VeriDoc/issues/279), [#286](https://github.com/TommyKammy/VeriDoc/issues/286) | `tests/test_web_pdf_preview.py`; current gap: P12G-08 warning/review keyboard-flow evidence | **未達** — no current browser record covers warnings, original-document jump, edit, and approve/reject/needs-fix by keyboard. |
| FC-REPRODUCIBILITY | 同一条件の再現性が説明できない | [#282](https://github.com/TommyKammy/VeriDoc/issues/282), [#283](https://github.com/TommyKammy/VeriDoc/issues/283), [#284](https://github.com/TommyKammy/VeriDoc/issues/284) | `tests/test_template_fingerprint.py`, `tests/test_llm_audit_parameters.py`, current base-commit run; current gap: P12G-11 | **一部達成** — the commit and manifest are fixed, but no package pins all fixture/config/model/prompt/schema inputs and demonstrates an equivalent rerun. |

## Evaluation Methods

| ID | 15.3 evaluation method | Linked issue(s) | Required evidence | Current status |
| --- | --- | --- | --- | --- |
| EM-USER-REVIEW | 実務担当者によるレビュー試行（時間・見逃し・過検出） | [#283](https://github.com/TommyKammy/VeriDoc/issues/283), [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | Current gap: P12G-02/P12G-13 protocol and schema, followed by named cohort, timing, miss/over-detection, and reviewer records | **未達** — no current authoritative protocol or human user-review record exists. |
| EM-E2E | 代表テンプレートでのE2E評価 | [#283](https://github.com/TommyKammy/VeriDoc/issues/283), [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | Versioned five-case manifest and current report; current gap: P12G-03 through P12G-12 | **一部達成** — the dataset/harness/report chain now runs, but the current result is three `fail`, two `unknown`, and zero `pass`; review decisions and complete E2E evidence remain absent. |

## Open Decisions

| ID | 15.3 open item | Owner issue / phase | Evidence needed to close | Current status |
| --- | --- | --- | --- | --- |
| OD-TEMPLATES | 対象業務・テンプレートの確定 | Historical owner [#283](https://github.com/TommyKammy/VeriDoc/issues/283); current decision: P12G-02 | `datasets/mvp_evaluation_manifest_v1.json` plus authoritative approval | **未達** — five categories are versioned as `fixed_for_mvp`, but no authoritative decision approves that manifest revision as the representative MVP scope. |
| OD-EFFICIENCY-SCOPE | 効率指標の測定対象業務 | Historical owners [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#285](https://github.com/TommyKammy/VeriDoc/issues/285); current decision: P12G-02 | Baseline task, participant/cohort, timing boundaries, and comparison method | **未達** — ベースライン作業、参加者/コホート、計測境界、比較方法が未確定。 |
| OD-SEGREGATION | 職務分掌をMVPでどこまで満たすか | Historical owner [#276](https://github.com/TommyKammy/VeriDoc/issues/276); current decision/E2E: P12G-02/P12G-10; richer desktop workflow may be **Phase13以降** | Approved role matrix plus deny-path tests for missing/forbidden roles | **未達** — Phase13 deferral must be explicit; it cannot silently weaken the current MVP API boundary. |

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

Minimum verification for changes to this baseline:

```bash
python3 -m unittest tests.test_mvp_acceptance_traceability
python3 scripts/evaluate_dataset.py --mvp-acceptance-report
python3 scripts/ci/repo_hygiene.py
```
