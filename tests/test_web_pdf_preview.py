from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path


class _ElementIdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name == "id" and value is not None:
                self.ids.add(value)


def _web_html() -> str:
    return Path("apps/web/index.html").read_text(encoding="utf-8")


def test_pdf_preview_surface_and_bbox_controls_are_present() -> None:
    parser = _ElementIdParser()
    parser.feed(_web_html())

    assert {
        "pdf-preview-panel",
        "preview-page-select",
        "pdf-page-surface",
        "pdf-page-canvas",
        "pdf-source-frame",
        "bbox-layer",
        "bbox-invalid",
    }.issubset(parser.ids)


def test_bbox_overlay_guard_rejects_missing_or_invalid_source_coordinates() -> None:
    html = _web_html()

    assert "function validBbox(bbox, page)" in html
    assert 'bbox.origin !== "top-left"' in html
    assert "bbox.unit !== page.unit" in html
    assert "bbox.width > 0" in html
    assert "bbox.x + bbox.width <= page.width" in html
    assert "Skipped invalid bbox" in html


def test_review_item_can_jump_to_preview_bbox() -> None:
    html = _web_html()

    assert "async function jumpToReviewItem(item)" in html
    assert "Jump to bbox" in html
    assert "await renderSourcePreview(state.latestResult)" in html
    assert "state.previewPage = item.source_page" in html


def test_template_state_clear_resets_credential_bound_form_fields() -> None:
    html = _web_html()

    clear_template_state = re.search(
        r"function clearTemplateState\(\) \{(?P<body>.*?)\n      \}",
        html,
        flags=re.S,
    )
    assert clear_template_state is not None
    body = clear_template_state.group("body")
    for field_name in [
        "templateId",
        "templateName",
        "templateCategory",
        "templateDocumentType",
        "templateAnchors",
        "templateFields",
        "templateTables",
        "templateRiskRank",
        "templateValidationRules",
        "templateOutputMapping",
    ]:
        assert f"{field_name}.value = \"\";" in body


def test_review_item_exposes_edit_and_approve_audit_events() -> None:
    html = _web_html()

    assert "function buildReviewAuditEvent(item, action, savedEditText = null)" in html
    assert 'event_type: "conversion_review.action_requested"' in html
    assert "document_id: item.document_id" in html
    assert "block_id: item.block_id" in html
    assert "original_text: item.text" in html
    assert "event.conversion_id = state.latestResult.conversion_id" in html
    assert "source_page: reviewAuditSourcePage(item)" in html
    assert "function reviewAuditSourcePage(item)" in html
    assert "Number.isInteger(item.source_page)" in html
    assert "item.source_page < 1" in html
    assert "function reviewSourcePages()" in html
    assert "reviewSourcePages().has(item.source_page)" in html
    assert "function reviewActionBlockReason(item)" in html
    assert 'state.latestResult.status === "blocked"' in html
    assert "Review actions are disabled for blocked conversions." in html
    assert "result.available_review_actions" in html
    assert 'approve.dataset.reviewActionName = "approve"' in html
    assert 'approve.disabled = !reviewActionAvailable(item, "approve")' in html
    assert 'requestEdit.dataset.reviewActionName = "edit"' in html
    assert 'requestEdit.disabled = !reviewActionAvailable(item, "edit")' in html
    assert "source_bbox: reviewAuditSourceBbox(item)" in html
    assert "function reviewAuditSourceBbox(item)" in html
    assert "if (!reviewAuditSourcePage(item)) return null;" in html
    assert "validBbox(item.source_bbox, item.source_page_geometry)" in html
    assert "jump.disabled = !reviewAuditSourceBbox(item)" in html
    assert 'if (action === "edit")' in html
    assert "savedEditText = await loadLatestSavedReviewEditText(item);" in html
    assert "event.revised_text = revisedText" in html
    assert "function requestReviewAction(item, action)" in html
    assert "try {" in html
    assert "Review item source page is invalid." in html
    assert 'parsedBody && typeof parsedBody === "object" ? parsedBody : {}' in html
    assert "catch (_error)" in html
    assert "Review action failed." in html
    assert 'requestReviewAction(item, "edit")' in html
    assert 'requestReviewAction(item, "approve")' in html
    assert "Review action event queued for audit" in html


