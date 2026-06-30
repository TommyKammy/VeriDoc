# VeriDoc Desktop App

This directory is the Phase5 boundary for the VeriDoc Windows Desktop app.
ADR-003 selects Tauri v2 for the desktop thin client and records why .NET WPF
and .NET WinUI are not selected for the initial implementation.

## Scope

- Build a Tauri v2 thin client for Windows-first operator workflows.
- Reuse the existing web UI direction where practical.
- Keep API delegation as the enforcement boundary for conversion, OCR,
  extraction, inference, and result generation.
- Keep desktop logic focused on file selection, request submission, progress
  display, result retrieval, and save-location interaction.

## Explicit Non-Scope

- LLM/model runtime is not bundled.
- Document conversion logic is not embedded in the desktop app.
- Windows installer production and signing are not required in this directory
  until a later packaging issue explicitly adds them.
- Production GMP/CSV validation is not claimed by the desktop shell.

## Follow-Up Boundary

P5-02 through P5-09 should treat this directory as the app root and ADR-003 as
the technology decision record. The first implementation issue may add the
Tauri v2 scaffold here, but it should preserve these constraints:

- endpoint configuration uses documented environment variables or checked-in
  sample config placeholders, not workstation-local absolute paths;
- credentials, tokens, and auth context are never represented by sample secrets
  that could be mistaken for trusted values;
- the desktop app fails closed when the local API endpoint or selected-file
  provenance is missing or malformed;
- UI state is derived from API responses and durable job state, not inferred
  from filenames, local path shape, or display text.

## API Authentication Boundary

`apps.desktop.api_client` keeps endpoint configuration separate from API
credentials. `DesktopApiClientConfig` stores only a loopback/localhost API base
URL and timeout; bearer tokens are read through `ApiCredentialStore`, which
should be wired to the OS credential store by the desktop shell. Endpoint
configuration rejects embedded URL credentials and non-local hosts before any
token is read or attached to a request.

The client fails closed before network dispatch when no token is available or
when the configured value is an obvious placeholder such as `<viewer-token>` or
`TODO`. Authenticated requests attach the credential as an `Authorization:
Bearer ...` header and treat `401`/`403` responses as authentication failures.

## API Connection Settings

`DesktopConnectionSettings` is the desktop setting-layer boundary for the API
endpoint URL, timeout, and optional HTTPS-required policy. It converts directly
to `DesktopApiClientConfig`, so later upload, progress, and result-save flows
can reuse the same validated endpoint configuration instead of duplicating URL
normalization.

`check_desktop_api_connection` performs an authenticated `/api/jobs` read
through `DesktopApiClient` and returns a `DesktopConnectionHealthResult` for the
settings screen. Invalid URLs, non-local endpoints, missing or rejected
credentials, HTTP errors, and connection failures are reported as status values
with user-facing messages before the desktop shell proceeds to later API calls.

## Local Temporary File Cleanup

Desktop-owned upload, download, and intermediate staging files must be created
through `DesktopTemporaryFileManager`. The manager stores files under the
configured desktop temp root's `work/` subdirectory and removes those owned
files when the operation exits normally, fails with an exception, or is
cancelled through `cancel()`.

The manager keeps the desktop temp root and shared `work/` directory available
for concurrent operation-scoped managers. On POSIX platforms it applies private
`0700` directory permissions and `0600` staging-file permissions; on Windows it
removes existing ACL access rules and grants the current user full control
before writing staging content. If privacy hardening fails, staging creation
fails closed.

Files written to a user-selected final save location are explicit artifacts, not
temporary files. Register those paths with `register_explicit_artifact()` if they
are handled in the same workflow; cleanup skips explicit artifacts, removes only
manager-owned staging paths under the temp root, and leaves the shared `work/`
directory in place.

If a staging file cannot be removed, cleanup logs an error through
`apps.desktop.api_client` and raises `DesktopTemporaryCleanupError` when cleanup
is the primary operation. When another workflow error is already being raised,
the cleanup failure is still logged so the desktop shell can surface or collect
it without masking the original failure.

## Distribution and Update

ADR-004 selects a Tauri v2 NSIS installer with Tauri updater as the Phase5
distribution and update direction. MSIX, MSI, and ClickOnce are not selected for
the initial desktop thin client.

The real package command is expected to be:

```bash
npm --prefix apps/desktop run tauri -- build --bundles nsis
```

Until the Tauri scaffold and Windows packaging runner are committed, local and
GitHub CI use the dry-run verifier:

```bash
python3 scripts/desktop_package_dry_run.py --dry-run
```

Production packaging must source signing material from trusted CI secrets, not
from checked-in files or placeholder values. The Tauri scaffold cannot replace
the dry-run until CI can prove the updater-ready package gates: add the
`tauri-plugin-updater` dependency, initialize the plugin in `lib.rs`, set
`bundle.createUpdaterArtifacts` to `true`, and wire updater metadata to an
authoritative `plugins.updater.endpoints` endpoint and
`plugins.updater.pubkey`. The scaffold must also enable `updater:default` in
`src-tauri/capabilities/default.json` and expose a runtime updater `check()`
path with download/install handling before automatic updates are claimed.
Building only the NSIS installer does not satisfy the updater gate. Windows
installer code signing is a separate gate from updater artifact signing and
must use a trusted certificate plus `bundle.windows.signCommand` or equivalent
signer configuration. The Windows installer code-signing certificate remains an
unresolved release gate. Other unresolved release gates are
`TAURI_SIGNING_PRIVATE_KEY`, `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`,
`plugins.updater.endpoints` sourced from an HTTPS
`VERIDOC_DESKTOP_UPDATE_ENDPOINT`, the updater public key, runtime update
policy, rollback policy including any required `version_comparator` downgrade
policy, and managed endpoint distribution for Windows 10 22H2 or later and
Windows 11 devices.

## Initial Local Checks

The documentation-only boundary introduced by P5-01 is verified with:

```bash
python3 -m unittest tests.test_desktop_technology_decision
python3 scripts/desktop_package_dry_run.py --dry-run
python3 scripts/ci/repo_hygiene.py
```
