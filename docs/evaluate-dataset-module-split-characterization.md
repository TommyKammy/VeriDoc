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
| Module setup and constants | lines 1-319 | 82 assigned names: PoC auth 21 (including `AST_TRY_STATEMENT_TYPES`), Phase9 13, MVP 14, GMP 8, shared defaults/types/schemas 26; the remaining seven assignments are inventoried in their actual ranges below |
| PoC authentication evidence analyzer | lines 320-3252, from `poc_auth_session_coverage_evidence_refs` through `poc_auth_session_coverage_is_present` | 138 functions and 10 private analysis data classes; parses README and Python AST and fails closed when evidence is ambiguous |
| Result models and shared error | lines 3253-4017, from `EvaluationMetrics` through `EvaluationCaseError` | 13 report/metric/evidence classes plus the shared `EvaluationCaseError` and `LLM_STABILITY_ACCEPTANCE_THRESHOLD` at line 3347; `P9HarnessReport`, `MVPHarnessReport`, `MVPAcceptanceReport`, and `PoCAcceptanceReport` serialize domain results |
| Shared JSON, normalization, and fixture validation | lines 4020-4336, from `load_json` through `fixture_paths_from_manifest` | 23 functions; file loading plus otherwise mostly pure validation/indexing helpers |
| Phase9 fixture selection and conversion evaluation | lines 4337-5780 plus `evaluate_p9_harness` | 29 `p9_*` functions plus the harness orchestrator; manifest selection, artifact validation, external-AI guard, conversion timing, and fail-closed results |
| MVP metrics and limits | lines 5783-7156 plus pure high-risk classification helpers at lines 8085-8111 | `mvp_acceptance_status`, `mvp_acceptance_items_with_rollup`, `_mvp_ratio_metric`, `mvp_case_metrics`, `_mvp_snapshot_integrity`, `mvp_metrics_rollup`, `mvp_high_risk_target_key`, `mvp_review_item_is_high_risk`, manifest/limit helpers, and `MVPConversionTimeoutError` |
| MVP conversion and harness | lines 7159-8825 except the classification helpers at lines 8085-8111 | conversion I/O, audit/review evaluation, timeout handling, authoritative review recording, fixture approval hashing, acceptance report assembly, and `MVP_ACCEPTANCE_ITEM_IDS`, `MVP_ACCEPTANCE_SECTIONS`, `MVP_ACCEPTANCE_STATUS_SEPARATORS`, `MVP_MANIFEST_DECISION_CONTRACT_FIELDS`, `MVP_SCOPE_DECISION_APPROVED_CONTRACTS`, and `MVP_ACCEPTANCE_HARNESS_REFS` at lines 8153-8211 |
| Repository state | lines 8899-9006 | git commit, tracking, worktree cleanliness, stdout path, and exclusion pathspec helpers |
| PoC acceptance | lines 9007-9749 plus auth helpers above | report construction, evidence-path validation, status aggregation, scenario checks, known limitations, and follow-up candidates |
| Phase0/8 evaluation | lines 9750-10857, PoC comparison helpers `validate_text_list_allow_empty`, `review_key_for_diff`, and `mode_diff_summary` at lines 10874-10915, plus `evaluate_llm_stability_report` | case validation, deterministic metrics, LLM stability, manual correction time, and PoC mode comparison |
| GMP acceptance | `validate_text_list` at lines 10858-10871 and lines 10918-11332 | public evidence validation, verification-command safety checks, segregation-of-duties checks, and target calculation |
| CLI orchestration | `main`, lines 11357-11500 | argument parsing, mode selection, error translation, JSON output, and exit status |

### Constants and shared types

Constants should move with their owning responsibility, but shared error and
report types must not be duplicated.

| Owner | Names / rule |
| --- | --- |
| PoC authentication | all 20 `POC_AUTH_SESSION_*` constants plus `AST_TRY_STATEMENT_TYPES`, which `_ordered_function_statements` consumes |
| Phase9 | `DEFAULT_P9_HARNESS_MANIFEST` and the 12 `P9_*` constants |
| MVP | `DEFAULT_MVP_*` and the 17 `MVP_*` constants |
| GMP | the 8 `GMP_*` / `REQUIRED_GMP_ACCEPTANCE_CRITERIA` constants |
| Shared CLI/defaults | `REPO_ROOT`, `UTC`, `DEFAULT_EVALUATION_CASES`, `DEFAULT_LLM_STABILITY_RUNS`, `DEFAULT_POC_COMPARISON`, `DEFAULT_GMP_ACCEPTANCE` |
| Shared schemas and validation | evaluation, fixture, high-risk-label, PoC comparison, and LLM stability schema constants; `HighRiskLabelKey`; `EvaluationCaseError` |
| Result models | the 13 report/metric/evidence classes at lines 3253-4013; keep them in the compatibility facade until their `as_dict()` dependencies point in one direction. This hold does not include `EvaluationCaseError` |

## Candidate modules and dependency map