def test_approve_review_action_uses_saved_edit_not_unsaved_draft() -> None:
    html = _web_html()

    assert 'if (action === "edit" || action === "approve")' not in html
    assert "async function loadLatestSavedReviewEditText(item)" in html
    assert "function surfaceSavedReviewEditText(item, savedEditText)" in html
    assert "function sameReviewAuditTarget(event, item)" in html
    assert "for (const event of reviewEvents.slice().reverse())" in html
    assert 'if (event.action !== "edit" || !sameReviewAuditTarget(event, item)) continue;' in html
    assert re.search(
        r'\} else if \(action === "approve"\) \{\s+'
        r"if \(savedEditText !== null\) \{\s+"
        r"event\.revised_text = revisedText;\s+"
        r"\}\s+"
        r"\}",
        html,
        flags=re.S,
    )


def test_approve_review_action_refreshes_saved_server_edits() -> None:
    html = _web_html()

    assert "async function refreshReviewAuditEvents(item)" in html
    assert "const query = new URLSearchParams();" in html
    assert 'query.set("document_id", item.document_id);' in html
    assert 'query.set("block_id", item.block_id);' in html
    assert 'query.set("conversion_id", activeConversionId);' not in html
    assert 'const response = await apiFetch(path);' in html
    assert 'apiFetch("/api/review-events");' not in html
    assert "state.reviewAuditEvents = reviewEvents;" in html
    assert "return reviewEvents;" in html
    assert "const reviewEvents = await refreshReviewAuditEvents(item);" in html
    assert "savedEditText = await loadLatestSavedReviewEditText(item);" in html
    assert "async function prepareSavedReviewEditApproval(item)" in html
    assert "surfaceSavedReviewEditText(item, savedEditText);" in html
    assert 'text.dataset.reviewTextFor = item.block_id;' in html
    assert 'edit.value = savedEditText;' in html
    assert 'text.textContent = savedEditText;' in html
    assert "buildReviewAuditEvent(item, action, savedEditText);" in html
    assert (
        "Saved review edit loaded. Review the updated text, then approve again."
        in html
    )
    assert re.search(
        r"async function prepareSavedReviewEditApproval\(item\) \{\s+"
        r"const savedEditText = await loadLatestSavedReviewEditText\(item\);\s+"
        r"const refreshedBlockReason = reviewActionBlockReason\(item\);\s+"
        r"if \(refreshedBlockReason\) \{\s+"
        r"reviewActionStatus\.textContent = refreshedBlockReason;\s+"
        r'reviewActionStatus\.className = "page-status error";\s+'
        r"return \{ stop: true, savedEditText: null \};\s+"
        r"\}\s+"
        r"if \(savedEditText === null\) return \{ stop: false, savedEditText: null \};\s+"
        r"if \(reviewDraftText\(item\) === savedEditText\) \{\s+"
        r"return \{ stop: false, savedEditText \};\s+"
        r"\}\s+"
        r"surfaceSavedReviewEditText\(item, savedEditText\);\s+"
        r"reviewActionStatus\.textContent =\s+"
        r'"Saved review edit loaded\. Review the updated text, then approve again\.";\s+'
        r'reviewActionStatus\.className = "page-status";\s+'
        r"return \{ stop: true, savedEditText \};\s+"
        r"\}\s+"
        r".+?"
        r'if \(action === "approve"\) \{\s+'
        r"const approvalReadiness = await prepareSavedReviewEditApproval\(item\);\s+"
        r"savedEditText = approvalReadiness\.savedEditText;\s+"
        r"if \(approvalReadiness\.stop\) return;\s+"
        r"\}\s+"
        r"const auditEvent = buildReviewAuditEvent\(item, action, savedEditText\);",
        html,
        flags=re.S,
    )


