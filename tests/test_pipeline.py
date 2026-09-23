from io import BytesIO
from uuid import UUID, uuid4

import pytest
from PIL import Image

from app.pipeline import (
    PendingReceipt,
    ReceiptDraft,
    ReceiptPipeline,
    normalize_vendor,
)
from app.vision import ConfidenceScore, ReceiptExtraction


def image_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 32), "white").save(output, format="JPEG")
    return output.getvalue()


def extraction(
    confidence: ConfidenceScore = ConfidenceScore.HIGH,
) -> ReceiptExtraction:
    return ReceiptExtraction(
        Vendor_Name="Acme Supplies",
        Date="10/09/2026",
        Total_Amount="125.50",
        VAT_Amount="15.50",
        Category="Maintenance",
        Confidence_Score=confidence,
    )


class FakeStore:
    def __init__(
        self,
        *,
        category: str | None = "Maintenance",
        duplicate: bool = False,
    ) -> None:
        self.category = category
        self.duplicate = duplicate
        self.events: list[tuple[str, str, str | None]] = []
        self.drafts: list[ReceiptDraft] = []
        self.pending: PendingReceipt | None = None

    def get_or_create_user(self, chat_id: int) -> UUID:
        return UUID(int=chat_id)

    def mark_event(
        self,
        event_id: UUID,
        status: str,
        error_code: str | None = None,
    ) -> None:
        self.events.append((str(event_id), status, error_code))

    def record_attempt(self, *args: object, **kwargs: object) -> None:
        pass

    def find_vendor_category(
        self,
        user_id: UUID,
        normalized_vendor: str,
    ) -> str | None:
        return self.category

    def is_duplicate(self, *args: object) -> bool:
        return self.duplicate

    def create_receipt(self, draft: ReceiptDraft) -> UUID:
        self.drafts.append(draft)
        receipt_id = uuid4()

        if draft.status == "PENDING_CATEGORY":
            self.pending = PendingReceipt(
                receipt_id=receipt_id,
                user_id=draft.user_id,
                vendor_name=draft.vendor_name,
                vendor_normalized=draft.vendor_normalized,
                receipt_date=draft.receipt_date,
                total_amount=draft.total_amount,
                vat_amount=draft.vat_amount,
                confidence=draft.confidence,
                image_path=draft.image_path,
            )

        return receipt_id

    def create_pending_conversation(
        self,
        user_id: UUID,
        receipt_id: UUID,
    ) -> None:
        pass

    def get_open_pending(
        self,
        user_id: UUID,
    ) -> PendingReceipt | None:
        return self.pending

    def resolve_category(
        self,
        user_id: UUID,
        receipt_id: UUID,
        category: str,
    ) -> None:
        self.category = category
        self.pending = None


class FakeVision:
    def __init__(self, result: ReceiptExtraction) -> None:
        self.result = result

    async def extract(
        self,
        jpeg_bytes: bytes,
    ) -> ReceiptExtraction:
        return self.result


class FakeStorage:
    async def upload_receipt(
        self,
        *,
        object_path: str,
        content: bytes,
    ) -> str:
        return object_path

    async def create_signed_url(
        self,
        *,
        object_path: str,
        expires_in_seconds: int = 900,
    ) -> str:
        return f"https://storage.test/{object_path}?temporary=true"


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(
        self,
        chat_id: int,
        markdown: str,
    ) -> None:
        self.messages.append(markdown)


@pytest.mark.asyncio
async def test_unknown_vendor_category_reply_returns_summary() -> None:
    store = FakeStore(category=None)
    notifier = FakeNotifier()

    pipeline = ReceiptPipeline(
        store=store,
        vision=FakeVision(extraction()),
        storage=FakeStorage(),
        notifier=notifier,
    )

    await pipeline.process_photo(
        event_id=uuid4(),
        chat_id=42,
        image_bytes=image_bytes(),
    )

    assert store.drafts[0].status == "PENDING_CATEGORY"
    assert "Unrecognized vendor" in notifier.messages[-1]

    await pipeline.process_category_reply(
        event_id=uuid4(),
        chat_id=42,
        category="Food Supplies",
    )

    assert store.category == "Food Supplies"
    assert "Receipt recorded" in notifier.messages[-1]
    assert "Food Supplies" in notifier.messages[-1]

