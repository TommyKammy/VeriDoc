# Temporary File Management

`services/api/temp_file_store.py` defines the MVP temporary artifact boundary for API
uploads and conversion results that must outlive a single in-memory request.

## Storage Location

Callers provide an explicit storage root, such as `<veridoc-temp-root>`, when
constructing `TemporaryFileStore`. Artifacts are stored below a data-category
subdirectory:

- `upload`: uploaded source material awaiting conversion.
- `result`: generated conversion output awaiting download.

The store rejects malformed artifact identifiers and verifies metadata paths stay
inside the configured storage root before reading artifact bytes.

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
