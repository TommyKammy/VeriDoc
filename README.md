# VeriDoc

## Template Definition Schema

Phase3 template definitions are modeled by `core/ir/template-definition.schema.json`.
The synthetic example in `core/ir/examples/sample-template-definition.json` covers
template identity/versioning, document type, anchors, fields, tables, risk ranking,
validation rules, and output mapping. Validate the schema shape and semantic
cross-reference contract with:

```bash
python3 scripts/ci/validate_document_ir.py --schema core/ir/template-definition.schema.json --document core/ir/examples/sample-template-definition.json
```

Run the focused regression suite with:

```bash
python3 -m unittest tests.test_template_definition_schema
```
VeriDocはPDF／Word／Excelを対象とした文書変換・再構成システム

## Local PoC API startup and smoke checks

The current PoC API is a developer-only stdlib HTTP server. It is intended for
local startup checks, supervisor reproduction, and browser/API smoke tests; it
is not a production deployment, authentication, or Desktop distribution guide.

From the repository root, first confirm that the script entrypoint can import
the local package tree:

```bash
python3 services/api/poc_web.py --check
```

Expected result: the command exits with status `0` and prints no output. A
non-zero exit usually means the command was not run from the repository root, a
required source file is missing, or the Python environment cannot import one of
the local modules named in the traceback.

Start the local PoC API with:

```bash
python3 services/api/poc_web.py
```

By default it listens on `http://127.0.0.1:8788` and serves the PoC web UI from
`GET /`. Stop it with `Ctrl-C`.

`GET /api/llm-settings` returns the read-only local LLM operating state used by
the web UI: the local-only boundary, configured endpoint and model, prompt and
schema versions, and deterministic fallback state. The endpoint never returns
credentials. External AI endpoints remain blocked before conversion, and the
UI states explicitly that document content must not be sent outside the local
API boundary.

With `VERIDOC_LOCAL_AUTH_TOKENS` unset, use this minimal HTTP smoke test from
another terminal while the server is running:

```bash
python3 - <<'PY'
import base64
import json
from urllib.request import Request, urlopen

base_url = "http://127.0.0.1:8788"

with urlopen(base_url + "/", timeout=5) as response:
    assert response.status == 200
    assert "text/html" in response.headers.get("content-type", "")

payload = {
    "filename": "smoke.json",
    "content_base64": base64.b64encode(
        json.dumps(
            {
                "pages": [
                    {
                        "page_number": 1,
                        "width": 320,
                        "height": 240,
                        "unit": "pt",
                        "fragments": [
                            {"text": "Lot: SAMPLE-001", "confidence": 0.95}
                        ],
                    }
                ]
            }
        ).encode("utf-8")
    ).decode("ascii"),
    "conversion_mode": "auto",
    "use_llm": False,
    "use_ocr": False,
}
request = Request(
    base_url + "/api/convert",
    data=json.dumps(payload).encode("utf-8"),
    headers={"content-type": "application/json"},
    method="POST",
)
with urlopen(request, timeout=5) as response:
    body = json.load(response)

assert body["download"]["filename"] == "smoke.veridoc-result.json"
assert body["artifacts"][0]["id"] == "debug-json"
print("PoC API smoke check passed")
PY
```

Expected result: `GET /` returns HTML, `POST /api/convert` returns JSON, and the
script prints `PoC API smoke check passed`.

### Conversion API responsibilities

`POST /api/jobs` is the application workflow for conversions. Submit the source
bytes and conversion settings there, read status from `GET /api/jobs/{job_id}`,
and follow the returned `job.result.href` when `job.result.available` is true.
That result URL returns the full conversion payload used by the review UI.
Job responses also include the durable `job_id`, a `job.download.href` for the
existing debug JSON download, and sanitized `artifacts[]` references. Completed
jobs persist primary DOCX/XLSX, debug JSON, and audit JSON artifacts with a
job-bound `artifact_id`; each manifest entry exposes an artifact download URL.
The local PoC currently runs a source-bearing job during submission, while
preserving the job-shaped contract for a later asynchronous worker.