@pytest.mark.asyncio
async def test_duplicate_is_not_saved() -> None:
    store = FakeStore(duplicate=True)
    notifier = FakeNotifier()

    pipeline = ReceiptPipeline(
        store=store,
        vision=FakeVision(extraction()),
        storage=FakeStorage(),
        notifier=notifier,
    )

    await pipeline.process_photo(
        event_id=uuid4(),
        chat_id=42,
        image_bytes=image_bytes(),
    )

    assert not store.drafts
    assert store.events[-1][1] == "DUPLICATE"
    assert "Duplicate" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_unknown_vendor_pauses_then_resumes_on_category_reply() -> None:
    store = FakeStore(category=None)
    notifier = FakeNotifier()

    pipeline = ReceiptPipeline(
        store=store,
        vision=FakeVision(extraction()),
        storage=FakeStorage(),
        notifier=notifier,
    )

    await pipeline.process_photo(
        event_id=uuid4(),
        chat_id=42,
        image_bytes=image_bytes(),
    )

    assert store.drafts[0].status == "PENDING_CATEGORY"
    assert "Unrecognized vendor" in notifier.messages[-1]

    await pipeline.process_category_reply(
        event_id=uuid4(),
        chat_id=42,
        category="Food Supplies",
    )

    assert store.category == "Food Supplies"
    assert "Receipt recorded" in notifier.messages[-1]
    assert "Food Supplies" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_low_confidence_requests_a_clearer_image() -> None:
    store = FakeStore()
    notifier = FakeNotifier()

    pipeline = ReceiptPipeline(
        store=store,
        vision=FakeVision(extraction(ConfidenceScore.LOW)),
        storage=FakeStorage(),
        notifier=notifier,
    )

    await pipeline.process_photo(
        event_id=uuid4(),
        chat_id=42,
        image_bytes=image_bytes(),
    )

    assert not store.drafts
    assert store.events[-1][1:] == (
        "RETRY_REQUESTED",
        "LOW_CONFIDENCE",
    )
    assert "clearer" in notifier.messages[-1]


def test_vendor_normalization_removes_case_spaces_and_punctuation() -> None:
    assert normalize_vendor("Acme, Supplies Ltd.") == "ACMESUPPLIESLTD"


@pytest.mark.asyncio
async def test_two_chats_learn_independently_through_the_pipeline(store, session_factory):
    from sqlalchemy import select
    from app.db import Receipt

    notifier = FakeNotifier()
    vision = FakeVision(extraction())
    pipeline = ReceiptPipeline(store=store, vision=vision, storage=FakeStorage(), notifier=notifier)

    async def photo(chat_id, update_id):
        event = store.create_webhook_event(update_id=update_id, chat_id=chat_id, kind="photo")
        await pipeline.process_photo(event_id=event.id, chat_id=chat_id, image_bytes=image_bytes())

    async def reply(chat_id, update_id, category):
        event = store.create_webhook_event(update_id=update_id, chat_id=chat_id, kind="text", text=category)
        await pipeline.process_category_reply(event_id=event.id, chat_id=chat_id, category=category)

    await photo(42, 500)
    await photo(43, 501)  # Identical receipt, different owner: both are accepted.
    user_a, user_b = store.get_or_create_user(42), store.get_or_create_user(43)
    pending_a, pending_b = store.get_open_pending(user_a), store.get_open_pending(user_b)
    assert pending_a is not None and pending_b is not None

    await reply(44, 502, "Unrelated category")
    assert store.get_open_pending(user_a) == pending_a
    assert store.get_open_pending(user_b) == pending_b

    await reply(42, 503, "Meals")
    await reply(43, 504, "Client Entertainment")
    assert store.find_vendor_category(user_a, "ACMESUPPLIES") == "Meals"
    assert store.find_vendor_category(user_b, "ACMESUPPLIES") == "Client Entertainment"

    # Later receipts use the respective owner's memory without another question.
    vision.result = ReceiptExtraction(
        Vendor_Name="Acme Supplies", Date="11/09/2026", Total_Amount="130.00",
        VAT_Amount="15.50", Category="Model suggestion", Confidence_Score="High",
    )
    await photo(42, 505)
    assert "*Category:* Meals" in notifier.messages[-1]
    await photo(43, 506)
    assert "*Category:* Client Entertainment" in notifier.messages[-1]
    await photo(42, 507)
    assert "Duplicate" in notifier.messages[-1]

    with session_factory() as session:
        receipts = list(session.scalars(select(Receipt)))
        assert len(receipts) == 4
        assert all(r.status == "COMPLETED" for r in receipts)
        assert all(r.user_id == {42: user_a, 43: user_b}[r.chat_id] for r in receipts)
        assert all(r.image_path.startswith(f"{r.chat_id}/") for r in receipts)
