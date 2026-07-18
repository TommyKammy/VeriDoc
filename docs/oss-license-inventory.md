# OSS License Inventory

Reviewed: 2026-07-18

This document is the Phase0 SBOM-equivalent inventory for candidate document
conversion and extraction dependencies. It is not a production, GMP, legal, or
commercial-use approval. Real confidential documents and real batch records stay
out of the repository.

## Dependency Inventory

| Package | Candidate use | License | Maintenance signal | Phase1 provisional decision |
| --- | --- | --- | --- | --- |
| pypdf | PDF structure inspection, metadata, split/merge, light text extraction | BSD-3-Clause | Active public package and docs | Phase1-allowed for PoC evaluation with pinned versions |
| pdfminer.six | PDF text and layout extraction foundation | MIT | Community-maintained fork with current package metadata | Phase1-allowed for PoC evaluation with pinned versions |
| pdfplumber | PDF layout/table exploration on top of pdfminer.six | MIT | Current package metadata; depends on pdfminer.six | Phase1-allowed for PoC evaluation with pinned versions |
| camelot-py | Ruled and stream PDF table extraction comparison | MIT | Current package metadata; requires transitive PDF/image processing stack review | Phase1-allowed only for public-fixture evaluation with pinned versions |
| python-docx | DOCX read/write experiments | MIT | Public package and source license available | Phase1-allowed for PoC evaluation with pinned versions |
| openpyxl | XLSX read/write experiments | MIT | Public package metadata and docs available; security docs require `defusedxml` to guard against XML expansion attacks | Phase1-allowed only for fully synthetic/trusted XLSX fixtures unless `defusedxml` is pinned and enforced before parsing |
| pandas | Tabular analysis for evaluation reports only | BSD-3-Clause | Public package metadata available | Phase1-allowed only for evaluation/reporting helpers, not document authority |
| PDF.js 4.10.38 | Browser-side PDF preview rendering | Apache-2.0 | Versioned runtime and upstream license are vendored under `apps/web/vendor/pdfjs`; file hashes are regression-tested | MVP PoC-allowed for repo-owned preview rendering with the pinned vendored files |
| PyMuPDF | High-fidelity PDF parsing/rendering candidate | AGPL-3.0 or commercial Artifex license | Actively maintained by Artifex | evaluation-only unless AGPL obligations are accepted or a commercial license is approved |
| pdf2docx | PDF-to-DOCX conversion candidate | MIT for pdf2docx package pins 0.5.12 and later; requires separate review of transitive PyMuPDF licensing | Upstream repository says it is no longer actively maintained by Artifex; documentation describes extraction through PyMuPDF | evaluation-only; do not make it a Phase1 core dependency |

## Phase1 Provisional Decision

Phase1 may use permissive-license libraries for public-fixture-only PoC work:
`pypdf`, `pdfminer.six`, `pdfplumber`, `camelot-py`, `python-docx`, and
evaluation helpers such as `pandas`. These candidates must still be pinned
before use, covered by focused tests, and rechecked before any Phase2/MVP
adoption decision.

`openpyxl` is Phase1-allowed only for fully synthetic or otherwise trusted XLSX
fixtures. Any Phase1 parsing of external, operator-supplied, or untrusted XLSX
inputs must first pin `defusedxml` and prove the parser path uses that hardened
dependency; otherwise the XLSX path remains blocked.

`PyMuPDF` is not Phase1-allowed as a default dependency because its open-source
license path is AGPL-3.0. It may be used only in isolated evaluation notes or
throwaway local experiments until the project explicitly accepts AGPL obligations
or records an approved commercial-license path. The Phase0 PDF extraction spike
therefore keeps PyMuPDF out of default `requirements.txt`; local evaluation must
opt in with `python3 -m pip install -r requirements-pdf-eval.txt`.

`pdf2docx` is not Phase1-allowed as a core dependency because the upstream
repository states that it is no longer actively maintained. It may be used only
for evaluation-only comparison against public synthetic fixtures, with no
production-readiness or GMP-readiness claim. The MIT license note applies only
to the pdf2docx package itself for evaluation pins at 0.5.12 or later; older
pins must be rechecked instead of inheriting this license conclusion. Any
install or runtime use also brings PyMuPDF into scope and must satisfy the
PyMuPDF AGPL-3.0 or commercial-license decision separately.

## Explicit Risk Notes

- PyMuPDF: PyPI describes the package as dual licensed under GNU AGPL v3 or an
  Artifex commercial license. Fail closed: do not infer commercial suitability
  from package availability, examples, or nearby project naming.
- pdf2docx: the upstream repository says pdf2docx is no longer actively
  maintained by Artifex. Fail closed: treat successful conversion in a fixture
  experiment as a capability signal only, not a maintenance or support signal.
  Do not treat pdf2docx's MIT package license as approval for its transitive
  PyMuPDF dependency.
- openpyxl: the XLSX format is XML-based, and openpyxl docs say to install
  `defusedxml` for protection against quadratic blowup and billion laughs XML
  attacks. Fail closed: untrusted XLSX parsing is not Phase1-approved unless
  that dependency is pinned and verified at the parsing boundary.
- camelot-py: the table-extraction spike may install Camelot only through
  `requirements-pdf-eval.txt` for public synthetic fixtures. Its transitive
  PDF/image processing dependencies still need resolved-lockfile review before
  any Phase2/MVP or confidential-document workflow can depend on it.
- PDF.js: the browser preview uses only the pinned, hash-checked 4.10.38 runtime
  committed under `apps/web/vendor/pdfjs`. Keep the included Apache-2.0 license
  with those files, and do not replace them from an unpinned CDN at runtime.
- Transitive dependencies: every candidate above still needs lockfile-level
  review when it is added to an installable environment. This Phase0 inventory
  records candidate posture, not a complete resolved dependency graph.
- Security posture: no candidate may be used on confidential source documents
  until fixture policy, source provenance, and repository hygiene checks remain
  clean at the actual enforcement boundary.

## Sources Checked

- PyMuPDF PyPI project metadata: https://pypi.org/project/PyMuPDF/
- PyMuPDF licensing page: https://pymupdf.io/pymupdf
- pdf2docx PyPI project metadata: https://pypi.org/project/pdf2docx/
- pdf2docx documentation: https://pdf2docx.readthedocs.io/
- pdf2docx upstream repository status: https://github.com/ArtifexSoftware/pdf2docx
- pypdf license FAQ: https://pypdf.readthedocs.io/en/stable/meta/faq.html
- pdfminer.six PyPI project metadata: https://pypi.org/project/pdfminer.six/
- pdfplumber PyPI project metadata: https://pypi.org/project/pdfplumber/
- Camelot PyPI project metadata: https://pypi.org/project/camelot-py/
- Camelot documentation: https://camelot-py.readthedocs.io/
- python-docx upstream license: https://github.com/python-openxml/python-docx/blob/master/LICENSE
- openpyxl PyPI project metadata: https://pypi.org/project/openpyxl/
- openpyxl security note: https://openpyxl.readthedocs.io/
- defusedxml PyPI project metadata: https://pypi.org/project/defusedxml/
- pandas PyPI project metadata: https://pypi.org/project/pandas/
- Vendored PDF.js 4.10.38 package metadata and
  `apps/web/vendor/pdfjs/LICENSE`
