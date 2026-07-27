# MVP Human-Review Execution Checklist and Record Template

Use this checklist with protocol `p12g-13-human-review-v1`. Record controlled
values in the JSON evidence file; do not put participant names, contact details,
employee IDs, or free-text participant notes in the repository.

## Study setup

- [ ] Consent approval and independent quality approval are complete before any
      timed work.
- [ ] An independently generated opaque `CF-`-prefixed uppercase UUIDv4,
      study-owner role, and approval timestamp are recorded in
      `consent_approval`; the version contains no organizer-selected text.
- [ ] Every pseudonymous participant records `consent_status: consented`, the
      approved `consent_form_version`, and a `consented_at` timestamp after form
      approval and before that participant's practice or timed work.
- [ ] Quality-approver role, approval status/timestamp, and independently
      generated opaque `QAR-`-prefixed uppercase UUIDv4 are recorded in
      `quality_approval`.
- [ ] At least three relevant document reviewers have IDs generated
      independently as `P-` plus a cryptographically random uppercase UUIDv4.
- [ ] No participant ID is transformed, truncated, hashed, or prefixed from an
      employee number or other existing identifier.
- [ ] `study_id` is generated as an `HR-`-prefixed uppercase UUIDv4 and is not
      derived from any participant, organizer, employer, or project name.
- [ ] The identity-to-pseudonym mapping is outside the repository.
- [ ] The five `phase12-mvp-v1` cases, task revisions, gold revisions, target
      formats, and completion checklist are fixed for both arms.
- [ ] The evidence pins target commit
      `584ef2db12a6676abb65f75de1ec38145e06b487`, source-tree listing SHA-256
      `0bec46f7d8240796a137a163c20c4ee5f98f867f5730d78fe56b571eeffd6b3c`,
      manifest Git blob
      `13450762d323198b1b6e87315be173c784fc4880`, and approved manifest
      contract SHA-256
      `5d91a67915d79c649954c5c8af02e74d08d94d0b97e7e673a7db690df61ebfff`.
- [ ] Every case uses `task-phase12-v1`, `gold-phase12-v1`, and
      `checklist-phase12-v1`; cohort agreement on a different revision is not
      accepted.
- [ ] `docs/mvp-human-review-timed-task-package.json` has SHA-256
      `55c15447c23b46cfee458a0bd13c3eac9916454b446a459dd2588412708aba47`
      and fixes common instructions, prohibited assistance, arm-specific tool
      allowances, and all five case expectations.
- [ ] `docs/mvp-human-review-completion-checklist-package.json` has SHA-256
      `15c40eebd279600abb8d0f0eaef8c6ecd595f77bf0b81cc0bbe5a7de01fc1b64`
      and fixes the ordered checklist, completion rules, hidden-gold boundary,
      and per-case timed-task bindings shared by both arms.
- [ ] `datasets/mvp_human_review_gold_package_v1.json` has SHA-256
      `d4dd34836d38eecc721af3d512caa978eaf9fa40cdf988d48e72ef8f1db44716`;
      its per-case canonical digests bind target format and scoring content.
- [ ] The pinned gold package remains `unapproved_validation_only`; it makes
      validation scoring reproducible but does not approve an efficiency claim.
- [ ] Gold answers are hidden until each timed run stops.
- [ ] Each completed participant completed one fixed, unscored practice task
      per arm; withdrawn participants retain actual flags and `null` timestamps
      for uncompleted arms.
- [ ] `docs/mvp-human-review-practice-package.json` has SHA-256
      `e01b405b9898ec6a52a3e8c67d4e78419559df0ba9686f5d9b4d8738a85d7b16`
      and defines both arm fixtures, training material, task instructions, and
      assistance contracts.
- [ ] Every participant records `practice-phase12-v1` and that package digest
      separately for manual and VeriDoc practice.
- [ ] Each completed participant records `withdrawn_at: null`; each withdrawn
      participant records a controlled UTC withdrawal boundary.
- [ ] Each arm's practice completion timestamp precedes that participant's
      earliest timed run and does not occur after withdrawal.
- [ ] Arm order is assigned before timed work and is counterbalanced.
- [ ] Timing device and pause/interrupt recording method are ready.

## Per-run procedure

- [ ] Confirm participant ID, case ID, arm, attempt number, task revision,
      gold-answer revision, and `checklist-phase12-v1`.
- [ ] Generate `run_id` only from participant ID, full case ID, arm, and
      attempt number; do not enter organizer-selected text.
- [ ] Confirm `source_fixture_id`, `source_fixture_path`, and
      `source_fixture_sha256` against the approved manifest fixture before
      timing begins.
- [ ] Confirm `conversion_mode` and `target_artifact_type` against the approved
      manifest case before timing begins.
- [ ] Confirm the pinned `task_package_path`, package SHA-256, case-specific
      `task_case_sha256`, and assigned-arm `task_arm_sha256` before timing.
