# MVP Acceptance Traceability

This document is the repository-owned traceability baseline for every item in
the Obsidian source `15.3_MVP受入基準`. It records the current Phase10–12 issue,
test, and evidence boundary without treating an issue link or an isolated unit
test as proof of MVP acceptance.

Baseline owner: [#275](https://github.com/TommyKammy/VeriDoc/issues/275).

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
| AC-UI | UI: 投入→設定→ジョブ→プレビュー→レビュー→DLが通る | [#245](https://github.com/TommyKammy/VeriDoc/issues/245), [#247](https://github.com/TommyKammy/VeriDoc/issues/247), [#249](https://github.com/TommyKammy/VeriDoc/issues/249), [#259](https://github.com/TommyKammy/VeriDoc/issues/259), [#260](https://github.com/TommyKammy/VeriDoc/issues/260), [#284](https://github.com/TommyKammy/VeriDoc/issues/284) | `tests/test_poc_web_api.py`, `tests/test_web_pdf_preview.py`; final evidence: #284/#289 E2E record | **一部達成** — screen, job, preview, and artifact APIs exist; one browser-level upload-to-download record is still required. |
| AC-TEMPLATE | テンプレート: 代表3〜5種で変換が成立 | [#283](https://github.com/TommyKammy/VeriDoc/issues/283), [#284](https://github.com/TommyKammy/VeriDoc/issues/284) | `tests/test_template_definition_schema.py`, `tests/test_dataset_fixtures.py`; final evidence: versioned MVP dataset manifest and E2E results | **未達** — schema and fixtures are component evidence, not a recorded 3–5 template acceptance run. |
| AC-QUALITY | 品質: 主要帳票でセル一致率80%+の目安 | [#283](https://github.com/TommyKammy/VeriDoc/issues/283), [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | `tests/test_evaluate_dataset.py`; final evidence: #289 acceptance report containing per-template cell-match results | **未達** — the evaluator exists, but the MVP dataset and current 80%+ result are not fixed. |
| AC-PROVENANCE | 追跡性: 出典紐づけ率95%+目安、要確認から原本へ辿れる | [#278](https://github.com/TommyKammy/VeriDoc/issues/278), [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | `tests/test_document_ir_v1.py`, `tests/test_web_pdf_preview.py`, `tests/test_evaluate_dataset.py`; final evidence: source-link coverage output | **一部達成** — source coordinates and preview behavior have tests; the MVP-wide 95%+ measurement is missing. |
| AC-REVIEW | レビュー: 高リスク項目の自動確定ゼロ・見逃し率0 | [#278](https://github.com/TommyKammy/VeriDoc/issues/278), [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | `tests/test_automatic_validation.py::test_high_risk_item_cannot_be_auto_confirmed_even_when_value_matches`, `tests/test_poc_web_api.py::test_convert_uploaded_document_requires_review_for_high_risk_template_field`, `tests/test_poc_web_api.py::test_poc_http_api_rejects_review_approve_without_approver_role`, `tests/test_evaluate_dataset.py`; final evidence: representative high-risk gate output with zero misses | **一部達成** — core、API変換・再実行、review/approverロール境界は自動テスト済み。代表データセット全体のゼロ見逃し結果は #289 で確定する。 |
| AC-EFFICIENCY | 効率: 人手修正時間の削減を測定し効果を確認（30%+目安） | [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#285](https://github.com/TommyKammy/VeriDoc/issues/285), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | final evidence: defined baseline/task cohort and measured review-time comparison | **未達** — measurement scope is an open decision and no 30%+ result exists. |
| AC-AUDIT | 監査: 変換/レビュー/承認・ハッシュ・版数が記録 | [#258](https://github.com/TommyKammy/VeriDoc/issues/258), [#282](https://github.com/TommyKammy/VeriDoc/issues/282), [#284](https://github.com/TommyKammy/VeriDoc/issues/284) | `tests/test_persistence_audit_contracts.py`, `tests/test_persistence_audit_evidence.py`, `tests/test_llm_audit_parameters.py`; final evidence: #284 full-flow audit assertions | **一部達成** — persistence/audit components exist; review/approval plus model/prompt/schema version coverage remains incomplete. |
| AC-AUTH | 認証: 簡易認証＋ロールが機能 | [#276](https://github.com/TommyKammy/VeriDoc/issues/276), [#277](https://github.com/TommyKammy/VeriDoc/issues/277) | `tests/test_desktop_api_auth.py`, `tests/test_poc_web_api.py`; final evidence: role matrix and token UX rejection tests | **一部達成** — authenticated API behavior exists; Phase12 role separation and token lifecycle UX are not complete. |
| AC-SECURITY | セキュリティ: 外部送信なし | [#281](https://github.com/TommyKammy/VeriDoc/issues/281), [#288](https://github.com/TommyKammy/VeriDoc/issues/288), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | `docs/local-inference-setup.md`, `tests/test_local_inference_setup.py`, `docs/phase5-terminal-security-acceptance.md`; final evidence: #289 boundary check | **一部達成** — local inference policy is documented and tested, but the complete MVP runtime boundary needs acceptance evidence. |

## Failure Conditions

| ID | 15.3 failure condition | Linked issue(s) | Test / evidence | Current status and required guard |
| --- | --- | --- | --- | --- |
| FC-HIGH-RISK | 高リスク項目が自動確定される、または要確認に乗らない | [#278](https://github.com/TommyKammy/VeriDoc/issues/278), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | `tests/test_automatic_validation.py::test_high_risk_item_cannot_be_auto_confirmed_even_when_value_matches`, `tests/test_poc_web_api.py::test_convert_uploaded_document_requires_review_for_high_risk_template_field`, `tests/test_evaluate_dataset.py` | **一部達成** — #278 の実境界では high-risk テンプレート項目を変換成功時も再実行時も `requires_review` に固定する。代表データセットで一件でも見逃せば #289 の gate を失敗させる。 |
| FC-EVIDENCE | 出典・監査ログが欠落する | [#278](https://github.com/TommyKammy/VeriDoc/issues/278), [#282](https://github.com/TommyKammy/VeriDoc/issues/282), [#284](https://github.com/TommyKammy/VeriDoc/issues/284) | `tests/test_document_ir_v1.py`, `tests/test_persistence_audit_evidence.py` | **一部達成** — E2E must reject missing provenance or audit evidence instead of assembling a partial pass. |
| FC-EXTERNAL-SEND | 外部送信が発生する | [#281](https://github.com/TommyKammy/VeriDoc/issues/281), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | `tests/test_local_inference_setup.py`, `docs/local-inference-setup.md` | **一部達成** — configured local-only behavior exists; an acceptance-time network boundary check remains required. |
| FC-REVIEW-UI | レビューUIで要確認箇所が把握できない | [#278](https://github.com/TommyKammy/VeriDoc/issues/278), [#279](https://github.com/TommyKammy/VeriDoc/issues/279), [#286](https://github.com/TommyKammy/VeriDoc/issues/286) | `tests/test_web_pdf_preview.py`; final evidence: warning/review keyboard-flow test | **未達** — stable warnings, complete review presentation, and accessibility checks are Phase12 work. |
| FC-REPRODUCIBILITY | 同一条件の再現性が説明できない | [#282](https://github.com/TommyKammy/VeriDoc/issues/282), [#283](https://github.com/TommyKammy/VeriDoc/issues/283), [#284](https://github.com/TommyKammy/VeriDoc/issues/284) | `tests/test_template_fingerprint.py`, `tests/test_llm_audit_parameters.py`; final evidence: pinned dataset/config/version rerun | **一部達成** — fingerprints and parameters exist, but the Phase12 rerun package is not assembled. |

## Evaluation Methods

| ID | 15.3 evaluation method | Linked issue(s) | Required evidence | Current status |
| --- | --- | --- | --- | --- |
| EM-USER-REVIEW | 実務担当者によるレビュー試行（時間・見逃し・過検出） | [#283](https://github.com/TommyKammy/VeriDoc/issues/283), [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | Named representative cohort, task protocol, timing, miss/over-detection results, and reviewer record | **未達** — no current Phase12 user-review record. |
| EM-E2E | 代表テンプレートでのE2E評価 | [#283](https://github.com/TommyKammy/VeriDoc/issues/283), [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#289](https://github.com/TommyKammy/VeriDoc/issues/289) | Versioned dataset manifest, commands, per-template outputs, artifacts, review decisions, audit, and timing | **未達** — the dataset/harness/report chain is scheduled in #283/#284/#289. |

## Open Decisions

| ID | 15.3 open item | Owner issue / phase | Evidence needed to close | Current status |
| --- | --- | --- | --- | --- |
| OD-TEMPLATES | 対象業務・テンプレートの確定 | [#283](https://github.com/TommyKammy/VeriDoc/issues/283) / Phase12 | Approved/versioned MVP dataset manifest naming the representative 3–5 templates | **未達**. |
| OD-EFFICIENCY-SCOPE | 効率指標の測定対象業務 | [#284](https://github.com/TommyKammy/VeriDoc/issues/284), [#285](https://github.com/TommyKammy/VeriDoc/issues/285) / Phase12 | Baseline task, participant/cohort, timing boundaries, and comparison method | **未達**. |
| OD-SEGREGATION | 職務分掌をMVPでどこまで満たすか | [#276](https://github.com/TommyKammy/VeriDoc/issues/276) / Phase12; richer desktop workflow may be **Phase13以降** | Approved role matrix plus deny-path tests for missing/forbidden roles | **未達** — Phase13 deferral must be explicit; it cannot silently weaken the Phase12 API boundary. |

## Stable MVP Gate

The Phase12 MVP gate is fail-closed and is evaluated from the table above:

1. Every `AC-*`, `FC-*`, `EM-*`, and `OD-*` row remains present and has an
   issue, test/evidence boundary, and status or explicit unmet reason.
2. Any `未達` row blocks an overall pass unless the row is explicitly accepted
   as `非対応` or `Phase13以降` by the authoritative acceptance decision.
3. `一部達成`, an open/closed issue state, or a component test never counts as
   a final pass by itself.
4. #289 must generate the final result from one consistent evidence set; it
   must not combine partial results from different runs.
5. The historical Phase9 decision in `docs/mvp-transition-decision.md` is
   context, not current Phase12 acceptance evidence.

Minimum verification for changes to this baseline:

```bash
python3 -m unittest tests.test_mvp_acceptance_traceability
python3 scripts/ci/repo_hygiene.py
```
