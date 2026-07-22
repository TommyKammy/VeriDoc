# MVP Scope Decisions

This repository-owned record captures the three authoritative Phase 12.5 scope
decisions required by P12G-02. The decisions below were approved by the named
human decision owner against the pinned manifest and product commit. Downstream
work must preserve the rejection conditions and must request a new decision
revision before changing the approved scope.

The acceptance evaluator recomputes the approved manifest contract from its
case, fixture, source-policy, and expectation fields and recomputes the sorted
`ROLE_PERMISSIONS` contract. A mismatch fails the affected `OD-*` item until a
new human-approved decision revision updates these pins.

## Record metadata

- Record schema: `veridoc-mvp-scope-decisions/v1`
- Decision revision: `p12g-02-v1`
- Target product commit: `584ef2db12a6676abb65f75de1ec38145e06b487`
- Target manifest: `datasets/mvp_evaluation_manifest_v1.json`
- Target manifest revision: `phase12-mvp-v1`
- Target manifest Git blob: `13450762d323198b1b6e87315be173c784fc4880`
- Approved manifest contract SHA-256: `18996f997b7f6f9909ae2cd9f98a992713f76e7d95057ca5116f365bc8a88a75`
- Approved ROLE_PERMISSIONS contract SHA-256: `dad052a8f6fe7acd549b2fa974c20e09b702fbcf31917deecf1636db61dfb322`
- Decision owner: `TommyKammy`
- Approved by: `TommyKammy`
- Approval date: `2026-07-22`
- Approval status: `approved`

## OD-TEMPLATES

- Decision: adopt all five cases in manifest revision
  `phase12-mvp-v1` as the representative MVP scope:
  `mvp-word-001`, `mvp-excel-001`, `mvp-text-pdf-001`,
  `mvp-scanned-pdf-001`, and `mvp-record-pdf-001`.
- Rationale: the five public or synthetic fixtures cover Word, Excel, text PDF,
  scanned PDF, and record PDF with one fixed case per required category. Keeping
  all five preserves the existing fail-closed OCR boundary instead of removing
  the currently failing scanned-PDF case from the denominator.
- Rejection conditions: any change to the manifest revision, required category
  set, case ID, fixture binding, source policy, expected status, or confidential
  source policy requires a new decision revision and renewed approval.

## OD-EFFICIENCY-SCOPE

- Baseline task: for the five fixed representative cases, produce the
  expected editable artifact manually from the source, then verify and correct
  it against the same review checklist used for the VeriDoc-assisted arm. Both
  arms are scored against the same gold expectations, which are not shown to a
  participant until that timed task is complete.
- Cohort: at least three designated document reviewers with relevant
  document-review experience, recorded only under pseudonymous participant IDs.
- Training: one fixed, unscored practice task per arm before timed
  measurement begins.
- Timing boundary: start when the participant receives the source,
  target format, and checklist; stop when the participant records an approved
  artifact or an explicit blocked outcome and completes the checklist. Breaks,
  interruptions, retries, and excluded runs remain separately recorded and are
  not silently removed.
- Comparison: a within-participant manual-versus-VeriDoc comparison
  with counterbalanced arm order. Report each participant/case pair and the
  paired cohort median. The 30% target passes only when the paired median review
  time is reduced by at least 30% and every required run is accounted for.
- Safety guard: no high-risk miss may be introduced by the assisted arm.
- Rationale: fixed tasks, gold expectations, training, timing boundaries, and
  paired results make the comparison reproducible without storing participant
  identity or weakening the existing quality and safety gates.
- Rejection conditions: different source tasks or gold expectations between
  arms, exposing gold answers during a timed task, changing or omitting the
  fixed training, fewer than three valid participants, retaining direct
  participant identity, unrecorded exclusions, missing timing boundaries,
  unbalanced ordering, or a high-risk miss invalidate the efficiency claim.

## OD-SEGREGATION

- Decision: adopt the six-role `ROLE_PERMISSIONS` matrix in
  `services/api/poc_web.py` as the mandatory MVP authorization boundary:
  `viewer`, `operator`, `reviewer`, `approver`, `admin`, and `audit_viewer`.
- Mandatory MVP deny paths:
  - unauthenticated requests cannot use protected API operations;
  - `viewer` and `audit_viewer` remain read-only;
  - `operator` cannot edit or approve review decisions;
  - `reviewer` cannot approve review decisions;
  - only `admin` can manage templates;
  - approval requires a preceding review/edit event for the same workflow
    target from a distinct authenticated actor, even when an `approver` role
    can edit and approve;
  - UI visibility never substitutes for server authorization.
- Current implementation gaps, which this decision does not count as completed
  controls:
  - when `VERIDOC_LOCAL_AUTH_TOKENS` is unset, the documented local smoke-test
    mode permits unauthenticated non-approval operations; it is not an accepted
    MVP authorization configuration;
  - `_validate_review_workflow_event()` rejects same-actor approval only when a
    matching prior edit exists, but currently accepts approval with no prior
    review/edit event.
- Required follow-up: P12G-10 must make authentication mandatory for protected
  MVP operations, require the preceding distinct-actor review/edit event, and
  prove both paths with API/UI evidence before `AC-AUTH` can pass.
- Phase 13 carryover: production IdP/SSO integration, operating-system credential
  storage, enterprise role governance, and richer desktop reauthentication and
  rotation workflows. Deferral of these integrations does not relax the
  approved permission matrix, mandatory authentication, preceding review, or
  distinct reviewer/approver identity boundaries.
- Rationale: this fixes the role matrix and required deny paths as the target
  boundary while keeping the known server-enforcement gaps explicit and
  separating them from Phase 13 integration work. `OD-SEGREGATION=pass` records
  that the scope decision exists; it is not evidence that `AC-AUTH` is complete.
- Rejection conditions: permission-matrix changes, shared credentials across
  reviewer and approver duties, client-only enforcement, or any Phase 13
  deferral that weakens an MVP deny path requires a new decision revision and
  renewed approval.

## Approval

`TommyKammy` approved all three decisions together on `2026-07-22`. This record
captures that human decision; the coding agent prepared and committed the
repository representation but did not supply or infer the approval.
