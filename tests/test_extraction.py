from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.vision import ConfidenceScore, ReceiptExtraction, is_usable_extraction


def valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "Vendor_Name": " Acme   Supplies ",
        "Date": "10/09/2026",
        "Total_Amount": "125.50",
        "VAT_Amount": "15.50",
        "Category": " Maintenance ",
        "Confidence_Score": "High",
    }
    payload.update(overrides)
    return payload


def test_extraction_accepts_and_normalizes_required_schema() -> None:
    receipt = ReceiptExtraction.model_validate(valid_payload())

    assert receipt.vendor_name == "Acme Supplies"
    assert receipt.category == "Maintenance"
    assert receipt.receipt_date == date(2026, 9, 10)
    assert receipt.total_amount == Decimal("125.50")
    assert receipt.confidence_score is ConfidenceScore.HIGH


def test_extraction_rejects_non_required_date_format() -> None:
    with pytest.raises(ValidationError, match="DD/MM/YYYY"):
        ReceiptExtraction.model_validate(valid_payload(Date="2026-09-10"))


def test_extraction_rejects_invalid_amount_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ReceiptExtraction.model_validate(valid_payload(Total_Amount="0"))
    with pytest.raises(ValidationError):
        ReceiptExtraction.model_validate(valid_payload(unexpected="value"))


def test_low_confidence_is_not_usable() -> None:
    receipt = ReceiptExtraction.model_validate(valid_payload(Confidence_Score="Low"))

    assert not is_usable_extraction(receipt)
