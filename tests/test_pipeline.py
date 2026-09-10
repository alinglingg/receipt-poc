from datetime import date
from decimal import Decimal
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from PIL import Image

from app.pipeline import DuplicateReceiptError, PendingReceipt, ReceiptDraft, ReceiptPipeline, normalize_vendor
from app.vision import ConfidenceScore, ReceiptExtraction


def image_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 32), "white").save(output, format="JPEG")
    return output.getvalue()


def extraction(confidence: ConfidenceScore = ConfidenceScore.HIGH) -> ReceiptExtraction:
    return ReceiptExtraction(
        Vendor_Name="Acme Supplies", Date="10/09/2026", Total_Amount="125.50", VAT_Amount="15.50",
        Category="Maintenance", Confidence_Score=confidence,
    )


class FakeStore:
    def __init__(self, *, category: str | None = "Maintenance", duplicate: bool = False) -> None:
        self.category, self.duplicate = category, duplicate
        self.events: list[tuple[str, str, str | None]] = []
        self.drafts: list[ReceiptDraft] = []
        self.pending: PendingReceipt | None = None

    def mark_event(self, event_id: UUID, status: str, error_code: str | None = None) -> None:
        self.events.append((str(event_id), status, error_code))

    def record_attempt(self, *args: object, **kwargs: object) -> None:
        pass

    def find_vendor_category(self, normalized_vendor: str) -> str | None:
        return self.category

    def is_duplicate(self, *args: object) -> bool:
        return self.duplicate

    def create_receipt(self, draft: ReceiptDraft) -> UUID:
        self.drafts.append(draft)
        receipt_id = uuid4()
        if draft.status == "PENDING_CATEGORY":
            self.pending = PendingReceipt(receipt_id, draft.vendor_name, draft.vendor_normalized)
        return receipt_id

    def create_pending_conversation(self, chat_id: int, receipt_id: UUID) -> None:
        pass

    def get_open_pending(self, chat_id: int) -> PendingReceipt | None:
        return self.pending

    def resolve_category(self, chat_id: int, receipt_id: UUID, category: str, normalized_vendor: str, display_vendor: str) -> None:
        self.category = category
        self.pending = None


class FakeVision:
    def __init__(self, result: ReceiptExtraction) -> None:
        self.result = result

    async def extract(self, jpeg_bytes: bytes) -> ReceiptExtraction:
        return self.result


class FakeStorage:
    async def upload_receipt(self, *, object_path: str, content: bytes) -> str:
        return object_path

    async def create_signed_url(self, *, object_path: str, expires_in_seconds: int = 900) -> str:
        return f"https://storage.test/{object_path}?temporary=true"


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, chat_id: int, markdown: str) -> None:
        self.messages.append(markdown)


@pytest.mark.asyncio
async def test_known_vendor_creates_completed_receipt_and_summary() -> None:
    store, notifier = FakeStore(), FakeNotifier()
    pipeline = ReceiptPipeline(store=store, vision=FakeVision(extraction()), storage=FakeStorage(), notifier=notifier)

    await pipeline.process_photo(event_id=uuid4(), chat_id=42, image_bytes=image_bytes())

    assert store.drafts[0].status == "COMPLETED"
    assert store.events[-1][1] == "COMPLETED"
    assert "Receipt recorded" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_duplicate_is_not_saved() -> None:
    store, notifier = FakeStore(duplicate=True), FakeNotifier()
    pipeline = ReceiptPipeline(store=store, vision=FakeVision(extraction()), storage=FakeStorage(), notifier=notifier)

    await pipeline.process_photo(event_id=uuid4(), chat_id=42, image_bytes=image_bytes())

    assert not store.drafts
    assert store.events[-1][1] == "DUPLICATE"
    assert "Duplicate" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_unknown_vendor_pauses_then_resumes_on_category_reply() -> None:
    store, notifier = FakeStore(category=None), FakeNotifier()
    pipeline = ReceiptPipeline(store=store, vision=FakeVision(extraction()), storage=FakeStorage(), notifier=notifier)

    await pipeline.process_photo(event_id=uuid4(), chat_id=42, image_bytes=image_bytes())
    assert store.drafts[0].status == "PENDING_CATEGORY"
    assert "Unrecognized vendor" in notifier.messages[-1]

    await pipeline.process_category_reply(event_id=uuid4(), chat_id=42, category="Food Supplies")
    assert store.category == "Food Supplies"
    assert "Category saved" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_low_confidence_requests_a_clearer_image() -> None:
    store, notifier = FakeStore(), FakeNotifier()
    pipeline = ReceiptPipeline(store=store, vision=FakeVision(extraction(ConfidenceScore.LOW)), storage=FakeStorage(), notifier=notifier)

    await pipeline.process_photo(event_id=uuid4(), chat_id=42, image_bytes=image_bytes())

    assert not store.drafts
    assert store.events[-1][1:] == ("RETRY_REQUESTED", "LOW_CONFIDENCE")
    assert "clearer" in notifier.messages[-1]


def test_vendor_normalization_removes_case_spaces_and_punctuation() -> None:
    assert normalize_vendor("Acme, Supplies Ltd.") == "ACMESUPPLIESLTD"
