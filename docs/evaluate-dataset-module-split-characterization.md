# `evaluate_dataset.py` module split characterization

Issue: [#343](https://github.com/TommyKammy/VeriDoc/issues/343)

## Scope and baseline

This document characterizes the current module before any production symbol is
moved. It is a compatibility and migration plan, not authorization to change
evaluation behavior.

The baseline is `main` commit
`9ea6f6b25b101bde72c57a1e0c3e4549b804caf8` on 2026-07-30:

| Measure | Baseline |
| --- | ---: |
| `scripts/evaluate_dataset.py` physical lines | 11,504 |
| Top-level functions | 309 |
| Function definitions including methods | 338 |
| Top-level classes | 25 |
| Top-level assigned constants/type aliases | 89 |
| Top-level function/class definitions | 334 |
| `tests/test_evaluate_dataset.py` physical lines | 13,864 |
| `EvaluateDatasetTest` test methods | 357 |

The test module loads the script with
`spec_from_file_location("evaluate_dataset", SCRIPT_PATH)`. This is a supported
import surface even though the file is under `scripts/`. Repository
documentation and CI also invoke the executable path directly.

## Responsibility inventory

The line ranges below describe the baseline only. They are not proposed module
boundaries by themselves.

| Responsibility | Current symbols and range | Count / ownership |
| --- | --- | --- |
| Module setup and constants | lines 1-319 | 89 assigned names: PoC auth 20, Phase9 13, MVP 20, GMP 8, shared defaults/types/schemas 28 |
| PoC authentication evidence analyzer | lines 320-3252, from `poc_auth_session_coverage_evidence_refs` through `poc_auth_session_coverage_is_present` | 138 functions and 10 private analysis data classes; parses README and Python AST and fails closed when evidence is ambiguous |
| Result and error types | lines 3253-4017, from `EvaluationMetrics` through `EvaluationCaseError` | 14 report/metric/error classes; `P9HarnessReport`, `MVPHarnessReport`, `MVPAcceptanceReport`, and `PoCAcceptanceReport` serialize domain results |
| Shared JSON, normalization, and fixture validation | lines 4020-4336, from `load_json` through `fixture_paths_from_manifest` | 23 functions; file loading plus otherwise mostly pure validation/indexing helpers |
| Phase9 fixture selection and conversion evaluation | lines 4337-5770 plus `evaluate_p9_harness` | 29 `p9_*` functions plus the harness orchestrator; manifest selection, artifact validation, external-AI guard, conversion timing, and fail-closed results |
| MVP metrics and limits | lines 5783-7156 | `mvp_acceptance_status`, `mvp_acceptance_items_with_rollup`, `_mvp_ratio_metric`, `mvp_case_metrics`, `_mvp_snapshot_integrity`, `mvp_metrics_rollup`, manifest/limit helpers, and `MVPConversionTimeoutError` |
| MVP conversion and harness | lines 7159-8825 | conversion I/O, audit/review evaluation, timeout handling, authoritative review recording, fixture approval hashing, and acceptance report assembly |
| Repository state | lines 8899-9006 | git commit, tracking, worktree cleanliness, stdout path, and exclusion pathspec helpers |
| PoC acceptance | lines 9007-9749 plus auth helpers above | report construction, evidence-path validation, status aggregation, scenario checks, known limitations, and follow-up candidates |
| Phase0/8 evaluation | lines 9750-10857 and `evaluate_llm_stability_report` | case validation, deterministic metrics, LLM stability, manual correction time, and PoC mode comparison |
| GMP acceptance | lines 10858-11332 | public evidence validation, verification-command safety checks, segregation-of-duties checks, and target calculation |
| CLI orchestration | `main`, lines 11357-11500 | argument parsing, mode selection, error translation, JSON output, and exit status |

### Constants and shared types

Constants should move with their owning responsibility, but shared error and
report types must not be duplicated.

| Owner | Names / rule |
| --- | --- |
| PoC authentication | all 20 `POC_AUTH_SESSION_*` constants |
| Phase9 | `DEFAULT_P9_HARNESS_MANIFEST` and the 12 `P9_*` constants |
| MVP | `DEFAULT_MVP_*` and the 17 `MVP_*` constants |
| GMP | the 8 `GMP_*` / `REQUIRED_GMP_ACCEPTANCE_CRITERIA` constants |
| Shared CLI/defaults | `REPO_ROOT`, `UTC`, `DEFAULT_EVALUATION_CASES`, `DEFAULT_LLM_STABILITY_RUNS`, `DEFAULT_POC_COMPARISON`, `DEFAULT_GMP_ACCEPTANCE` |
| Shared schemas and validation | evaluation, fixture, high-risk-label, PoC comparison, and LLM stability schema constants; `HighRiskLabelKey`; `EvaluationCaseError` |
| Result models | the 14 classes at lines 3253-4017; keep them in the compatibility facade until their `as_dict()` dependencies point in one direction |

## Candidate modules and dependency map

The eventual internal package should be
`scripts/evaluate_dataset_modules/`. `scripts/evaluate_dataset.py` remains the
executable compatibility facade. `REPO_ROOT` is already inserted into
`sys.path`, so imports such as
`from scripts.evaluate_dataset_modules.mvp_metrics import ...` work for both
direct CLI execution and the current test loader.

| Candidate module | Owns | Direct dependencies | I/O boundary | Main risk |
| --- | --- | --- | --- | --- |
| `poc_auth.py` | `POC_AUTH_SESSION_*`, AST evidence analyzer | `ast`, `Path`, shared evidence types | reads README and `tests/test_poc_web_api.py` | `poc_auth_session_coverage_inputs_tracked_in_repo` currently calls a later PoC acceptance path helper |
| `shared.py` | shared schemas, `EvaluationCaseError`, pure normalization/index helpers | standard library only | `load_json` is the sole file reader and may be separated | becoming a miscellaneous dumping ground |
| `phase9.py` | `P9_*`, `p9_*`, Phase9 harness | shared validators, conversion APIs, report model | manifests, fixture bytes, clock | calls `mvp_scanned_pdf_boundary_evaluation` |
| `mvp_metrics.py` | pure MVP status/ratio/snapshot/rollup calculations | shared types/constants; selected Phase9 observations today | none for the first wave | importing Phase9 from MVP would preserve the current cycle |
| `mvp_harness.py` | conversion, timeout, review/audit, fixture approval | Phase9 artifact helpers, MVP metrics, converter APIs | fixture bytes, temp files, clock | broad side effects and exception semantics |
| `poc_acceptance.py` | PoC report builder and matrix/status helpers | PoC auth, Phase9, Phase0/8 evaluation, git helpers | manifests, repository paths, git | high fan-in; move after leaf modules |
| `phase0.py` | case metrics, LLM stability, PoC comparison | shared validators and result models | public JSON inputs | shared high-risk-label helpers also feed GMP |
| `gmp.py` | GMP validators and evaluator | Phase0 comparison metrics, shared validators | repository evidence paths | command validation must remain fail closed |
| `repository.py` | git/path/snapshot helpers | `Path`, `subprocess` | git process and worktree | exact path exclusions affect reproducibility |
| `cli.py` | parser and mode dispatch | every report builder | stdout/stderr | last module to move; preserves all user-facing behavior |

Current high-level dependency shape:

```text
evaluate_dataset.py facade / main
  +-> PoC acceptance -> PoC auth
  |                  -> Phase9 harness
  |                  -> Phase0/8 metrics
  |                  -> repository/git
  +-> MVP acceptance -> MVP harness -> MVP metrics
  |                              \\-> Phase9 artifact helpers
  +-> Phase9 harness -------------> MVP scanned-PDF boundary helper
  +-> GMP acceptance -> Phase0 comparison/shared validation
  +-> result models -> MVP metric functions
```

Three cycles must be broken deliberately:

1. Phase9 calls `mvp_scanned_pdf_boundary_evaluation`, while MVP functions call
   `p9_external_ai_api_guard_observation`, `p9_primary_artifact`, and
   `p9_validate_artifact_expectations`. Do not create mutually importing
   modules. Keep these bridge functions in the facade until a later issue
   assigns the artifact-observation primitives to a neutral module.
2. report classes call metric functions in `as_dict()`, while builders return
   those report classes. Keep result models in the facade initially.
3. the PoC auth analyzer calls `poc_acceptance_tracked_repo_path`. Move that
   path-aware wrapper with PoC acceptance, or pass a tracking predicate into
   the analyzer; a leaf auth module must not import the facade.

Leaf modules must never import `scripts.evaluate_dataset`. Shared primitives
move before dependants, or dependencies are injected. This rule makes an
accidental facade/leaf cycle reviewable.

## I/O boundaries

The following operations require explicit characterization before their owning
responsibility moves:

| Boundary | Current functions |
| --- | --- |
| JSON/text input | `load_json`, `_poc_auth_session_evidence_sources`, `mvp_role_permissions_from_source`, acceptance builders |
| Fixture bytes and archives | `p9_conversion_result`, `mvp_conversion_result`, `mvp_fixture_approval_contract`, acceptance builders |
| Converter APIs and temp files | `p9_conversion_result`, `mvp_convert_uploaded_document`, `mvp_conversion_result` |
| Clock/timeout | `p9_conversion_result`, `mvp_conversion_result`, `evaluate_mvp_harness` |
| Git subprocess/worktree | `current_git_commit`, `git_path_is_tracked`, `current_git_worktree_clean`, `poc_acceptance_tracked_repo_path` |
| CLI stdout/stderr | `main` only |

No leaf calculator should gain filesystem, subprocess, clock, stdout, or
environment access as part of a move.

## Compatibility contract

Every migration PR must preserve all of the following:

### Import and executable surface

- `python3 scripts/evaluate_dataset.py` remains the executable entry point.
- loading the path as module name `evaluate_dataset` continues to succeed.
- every name currently accessed as `evaluate_dataset.<name>` by repository
  tests remains available from the facade with the same call signature.
- internal module paths are not public API in the first migration wave.

### CLI

The accepted options remain:

- `--cases PATH`
- `--llm-stability-report`
- `--llm-stability-runs PATH`
- `--poc-comparison PATH`
- `--gmp-acceptance PATH`
- `--p9-harness [PATH]`
- `--mvp-harness [PATH]`
- `--mvp-acceptance-report [PATH]`
- `--poc-acceptance-report [PATH]`

The six report modes remain mutually exclusive exactly as enforced by `main`.
An argparse usage error exits 2. A caught `OSError`, `JSONDecodeError`, or
`EvaluationCaseError` prints `Evaluation failed: ...` to stderr and exits 1.
An unmet GMP target exits 1. Other successfully generated reports retain their
current exit status, including reports whose JSON contains non-passing domain
status.

### JSON and status semantics

- stdout remains `json.dumps(metrics.as_dict(), indent=2, sort_keys=True)` plus
  its terminating newline.
- schema-version fields, keys, list ordering, numeric values, and `null`
  placement do not change.
- `pass`, `fail`, and `unknown` remain distinct. Missing or malformed evidence
  that is currently `unknown` must not become `fail` or `pass`.
- `failure_reasons`, `unknown`, and `exclusions` retain their present meaning
  and deterministic order.
- fail-closed security and GMP checks retain their current messages and
  thresholds.
- canonical hashing inputs, sorted-key serialization, fixture iteration order,
  and evidence-ref order remain deterministic.

The complete stdout is not byte-for-byte deterministic across live runs.
Two consecutive same-commit baseline `--poc-acceptance-report` runs differed at
exactly 29 leaves: the top-level `generated_at`, 14
`p9_harness.results[*].processing_time_ms` values, and the same 14 timings
repeated under `p9_harness_results[*]`. Migration comparisons must either mock
the wall/performance clocks or normalize only those documented volatile paths.
Across two clean commits, the four SHA-valued
`tested_environment.{commit,evaluator_commit}` and
`matrix_evidence.reproducibility.{commit,evaluator_commit}` leaves also differ.
The `acceptance_matrix` reproducibility evidence embeds the commit again.
Comparisons made from different worktree roots also produce two different
absolute Phase8 input-source paths. The corresponding four cleanliness leaves
are revision-bound guards, not ordinary volatility.

Apply only this normalization after the stated validation:

| Paths | Validate before normalization | Normalize |
| --- | --- | --- |
| `generated_at`; every `processing_time_ms` | values have the documented string/number types | fixed volatile sentinels |
| four commit and four cleanliness leaves under `tested_environment` and `matrix_evidence.reproducibility` | both commits equal the checkout HEAD; both cleanliness values are `true`; both sections agree | fixed repository-state sentinels |
| `acceptance_matrix` row whose `criterion_id` is `reproducibility` | status is `pass`; evidence contains that checkout's exact HEAD and `worktree clean: True` | replace only the validated HEAD substring |
| `p9_harness.phase8_comparison.{llm_stability_source,poc_comparison_source}` | resolved paths equal the expected files below the checkout root | repo-relative POSIX paths |

After time, validated repository-state, and validated checkout-root
normalization, every remaining JSON path and value must match.

## Characterization tests

`tests/test_evaluate_dataset.py` already exercises end-to-end CLI and report
paths. The module-split seam test
`test_module_split_characterization_preserves_representative_contracts` adds
compact exact contracts for:

- PoC auth: facade signature and ordered evidence references;
- Phase9: the external-AI guard's `True` / `False` / `None` semantics;
- MVP metrics: ratio threshold pass/fail/unknown behavior and reason strings.

`test_mvp_snapshot_integrity_preserves_existing_mapping` remains the exact
characterization for the selected first-wave symbol.

Required verification for this characterization PR and every migration PR:

```sh
python3 -m py_compile scripts/evaluate_dataset.py tests/test_evaluate_dataset.py
python3 -m unittest tests.test_evaluate_dataset
python3 -m unittest discover -q
python3 scripts/evaluate_dataset.py --poc-acceptance-report
git diff --check origin/main...HEAD
python3 scripts/ci/repo_hygiene.py
```

For migration PRs, capture baseline and candidate
`--poc-acceptance-report` JSON from clean commits. Before normalization, assert
all preconditions in the normalization table above. Apply exactly those
normalizations, serialize with sorted keys, and compare SHA-256 hashes in
addition to checking exit status. Do not compare raw stdout hashes or normalize
repository-state and checkout-root fields without validating them first.

## First migration wave

The first implementation issue must move one responsibility in one PR:
`_mvp_snapshot_integrity` only.

### Symbol and facade plan

1. Add `scripts/evaluate_dataset_modules/__init__.py`.
2. Add `scripts/evaluate_dataset_modules/mvp_metrics.py`.
3. Move `_mvp_snapshot_integrity` unchanged into that module. Its only runtime
   dependencies are `re`, `Mapping`, `isinstance`, and `int`; it performs no
   I/O and reads no module constant.
4. Import and re-export `_mvp_snapshot_integrity` from
   `scripts/evaluate_dataset.py` under the same name.
5. Do not move `_mvp_ratio_metric`, `mvp_metrics_rollup`, constants, report
   classes, or any second helper in that PR.

### Validation and rollback

- Run the six required commands above.
- Compare normalized `--poc-acceptance-report` SHA-256 before and after the
  move, excluding only the documented timestamp, processing-time, separately
  validated revision-bound values, and separately validated checkout roots.
- Confirm the facade signature remains
  `(snapshot_metadata: 'Mapping[str, object]') -> 'dict[str, object]'`.
- Confirm `test_mvp_snapshot_integrity_preserves_existing_mapping` and the
  module-split seam test pass.
- Roll back by restoring the unchanged function body in the facade and removing
  its import plus the two new package files. No data migration or schema
  rollback is involved.

Later PRs may move additional pure MVP metric helpers one at a time, but only
after the first wave proves the facade and test strategy. Phase9/MVP bridge
functions, result models, builders, and CLI orchestration are explicitly not
part of the first wave.
