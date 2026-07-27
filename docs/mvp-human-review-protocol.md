# MVP Human-Review Protocol

- Protocol version: `p12g-13-human-review-v1`
- Evidence schema version: `veridoc-mvp-human-review-evidence/v1`
- Approved scope decision: `p12g-02-v1` (`OD-EFFICIENCY-SCOPE`)
- Target manifest revision: `phase12-mvp-v1`
- Approved target product commit:
  `584ef2db12a6676abb65f75de1ec38145e06b487`
- Approved source-tree listing SHA-256:
  `0bec46f7d8240796a137a163c20c4ee5f98f867f5730d78fe56b571eeffd6b3c`
- Approved manifest Git blob: `13450762d323198b1b6e87315be173c784fc4880`
- Approved manifest contract SHA-256:
  `5d91a67915d79c649954c5c8af02e74d08d94d0b97e7e673a7db690df61ebfff`
- Approved task-instruction revision: `task-phase12-v1`
- Approved gold-answer revision: `gold-phase12-v1`
- Applicable cases: `mvp-word-001`, `mvp-excel-001`, `mvp-text-pdf-001`,
  `mvp-scanned-pdf-001`, and `mvp-record-pdf-001`

This protocol turns the approved efficiency scope into a repeatable human study.
It does not contain participant results and does not establish that the 30%+
efficiency target has been met. A completed record is valid only when it passes
`python3 scripts/ci/validate_mvp_human_review_evidence.py RECORD.json`.

## Study design

Use a within-participant comparison. Every completed participant performs the
same five tasks once in the `manual` arm and once in the `veridoc` arm. Both
arms use the same source fixture, task instructions, gold-answer revision,
completion checklist, and timing boundary. The gold answer remains hidden until
the timed run has stopped.
Every run attests this boundary with
`gold_answer_hidden_until_ended_at: true`; a procedure note alone is not
evidence that the gold remained hidden.
The participant never performs the gold comparison. An `independent_assessor`
compares sealed artifacts with the gold outside the participant's view and
withholds the result from that participant. Every run records
`gold_answer_compared_by_role: independent_assessor` and
`gold_answer_comparison_withheld_from_participant: true`. This separation keeps
the second paired run blinded even if assessment of the first artifact occurs
before that second run.

Retain at least three completed designated document reviewers with relevant
experience. Generate `study_id` as an `HR-`-prefixed uppercase UUIDv4; do not
derive it from a participant, organizer, employer, or project name. Independently
generate each participant pseudonym as `P-` plus a cryptographically random
uppercase UUIDv4 before associating it with a participant. Do not transform,
truncate, hash, or prefix an employee number or other existing identifier.
Keep the mapping from pseudonym to identity outside the repository and outside
the evidence record. Do not retain direct participant identity: names, email
addresses, employee IDs, free-text biographies, or other direct identifiers
are prohibited.

Before timed work, each completed participant completes one fixed, unscored
practice task for each arm. Record the fixed task, training material, and
assistance contract as one study-level `practice_revision`; it must not vary
between participants or arms. Completed participants record both practice flags
as `true`, and both UTC completion timestamps must strictly precede their
earliest timed run. A participant who withdraws before or during practice
retains each actual completion flag; an uncompleted arm records `false` with a
`null` timestamp, and `arm_order` may remain `null` if it was not assigned.
Every participant records `withdrawn_at`: completed participants use `null`,
while withdrawn participants record the controlled UTC withdrawal boundary.
Counterbalance arm order among completed participants: both `manual`-first and
`veridoc`-first orders must occur, and their participant counts may differ by at
most one.

Every run records `checklist_revision: checklist-phase12-v1`. This is the
approved completion checklist shared by both arms and every participant; a
different or missing checklist revision invalidates the run rather than
creating a second baseline.

## Timing and run accounting

Start `started_at` when the reviewer has the source, target format, and checklist.
Stop `ended_at` only when either:

1. the reviewer has an approved artifact and a completed checklist; or
2. the reviewer declares the run blocked and records a controlled
   `blocker_code`, then completes the checklist.

All timestamps use the lexical form
`YYYY-MM-DDTHH:MM:SS[.fraction](Z|+00:00)`. Alternate separators, reduced
precision, missing UTC designators, and non-zero offsets are invalid.

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

At least three participants must have `participation_status: completed`. Each
completed participant/case/arm must have exactly one non-excluded attempt.
Participants who withdraw remain declared with
`participation_status: withdrawn`; their existing attempts remain in the record,
including any `participant_withdrew` exclusion, but their unstarted future
participant/case/arm groups are not required and they are excluded from the
paired cohort median. Any fully recorded pair completed before withdrawal
remains in `pair_results` as ineligible. A blocked non-excluded attempt is
accounted for, requires a completed checklist, and makes its pair ineligible for
the 30% calculation; it is not an exclusion. Attempt numbers for each recorded
participant/case/arm are contiguous from 1, and no two timed runs for one
participant may overlap.
No timed attempt may start at or end after that participant's `withdrawn_at`
boundary; an attempt ending exactly at withdrawal is retained.
Within each participant/case/arm, attempt timestamps must also advance in
`attempt_number` order; a retry cannot occur before the attempt it retries.
Each attempt uses the generated opaque ID
`RUN-{participant_id}-{uppercase case_id}-{uppercase arm}-{attempt_number}`.
For example, participant
`P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A` reviewing `mvp-word-001`
manually on the first attempt records
`RUN-P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A-MVP-WORD-001-MANUAL-1`.
Organizer-selected text, names, employee identifiers, and other direct identity
fragments are not permitted in `run_id`.

