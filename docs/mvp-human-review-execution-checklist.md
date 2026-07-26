# MVP Human-Review Execution Checklist and Record Template

Use this checklist with protocol `p12g-13-human-review-v1`. Record controlled
values in the JSON evidence file; do not put participant names, contact details,
employee IDs, or free-text participant notes in the repository.

## Study setup

- [ ] Consent approval and independent quality approval are complete before any
      timed work.
- [ ] Consent form version, study-owner role, and approval timestamp are
      recorded in `consent_approval`.
- [ ] Quality-approver role, approval status/timestamp, and external-record
      version are recorded in `quality_approval`.
- [ ] At least three relevant document reviewers have repository-safe IDs
      matching `P[0-9]{3,}`.
- [ ] The identity-to-pseudonym mapping is outside the repository.
- [ ] The five `phase12-mvp-v1` cases, task revisions, gold revisions, target
      formats, and completion checklist are fixed for both arms.
- [ ] Every case uses `task-phase12-v1` and `gold-phase12-v1`; cohort agreement
      on a different revision is not accepted.
- [ ] Gold answers are hidden until each timed run stops.
- [ ] Each participant completed one fixed, unscored practice task per arm.
- [ ] Each arm's practice completion timestamp precedes that participant's
      earliest timed run.
- [ ] One `practice_revision` fixes the practice task, training material, and
      assistance contract for the whole cohort.
- [ ] Arm order is assigned before timed work and is counterbalanced.
- [ ] Timing device and pause/interrupt recording method are ready.

## Per-run procedure

- [ ] Confirm participant ID, case ID, arm, attempt number, task revision, and
      gold-answer revision.
- [ ] Start timing only when source, target format, and checklist are available.
- [ ] Record every pause/interrupt in whole seconds.
- [ ] Do not expose the gold answer before the run stops.
- [ ] Set `gold_answer_hidden_until_ended_at` to `true` only after confirming
      that the gold answer stayed hidden through `ended_at`.
- [ ] Stop timing only at approved artifact plus completed checklist, or at an
      explicit blocked outcome.
- [ ] Seal the stopped artifact without exposing the gold answer to the
      participant.
- [ ] An independent assessor compares sealed artifacts with the gold outside
      the participant's view and does not disclose the gold or comparison
      result to that participant.
- [ ] Record the independent-assessor and non-disclosure attestations,
      high-risk expected targets, high-risk misses, and over-detections.
- [ ] For a blocked run, select a controlled blocker code.
- [ ] For an excluded attempt, retain the attempt and select a controlled
      exclusion reason; otherwise record no exclusion reason.
- [ ] Check that the record contains no direct identifier or free-text
      participant note.

## Per-run record template

| Field | Value |
| --- | --- |
| `run_id` | `RUN-...` |
| `participant_id` | `P...` |
| participant `participation_status` | `completed` / `withdrawn` |
| participant `manual_practice_completed_at` | UTC RFC 3339 timestamp |
| participant `veridoc_practice_completed_at` | UTC RFC 3339 timestamp |
| `case_id` | one declared Phase 12 case |
| `arm` | `manual` / `veridoc` |
| `attempt_number` | positive integer |
| `task_revision` | `task-phase12-v1` |
| `gold_answer_revision` | `gold-phase12-v1` |
| `gold_answer_hidden_until_ended_at` | `true` attestation |
| `gold_answer_compared_by_role` | `independent_assessor` |
| `gold_answer_comparison_withheld_from_participant` | `true` attestation |
| `started_at` | UTC RFC 3339 timestamp |
| `ended_at` | UTC RFC 3339 timestamp |
| `excluded_pause_seconds` | non-negative whole seconds |
| `outcome` | `approved` / `blocked` |
| `checklist_complete` | `true` / `false` |
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
      unstarted future groups are not fabricated.
- [ ] All retries and exclusions remain in the evidence.
- [ ] Both arm orders are represented among completed participants and their
      counts differ by at most one.
- [ ] Paired arms use identical task and gold-answer revisions.
- [ ] Every retained attempt for a case uses the same cohort-wide task and
      gold-answer revisions.
- [ ] Attempt numbers are contiguous from 1 and no participant run intervals
      overlap.
- [ ] Attempt timestamps advance in attempt-number order within each
      participant/case/arm.
- [ ] Expected high-risk counts match the pinned manifest.
- [ ] Correction time is recomputed from timestamps minus recorded pauses.
- [ ] Every completed-participant/case reduction and the paired cohort median
      are reported.
- [ ] Per-arm misses, over-detections, completions, blockers, retries, and
      exclusions are reported.
- [ ] Overall totals report misses, over-detections, approved completions,
      blockers, retries, and exclusions.
- [ ] Approved completions include excluded attempts whose outcome is
      `approved` and whose checklist is complete.
- [ ] The 30%+ claim is made only for a valid completed record whose paired
      median is at least 30% and whose VeriDoc arm introduced zero high-risk
      misses.
- [ ] The final JSON passes:

```bash
python3 scripts/ci/validate_mvp_human_review_evidence.py RECORD.json
```
