"""Strict receipt extraction using OpenAI Vision structured outputs."""

from __future__ import annotations

import base64
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class VisionExtractionError(RuntimeError):
    """The Vision API did not return usable receipt data."""


class ConfidenceScore(str, Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class ReceiptExtraction(BaseModel):
    """The only receipt shape accepted from the model."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    vendor_name: str = Field(alias="Vendor_Name", min_length=1, max_length=200)
    receipt_date: date = Field(alias="Date")
    total_amount: Decimal = Field(alias="Total_Amount", gt=0, max_digits=12, decimal_places=2)
    vat_amount: Decimal | None = Field(alias="VAT_Amount", ge=0, max_digits=12, decimal_places=2)
    category: str = Field(alias="Category", min_length=1, max_length=100)
    confidence_score: ConfidenceScore = Field(alias="Confidence_Score")

    @field_validator("vendor_name", "category")
    @classmethod
    def trim_text(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("receipt_date", mode="before")
    @classmethod
    def parse_required_date_format(cls, value: Any) -> date:
        if isinstance(value, date):
            return value
        if not isinstance(value, str):
            raise ValueError("must be DD/MM/YYYY")
        try:
            return datetime.strptime(value, "%d/%m/%Y").date()
        except ValueError as exc:
            raise ValueError("must use DD/MM/YYYY") from exc


VISION_INSTRUCTIONS = """Extract data from this receipt image.
Return only the requested structured output. Do not invent a value that cannot
be read. Use Confidence_Score Low whenever the receipt is blurry, damaged,
unreadable, missing a required field, or the total/date/vendor is uncertain.
Date must be DD/MM/YYYY. Total_Amount and VAT_Amount must be plain numeric
amounts without currency symbols. VAT_Amount is null when no VAT is shown.
Category should be the most likely expense category based only on the receipt.
"""


class OpenAIVisionExtractor:
    def __init__(self, api_key: str, model: str = "gpt-4o", client: AsyncOpenAI | None = None) -> None:
        self._client = client or AsyncOpenAI(api_key=api_key)
        self._model = model

    async def extract(self, jpeg_bytes: bytes) -> ReceiptExtraction:
        if not jpeg_bytes:
            raise VisionExtractionError("Cannot extract from an empty image.")

        image_data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode("ascii")
        try:
            response = await self._client.responses.parse(
                model=self._model,
                instructions=VISION_INSTRUCTIONS,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Extract this receipt."},
                            {"type": "input_image", "image_url": image_data_url, "detail": "low"},
                        ],
                    }
                ],
                text_format=ReceiptExtraction,
            )
            parsed = response.output_parsed
        except Exception as exc:  # Pipeline classifies transient provider errors separately.
            raise VisionExtractionError("Vision extraction request failed.") from exc

        if parsed is None:
            raise VisionExtractionError("Vision extraction returned no structured result.")
        if not isinstance(parsed, ReceiptExtraction):
            try:
                parsed = ReceiptExtraction.model_validate(parsed)
            except ValidationError as exc:
                raise VisionExtractionError("Vision extraction returned an invalid schema.") from exc
        return parsed


def is_usable_extraction(extraction: ReceiptExtraction) -> bool:
    """Low confidence requires a retry request rather than a saved receipt."""
    return extraction.confidence_score is not ConfidenceScore.LOW