At timing stop, seal either the produced output artifact or, for a blocked run
without an output, a canonical blocked-attempt envelope. Every attempt records
an opaque `sealed_artifact_record_id`, non-zero
`sealed_artifact_sha256`, and outcome-consistent `sealed_artifact_kind`. The
external sealed record is retained under that ID so the independent assessor's
miss and over-detection counts can be audited against the exact bytes assessed.
Artifact record IDs are unique across attempts.

For every approved case, every participant and every retained attempt uses the
protocol-pinned `task_revision: task-phase12-v1` and
`gold_answer_revision: gold-phase12-v1`. Cohort-wide agreement on any other
value is not sufficient. Every attempt records `source_fixture_id`,
`source_fixture_path`, and `source_fixture_sha256`; the validator reconstructs
the approved manifest and selected fixture contents from the approved target
commit, verifies the recorded Git blob and canonical contract SHA-256, and
requires the three source fields to match that immutable contract.

Manual runs record `veridoc_build_provenance: null`. Every VeriDoc run records
a closed build-provenance object containing an opaque attestation record ID,
the approved product commit and Git tree, clean-checkout state, source-tree
derivation status, the approved source-tree listing SHA-256, an explicit
execution-attestation status, and an attestation SHA-256 over the canonical
provenance fields. The source-tree digest is SHA-256 over the exact bytes emitted by
`git ls-tree -r -z --full-tree APPROVED_PRODUCT_COMMIT`; the validator
independently derives that digest, resolves the approved Git tree, recomputes
the attestation digest, and rejects other commits, trees, listings, states, or
unsealed provenance.

That Git listing identifies approved source, not the executable or checkout
actually used for a run. This protocol therefore fixes
`derivation_status: approved_source_tree_verified_execution_unattested` and
`execution_attestation_status: unverified_validation_only`. A validation
example may exercise the paired calculation, but completed-study pairs remain
ineligible, the paired median remains unavailable, and
`efficiency_target_met` remains `false` until an authenticated external
execution attestation and its schema/validator integration are separately
approved. Issue #319 excludes product modification and actual participant
trials, so this revision does not invent an executable identity or signing
service.

The approved `p12g-02-v1` manifest does not contain the later structured
`expected_high_risk_targets` fields. The validator therefore derives an empty
approved target set instead of reading the mutable current checkout and reports
`structured_high_risk_targets_ready: false`. Even if timing improves by 30%,
`efficiency_target_met` remains `false` until the decision owner approves a new
manifest contract and decision revision containing those structured targets.
Evidence records cannot substitute the current unapproved counts.

## Measures and calculations

For each retained run, including excluded attempts, record:

- `high_risk_expected_count`: high-risk gold targets in the task;
- `high_risk_miss_count`: expected high-risk targets not identified and
  correctly resolved before completion;
- `over_detection_count`: reviewed high-risk flags that are not high-risk gold
  targets;
- `outcome`: `approved` or `blocked`;
- `checklist_complete`: required `true` for every non-excluded outcome,
  including a controlled `blocked` outcome;
- `blocker_code`: a controlled reason for a blocked outcome;
- `gold_answer_hidden_until_ended_at`: the controlled attestation that the gold
  answer remained hidden until timing stopped;
- `gold_answer_compared_by_role` and
  `gold_answer_comparison_withheld_from_participant`: controlled attestations
  that an independent assessor performed the comparison without disclosing the
  gold or result to the participant; and
- the timing fields used by the correction-time formula.

For participant `p` and case `c`, form a pair only when both arms are
non-excluded, approved, checklist-complete, and have positive correction time.
For a completed study, authenticated execution attestation is also required;
the validation example may demonstrate recomputation while remaining
execution-unattested:

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
`approved` outcome with `checklist_complete: true`, including an excluded
attempt. A blocker is a `blocked` outcome with a non-null `blocker_code`.

## Privacy and approval boundary

The study owner obtains consent before timed work and records it in
`consent_approval` with role `study_owner`, the controlled approval state and
timestamp, and the consent form version. Independently, a quality approver
records `quality_approval` with role `quality_approver`, the controlled approval
state and timestamp, and the external system-of-record version. Both approval
timestamps must strictly precede every timed run. Neither record stores the
participant or approver identity. Set `direct_identifiers_stored` to `false`.

The schema rejects unknown fields so that identity-like additions cannot be
silently retained. Controlled codes replace free-text participant notes.
The CLI parser also rejects duplicate JSON object keys instead of accepting the
last value, so every retained record has one deterministic interpretation.
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
