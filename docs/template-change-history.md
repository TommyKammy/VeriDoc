# Template Change History Audit Note

Template registrations are treated as controlled GMP records. Every
`TemplateStore.register_template` mutation records a version-bound change event
before the in-memory template record is updated. The event is stored both in the
template-level `change_history` projection and inside the created version's
`change_history`, so consumers can trace `template_id` and `version` back to the
exact reason, actor, approval state, and timestamp for that version.

The mutation boundary fails closed:

- `change_reason` is required and must be non-empty.
- `actor.principal_id` and `actor.role` are required and must be non-empty.
- When local auth is enabled, HTTP template mutations derive the recorded actor
  from the authenticated token context instead of trusting request-supplied
  identity fields.
- Missing `approved_by` is not inferred as approval; it is recorded explicitly
  as `{"status": "unapproved", "approved_by": None}`.
- Provided approvals require `approved_by.principal_id` and `approved_by.role`.
- Invalid audit context is rejected before any new version or history event is
  appended, preventing partial durable state.

Version events use an explicit action:

- `created` for the first version of a template.
- `versioned` for ordinary updates and version increments.
- `disabled` when an update changes the authoritative status to `inactive`.
- `enabled` when an update explicitly changes an inactive template back to
  `active`.

For the MVP in-memory store, this preserves traceability at the same boundary
that creates template versions. A future persistent store should keep the same
all-or-nothing transaction shape: validate audit context, create the version,
append its change-history event, and update the current-template projection in
one committed write.
