from uuid import uuid4

import pytest

from app.pipeline import ReceiptPipeline, normalize_vendor
from app.vision import ConfidenceScore, ReceiptExtraction
from tests.helpers import FakeStore, FakeVision, FakeStorage, FakeNotifier, extraction, image_bytes


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

    async def photo(chat_id, update_id, color="white"):
        event = store.create_webhook_event(update_id=update_id, chat_id=chat_id, kind="photo")
        await pipeline.process_photo(event_id=event.id, chat_id=chat_id, image_bytes=image_bytes(color))

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
        Vendor_Name="Acme Supplies", Date="11/09/2026", Date_Text="11 September 2026", Total_Amount="130.00",
        VAT_Amount="15.50", Category="Model suggestion", Confidence_Score="High",
    )
    await photo(42, 505, "gray")
    assert "*Category:* Meals" in notifier.messages[-1]
    await photo(43, 506, "gray")
    assert "*Category:* Client Entertainment" in notifier.messages[-1]
    await photo(42, 507, "gray")
    assert "Duplicate" in notifier.messages[-1]

    with session_factory() as session:
        receipts = list(session.scalars(select(Receipt)))
        assert len(receipts) == 4
        assert all(r.status == "COMPLETED" for r in receipts)
        assert all(r.user_id == {42: user_a, 43: user_b}[r.chat_id] for r in receipts)
        assert all(r.image_path.startswith(f"{r.chat_id}/") for r in receipts)
