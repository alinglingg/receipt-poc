from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.db import Receipt
from app.pipeline import DuplicateReceiptError, ReceiptPipeline
from tests.test_pipeline import FakeStore, FakeStorage, FakeNotifier, extraction, image_bytes
from tests.test_repository import draft


@pytest.mark.asyncio
@pytest.mark.parametrize('category', ['Pet Care', None])
async def test_repeat_image_skips_changed_extraction_and_preserves_original(category):
    store = FakeStore(category=category)
    vision = AsyncMock()
    vision.extract.side_effect = [
        extraction().model_copy(update={'total_amount': Decimal('6750')}),
        extraction().model_copy(update={'total_amount': Decimal('7500')}),
    ]
    storage = AsyncMock(wraps=FakeStorage())
    notifier = FakeNotifier()
    pipeline = ReceiptPipeline(store=store, vision=vision, storage=storage, notifier=notifier)
    await pipeline.process_photo(event_id=uuid4(), chat_id=42, image_bytes=image_bytes())
    pending = store.pending
    await pipeline.process_photo(event_id=uuid4(), chat_id=42, image_bytes=image_bytes())
    assert len(store.drafts) == 1
    assert store.drafts[0].total_amount == Decimal('6750')
    assert store.pending == pending
    assert vision.extract.await_count == storage.upload_receipt.await_count == 1
    assert store.events[-1][1] == 'DUPLICATE'
    assert 'Duplicate' in notifier.messages[-1]


def test_image_duplicate_guard_ignores_changed_total_and_is_user_scoped(store, session_factory):
    user = store.get_or_create_user(42)
    other = store.get_or_create_user(43)
    events = [store.create_webhook_event(update_id=n, chat_id=42, kind='photo') for n in (901,902)]
    first = replace(draft(events[0].id, user), total_amount=Decimal('6750'))
    store.create_receipt(first)
    assert store.is_duplicate_image(user, first.image_sha256)
    assert not store.is_duplicate_image(other, first.image_sha256)
    assert not store.is_duplicate_image(user, '')
    with pytest.raises(DuplicateReceiptError):
        store.create_receipt(replace(first, event_id=events[1].id, total_amount=Decimal('7500')))
    with session_factory() as session:
        assert session.scalar(select(func.count(Receipt.id))) == 1


@pytest.mark.asyncio
async def test_unsaved_low_confidence_image_can_be_retried():
    from app.vision import ConfidenceScore
    store = FakeStore()
    vision = AsyncMock()
    vision.extract.side_effect = [extraction(ConfidenceScore.LOW), extraction()]
    pipeline = ReceiptPipeline(store=store, vision=vision, storage=FakeStorage(), notifier=FakeNotifier())
    for _ in range(2):
        await pipeline.process_photo(event_id=uuid4(), chat_id=42, image_bytes=image_bytes())
    assert vision.extract.await_count == 2
    assert len(store.drafts) == 1
    assert store.events[-1][1] == 'COMPLETED'


@pytest.mark.asyncio
async def test_vision_requests_high_detail_and_parses_response():
    import json
    from types import SimpleNamespace
    from app.vision import OpenAIVisionExtractor
    from tests.test_extraction import valid_payload
    create = AsyncMock(return_value=SimpleNamespace(output_text=json.dumps(valid_payload())))
    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    result = await OpenAIVisionExtractor(api_key='test', client=client).extract(b'image')
    assert result.total_amount == Decimal('125.50')
    request = create.call_args.kwargs
    assert request['input'][0]['content'][1]['detail'] == 'high'
