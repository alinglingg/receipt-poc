from uuid import uuid4

import pytest

from app.pipeline import ReceiptPipeline, normalize_vendor
from app.vision import ConfidenceScore, ReceiptExtraction
from tests.helpers import FakeStore, FakeVision, FakeStorage, FakeNotifier, extraction, image_bytes


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
    assert "Category saved" in notifier.messages[-2]
    assert "Receipt recorded" in notifier.messages[-1]
    assert "Food Supplies" in notifier.messages[-1]


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