The eventual internal package should be
`scripts/evaluate_dataset_modules/`. `scripts/evaluate_dataset.py` remains the
executable compatibility facade. `REPO_ROOT` is already inserted into
`sys.path`, so imports such as
`from scripts.evaluate_dataset_modules.mvp_metrics import ...` work for both
direct CLI execution and the current test loader.

| Candidate module | Owns | Direct dependencies | I/O boundary | Main risk |
| --- | --- | --- | --- | --- |
| `poc_auth.py` | `POC_AUTH_SESSION_*`, `AST_TRY_STATEMENT_TYPES`, AST evidence analyzer | `ast`, `Path`, shared evidence types | reads README and `tests/test_poc_web_api.py` | `poc_auth_session_coverage_inputs_tracked_in_repo` currently calls a later PoC acceptance path helper |
| `shared.py` | shared schemas, `EvaluationCaseError`, normalization/index helpers | standard library only | `load_json` reads JSON; `fixture_paths_from_manifest` resolves and checks filesystem paths | path resolution, existence, and symlink containment semantics must remain fail closed; avoid becoming a miscellaneous dumping ground |
| `phase9.py` | `P9_*`, `p9_*`, Phase9 harness | shared validators, conversion APIs, report model, Phase0/8 report builder | manifests, fixture bytes, clock | calls both `mvp_scanned_pdf_boundary_evaluation` and `evaluate_llm_stability_report`; keep the harness in the facade until both dependencies are available |
| `mvp_metrics.py` | pure MVP status/ratio/snapshot/rollup calculations plus `mvp_high_risk_target_key` and `mvp_review_item_is_high_risk` | shared types/constants; selected Phase9 observations today | none for the first wave | importing Phase9 from MVP would preserve the current cycle |
| `mvp_harness.py` | conversion, timeout, review/audit, fixture approval | Phase9 artifact helpers, MVP metrics, converter APIs, repository snapshot helpers | fixture bytes, temp files, clock, process signals/timers, git, stdout fd | broad side effects and exception semantics |
| `poc_acceptance.py` | PoC report builder and matrix/status helpers | PoC auth, Phase9, Phase0/8 evaluation, git helpers | manifests, repository paths, git, clock, stdout fd | high fan-in; move after leaf modules |
| `phase0.py` | case metrics, LLM stability, PoC comparison, `validate_text_list_allow_empty`, `review_key_for_diff`, and `mode_diff_summary` | shared validators and result models | public JSON inputs | shared high-risk-label helpers also feed GMP |
| `gmp.py` | GMP validators and evaluator | Phase0 comparison metrics, shared validators | repository evidence paths | command validation must remain fail closed |
| `repository.py` | git/path/snapshot helpers | `Path`, `os`, `subprocess` | git process, worktree, and stdout fd inspection | exact path exclusions affect reproducibility |
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
  +-> Phase9 harness -> MVP scanned-PDF boundary helper
  |                  \\-> Phase0/8 report builder
  +-> GMP acceptance -> Phase0 comparison/shared validation
  +-> result models -> MVP metric functions
