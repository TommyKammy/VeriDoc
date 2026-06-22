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


def test_item_scope_binding_requires_matching_document_and_block_ids() -> None:
    expected = _expected_item(
        risk_level="medium",
        requires_review=False,
        document_id="doc-001",
        block_id="block-001",
    )

    missing_scope = validate_extracted_item(expected=expected, actual=_actual_item())
    wrong_scope = validate_extracted_item(
        expected=expected,
        actual=_actual_item(document_id="doc-001", block_id="block-002"),
    )

    assert missing_scope.auto_confirm_allowed is False
    assert missing_scope.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "scope_binding" in missing_scope.failed_rules
    assert wrong_scope.auto_confirm_allowed is False
    assert wrong_scope.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "scope_binding" in wrong_scope.failed_rules


def test_item_scope_binding_requires_matching_fixture_id() -> None:
    expected = _expected_item(
        risk_level="medium",
        requires_review=False,
        fixture_id="fixture-001",
        document_id="doc-001",
        block_id="block-001",
    )

    decision = validate_extracted_item(
        expected=expected,
        actual=_actual_item(
            fixture_id="fixture-002",
            document_id="doc-001",
            block_id="block-001",
        ),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "scope_binding" in decision.failed_rules


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


def test_string_value_rejects_numeric_actual_value() -> None:
    expected = _expected_item(
        expected_value="123",
        risk_level="medium",
        requires_review=False,
    )

    decision = validate_extracted_item(expected=expected, actual=_actual_item(value=123))

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "value_non_modification" in decision.failed_rules


def test_actual_review_flag_blocks_item_auto_confirm_without_failures() -> None:
    expected = _expected_item(risk_level="medium", requires_review=False)

    decision = validate_extracted_item(
        expected=expected,
        actual=_actual_item(requires_review=True, auto_confirmed=False),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.REQUIRES_REVIEW
    assert decision.requires_review is True
    assert decision.failed_rules == ()


def test_actual_high_risk_item_blocks_auto_confirm() -> None:
    expected = _expected_item(risk_level="medium", requires_review=False)

    decision = validate_extracted_item(
        expected=expected,
        actual=_actual_item(risk_level="high", auto_confirmed=True),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_malformed_item_review_flag_blocks_auto_confirm() -> None:
    expected = _expected_item(risk_level="medium", requires_review="true")

    decision = validate_extracted_item(expected=expected, actual=_actual_item())
    actual_malformed = validate_extracted_item(
        expected=_expected_item(risk_level="medium", requires_review=False),
        actual=_actual_item(requires_review="true"),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules
    assert actual_malformed.auto_confirm_allowed is False
    assert actual_malformed.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "risk_gate" in actual_malformed.failed_rules


def test_null_item_review_flag_blocks_auto_confirm() -> None:
    expected_null = validate_extracted_item(
        expected=_expected_item(risk_level="medium", requires_review=None),
        actual=_actual_item(),
    )
    actual_null = validate_extracted_item(
        expected=_expected_item(risk_level="medium", requires_review=False),
        actual=_actual_item(requires_review=None),
    )

    assert expected_null.auto_confirm_allowed is False
    assert expected_null.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert expected_null.requires_review is True
    assert "risk_gate" in expected_null.failed_rules
    assert actual_null.auto_confirm_allowed is False
    assert actual_null.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "risk_gate" in actual_null.failed_rules


def test_missing_expected_item_review_flag_blocks_auto_confirm() -> None:
    expected = _expected_item(risk_level="medium")
    del expected["requires_review"]

    decision = validate_extracted_item(expected=expected, actual=_actual_item())

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_blank_expected_item_value_blocks_auto_confirm() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(
            expected_value=" ",
            risk_level="medium",
            requires_review=False,
        ),
        actual=_actual_item(value=""),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "value_non_modification" in decision.failed_rules


def test_matching_negative_source_coordinates_block_auto_confirm() -> None:
    off_page_source = {
        "source_page": 1,
        "bbox": {"x": -1.0, "y": 112.0, "width": 180.0, "height": 18.0},
    }
    expected = _expected_item(
        risk_level="medium",
        requires_review=False,
        evidence=off_page_source,
    )

    decision = validate_extracted_item(
        expected=expected,
        actual=_actual_item(evidence=off_page_source),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "provenance" in decision.failed_rules


def test_table_consistency_rejects_missing_cells_and_wrong_text() -> None:
    expected_table = {
        "id": "table-001",
        "fixture_table_id": "table-001",
        "cells": [
            {"id": "table-001-r1-c1", "text": "Lot number", "requires_review": False},
            {
                "id": "table-001-r1-c2",
                "text": "SAMPLE-LOT-001",
                "requires_review": False,
            },
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
    assert "table_consistency" in decision.failed_rules
    assert "provenance" in decision.failed_rules
    assert decision.requires_review is True


def test_table_cells_enforce_provenance_and_review_gates() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "fixture_table_id": "table-001",
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


def test_table_cell_missing_expected_source_blocks_auto_confirm() -> None:
    expected_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "requires_review": False,
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": _evidence(),
                "auto_confirmed": False,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "provenance" in decision.failed_rules


def test_table_cell_requires_review_blocks_auto_confirm_even_without_failures() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "fixture_table_id": "table-001",
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
                "source": source,
                "auto_confirmed": False,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.REQUIRES_REVIEW
    assert decision.requires_review is True
    assert decision.failed_rules == ()


def test_actual_table_cell_requires_review_blocks_auto_confirm() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "fixture_table_id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "requires_review": False,
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "requires_review": True,
                "auto_confirmed": False,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.REQUIRES_REVIEW
    assert decision.requires_review is True
    assert decision.failed_rules == ()


def test_malformed_table_cell_review_flag_blocks_auto_confirm() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "requires_review": "true",
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "auto_confirmed": False,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_null_actual_table_cell_review_flag_blocks_auto_confirm() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "requires_review": False,
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "requires_review": None,
                "auto_confirmed": False,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_table_cell_text_rejects_numeric_actual_value() -> None:
    expected_table = {
        "id": "table-001",
        "cells": [
            {"id": "table-001-r1-c1", "text": "123", "requires_review": False},
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {"id": "table-001-r1-c1", "text": 123},
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "table_consistency" in decision.failed_rules


def test_empty_expected_table_cells_block_auto_confirm() -> None:
    expected_table = {"id": "table-001", "cells": []}
    actual_table = {"id": "table-001", "cells": []}

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "table_consistency" in decision.failed_rules


def test_mismatched_expected_table_fixture_binding_blocks_auto_confirm() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "fixture_table_id": "table-002",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "requires_review": False,
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "auto_confirmed": False,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "table_consistency" in decision.failed_rules


def test_blank_table_cell_text_blocks_auto_confirm() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "  ",
                "source": source,
                "requires_review": False,
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "",
                "source": source,
                "auto_confirmed": False,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "table_consistency" in decision.failed_rules


def test_current_head_review_examples_fail_closed() -> None:
    source = _evidence()
    expected_scoped_item = _expected_item(
        risk_level="medium",
        requires_review=False,
        document_id="doc-001",
        block_id="block-001",
    )

    cases = [
        (
            "document_block_scope",
            validate_extracted_item(
                expected=expected_scoped_item,
                actual=_actual_item(document_id="doc-002", block_id="block-001"),
            ),
            "scope_binding",
        ),
        (
            "empty_expected_table_cells",
            validate_table_consistency(
                {"id": "table-001", "cells": []},
                {"id": "table-001", "cells": []},
            ),
            "table_consistency",
        ),
        (
            "blank_table_cell_text",
            validate_table_consistency(
                {
                    "id": "table-001",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": " ",
                            "source": source,
                            "requires_review": False,
                        },
                    ],
                },
                {
                    "id": "table-001",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "",
                            "source": source,
                        },
                    ],
                },
            ),
            "table_consistency",
        ),
        (
            "missing_expected_cell_review_flag",
            validate_table_consistency(
                {
                    "id": "table-001",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "SAMPLE-LOT-001",
                            "source": source,
                        },
                    ],
                },
                {
                    "id": "table-001",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "SAMPLE-LOT-001",
                            "source": source,
                        },
                    ],
                },
            ),
            "risk_gate",
        ),
        (
            "fixture_scope",
            validate_extracted_item(
                expected=_expected_item(
                    risk_level="medium",
                    requires_review=False,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
                actual=_actual_item(
                    fixture_id="fixture-002",
                    document_id="doc-001",
                    block_id="block-001",
                ),
            ),
            "scope_binding",
        ),
        (
            "missing_fixture_table_id",
            validate_table_consistency(
                {
                    "id": "table-001",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "SAMPLE-LOT-001",
                            "source": source,
                            "requires_review": False,
                        },
                    ],
                },
                {
                    "id": "table-001",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "SAMPLE-LOT-001",
                            "source": source,
                        },
                    ],
                },
            ),
            "table_consistency",
        ),
        (
            "mismatched_fixture_table_id",
            validate_table_consistency(
                {
                    "id": "table-001",
                    "fixture_table_id": "table-002",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "SAMPLE-LOT-001",
                            "source": source,
                            "requires_review": False,
                        },
                    ],
                },
                {
                    "id": "table-001",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "SAMPLE-LOT-001",
                            "source": source,
                        },
                    ],
                },
            ),
            "table_consistency",
        ),
        (
            "missing_expected_item_review_flag",
            validate_extracted_item(
                expected={
                    "id": "gold-001",
                    "label_id": "lot_number",
                    "expected_value": "SAMPLE-LOT-001",
                    "risk_level": "medium",
                    "evidence": source,
                },
                actual=_actual_item(),
            ),
            "risk_gate",
        ),
        (
            "blank_expected_item_value",
            validate_extracted_item(
                expected=_expected_item(
                    expected_value=" ",
                    risk_level="medium",
                    requires_review=False,
                ),
                actual=_actual_item(value=""),
            ),
            "value_non_modification",
        ),
    ]

    for case_name, decision, failed_rule in cases:
        assert decision.auto_confirm_allowed is False, case_name
        assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM, case_name
        assert decision.requires_review is True, case_name
        assert failed_rule in decision.failed_rules, case_name


def test_missing_expected_table_cell_review_flag_blocks_auto_confirm() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "auto_confirmed": False,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules
