# MVP Human-Review Execution Checklist and Record Template

Use this checklist with protocol `p12g-13-human-review-v1`. Record controlled
values in the JSON evidence file; do not put participant names, contact details,
employee IDs, or free-text participant notes in the repository.

## Study setup

- [ ] Approval is complete before any timed work.
- [ ] Consent form version and approval role/timestamp are recorded.
- [ ] At least three relevant document reviewers have repository-safe IDs
      matching `P[0-9]{3,}`.
- [ ] The identity-to-pseudonym mapping is outside the repository.
- [ ] The five `phase12-mvp-v1` cases, task revisions, gold revisions, target
      formats, and completion checklist are fixed for both arms.
- [ ] Gold answers are hidden until each timed run stops.
- [ ] Each participant completed one fixed, unscored practice task per arm.
- [ ] Arm order is assigned before timed work and is counterbalanced.
- [ ] Timing device and pause/interrupt recording method are ready.

## Per-run procedure

- [ ] Confirm participant ID, case ID, arm, attempt number, task revision, and
      gold-answer revision.
- [ ] Start timing only when source, target format, and checklist are available.
- [ ] Record every pause/interrupt in whole seconds.
- [ ] Do not expose the gold answer before the run stops.
- [ ] Stop timing only at approved artifact plus completed checklist, or at an
      explicit blocked outcome.
- [ ] Compare the stopped artifact with the gold answer.
- [ ] Record high-risk expected targets, high-risk misses, and over-detections.
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
| `case_id` | one declared Phase 12 case |
| `arm` | `manual` / `veridoc` |
| `attempt_number` | positive integer |
| `task_revision` | same in both arms |
| `gold_answer_revision` | same in both arms |
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

- [ ] Every participant has one non-excluded record for every case and arm.
- [ ] All retries and exclusions remain in the evidence.
- [ ] Both arm orders are represented and participant counts differ by at most
      one.
- [ ] Paired arms use identical task and gold-answer revisions.
- [ ] Correction time is recomputed from timestamps minus recorded pauses.
- [ ] Every participant/case reduction and the paired cohort median are reported.
- [ ] Per-arm misses, over-detections, completions, blockers, retries, and
      exclusions are reported.
- [ ] The 30%+ claim is made only for a valid completed record whose paired
      median is at least 30% and whose VeriDoc arm introduced zero high-risk
      misses.
- [ ] The final JSON passes:

```bash
python3 scripts/ci/validate_mvp_human_review_evidence.py RECORD.json
```