- [ ] Confirm the pinned `checklist_package_path`, package SHA-256, and
      case-specific `checklist_case_sha256` before timing; use its ordered
      checklist unchanged for both arms.
- [ ] Use only the tools allowed by the pinned manual or VeriDoc arm contract;
      do not substitute another converter, OCR, generative-AI, or automation
      tool.
- [ ] Confirm the pinned `gold_package_path`, package SHA-256, and case-specific
      `gold_case_sha256` before independent assessment.
- [ ] For a VeriDoc arm, verify the per-run provenance record identifies the
      approved product commit/tree, clean checkout, reproducibly derived
      `git ls-tree` source-listing digest, explicit
      `unverified_validation_only` execution status, and attestation digest
      over the canonical provenance fields. Record `null` for a manual arm.
- [ ] Do not treat the source-listing digest as proof of the executable or
      checkout used for the run.
- [ ] Start timing only when source, target format, and checklist are available.
- [ ] Record every pause/interrupt in whole seconds.
- [ ] Do not expose the gold answer before the run stops.
- [ ] Set `gold_answer_hidden_until_ended_at` to `true` after confirming that
      the gold answer stayed hidden through `ended_at`. If it was exposed
      early, retain the attempt with `false`, `excluded: true`, and
      `exclusion_reason_code: protocol_deviation`.
- [ ] Stop timing only at approved artifact plus completed checklist, or at an
      explicit blocked outcome followed by checklist completion.
- [ ] Use strict UTC RFC 3339 timestamps with full seconds and `Z` or
      `+00:00`; do not use alternate separators or reduced precision. If a
      one-to-nine-digit fractional second is recorded, preserve every digit.
- [ ] Seal the stopped output artifact, or a canonical blocked-attempt envelope
      when no output exists, without exposing the gold answer to the participant.
- [ ] Record the unique opaque sealed-record ID, artifact/envelope SHA-256, and
      outcome-consistent artifact kind before assessment.
- [ ] An independent assessor compares sealed artifacts with the gold outside
      the participant's view and does not disclose the gold or comparison
      result to that participant.
- [ ] Record the independent-assessor and non-disclosure attestations,
      high-risk expected targets, high-risk misses, and over-detections.
- [ ] For a blocked run, select a controlled blocker code.
- [ ] For an excluded attempt, retain the attempt and select a controlled
      exclusion reason; otherwise record no exclusion reason.
- [ ] For `invalid_timing`, retain the original syntactically valid timestamp
      strings and non-negative pause value even when boundaries are equal,
      reversed, or irreconcilable. Do not repair the timer values or include
      that attempt in an interval or paired measurement.
- [ ] Treat every retained `started_at`, including `invalid_timing`, as
      participant activity that must follow study approval, participant
      consent, and both practice completions, and must precede withdrawal.
- [ ] Check that the record contains no direct identifier or free-text
      participant note.

## Per-run record template

| Field | Value |
| --- | --- |
| `run_id` | `RUN-{participant_id}-{uppercase case_id}-{uppercase arm}-{attempt_number}` |
| `sealed_artifact_record_id` | unique `SAR-`-prefixed uppercase UUIDv4 |
| `sealed_artifact_sha256` | non-zero lowercase SHA-256 of sealed bytes |
| `sealed_artifact_kind` | `output_artifact` / `blocked_attempt_envelope` |
| `participant_id` | `P-` plus an independently generated uppercase UUIDv4 |
| participant `participation_status` | `completed` / `withdrawn` |
| participant `withdrawn_at` | `null` when completed / controlled UTC RFC 3339 boundary when withdrawn |
| participant `consent_status` | `consented` |
| participant `consented_at` | UTC RFC 3339 timestamp after form approval and before practice/timed work |
| participant `consent_form_version` | same opaque `CF-`-prefixed UUIDv4 as `consent_approval` |
| participant `manual_practice_completed_at` | UTC RFC 3339 timestamp / `null` when withdrawn before completion |
| participant `manual_practice_revision` | `practice-phase12-v1` |
| participant `manual_practice_package_sha256` | approved immutable package digest |
| participant `veridoc_practice_completed_at` | UTC RFC 3339 timestamp / `null` when withdrawn before completion |
| participant `veridoc_practice_revision` | `practice-phase12-v1` |
| participant `veridoc_practice_package_sha256` | approved immutable package digest |
| `case_id` | one declared Phase 12 case |
| `source_fixture_id` | approved manifest fixture ID |
| `source_fixture_path` | approved repository-relative fixture path |
| `source_fixture_sha256` | lowercase SHA-256 of approved fixture content |
| `conversion_mode` | approved manifest conversion mode for the case |
| `target_artifact_type` | approved manifest artifact type for the case |
| `arm` | `manual` / `veridoc` |
| `veridoc_build_provenance` | closed source-tree provenance with execution explicitly unattested / `null` for manual |
| `attempt_number` | positive integer |
| `task_revision` | `task-phase12-v1` |
| `task_package_path` | `docs/mvp-human-review-timed-task-package.json` |
| `task_package_sha256` | pinned timed-task package SHA-256 |
| `task_case_sha256` | canonical SHA-256 of the matching timed-task case |
| `task_arm_sha256` | canonical SHA-256 of the assigned arm assistance contract |
| `gold_answer_revision` | `gold-phase12-v1` |
| `gold_package_path` | `datasets/mvp_human_review_gold_package_v1.json` |
| `gold_package_sha256` | pinned package SHA-256 |
| `gold_case_sha256` | canonical SHA-256 of the matching gold case object |
| `checklist_revision` | `checklist-phase12-v1` |
| `checklist_package_path` | `docs/mvp-human-review-completion-checklist-package.json` |
| `checklist_package_sha256` | pinned completion-checklist package SHA-256 |
| `checklist_case_sha256` | canonical SHA-256 of the shared checklist plus matching case binding |
| `gold_answer_hidden_until_ended_at` | `true` / `false` only for excluded `protocol_deviation` |
| `gold_answer_compared_by_role` | `independent_assessor` |
| `gold_answer_comparison_withheld_from_participant` | `true` attestation |
| `started_at` | UTC RFC 3339 timestamp |
| `ended_at` | UTC RFC 3339 timestamp |
| `excluded_pause_seconds` | non-negative whole seconds |
| `outcome` | `approved` / `blocked` |
| `checklist_complete` | `true` for every non-excluded outcome |
| `blocker_code` | controlled code / `null` |
| `high_risk_expected_count` | non-negative integer |
| `high_risk_miss_count` | integer not greater than expected |
| `over_detection_count` | non-negative integer |
| `excluded` | `true` / `false` |
| `exclusion_reason_code` | controlled code / `null` |

