from __future__ import annotations

import math

from core.validate.automatic import (
    ValidationStatus,
    validate_extracted_item,
    validate_table_consistency,
)


def _evidence() -> dict[str, object]:
    return {
        "source_page": 1,
        "bbox": {
            "x": 72.0,
            "y": 112.0,
            "width": 180.0,
            "height": 18.0,
        },
    }


def _expected_item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "id": "gold-001",
        "label_id": "lot_number",
        "expected_value": "SAMPLE-LOT-001",
        "risk_level": "high",
        "requires_review": True,
        "evidence": _evidence(),
    }
    item.update(overrides)
    return item


def _actual_item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "id": "gold-001",
        "label_id": "lot_number",
        "value": "SAMPLE-LOT-001",
        "auto_confirmed": False,
        "evidence": _evidence(),
    }
    item.update(overrides)
    return item


def test_high_risk_item_cannot_be_auto_confirmed_even_when_value_matches() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(),
        actual=_actual_item(auto_confirmed=True),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_changed_value_fails_closed_and_requires_review() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(),
        actual=_actual_item(value="SAMPLE-LOT-002"),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "value_non_modification" in decision.failed_rules


def test_missing_or_mismatched_provenance_blocks_auto_confirm() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(),
        actual=_actual_item(evidence={"source_page": 1}),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "provenance" in decision.failed_rules


def test_numeric_rule_rejects_non_finite_or_stringified_numbers() -> None:
    expected = _expected_item(expected_value=12.5, risk_level="medium", requires_review=False)

    stringified = validate_extracted_item(expected=expected, actual=_actual_item(value="12.5"))
    non_finite = validate_extracted_item(expected=expected, actual=_actual_item(value=math.nan))

    assert stringified.auto_confirm_allowed is False
    assert non_finite.auto_confirm_allowed is False
    assert "numeric" in stringified.failed_rules
    assert "numeric" in non_finite.failed_rules


def test_numeric_rule_blocks_oversized_integer_without_crashing() -> None:
    expected = _expected_item(expected_value=1, risk_level="medium", requires_review=False)

    decision = validate_extracted_item(
        expected=expected,
        actual=_actual_item(value=10**400),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "numeric" in decision.failed_rules


def test_table_consistency_rejects_missing_cells_and_wrong_text() -> None:
    expected_table = {
        "id": "table-001",
        "cells": [
            {"id": "table-001-r1-c1", "text": "Lot number"},
            {"id": "table-001-r1-c2", "text": "SAMPLE-LOT-001"},
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {"id": "table-001-r1-c1", "text": "Lot number changed"},
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.failed_rules == ("table_consistency",)
    assert decision.requires_review is True


def test_table_cells_enforce_provenance_and_review_gates() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "requires_review": True,
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": {"source_page": 1},
                "auto_confirmed": True,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "provenance" in decision.failed_rules
    assert "risk_gate" in decision.failed_rules