`POST /api/convert` is retained as a synchronous development and compatibility
endpoint. It returns the conversion payload directly and is used by the smoke
check above, but the bundled web UI and other application workflows should use
`/api/jobs`.

Supported input formats are PDF (`.pdf`), Word (`.docx`), Excel (`.xlsx`), and
the current parser-output JSON shape used by the smoke test above. Uploads are
limited to 2 MiB before base64 request expansion. PDF parsing depends on the
optional local PDF extractor dependency; if it is missing, PDF conversion
returns `server_dependency_unavailable`.

Supported `conversion_mode` values are:

- `auto`: produce the current debug JSON artifact from the detected input.
- `pdf_to_excel`: accept PDF input and include a downloadable XLSX primary
  artifact.
- `pdf_to_word`: accept PDF input and include a downloadable DOCX primary
  artifact reconstructed from extracted text blocks.
- `word_to_excel`: accept DOCX input and include a downloadable XLSX primary
  artifact.
- `excel_to_word`: accept XLSX input and include a downloadable DOCX primary
  artifact.

The direct conversion request accepts boolean `use_llm` and `use_ocr` settings
and records accepted values in `audit.conversion_settings`. OCR is unsupported
for the MVP: `use_ocr: true` is rejected with HTTP 400 and
`ocr_not_supported`, and the web UI keeps the OCR control disabled. When
`use_llm` is `true`, the response keeps the setting disabled and warns that the
selected setting is not yet implemented in the local PoC API unless a supported
local-only inference profile is configured.

Each conversion result also records an immutable `audit.versions` snapshot with
the selected model identifier, prompt ID/version, and conversion audit,
conversion plan, and Document IR schema versions. The same snapshot is retained
in the downloadable audit JSON so reruns can explain version differences.

The artifact manifest is intentionally honest about current PoC limits. The
debug JSON artifact remains available at `download` and in `artifacts[]` with
id `debug-json`. Renderer-backed DOCX and XLSX primary artifacts are returned in
`artifacts[]` with `metadata.download.field` pointing at the base64 response
field. `pdf_to_word` prioritizes editable heading, paragraph, and table
structure for review; exact PDF layout, fonts, coordinates, columns, footnotes,
and OCR fidelity are not guaranteed.

## PoC review UI information architecture

The Phase6 PoC web UI keeps the direct conversion review flow split into five
stable regions so later P6 issues can add behavior without making raw JSON the
main review surface:

- Upload: accepts the source file and sends `content_base64`; the converted
  `document_ir` feeds preview and source-location surfaces after conversion.
- Conversion settings: sends the selected `conversion_mode` and `use_llm`
  value, while showing the disabled MVP OCR control separately from review
  judgment.
- Review: displays `review_items`, `warnings`, and `document_ir` source page or
  bbox context as the primary operator review surface.
- Artifact downloads: presents primary files and debug exports from
  `artifacts[]`, `download`, and `audit` metadata.
- Detail JSON: shows `document_ir`, `review_items`, `warnings`, `artifacts[]`,
  and `audit` for troubleshooting and audit inspection.

JSON is retained for detail and audit inspection, not as the primary review workflow.
Follow-up P6 work should assert that review decisions are available from the
Review and Artifact downloads regions before relying on Detail JSON.

## Browser upload-to-download acceptance run

Install the browser acceptance dependencies and the headless Chromium runtime:

```bash
python3 -m pip install -r requirements-browser-e2e.txt
python3 -m playwright install chromium
```

Run the focused acceptance test from the repository root:

```bash
python3 -m unittest tests.test_mvp_browser_e2e
```

The same scenario can retain an evidence package at an explicit location:

```bash
VERIDOC_E2E_EVIDENCE_DIR=<evidence-root> python3 scripts/ci/mvp_browser_e2e.py
```

