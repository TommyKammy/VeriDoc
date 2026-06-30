from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = REPO_ROOT / "docs" / "phase5-terminal-security-acceptance.md"


def test_phase5_terminal_security_acceptance_report_records_required_evidence() -> None:
    assert REPORT_PATH.is_file(), (
        "missing Phase5 terminal security acceptance report: "
        f"{REPORT_PATH.relative_to(REPO_ROOT)}"
    )

    report = REPORT_PATH.read_text(encoding="utf-8")

    for required_heading in (
        "# Phase5 Terminal Security Acceptance Report",
        "## Scope",
        "## Evidence Summary",
        "## Desktop Audit Log Verification",
        "## Residual Risks And Operating Assumptions",
        "## Acceptance Result",
        "## Verification",
    ):
        assert required_heading in report

    for required_text in (
        "LLM/model runtime is not bundled",
        "Document conversion logic is not embedded in the desktop app",
        "API delegation remains the enforcement boundary",
        "Desktop upload",
        "Desktop result download/save",
        "server-side audit log",
        "credential store",
        "DesktopTemporaryFileManager",
        "user-selected final save location",
        "python3 -m pytest tests -q",
        "python3 scripts/ci/repo_hygiene.py",
    ):
        assert required_text in report

    forbidden_fragments = ("/" + "Users" + "/", "C:" + "\\Users" + "\\")
    for fragment in forbidden_fragments:
        assert fragment not in report
