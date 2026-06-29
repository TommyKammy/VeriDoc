# GMP-07 Validation Draft

This document is a draft only and is not an approved formal CSV package. It
turns the Phase4 GMP plan, `15.7_GMP対応受入基準`, and the CSV policy note into a
reviewable validation skeleton for VeriDoc. Production validation remains
QA-led after scope approval, and teams must not infer GMP fitness from this
draft alone. In other words, production validation remains QA-led after scope
approval, and do not infer GMP fitness from this draft alone.

## Scope And CSV Posture

VeriDoc is treated here as a computerized system candidate whose validation
depth depends on intended use. General office-document use remains outside the
CSV target. Use for GMP manufacturing-record PDF review, high-risk extraction,
review decision support, audit evidence, or traceable rendered records requires
risk-based control.

This draft covers:

- Confirmation that high-risk items are never auto-confirmed.
- URS draft for Phase4 GMP control expectations.
- Risk assessment draft for high-risk extraction, review, audit, source
  traceability, change control, and validation evidence.
- Traceability matrix from `15.7_GMP対応受入基準` and GMP-01 through GMP-08.
- A traceability matrix that keeps each acceptance condition linked to a URS,
  risk, issue, and evidence boundary.
- IQ/OQ/PQ-equivalent verification draft for repository evidence and future QA
  validation planning.

This draft does not approve production CSV, define the final regulatory scope,
or complete real environment IQ/OQ/PQ execution. The formal CSV owner and scope
are not yet approved; QA confirmation required before this can become a
controlled validation plan. The phrase formal CSV owner and scope are not yet approved
means the draft must remain non-authoritative until QA records the owner and
scope decision.

## User Requirements Specification Draft

| URS ID | Requirement | Source / Acceptance Anchor | Draft Verification |
| --- | --- | --- | --- |
| URS-01 | High-risk items are never auto-confirmed and always require human review. | `15.7_GMP対応受入基準`; GMP-01 | Unit and evaluation evidence show high-risk auto-confirmed miss count is 0. |
| URS-02 | Audit records for review and approval events are append-only and integrity-protected. | GMP-02 | API or persistence tests prove append-only behavior and tamper detection. |
| URS-03 | Approval authority is separated from execution authority; self-approval remains blocked. | GMP-03 | Authorization tests reject same-user execution and approval paths. |
| URS-04 | Electronic record and signature posture is explicitly bounded; VeriDoc is not silently treated as a full signature system. | GMP-04 | `docs/gmp04-electronic-records-signatures.md` remains linked and QA questions stay open until answered. |
| URS-05 | High-risk extracted values keep source links sufficient for original-page and coordinate review. | GMP-05 | Review UI and IR tests prove source coverage and original-document navigation. |
| URS-06 | Model, prompt, logic, template, renderer, and gate-data changes trigger re-evaluation. | GMP-06 | `docs/change-management-reevaluation.md` defines required rerun gates and PR evidence. |
| URS-07 | Validation planning artifacts identify URS, risk assessment, traceability, and IQ/OQ/PQ-equivalent checks without over-promising formal CSV completion. | GMP-07 | This draft exists and is reviewed by QA / GMP SMEs. |
| URS-08 | GMP acceptance evaluation can be run against representative record PDFs and records pass/fail against the 15.7 criteria. | GMP-08 | Future GMP acceptance record captures pass/fail outcomes, evidence, and deviations. |

## Risk Assessment Draft

