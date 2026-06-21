# OSS License Inventory

Reviewed: 2026-06-21

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
| python-docx | DOCX read/write experiments | MIT | Public package and source license available | Phase1-allowed for PoC evaluation with pinned versions |
| openpyxl | XLSX read/write experiments | MIT | Public package metadata and docs available | Phase1-allowed for PoC evaluation with pinned versions |
| pandas | Tabular analysis for evaluation reports only | BSD-3-Clause | Public package metadata available | Phase1-allowed only for evaluation/reporting helpers, not document authority |
| PyMuPDF | High-fidelity PDF parsing/rendering candidate | AGPL-3.0 or commercial Artifex license | Actively maintained by Artifex | evaluation-only unless AGPL obligations are accepted or a commercial license is approved |
| pdf2docx | PDF-to-DOCX conversion candidate | MIT | Upstream repository says it is no longer actively maintained by Artifex | evaluation-only; do not make it a Phase1 core dependency |

## Phase1 Provisional Decision

Phase1 may use permissive-license libraries for public-fixture-only PoC work:
`pypdf`, `pdfminer.six`, `pdfplumber`, `python-docx`, `openpyxl`, and evaluation
helpers such as `pandas`. These candidates must still be pinned before use,
covered by focused tests, and rechecked before any Phase2/MVP adoption decision.

`PyMuPDF` is not Phase1-allowed as a default dependency because its open-source
license path is AGPL-3.0. It may be used only in isolated evaluation notes or
throwaway local experiments until the project explicitly accepts AGPL obligations
or records an approved commercial-license path.

`pdf2docx` is not Phase1-allowed as a core dependency because the upstream
repository states that it is no longer actively maintained. It may be used only
for evaluation-only comparison against public synthetic fixtures, with no
production-readiness or GMP-readiness claim.

## Explicit Risk Notes

- PyMuPDF: PyPI describes the package as dual licensed under GNU AGPL v3 or an
  Artifex commercial license. Fail closed: do not infer commercial suitability
  from package availability, examples, or nearby project naming.
- pdf2docx: the upstream repository says pdf2docx is no longer actively
  maintained by Artifex. Fail closed: treat successful conversion in a fixture
  experiment as a capability signal only, not a maintenance or support signal.
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
- pdf2docx upstream repository status: https://github.com/ArtifexSoftware/pdf2docx
- pypdf license FAQ: https://pypdf.readthedocs.io/en/stable/meta/faq.html
- pdfminer.six PyPI project metadata: https://pypi.org/project/pdfminer.six/
- pdfplumber PyPI project metadata: https://pypi.org/project/pdfplumber/
- python-docx upstream license: https://github.com/python-openxml/python-docx/blob/master/LICENSE
- openpyxl PyPI project metadata: https://pypi.org/project/openpyxl/
- pandas PyPI project metadata: https://pypi.org/project/pandas/