```

Four cycles must be broken deliberately:

1. Phase9 calls `mvp_scanned_pdf_boundary_evaluation`, while MVP functions call
   `p9_external_ai_api_guard_observation`, `p9_primary_artifact`, and
   `p9_validate_artifact_expectations`. Do not create mutually importing
   modules. Keep these bridge functions in the facade until a later issue
   assigns the artifact-observation primitives to a neutral module.
2. `mvp_case_metrics` calls both `mvp_high_risk_target_key` and
   `mvp_review_item_is_high_risk`, while `mvp_evaluation_cases` calls the
   target-key helper. Both helpers are physically inside the harness range, and
   the harness calls MVP metric functions (including
   `mvp_record_authoritative_review_decisions` calling the review-item
   helper). Move both pure classification helpers into `mvp_metrics.py`
   before moving the dependent calculators. Until then, keep all affected
   functions in the facade rather than creating metrics-to-harness imports.
3. report classes call metric functions in `as_dict()`, while builders return
   those report classes. Keep the 13 result models in the facade initially.
   `EvaluationCaseError` is not part of that hold: move it to `shared.py` before
   any validator, re-export the same class object from the facade, and have leaf
   modules import it from `shared.py`.
4. the PoC auth analyzer calls `poc_acceptance_tracked_repo_path`. Move that
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
| Fixture path resolution and existence | `fixture_paths_from_manifest` resolves the allowed root and each fixture path, rejects paths that escape after symlink-aware resolution, and requires each resolved target to satisfy `is_file()` |
| Fixture bytes and archives | `p9_xlsx_comments_by_ref` and `p9_docx_source_linkage` directly open OOXML ZIPs and parse XML; `p9_conversion_result`, `mvp_conversion_result`, `mvp_fixture_approval_contract`, acceptance builders |
| Converter APIs | `p9_conversion_result`, `mvp_convert_uploaded_document`, `mvp_conversion_result` |
| Direct temporary resources and persistence | `p9_validate_artifact_expectations` owns a written/flushed/unlinked `NamedTemporaryFile`; `mvp_record_authoritative_review_decisions` owns a `TemporaryDirectory` containing its SQLite repository |
| Wall/performance clock | `p9_conversion_result` and `mvp_conversion_result` measure elapsed time; `build_mvp_acceptance_report` emits an ISO-8601 `+00:00` timestamp, while `build_poc_acceptance_report` emits the UTC `Z` form |
| Process signal/timer | `mvp_convert_uploaded_document` directly reads, replaces, and restores the process-wide `SIGALRM` handler and `ITIMER_REAL` timer, including restoration from `finally` after `BaseException` |
| Git subprocess/worktree | `current_git_commit`, `git_path_is_tracked`, `current_git_worktree_clean`, `poc_acceptance_tracked_repo_path` |
| Redirected stdout path inspection | `current_stdout_path` reads `/proc/self/fd/1` or `/dev/fd/1`; `build_mvp_acceptance_report` and `build_poc_acceptance_report` use it to exclude an untracked redirected report from cleanliness checks |
| CLI JSON stdout/stderr | `main` only |

The path checks in `fixture_paths_from_manifest` are observable filesystem I/O:
preserve `Path.resolve()` before containment checking, the resulting symlink
behavior, the `is_relative_to(allowed_root)` rejection, and the final
`Path.is_file()` existence/type check.

For direct OOXML reads, preserve the existing asymmetric failure behavior.
`p9_xlsx_comments_by_ref` lets malformed ZIPs and archive-read/XML parse errors
propagate; an archive with no discovered comment parts yields an empty mapping.
`p9_docx_source_linkage` also lets ZIP and
`word/document.xml` read/parse failures propagate, but treats a missing or
malformed relationships part as having no comments relationship and a missing
or malformed linked comments part as having no source-linkage comments. Those
fallbacks feed ordinary linkage validation and must not be broadened into
silent recovery for the primary document or archive.

No leaf calculator should gain filesystem, subprocess, clock, process-signal,
stdout-fd, or environment access as part of a move.

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
| `generated_at` | parses as timezone-aware ISO-8601 UTC and uses the existing `Z` suffix | fixed volatile-time sentinel |
| `p9_harness.results[0:14].processing_time_ms` and mirrored `p9_harness_results[0:14].processing_time_ms` | both arrays contain the same 14 measured rows in the same order; each value is a positive number | fixed volatile-duration sentinels |
| four commit and four cleanliness leaves under `tested_environment` and `matrix_evidence.reproducibility` | both commits equal the checkout HEAD; both cleanliness values are `true`; both sections agree | fixed repository-state sentinels |
| `acceptance_matrix` row whose `criterion_id` is `reproducibility` | status is `pass`; evidence contains that checkout's exact HEAD and `worktree clean: True` | replace only the validated HEAD substring |
| `p9_harness.phase8_comparison.{llm_stability_source,poc_comparison_source}` | each raw value is an absolute path and resolves to the expected file below the checkout root | repo-relative POSIX paths |

After time, validated repository-state, and validated checkout-root
normalization, every remaining JSON path and value must match.
In particular, do not normalize `processing_time_ms` for index 14 or later:
the unavailable-fixture rows currently carry deterministic `0.0` values that
remain part of the ordinary JSON comparison.

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
python3 scripts/evaluate_dataset.py --mvp-acceptance-report
git diff --check origin/main...HEAD
python3 scripts/ci/repo_hygiene.py
```

For migration PRs, capture baseline and candidate
`--poc-acceptance-report` and `--mvp-acceptance-report` JSON from clean commits.
Before normalizing the PoC report, assert all preconditions in the normalization
table above. Apply exactly those normalizations, serialize with sorted keys, and
compare SHA-256 hashes in addition to checking exit status. Do not compare raw
stdout hashes or normalize repository-state and checkout-root fields without
validating them first.

The MVP report contains additional live conversion IDs, review timestamps, and
timings, so the first migration wave compares the production-path projection
that actually invokes the moved symbol instead of normalizing the whole report.
For each captured MVP report:

1. Assert `evidence_snapshot.metadata.{commit,evaluator_commit}` equal that
   checkout's HEAD and both cleanliness fields are `true`.
2. Extract
   `evidence_snapshot.metrics_rollup.dimensions.snapshot_integrity` and assert
   its four revision/cleanliness leaves exactly match the validated metadata.
3. Replace only its `commit` and `evaluator_commit` values with fixed
   revision sentinels, serialize the complete projection with sorted keys, and
   compare its SHA-256 across baseline and candidate.

Do not remove, normalize, or compare separately the projection's `status`,
`numerator`, `denominator`, `exclusions`, `unknown`, or `failure_reasons`.
Together with the direct malformed/dirty/missing-metadata cases, this protects
the real `MVPAcceptanceReport.as_dict()` call path against extraction drift.

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

- Run the seven required commands above.
- Compare normalized `--poc-acceptance-report` SHA-256 before and after the
  move, excluding only the documented timestamp, 14 measured processing-time
  rows and their mirrors, separately validated revision-bound values, and
  separately validated checkout roots.
- Compare the validated, revision-normalized MVP snapshot-integrity projection
  SHA-256 before and after the move as specified above.
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
