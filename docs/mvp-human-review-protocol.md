# MVP Human-Review Protocol

- Protocol version: `p12g-13-human-review-v1`
- Evidence schema version: `veridoc-mvp-human-review-evidence/v1`
- Approved scope decision: `p12g-02-v1` (`OD-EFFICIENCY-SCOPE`)
- Target manifest revision: `phase12-mvp-v1`
- Applicable cases: `mvp-word-001`, `mvp-excel-001`, `mvp-text-pdf-001`,
  `mvp-scanned-pdf-001`, and `mvp-record-pdf-001`

This protocol turns the approved efficiency scope into a repeatable human study.
It does not contain participant results and does not establish that the 30%+
efficiency target has been met. A completed record is valid only when it passes
`python3 scripts/ci/validate_mvp_human_review_evidence.py RECORD.json`.

## Study design

Use a within-participant comparison. Every participant performs the same five
tasks once in the `manual` arm and once in the `veridoc` arm. Both arms use the
same source fixture, task instructions, gold-answer revision, completion
checklist, and timing boundary. The gold answer remains hidden until the timed
run has stopped.

Recruit at least three designated document reviewers with relevant experience.
Assign repository-safe pseudonyms matching `P[0-9]{3,}`. Keep the mapping from
pseudonym to identity outside the repository and outside the evidence record.
Do not retain direct participant identity: names, email addresses, employee IDs,
free-text biographies, or other direct identifiers are prohibited.

Before timed work, each participant completes one fixed, unscored practice task
for each arm. Record the fixed task, training material, and assistance contract
as one study-level `practice_revision`; it must not vary between participants or
arms. Counterbalance arm order: both `manual`-first and `veridoc`-first orders
must occur, and their participant counts may differ by at most one.

## Timing and run accounting

Start `started_at` when the reviewer has the source, target format, and checklist.
Stop `ended_at` only when either:

1. the reviewer has an approved artifact and a completed checklist; or
2. the reviewer declares the run blocked and records a controlled
   `blocker_code`.

Record pauses and interruptions in `excluded_pause_seconds`. The correction time
for a run is:

```text
correction_time_seconds =
  (ended_at - started_at in seconds) - excluded_pause_seconds
```

Do not silently delete a pause, retry, blocked run, or exclusion. Each attempt is
a run record. `attempt_number` starts at 1; later attempts retain the same
participant, case, arm, task revision, and gold-answer revision. An excluded
attempt sets `excluded` to `true` and uses one predeclared
`exclusion_reason_code`:

- `technical_failure`
- `protocol_deviation`
- `participant_withdrew`
- `invalid_timing`

Each participant/case/arm must have exactly one non-excluded attempt in a
completed study. Excluded attempts remain in the record. A blocked non-excluded
attempt is accounted for and makes its pair ineligible for the 30% calculation;
it is not an exclusion. Attempt numbers for each participant/case/arm are
contiguous from 1, and no two timed runs for one participant may overlap.

For a given case, every participant and every retained attempt uses one common
`task_revision` and one common `gold_answer_revision`. The validator derives
`high_risk_expected_count` from the pinned `phase12-mvp-v1` manifest; evidence
records cannot redefine that count.

## Measures and calculations

For each retained run, including excluded attempts, record:

- `high_risk_expected_count`: high-risk gold targets in the task;
- `high_risk_miss_count`: expected high-risk targets not identified and
  correctly resolved before completion;
- `over_detection_count`: reviewed high-risk flags that are not high-risk gold
  targets;
- `outcome`: `approved` or `blocked`;
- `blocker_code`: a controlled reason for a blocked outcome; and
- the timing fields used by the correction-time formula.

For participant `p` and case `c`, form a pair only when both arms are
non-excluded, approved, checklist-complete, and have positive correction time:

```text
pair_reduction_percent(p, c) =
  100 * (manual_seconds - veridoc_seconds) / manual_seconds
```

Report every participant/case pair, including arm outcomes and blocker codes for
ineligible pairs. The paired cohort median is the median of all eligible
`pair_reduction_percent` values. The efficiency target passes only if that
median is at least 30%, every required run is accounted for, and every retained
VeriDoc attempt—including excluded retries—contains zero high-risk misses.
Report total and per-arm high-risk misses, over-detections, approved
completions, blockers, exclusions, and retries even when the efficiency target
fails or cannot be calculated.

`high_risk_miss_count` is bounded by `high_risk_expected_count`.
`over_detection_count` is a non-negative count, not a rate. A completion is an
`approved` outcome with `checklist_complete: true`. A blocker is a `blocked`
outcome with a non-null `blocker_code`.

## Privacy and approval boundary

The study owner obtains the applicable consent and quality approval before
timed work. The record stores only the consent form version, approval state,
approval timestamp, and approving role. It never stores the participant or
approver identity. Set `direct_identifiers_stored` to `false`.

The schema rejects unknown fields so that identity-like additions cannot be
silently retained. Controlled codes replace free-text participant notes.
Operational identity mappings, signed consent forms, and approval records stay
in the approved external system of record and are referenced only by version.

Any change to the task, timing boundary, cohort minimum, comparison formula,
privacy boundary, decision revision, or manifest revision requires a new
protocol/schema version and renewed approval. Evidence with another decision or
manifest revision does not validate under this protocol.

## Validation examples

`datasets/mvp_human_review_evidence_valid.json` is a synthetic,
one-case `validation_example`. It demonstrates three pseudonymous participants,
both arm orders, matched tasks/gold revisions, timing, misses, over-detections,
completion, and a blocker. It is not a completed study and cannot support an
acceptance claim.

`datasets/mvp_human_review_evidence_invalid_examples.json` contains mutations
applied to the valid example. The test suite verifies that direct identity,
cross-arm task drift, and unaccounted timing are rejected with the stated
validation errors.

Use `docs/mvp-human-review-execution-checklist.md` as the P12G-14 handoff for
participant execution and per-run record capture.
