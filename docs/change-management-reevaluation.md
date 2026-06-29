# Change Management And Re-Evaluation Flow

This note defines the GMP-06 operating flow for controlled changes that can
alter extraction, LLM conversion, validation, or rendered output behavior. It is
not a formal QA SOP and does not claim production or GMP fitness. It records the
minimum repository-level gate that a pull request must make visible before the
change can be reviewed as a controlled VeriDoc change.

Related source notes:

- `docs/local-inference-setup.md`
- `adr/ADR-001-local-llm-standard-model.md`
- `datasets/README.md`
- `docs/template-change-history.md`

## Target Changes

Treat the following changes as controlled re-evaluation triggers:

| Change target | Examples | Required reason |
| --- | --- | --- |
| model | standard model profile, high-quality profile, model env contract, local inference routing | Model behavior can change extraction confidence, JSON stability, and high-risk review outcomes. |
| prompt | system prompt, extraction instruction, repair instruction, Japanese table handling instruction | Prompt behavior can change confirmed values or review recommendations without code changes. |
| logic | parser, validator, template matching, automatic confirmation, review-state transition | Logic changes can alter authoritative state and must fail closed when provenance or scope is missing. |
| template | template definition schema, template fingerprinting, version registry, field mapping | Template changes can move field anchors or risk labels and must remain auditable by version. |
| renderer | OOXML/PDF renderer behavior, cell formatting, exported record shape | Renderer changes can alter operator-facing records and must preserve traceability to the reviewed source. |
| evaluation gate data | `datasets/gold/high_risk_labels_v0.json`, `datasets/gold/evaluation_cases_v0.json`, `datasets/gold/poc_mode_comparison_v1.json` | Gate data defines the controlled target and expected high-risk outcome, so edits can weaken or move the GMP-01 boundary even without product-code changes. |

If a pull request touches more than one target, apply the strongest gate from
all affected rows. Do not infer that a neighboring template, prompt, or model is
covered unless the change record explicitly names it.

Changes to evaluation gate data are controlled changes. A PR that edits a gold
label set, evaluation case, PoC comparison record, or the fixture manifest it
depends on must explain why the controlled target changed, identify the
authoritative source or fixture record that justifies the update, rerun the
comparison harness, and preserve the GMP-01 high-risk miss target at zero. Do
not treat gate-data edits as harmless fixture maintenance.

## Required Re-Evaluation Gates

Every controlled change must document the exact changed target, the reason for
the change, and the focused verification that covers the enforcement boundary.
At minimum, run repository hygiene:

```sh
python3 scripts/ci/repo_hygiene.py
```

For model, prompt, logic, template, or renderer changes that can affect
automatic confirmation or review recommendations, also rerun the public
high-risk comparison harness:

```sh
python3 scripts/evaluate_dataset.py --poc-comparison datasets/gold/poc_mode_comparison_v1.json
```

This gate must use fresh public synthetic outputs from the changed branch for
model, prompt, or logic changes. First regenerate or recapture the affected mode
outputs in `datasets/gold/poc_mode_comparison_v1.json`, including the captured
`actual` cells and high-risk item results, then run the comparison harness
against that updated controlled record. Re-scoring a stale comparison file from
a previous model, prompt, or logic version does not satisfy the re-evaluation
gate. If the required inference profile, fixture, or capture path is
unavailable, the PR must stay blocked and record the missing prerequisite.

The comparison output must keep the GMP-01 high-risk miss gate at zero:

- `high_risk_false_auto_confirmed_count` must be `0`.
- `high_risk_false_auto_confirmed_target` must be `0`.
- `target_met` must remain true for the high-risk gate.

If the harness cannot run, the PR must stay blocked and record the missing
prerequisite. Do not replace the high-risk check with a subjective review note,
sample secret, placeholder credential, or unscored local file.

Use these additional focused checks when the touched target makes them relevant:

| Change target | Additional focused check |
| --- | --- |
| model | Confirm the profile and ADR still require explicit env binding and do not infer the model from host names or filenames. |
| prompt | Run the smallest conversion-plan or evaluation fixture test that exercises the changed instruction. |
| logic | Add or update a unit test at the authoritative state or validation boundary, including rejected-path state cleanliness when applicable. |
| template | Update template schema/version/fingerprint tests and confirm template change history records the reason, actor, approval state, and timestamp. |
| renderer | Run the renderer-specific unit test and inspect whether the rendered output still maps to the source record and reviewed value. |

## Approval And Audit Records

Each controlled change must leave a reviewable audit trail in the PR description
or linked change note:

- changed target: model, prompt, logic, template, renderer, or an explicit
  combination;
- change reason and expected behavior difference;
- evaluation commands run and their result summaries;
- high-risk gate result when the change can affect automatic confirmation;
- approval status, reviewer identity, and any required GMP SME follow-up;
- dataset or fixture scope used for re-evaluation;
- residual risks and non-goals.

Approval is not inferred from branch names, issue labels, file paths, or nearby
comments. Missing approval context stays explicit and unapproved until a real
review record is attached.

## Rollback And Difference Explanation

Every controlled change must describe how to roll back and how to explain any
observed output difference:

- rollback target: the prior model profile, prompt version, logic commit,
  template version, or renderer behavior to restore;
- difference explanation: the expected before/after effect on extracted values,
  review recommendations, rendered output, or audit records;
- compatibility note: whether existing fixture outputs, template fingerprints,
  or review histories need regeneration;
- failed-path note: whether a rejected or failed update leaves no orphan record,
  partial durable write, or half-restored state.

Rollback instructions must use repo-relative paths, documented environment
variables, or explicit placeholders. Do not publish workstation-local absolute
paths in durable docs or PR notes.

## PR Checklist

Before requesting review for a controlled change, confirm:

- the PR names the affected target changes: model, prompt, logic, template, or
  renderer, or evaluation gate data;
- the reason, expected behavior difference, approval status, audit record, and
  rollback plan are written down;
- gate-data edits explain the authoritative source or fixture record that
  justifies changing the controlled target;
- model, prompt, or logic changes refresh the affected public synthetic outputs
  in `datasets/gold/poc_mode_comparison_v1.json` before the comparison harness
  is run;
- `python3 scripts/evaluate_dataset.py --poc-comparison datasets/gold/poc_mode_comparison_v1.json`
  was run when automatic confirmation or review recommendation behavior can
  change;
- the GMP-01 high-risk gate stayed at
  `high_risk_false_auto_confirmed_count == 0` and
  `high_risk_false_auto_confirmed_target == 0`;
- the narrowest relevant unit or fixture test covers the authoritative boundary
  changed by the PR;
- `python3 scripts/ci/repo_hygiene.py` passes.