Each run creates one `p12g03-...` correlation directory containing
`evidence.json`, the API result, review audit events, the downloadable audit
artifact, the downloaded primary artifact, screenshots, a Playwright trace, and
`rerun-package.json`. The evidence JSON records every observed browser and
harness HTTP attempt, the allowlisted local origin, DNS/socket observations,
and `external_ai_api_send_count=0`. External endpoint configuration and any
external HTTP, DNS, or socket attempt fail the run closed.
The scenario uses the versioned representative PDF fixture, records an
incompatible-setting error and successful retry, verifies the downloaded
artifact against its server audit metadata, and opens the audit screen. It then
uses the committed high-risk template fixture to drive warning-to-bbox, value
edit, approve, reject, and needs-fix controls with Tab/Enter only. The retained
evidence records focus order and visible-focus checks, structured warning
contract fields, source page/bbox agreement, zero high-risk auto-confirms, and
the unresolved state before approval. A random trusted local approver credential
is generated for the short-lived test server and is never written to the
evidence package.
The pinned PDF.js 4.10.38 runtime is vendored under `apps/web/vendor/pdfjs` and
served by the local PoC server, so preview rendering works in a clean checkout
without npm installation or an external CDN.

The rerun package seals the commit, manifest and fixture hashes, inference
configuration, dependency set, model/prompt/schema versions, and repo-relative
commands. Re-run the exact package with:

```bash
VERIDOC_E2E_EVIDENCE_DIR=<evidence-root> python3 scripts/ci/mvp_browser_e2e.py \
  --rerun-package <rerun-package-path>
```

The rerun validates the package and every pinned input before starting. Its
result must match the packaged decision-relevant projection; run identifiers,
generated identifiers, artifact bytes, timestamps, and processing time are the
only excluded nondeterministic fields.

Set `VERIDOC_E2E_BROWSER_CHANNEL` only when the runner must use an installed
browser channel instead of Playwright Chromium.

## Local PoC API authentication

`services/api/poc_web.py` can enforce local bearer-token authentication when
`VERIDOC_LOCAL_AUTH_TOKENS` is configured. Use comma-separated
`role:principal-id=token` entries:

```bash
VERIDOC_LOCAL_AUTH_TOKENS='viewer:<viewer-id>=<viewer-token>,operator:<operator-id>=<operator-token>,reviewer:<reviewer-id>=<reviewer-token>,approver:<approver-id>=<approver-token>,admin:<admin-id>=<admin-token>,audit_viewer:<audit-id>=<audit-token>' python3 -m services.api.poc_web
```

The code-level source of truth is `ROLE_PERMISSIONS` in
`services/api/poc_web.py`. Its sensitive boundaries are intentionally narrow:

| Role | Job operations | Review | Audit events | Templates |
| --- | --- | --- | --- | --- |
| `viewer` | read | read | job and review read | read |
| `operator` | create, convert, read, retry | none | job read | read |
| `reviewer` | create, convert, read | read, edit | job and review read | read |
| `approver` | create, convert, read | read, edit, approve | job and review read | read |
| `admin` | create, convert, read, retry | read, edit, approve | job and review read | read, manage |
| `audit_viewer` | none | audit read only | job and review read | none |

The Web UI keeps the entered token in memory only for the current browser tab;
it does not write tokens to local or session storage. Choose **Clear token**
before leaving a shared workstation. When the UI reports that a token was
rejected or may have expired, clear it and obtain a replacement from the
operator responsible for `VERIDOC_LOCAL_AUTH_TOKENS`; do not reuse sample or
placeholder values. A permission warning means the token was authenticated but
its assigned role does not allow that operation. Request the narrowest required
role instead of sharing a more privileged token. Token rotation is performed by
updating the trusted environment value, restarting the local API, clearing the
old token in each open UI tab, and entering the replacement.