def test_review_actions_clear_and_reject_stale_file_selection() -> None:
    html = _web_html()

    assert 'input.addEventListener("change", () => {' in html
    assert "state.directConversionToken += 1;" in html
    assert "button.disabled = false;" in html
    assert "credentialAbortController: new AbortController()" in html
    assert "state.credentialAbortController.abort();" in html
    assert "state.credentialAbortController = new AbortController();" in html
    assert "function isActiveDirectConversion(conversionToken)" in html
    assert "if (!isActiveDirectConversion(conversionToken)) return;" in html
    assert "const signal = options.signal || state.credentialAbortController.signal;" in html
    assert "return fetch(url, { ...options, headers, signal });" in html
    assert re.search(
        r"\} finally \{\s+"
        r"if \(isActiveCredentialRequest\(requestAuthToken, requestAuthGeneration\)\) \{\s+"
        r"createJob\.disabled = false;\s+"
        r"\}\s+"
        r"\}",
        html,
        flags=re.S,
    )
    assert "clearReviewResult();" in html
    assert re.search(
        r'button\.addEventListener\("click", async \(\) => \{.*?'
        r"button\.disabled = true;\s+clearReviewResult\(\);\s+try \{",
        html,
        flags=re.S,
    )
    assert "function clearReviewResult()" in html
    assert "reviewList.replaceChildren();" in html
    assert "state.reviewAuditEvents = [];" in html
    assert "resultPanel.hidden = true;" in html
    assert "rawPanel.hidden = true;" in html
    assert "!(state.latestResult.review_items || []).includes(item)" in html
    assert "Review result is no longer active." in html
    assert "state.pendingReviewActions.clear();" in html
    assert "const postResponseBlockReason = reviewActionBlockReason(item)" in html
    assert "if (postResponseBlockReason) throw" not in html
    assert (
        "Review action accepted; current review result changed before the response returned."
        in html
    )
    assert re.search(
        r"if \(postResponseBlockReason\) \{\s+reviewActionStatus\.textContent =\s+"
        r'"Review action accepted; current review result changed before the response returned\.";\s+'
        r'reviewActionStatus\.className = "page-status";\s+return;\s+\}\s+'
        r"state\.reviewAuditEvents\.push\(body\.audit_event\);",
        html,
        flags=re.S,
    )


def test_review_actions_are_serialized_while_audit_request_is_pending() -> None:
    html = _web_html()

    assert "pendingReviewActions: new Map()" in html
    assert "function reviewActionKey(item)" in html
    assert "return `${item.document_id}:${item.block_id}`;" in html
    assert "approve.dataset.reviewActionKey = reviewActionKey(item);" in html
    assert "requestEdit.dataset.reviewActionKey = reviewActionKey(item);" in html
    assert "function setReviewActionPending(item, isPending)" in html
    assert "const controls = reviewList.querySelectorAll(" in html
    assert "controls.forEach((control) => {" in html
    assert "state.pendingReviewActions.has(actionKey)" in html
    assert "const pendingOwner = Symbol(actionKey)" in html
    assert "state.pendingReviewActions.set(actionKey, pendingOwner)" in html
    assert "actionStarted = true" in html
    assert "setReviewActionPending(item, true)" in html
    assert "state.pendingReviewActions.get(actionKey) === pendingOwner" in html
    assert "state.pendingReviewActions.delete(actionKey)" in html
    assert "setReviewActionPending(item, false)" in html
    assert "reviewActionKey(item, action)" not in html


def test_review_actions_ignore_stale_failures_after_result_changes() -> None:
    html = _web_html()

    assert re.search(
        r"\} catch \(error\) \{\s+"
        r"if \(actionStarted && reviewActionBlockReason\(item\)\) return;\s+"
        r"reviewActionStatus\.textContent =",
        html,
        flags=re.S,
    )


def test_pdf_preview_uses_canvas_coordinate_space_for_overlays() -> None:
    html = _web_html()

    assert "const PDFJS_MODULE_URL" in html
    assert "async function renderPdfPageToCanvas(pageGeometry, renderToken)" in html
    assert "pdfPageCanvas.width = Math.round(viewport.width * pixelRatio)" in html
    assert "function samePageGeometry(viewport, pageGeometry)" in html
    assert "PDF page could not be rendered; bbox overlays hidden." in html


def test_bbox_overlay_resets_button_sizing() -> None:
    html = _web_html()

    assert ".bbox-overlay" in html
    assert "appearance: none;" in html
    assert "display: block;" in html
    assert "min-height: 0;" in html
    assert "padding: 0;" in html
    assert "font-size: 0;" in html


def test_review_warning_badges_show_codes_levels_and_llm_involvement() -> None:
    html = _web_html()

    assert "function warningBadgeDescriptor(warning)" in html
    assert '"blocks[0].low confidence; block marked requires_review"' in html
    assert 'code: "W002"' in html
    assert 'severity: "yellow"' in html
    assert 'code: "W000"' in html
    assert "function severityClass(severity)" in html
    assert 'return "gray";' in html
    assert "function renderWarningBadges(item)" in html
    assert 'badge.className = `warning-badge ${severityClass(descriptor.severity)}`;' in html
    assert "function reviewItemForDetail(item)" in html
    assert "warning_badges: (item.warnings || []).map(warningBadgeDescriptor)" in html
    assert "review_items: result.review_items.map(reviewItemForDetail)" in html
    assert "function llmInvolvementBadge(item)" in html
    assert "item.llm_involved === true" in html
    assert 'badge.className = "llm-badge";' in html
    assert "wrapper.append(title, text, badges, edit, actions);" in html
