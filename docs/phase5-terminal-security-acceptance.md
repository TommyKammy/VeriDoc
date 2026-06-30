# Phase5 Terminal Security Acceptance Report

This report records the P5-09 terminal security acceptance check for the
Phase5 desktop thin-client boundary. It is based on repository evidence and
local verification only; it is not a substitute for formal QA approval,
external penetration testing, production IdP operation, MDM rollout, or Windows
code-signing operation.

## Scope

The reviewed boundary is the Phase5 desktop client plus the local PoC API
interfaces it delegates to:

- `apps/desktop/README.md`
- `apps/desktop/api_client.py`
- `services/api/poc_web.py`
- `services/api/job_queue.py`
- `docs/temp-file-management.md`
- `adr/ADR-003-windows-desktop-technology.md`
- `adr/ADR-004-desktop-distribution-update.md`

## Evidence Summary

LLM/model runtime is not bundled with the desktop client. ADR-003 and
`apps/desktop/README.md` define the desktop as a Tauri v2 thin client and state
that LLM/model runtime is not bundled. Document conversion logic is not embedded in the desktop app; API delegation remains the enforcement boundary for
conversion, OCR, extraction, inference, and result generation.

Desktop API credentials are not part of serializable endpoint settings.
`DesktopApiClientConfig` stores only endpoint configuration, while
`ApiCredentialStore` reads a bearer token from a trusted runtime credential
source. Missing tokens, placeholder values, embedded URL credentials, and
non-local API endpoints fail closed before dispatch.

Desktop temporary material is operation-scoped.
`DesktopTemporaryFileManager` creates private staging files under the configured
desktop temp root, removes owned staging files on success, failure, or cancel,
and raises/logs cleanup failures. A user-selected final save location is an
explicit artifact and is not removed as a temporary file.

Desktop result save behavior is bounded to the selected destination directory.
The client authenticates the result download API call, sanitizes
server-provided filenames, avoids Windows reserved names, handles collisions,
and removes partial result files after failed writes.

## Desktop Audit Log Verification

The server records primary desktop operations in the server-side audit log at
the API enforcement boundary:

- Desktop upload: successful `POST /api/jobs` requests with uploaded source
  content append a `desktop.job_operation` event with action `desktop_upload`.
- Desktop result download/save: the desktop client first submits
  `POST /api/job-events` with action `desktop_result_download`; the server
  derives the accepted audit event from the stored job result before the client
  fetches `GET /api/jobs/<job-id>/result` bytes.
- Review edit/approval and retry operations continue to use the existing
  validated server-side audit event flow.

These events are server-derived from validated job/source/result state and the
authenticated context. The desktop client does not supply trusted audit payloads
for upload or result save events, and ordinary browser result fetches are not
classified as desktop saves.

The focused regression test is:

```bash
python3 -m pytest tests/test_poc_web_api.py::test_poc_http_api_records_desktop_upload_and_download_audit_events -q
```

## Residual Risks And Operating Assumptions

- Credential store: production desktop shells must wire `ApiCredentialStore` to
  the operating-system credential store or another trusted runtime secret
  source. Placeholder credentials, TODO values, and sample tokens are not
  accepted as valid credentials.
- Local endpoint trust: the checked-in client accepts only loopback/localhost
  API endpoints. Production deployments still need an operator-approved local
  API launch, binding, and lifecycle policy.
- Temporary files: desktop staging files are removed by
  `DesktopTemporaryFileManager`, but host crash, power loss, or external file
  locks can leave residual files. Operations must surface cleanup failures and
  retain the documented cleanup procedure for `<desktop-temp-root>`.
- Save destination: the final result path is selected by the user and is
  intentionally retained. Operators remain responsible for choosing a governed
  destination and applying workstation storage policy to saved artifacts.
- Distribution: ADR-004 records installer, updater, signing, and managed
  endpoint gates. Production package signing and MDM/IdP operation remain open
  release gates.

## Acceptance Result

Accepted for Phase5 PoC terminal security scope with the residual risks above.
The acceptance is limited to the current thin-client boundary: no LLM/model
runtime or conversion engine is bundled in the desktop source, desktop
operations delegate to the local API, desktop-owned temporary files are cleaned
up by the desktop manager, final saves are explicit user-selected artifacts,
and server-side audit logging records the main desktop upload and result
download/save operations.

## Verification

Run the local acceptance checks with:

```bash
python3 -m pytest tests -q
python3 scripts/ci/repo_hygiene.py
```

Focused checks for this report:

```bash
python3 -m pytest tests/test_phase5_terminal_security_acceptance.py -q
python3 -m pytest tests/test_poc_web_api.py::test_poc_http_api_records_desktop_upload_and_download_audit_events -q
```