| Risk ID | Failure Mode | Impact | Existing / Planned Control | Residual Status |
| --- | --- | --- | --- | --- |
| R-01 | A high-risk item is auto-confirmed instead of sent to review. | Critical GMP data may be accepted without human review. | GMP-01 rules and acceptance criterion that high-risk auto-confirmed miss count is 0. | Must remain blocked in CI and GMP-08 evidence. |
| R-02 | Audit history can be edited or deleted after the fact. | Review and approval evidence becomes unreliable. | GMP-02 append-only and integrity checks. | Requires implementation evidence and QA review. |
| R-03 | The same actor executes and approves a controlled decision. | Segregation-of-duties failure. | GMP-03 authorization boundary. | Must fail closed if role or actor identity is missing. |
| R-04 | Electronic signature obligations are assumed but not implemented. | Regulated acceptance may rely on unsupported signature evidence. | GMP-04 boundary statement and QA questions. | Open until formal signature scope is decided. |
| R-05 | Source traceability is incomplete for high-risk values. | Reviewer cannot verify the original record evidence. | GMP-05 source coverage target and original-jump behavior. | GMP-08 must sample real record PDFs. |
| R-06 | Model, prompt, logic, or template drift bypasses revalidation. | Previously accepted behavior may regress silently. | GMP-06 change-management and re-evaluation gates. | Each controlled change must declare rerun evidence. |
| R-07 | Validation artifacts imply approved CSV before QA scope approval. | Project over-promises regulatory readiness. | This draft labels formal CSV owner and scope as unapproved. | QA confirmation required. |
| R-08 | GMP acceptance evidence is assembled from mixed or partial results. | Acceptance status may not reflect one committed evidence set. | GMP-08 should capture one evidence package with deviations and rerun commands. | Future record must reject partial evidence. |

## Traceability Matrix

| 15.7 Acceptance / Failure Condition | Linked Issue | URS | Risk | Evidence Boundary |
| --- | --- | --- | --- | --- |
| High-risk items are all routed to human review. | GMP-01 | URS-01 | R-01 | Validation and evaluation tests at the high-risk decision boundary. |
| High-risk auto-confirmed miss count is 0. | GMP-01, GMP-08 | URS-01, URS-08 | R-01, R-08 | GMP-08 acceptance record plus automated high-risk gate output. |
| Any lot, numeric value, date, specification, judgment, or operator high-risk item auto-confirmed is a failure. | GMP-01, GMP-08 | URS-01, URS-08 | R-01 | Reject evidence where any high-risk miss is non-zero. |
| Risks from the PDF conversion risk analysis are checked for containment. | GMP-05, GMP-08 | URS-05, URS-08 | R-05, R-08 | Source-linkage and record-PDF review evidence. |
| Audit evidence is presentable and trustworthy. | GMP-02, GMP-04 | URS-02, URS-04 | R-02, R-04 | Append-only audit tests and electronic-record boundary review. |
| Change impact is reevaluated for controlled behavior changes. | GMP-06 | URS-06 | R-06 | Required re-evaluation commands and PR checklist evidence. |
| CSV applicability, regulatory guidance, and formal validation scope are confirmed by QA. | GMP-07 | URS-07 | R-07 | QA confirmation required before controlled CSV approval. |

## IQ/OQ/PQ-Equivalent Verification Draft

| Phase | Draft Objective | Evidence To Collect | Current Status |
| --- | --- | --- | --- |
| IQ-equivalent | Confirm controlled repository artifacts and dependencies needed for validation evidence are present. | `README.md`, `LICENSE`, tracked docs, test files, pinned requirements where applicable, and `python3 scripts/ci/repo_hygiene.py`. | Draft only; repo hygiene is the current minimum check. |
| OQ-equivalent | Confirm controlled functions enforce expected operating boundaries under normal and rejected paths. | Focused tests for high-risk review routing, audit append-only behavior, authorization rejection, source coverage, and change-management rerun gates. | Split across GMP-01 through GMP-06 implementation evidence. |
| PQ-equivalent | Confirm representative GMP record PDFs produce acceptable review evidence in intended operating use. | GMP-08 record-PDF acceptance package with pass/fail criteria, deviations, rerun commands, and QA review notes. | Future execution; not performed by this draft. |

Minimum repository verification for this draft:

```bash
python3 -m unittest tests.test_gmp07_validation_draft
python3 scripts/ci/repo_hygiene.py
```

## Open Items And QA Confirmation

- QA confirmation required: applicable regulations and guidance, including GMP
  ordinance, PIC/S, GAMP, Annex 11, Part 11, and site-specific SOP references.
- QA confirmation required: intended GMP workflow where VeriDoc output is used
  as record review support.
- QA confirmation required: formal CSV owner, approval route, and validation
  schedule.
- QA confirmation required: whether electronic signatures are in scope or
  explicitly handled by an external approved system.
- Open item: define the GMP-08 evidence package format and deviation handling.
- Open item: decide whether LLM nondeterminism requires extra acceptance
  sampling beyond the current stability and review gates.
- Open item: confirm OSS version control, known-defect handling, and security
  evidence expected for Docling, Camelot, OCR, local model, and renderer
  components.
