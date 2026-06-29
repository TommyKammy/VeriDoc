# GMP-04 Electronic Records and Electronic Signatures Design Note

This note records VeriDoc's Phase4 position for electronic records and
electronic signatures. It is a design boundary for the product backlog and
makes no legal or regulatory compliance conclusion.

## VeriDoc Responsibility Boundary

VeriDoc supports reviewable electronic record preparation for converted PDF,
Word, and Excel content. Within the current product boundary, VeriDoc is
responsible for:

- preserving source provenance next to extracted or reconstructed content;
- producing audit-ready events for review edits, approvals, template changes,
  explicit job-event submissions, and related operator decisions on enforced
  event paths; when local auth is enabled, direct result downloads are
  protected by job-read authorization, while the default unauthenticated PoC
  mode permits downloads without making them authenticated GMP evidence;
  result downloads are not yet recorded as job-action audit events;
- keeping reviewer, approver, and admin review or explicit job-event actions
  tied to the authenticated local actor context when local auth is enabled,
  while default unauthenticated PoC review and job-event submissions store null
  actor and role fields and must not be treated as authenticated GMP evidence;
  unauthenticated template mutations use the caller payload instead of storing
  null actor and role fields;
- failing closed when required provenance or target binding is missing or
  malformed; when local auth is enabled, missing or invalid actor and role
  authentication also fails closed before those fields can be relied on;
- making controlled-record assumptions explicit so QA and GMP SMEs can decide
  whether the resulting workflow is acceptable for a specific site procedure.

VeriDoc records operational review intent. It does not convert that intent into
a legally binding electronic signature by itself.

## Non-Responsibility Boundary

VeriDoc is not the formal electronic signature system of record. The current
scope does not implement:

- electronic signature ceremony, signature manifestation, or signer identity
  proofing;
- legally binding signature validation, certificate validation, or long-term
  signature preservation;
- final GMP, CSV, Part 11, eIDAS, or other regulatory suitability judgment;
- external signature-service integration;
- retention policy enforcement beyond the repository's current prototype audit
  and controlled-record surfaces.

Any workflow that requires a formal signature must stay blocked until an
external validated signature service or an approved site-controlled signing
system owns the signature ceremony and signature record.

## Relationship to GMP-03

Until a standalone GMP-03 design artifact exists in this repository, this note
makes the role boundary and segregation of duties requirements self-contained
and traceable to the current implementation anchors. GMP-04 depends on this
current local PoC boundary:

- The `reviewer` role has `review_events:edit` but not
  `review_events:approve`; reviewer-only approval is rejected by the local PoC
  API.
- The `approver` role has both `review_events:edit` and
  `review_events:approve`, so it can approve review items where the workflow
  allows it.
- `admin` is currently authorized for both review edit and review approve API
  operations, so admin approval events can be accepted by the local PoC API;
  they still remain operational review events and must not be treated as QA or
  formal quality approval by role name alone.
- A witness or QA approval must be represented as an explicit workflow step
  before it can be treated as required evidence.
- Approval workflow validation must stay tied to the explicit review event:
  when comparable prior-edit evidence exists, the endpoint rejects approval
  text that differs from the latest saved revised text. Standalone approvals
  without a saved edit can be accepted from caller-supplied original and
  revised text, including the fallback where missing `revised_text` defaults to
  `original_text`; that path does not compare against the converted document's
  current text or independent prior reviewed text. Same-actor rejection applies
  only to enforced paths where comparable prior-review evidence and
  authenticated actor IDs exist; and `conversion_id`, when present, scopes the
  prior-review search instead of proving universal conversion-version binding.

The design must not infer approval from naming conventions, nearby metadata, or
comments. If a future signature workflow needs a signer, witness, or QA role,
that binding must be explicit in the authoritative workflow record.

The verifiable anchors for this boundary are the `ROLE_PERMISSIONS` matrix,
`_validate_review_event`, and `_validate_review_workflow_event` in
`services/api/poc_web.py`, plus the local PoC API tests in
`tests/test_poc_web_api.py` that reject reviewer-only approval, same-actor
approval on enforced comparable prior-review paths, stale approval text, and
unbound conversion approval. Future GMP-03 changes must update this note and
the focused GMP-04 documentation regression together.