## Study closeout

- [ ] At least three participants have `participation_status: completed`.
- [ ] Every completed participant has one non-excluded record for every case
      and arm.
- [ ] Withdrawn participants and their existing attempts remain recorded; their
      unstarted future groups are not fabricated, and any completed pairs remain
      reported as ineligible.
- [ ] No attempt starts at or ends after its participant's `withdrawn_at`
      boundary.
- [ ] No participant consent or practice completion is recorded after
      withdrawal.
- [ ] Every participant's two practice attestations bind to the approved
      revision and immutable package digest.
- [ ] All retries and exclusions remain in the evidence.
- [ ] Retained attempts, usable timing intervals, and eligible paired
      measurements are reviewed as three separate sets; no exclusion is
      dropped merely because its interval is unusable.
- [ ] Every attempt has a unique sealed-artifact record ID and non-zero digest;
      assessor counts refer to those exact sealed bytes.
- [ ] Every VeriDoc attempt retains approved commit/tree source provenance and
      explicit execution-attestation status; every manual attempt records
      `null`.
- [ ] Both arm orders are represented among completed participants and their
      counts differ by at most one.
- [ ] Paired arms use identical task, gold-answer, and checklist revisions.
- [ ] Every attempt binds the same pinned timed-task package and correct
      case-specific digest; each arm uses its own fixed assistance-contract
      digest.
- [ ] Every attempt binds the same pinned completion-checklist package and the
      correct case-specific digest in both arms.
- [ ] Every retained attempt for a case uses the same cohort-wide task and
      gold-answer and checklist revisions.
- [ ] Attempt numbers are contiguous from 1 and no participant run intervals
      overlap.
- [ ] Attempt timestamps advance in attempt-number order within each
      participant/case/arm.
- [ ] Expected high-risk counts match the case content in the pinned gold
      package, not an unbound revision label or mutable runtime value.
- [ ] If `structured_high_risk_targets_ready` is `false`, do not claim the
      efficiency target; obtain a new approved decision/manifest revision.
- [ ] If `execution_attestation_ready` is `false`, do not report completed
      study pairs as eligible or claim an efficiency median; obtain an approved
      authenticated execution-attestation integration.
- [ ] Correction time is recomputed from timestamps minus recorded pauses.
- [ ] Every otherwise calculable completed-participant/case pair reports manual
      seconds, VeriDoc seconds, and reduction even when it is ineligible.
- [ ] The paired cohort median includes eligible pairs only; an ineligible
      pair's reported measurement is not silently promoted into the median.
- [ ] Per-arm misses, over-detections, completions, blockers, retries, and
      exclusions are reported.
- [ ] Overall totals report misses, over-detections, approved completions,
      blockers, retries, and exclusions.
- [ ] The CLI returns a controlled input error without a traceback for invalid
      UTF-8, malformed JSON, duplicate keys, and unrepresentable numeric
      tokens.
- [ ] Approved completions include excluded attempts whose outcome is
      `approved` and whose checklist is complete.
- [ ] The 30%+ claim is made only for a valid completed record whose paired
      median is at least 30% and whose VeriDoc arm introduced zero high-risk
      misses.
- [ ] The final JSON passes:

```bash
python3 scripts/ci/validate_mvp_human_review_evidence.py RECORD.json
```

- [ ] A duplicate JSON object key is rejected rather than silently overwritten.
