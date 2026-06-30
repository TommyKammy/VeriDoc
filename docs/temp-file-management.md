# Temporary File Management

`services/api/temp_file_store.py` defines the MVP temporary artifact boundary for API
uploads and conversion results that must outlive a single in-memory request.

`apps/desktop/api_client.py` defines the desktop thin-client temporary file
boundary. Desktop staging files are short-lived local files owned by
`DesktopTemporaryFileManager`; user-selected result save paths are explicit
artifacts and are not part of automatic temporary cleanup.

## Storage Location

Callers provide an explicit storage root, such as `<veridoc-temp-root>`, when
constructing `TemporaryFileStore`. Artifacts are stored below a data-category
subdirectory:

- `upload`: uploaded source material awaiting conversion.
- `result`: generated conversion output awaiting download.

The store rejects malformed artifact identifiers and verifies metadata paths stay
inside the configured storage root before reading artifact bytes.

Desktop callers provide a configured desktop temp root to
`DesktopTemporaryFileManager`. The manager writes owned staging files below that
root's `work/` subdirectory and rejects cleanup paths that escape the configured
root. Documentation and tests use placeholders such as `<desktop-temp-root>`
instead of workstation-local absolute paths.

## Retention And Deletion

Every saved artifact records:

- `created_at`
- `expires_at`
- `category`
- `original_filename`
- SHA-256 of the plaintext payload
- encryption metadata

`delete(artifact_id)` is safe for already-missing artifacts and returns whether
anything was removed. `cleanup_expired()` removes artifacts whose `expires_at`
has passed and leaves unexpired artifacts intact.

Desktop cleanup is operation-scoped rather than time-window based. The manager
removes owned staging files on normal completion, on workflow failure, and when
the workflow calls `cancel()`. Paths registered with
`register_explicit_artifact()` represent user-selected final outputs and are
left in place.

Desktop cleanup failures are detectable: failed removals are logged through
`apps.desktop.api_client` and `DesktopTemporaryCleanupError` is raised when
cleanup itself is the primary operation.

## Encryption Boundary

The MVP implementation requires a configured key of at least 32 bytes at
construction time and writes encrypted bytes to disk with per-artifact nonces.
Metadata records the `hmac-sha256-stream` boundary and the key source as
`configured`; callers must wire the key from a trusted runtime secret source.
Placeholder, too-short, or empty keys are rejected.

Reads verify metadata HMAC before trusting retention fields, require the expected
nonce length, and then verify ciphertext HMAC and plaintext SHA-256 before
returning bytes. If metadata is malformed, the file escapes the configured root,
or integrity checks fail, the read fails closed.