## Audit Log and Electronic Record Posture

The current audit log is an electronic-record support surface. It is intended to
show who requested an action, what record target was affected, when the event
was recorded, and which source/provenance fields were used by the review
workflow.

Audit events should continue to:

- reject missing document, block, or required source-position signals at the
  enforcement boundary; when local auth is enabled, missing or invalid
  authentication must be rejected before actor and role can be relied on, while
  default unauthenticated PoC review and job-event submissions record null
  actor/role fields and are not an authenticated GMP boundary;
- treat `conversion_id` as an optional review-audit scope field in the current
  endpoint: when present it must be a non-empty string and participates in the
  approval-history conflict checks, but unchanged approvals do not require an
  existing edit for the same conversion; legacy events without a conversion ID
  remain constrained by document and block, and by latest edited text checks
  when comparable prior edits exist;
- reject approval attempts without authenticated actor identity before workflow
  validation, while no-auth edit capture remains a local PoC compatibility path;
- preserve caller-supplied source context fields with the review event after
  syntactic validation, without treating direct review-event submissions as
  verified lookup-backed links to a converted document, block, page, or bounding
  box;
- avoid broadening advisory or reconciliation context from sibling records;
- keep append and projection updates all-or-nothing when persistent storage is
  introduced;
- prove rejected or forbidden paths leave no partial durable record behind.

The audit log can support controlled review evidence, but it is not a signature
ledger unless a future validated signing boundary writes and verifies the
signature-specific record.

Because the current review audit endpoint accepts legacy events without
`conversion_id`, VeriDoc must not claim conversion-version binding as universal
electronic-record evidence for all existing audit rows. For regulated acceptance,
QA and GMP SMEs must either require conversion IDs for the controlled workflow or
document why document/block/latest-edit scoping is sufficient for the applicable
site procedure.

## External Signature Integration Option

If a customer workflow requires formal electronic signatures, VeriDoc should
delegate the signing ceremony to an external validated signature service or a
site-controlled quality system. The minimal integration shape is:

- VeriDoc exports or stages the reviewed record package with stable identifiers,
  source provenance, conversion version, template version, and audit event
  references.
- The external system authenticates the signer, performs the signature
  ceremony, records signature meaning, and stores the formal signature record.
- VeriDoc stores only a returned signature reference, signed package identifier,
  verification status, and immutable link back to the reviewed record.
- Missing, expired, unsigned, placeholder, or unverifiable signature references
  fail closed and keep the VeriDoc record in a non-signed or pending state.

The external reference must be bound to the authoritative review record. VeriDoc
must not infer signature completion from filenames, issue titles, operator
comments, forwarded headers, sample credentials, or TODO tokens.

## QA and GMP SME Acceptance Questions

QA and GMP SMEs should confirm these points before any regulated acceptance
claim is made:

- Which VeriDoc records are controlled GMP records and which remain temporary
  review artifacts?
- Which actions require reviewer approval, witness or QA approval, or formal
  electronic signature?
- What retention, backup, restore, export, and audit-review procedures apply to
  the generated records?
- What identity provider or site procedure is authoritative for signer identity?
- What evidence package must be retained when an external validated signature
  service signs a VeriDoc-reviewed record?
- Which failed or rejected flows require documented evidence that no orphan or
  half-restored record survived?

Until these questions are answered, VeriDoc can claim only audit-ready review
support, not final electronic-signature compliance.

## Open Items

- Define the authoritative lifecycle states for review records that are
  pending, approved, rejected, externally signed, or superseded.
- Decide whether signature references belong in the review audit event stream,
  a separate signature-reference table, or both.
- Define export package contents for an externally signed record.
- Add persistence-level tests when the prototype moves from in-memory storage to
  durable storage.
- Confirm the GMP-03 role model covers all required site roles, including QA,
  witness, and system administrator separation.
