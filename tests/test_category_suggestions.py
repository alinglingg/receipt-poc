from uuid import uuid4
from datetime import date

import pytest
from app.categories import category_suggestion
from app.db import Receipt
from app.pipeline import ReceiptPipeline
from app.repository import SqlAlchemyReceiptStore
from tests.helpers import FakeStore, FakeVision, FakeStorage, FakeNotifier, extraction, image_bytes
from tests.test_review import Harness


@pytest.mark.parametrize('value,expected', [(' groceries ', 'Groceries'), ('PET CARE', 'Pet Care'), ('Maintenance', None), ('CATEGORY Groceries', None)])
def test_only_preset_model_suggestions_are_exposed(value, expected):
    assert category_suggestion(value) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('known', [None, 'Custom household'])
async def test_suggestion_requires_choice_and_never_overrides_memory(known):
    store, notifier = FakeStore(category=known), FakeNotifier()
    pipeline = ReceiptPipeline(store=store, vision=FakeVision(extraction().model_copy(update={'category':'Groceries'})), storage=FakeStorage(), notifier=notifier)
    await pipeline.process_photo(event_id=uuid4(), chat_id=42, image_bytes=image_bytes())
    receipt = store.drafts[0]
    assert receipt.category == known
    assert receipt.suggested_category == ('Groceries' if known is None else None)
    if known is None:
        assert receipt.status == 'PENDING_CATEGORY'
        assert 'CATEGORY Groceries' in notifier.messages[-1]
        assert store.category is None
        await pipeline.process_category_reply(event_id=uuid4(), chat_id=42, category='Household supplies')
        assert store.category == 'Household supplies'
    else:
        assert receipt.status == 'COMPLETED'
        assert known in notifier.messages[-1]


@pytest.mark.asyncio
async def test_review_suggestion_survives_restart_and_is_private(store, session_factory, monkeypatch):
    monkeypatch.setattr('app.review.today', lambda: date(2026, 9, 30))
    h = Harness(store)
    h.vision.result = h.vision.result.model_copy(update={'category':'Groceries'})
    await h.photo()
    owner = store.get_or_create_user(42)
    pending = store.get_open_pending(owner)
    assert pending.category is None
    assert pending.suggested_category == 'Groceries'
    fresh = SqlAlchemyReceiptStore(session_factory)
    assert fresh.get_open_pending(fresh.get_or_create_user(43)) is None
    h.store = fresh
    h.pipeline._store = fresh
    await h.reply('CONFIRM')
    assert 'CATEGORY Groceries' in h.notifier.messages[-1]
    assert fresh.find_vendor_category(owner, 'ACMESUPPLIES') is None
    await h.reply('Groceries')
    with session_factory() as session:
        receipt = session.get(Receipt, pending.receipt_id)
        assert receipt.category == 'Groceries'
        assert receipt.status == 'COMPLETED'
    assert fresh.find_vendor_category(owner, 'ACMESUPPLIES') == 'Groceries'
