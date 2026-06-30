# ADR-004: Desktop Distribution and Update

## Context

P5-08 must choose a Windows installer and automatic update direction for the
Tauri v2 desktop thin client selected in ADR-003. The first deliverable is a
minimal, repeatable package path that does not require production signing
material or a completed desktop scaffold in local CI.

The desktop app remains a thin client. Distribution work must not add document
conversion logic, model runtime, tenant inference, or production CSV/GMP claims
to the desktop binary.

## Candidate Comparison

| Candidate | Installer fit | Automatic update fit | Signing posture | Operational tradeoff | Phase5 decision |
| --- | --- | --- | --- | --- | --- |
| Tauri v2 NSIS installer with Tauri updater | Good default for a Tauri desktop app. Produces a familiar Windows installer from the Tauri bundle flow. | Strong fit because Tauri updater uses signed update artifacts and can be wired to an HTTPS update manifest after release infrastructure exists. | Requires Windows code-signing for production trust and separate Tauri updater signing keys. Local CI can dry-run the intended command without secrets. | Keeps installer and update mechanics in the selected Tauri stack while leaving MDM and certificate procurement as release gates. | Selected. |
| MSIX | Good Windows packaging story and Store/enterprise alignment. | Updates are best when paired with Store, App Installer, or enterprise distribution infrastructure that is not yet selected. | Requires a certificate chain and endpoint policy decisions before it is useful for operators. | Better for a mature managed Windows distribution lane than this PoC-stage thin client. | Not selected for Phase5. |
| MSI | Familiar enterprise installer format. | Does not provide an automatic update mechanism by itself; would require an additional updater service or MDM policy. | Requires code-signing and installer authoring choices outside the current Tauri-first scope. | Useful later if enterprise packaging becomes a separate requirement. | Not selected for Phase5. |
| ClickOnce | Simple .NET-oriented install/update flow. | Update model is tied to the .NET deployment stack, not the selected Tauri v2 implementation. | Does not match the selected Rust/WebView packaging path. | Would pull distribution toward an unselected desktop technology. | Not selected. |

## Decision

Selected distribution path: Tauri v2 NSIS installer with Tauri updater.

The minimum build/package command is:

```bash
npm --prefix apps/desktop run tauri -- build --bundles nsis
```

Until the Tauri scaffold and package manager are committed, local CI verifies
the intended package path with:

```bash
python3 scripts/desktop_package_dry_run.py --dry-run
```

The dry-run is intentionally fail-closed: it checks that this ADR and
`apps/desktop/README.md` record the chosen installer, updater, required signing
environment variables, updater plugin setup, updater capability permission,
updater artifact generation, updater public key, runtime update check flow,
separate Windows installer signing, target endpoint class, and unresolved
release gates before reporting the package path as ready to wire into a real
Tauri scaffold.

## Non-Selected Options

MSIX is not selected for Phase5 because it needs a stronger Windows
distribution authority decision, certificate lifecycle, and App Installer or
enterprise deployment path before it can be the source of truth for updates.

MSI is not selected for Phase5 because it is an installer format rather than an
automatic update strategy. It remains a possible enterprise packaging output if
managed endpoint distribution later requires it.

ClickOnce is not selected because it is a .NET deployment model and does not
match the selected Tauri v2 desktop technology.

## Minimum Package Procedure

1. Keep `apps/desktop` as the desktop app root.
2. Add the Tauri v2 scaffold and package manager metadata under `apps/desktop`.
3. Add the `tauri-plugin-updater` dependency and initialize it in `lib.rs` before
   treating any build as auto-update capable.
4. Configure Tauri bundling for an NSIS Windows installer and set
   `bundle.createUpdaterArtifacts` to `true` so the Windows updater signature is
   generated with the installer.
5. Configure Tauri updater metadata only after the update endpoint, signing key
   storage, and `plugins.updater.pubkey` are authoritative.
6. Enable updater command permissions in `src-tauri/capabilities/default.json`
   with `updater:default` before wiring any webview/UI-triggered update check.
7. Add a runtime update flow that calls `check()` and handles download/install
   behavior through an explicit app policy or UI command before claiming
   automatic updates are active.
8. Configure Windows installer code signing separately from updater artifact
   signing, using `bundle.windows.signCommand` or an equivalent trusted signer
   configuration backed by CI secrets.
9. Run `python3 scripts/desktop_package_dry_run.py --dry-run` in local CI until
   the scaffold exists.
10. Replace or supplement the dry-run with
   `npm --prefix apps/desktop run tauri -- build --bundles nsis` on a Windows
   packaging runner once prerequisites are committed.

## Open Release Gates

- Production Windows installer code-signing certificate procurement, storage, and
  `bundle.windows.signCommand` or equivalent signer configuration remain
  unresolved.
- Tauri update signing keys must be generated and exposed only through trusted
  CI secrets, using `TAURI_SIGNING_PRIVATE_KEY` and
  `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`.
- The Tauri updater public key must be configured in `plugins.updater.pubkey`
  from an authoritative release key source before updater-ready packaging is
  allowed.
- `VERIDOC_DESKTOP_UPDATE_ENDPOINT` must point to an HTTPS update manifest
  controlled by the release process; placeholder or localhost values are not
  valid for production updates.
- Runtime update behavior is not finalized. The desktop scaffold must enable
  `updater:default` in `src-tauri/capabilities/default.json` and wire a
  `check()` path plus download/install handling to a startup policy or UI
  command before automatic updates are claimed.
- Rollback policy is not finalized. Release tooling must define whether
  rollback uses a higher-version recovery build, managed endpoint distribution,
  or re-publishing the last known-good updater manifest only with an explicit
  Tauri `version_comparator` policy that allows the intended downgrade.
- Target endpoints are Windows 10 22H2 or later and Windows 11 devices with
  Microsoft Edge WebView2 available. Broader Windows version support is not
  claimed.
- Enterprise MDM, Intune, Store distribution, and managed endpoint distribution
  remain explicit follow-up work.

## Verification

- `python3 -m unittest tests.test_desktop_technology_decision`
- `python3 scripts/desktop_package_dry_run.py --dry-run`
- `python3 scripts/ci/repo_hygiene.py`
