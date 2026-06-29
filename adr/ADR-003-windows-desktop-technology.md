# ADR-003: Windows Desktop Technology Selection

## Context

Phase5 needs a Windows desktop thin client for VeriDoc. The desktop app is a
local operator surface for upload, progress, result review, and save-location
workflows; it is not a new conversion engine.

The decision must let P5-02 through P5-09 build incrementally while preserving
the existing local API boundary. The desktop client must not silently bundle
LLM/model runtime, document conversion logic, or GMP/CSV production claims.

## Candidate Comparison

| Candidate | Web UI asset reuse | Windows integration | Distribution and maintenance | CI ease | OSS and license posture | Phase5 decision |
| --- | --- | --- | --- | --- | --- | --- |
| Tauri v2 | Strong. Tauri can use an existing HTML/CSS/JavaScript frontend, so Phase5 can reuse or adapt the current web UI patterns instead of rebuilding the interface in XAML. | Good for a thin client. Windows uses Microsoft Edge WebView2, with Rust commands available only for local shell integration and window/file-dialog boundaries that are explicitly allowed. | Good fit for a small desktop wrapper. Tauri uses the OS web renderer and supports Windows installer generation later, while this issue only records the skeleton boundary. | Moderate. Cross-platform CI can run documentation, lint, and frontend checks first; full Windows packaging remains a later Windows-runner concern. | Tauri project code is permissive-license oriented, but each plugin and Rust/frontend dependency must be reviewed before it is accepted into Phase5. | Selected: Tauri v2. |
| .NET WPF | Weak for reuse. WPF is XAML/code-behind native UI, so the current web UI would need to be reimplemented or hosted indirectly. | Strong classic Windows desktop integration and mature file/dialog APIs. | Mature on Windows, but the project would add a separate .NET UI stack and Visual Studio-centric maintenance path. | Moderate to weak for this repo's current shape because meaningful UI verification would require a Windows/.NET lane not otherwise needed by the web/API code. | WPF itself is open source on .NET, but the app would still require NuGet dependency review and a new native UI dependency policy. | WPF is not selected because it optimizes native Windows UI at the cost of web UI reuse and extra stack ownership. |
| .NET WinUI | Weak for reuse. WinUI is the modern native Windows UI path, but it still requires a XAML/.NET or C++ UI implementation rather than direct reuse of the existing web assets. | Strongest modern Windows integration through Windows App SDK and WinUI 3 for Windows 10/11-era UX. | Good for a native Windows-first product, but heavier than needed for a thin client whose core work remains in the local API. | Moderate to weak until a dedicated Windows runner and packaging lane are introduced. | Windows App SDK is Microsoft-maintained with open-source components, but Phase5 would still need NuGet/package provenance review. | WinUI is not selected because the stronger native UX posture does not outweigh the reuse and maintenance costs for this thin client. |

## Decision

Selected: Tauri v2.

Tauri v2 is the Phase5 desktop technology because it best matches VeriDoc's
current architecture: reuse the existing web UI direction, keep the desktop app
small, and delegate conversion and inference to the local API instead of moving
domain processing into a desktop binary.

The selected desktop surface lives under `apps/desktop`. The first committed
artifact is a documented app boundary rather than a generated Tauri project,
because follow-up issues must still decide the exact package manager,
frontend-sharing layout, test harness, and Windows runner. P5-02 may create the
actual Tauri scaffold inside this boundary without revisiting the technology
choice.

## Non-Selected Options

WPF is not selected for Phase5 because it would require reimplementing the UI in
XAML and would split the product surface from the existing web UI. It remains a
valid fallback only if Tauri cannot satisfy required local file/window
integration after a focused spike.

WinUI is not selected for Phase5 because it is the better fit for a deeply
native Windows product, while VeriDoc needs a thin local client around an
already-defined API. It remains a valid future option if Windows-native UX,
Store packaging, or deep shell integration becomes more important than web UI
reuse.

## Thin Client Boundary

- LLM/model runtime is not bundled in the desktop app.
- Document conversion, OCR, extraction, and inference stay behind the local API.
- The desktop app delegates conversion and inference to the local API over an
  explicit configured endpoint.
- Desktop-side logic may cover window lifecycle, file picker interaction,
  upload request assembly, progress display, result download, and local
  save-location selection.
- Desktop-side logic must fail closed when the API endpoint, auth context,
  provenance signal, or selected file boundary is missing or malformed.
- The desktop app must not infer tenant, repository, account, issue, or
  validation scope from path shape, filename, or comments.
- Packaging, Windows installer signing, and production CSV/GMP validation
  claims remain outside this ADR.

## Follow-Up Implementation Plan

`apps/desktop/README.md` defines the handoff for P5-02 through P5-09. The
expected sequence is:

| Follow-up | Boundary |
| --- | --- |
| P5-02 | Create the minimal Tauri v2 scaffold under `apps/desktop` and wire a static placeholder shell. |
| P5-03 | Define API endpoint configuration without hard-coded workstation paths or credentials. |
| P5-04 | Add upload request assembly against the local API boundary. |
| P5-05 | Add progress and failure display based on API status, not guessed local state. |
| P5-06 | Add result retrieval and save-location selection. |
| P5-07 | Add focused desktop test and lint commands suitable for local CI. |
| P5-08 | Add Windows packaging notes without requiring installer production in local CI. |
| P5-09 | Reconcile documentation, operator workflow, and remaining release gates. |

## Verification

- `python3 -m unittest tests.test_desktop_technology_decision`
- `python3 scripts/ci/repo_hygiene.py`

## Sources

- Tauri v2 documentation: https://v2.tauri.app/
- Tauri v2 prerequisites: https://v2.tauri.app/start/prerequisites/
- Tauri WebView versions: https://v2.tauri.app/reference/webview-versions/
- WPF documentation: https://learn.microsoft.com/en-us/dotnet/desktop/wpf/
- WPF overview: https://learn.microsoft.com/en-us/dotnet/desktop/wpf/overview/
- Windows App SDK documentation: https://learn.microsoft.com/en-us/windows/apps/windows-app-sdk/
- WinUI 3 documentation: https://learn.microsoft.com/en-us/windows/apps/winui/winui3/
