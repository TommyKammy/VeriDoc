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

## Initial Local Checks

The documentation-only boundary introduced by P5-01 is verified with:

```bash
python3 -m unittest tests.test_desktop_technology_decision
python3 scripts/ci/repo_hygiene.py
```
