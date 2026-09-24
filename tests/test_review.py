from dataclasses import replace
from datetime import date
from decimal import Decimal
from itertools import count
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, func

from app.db import Receipt, PendingConversation, WebhookEvent, UserVendorMemory
from app.pipeline import ReceiptPipeline, DuplicateReceiptError, PendingReceiptError
from app.review import review_reasons, ReviewReason, parse_confirmation, printed_date_candidates
from app.statuses import ReceiptStatus
from app.vision import ConfidenceScore, VisionExtractionError
from tests.helpers import extraction, image_bytes, FakeStorage, FakeVision, FakeNotifier
from tests.test_repository import draft


@pytest.fixture(autouse=True)
def stable_date(monkeypatch):
    monkeypatch.setattr('app.review.today', lambda: date(2026,9,24))


@pytest.mark.parametrize('raw,normalized,expected', [
    ('9/10/26', date(2026,10,9), {ReviewReason.AMBIGUOUS_DATE, ReviewReason.FUTURE_DATE}),
    ('9/10/26', date(2026,9,10), {ReviewReason.AMBIGUOUS_DATE}),
    ('22 September 2026', date(2026,9,22), set()),
    ('2026-09-10', date(2026,9,10), set()),
    ('10/10/2025', date(2025,10,10), set()),
    ('22/09/2026', date(2026,9,22), set()),
    (None, date(2026,9,10), {ReviewReason.DATE_UNVERIFIED}),
    ('unreadable', date(2026,9,10), {ReviewReason.DATE_UNVERIFIED}),
    ('22 September 2026', date(2026,9,10), {ReviewReason.DATE_UNVERIFIED}),
])
def test_date_review_does_not_trust_high_confidence(raw,normalized,expected):
    value=extraction().model_copy(update={'raw_date_text':raw, 'receipt_date':normalized})
    assert set(review_reasons(value)) == expected


def test_medium_and_vat_validation_are_independent():
    value=extraction(ConfidenceScore.MEDIUM).model_copy(update={'vat_amount':Decimal('999')})
    assert set(review_reasons(value)) == {ReviewReason.MEDIUM_CONFIDENCE,ReviewReason.VAT_EXCEEDS_TOTAL}


@pytest.mark.parametrize('message', ['yes', 'CONFIRM 9/10/26', 'CONFIRM 2026-02-30', 'CONFIRM 20260910', 'CONFIRM 2026-09-10 extra'])
def test_confirmation_grammar_rejects_ambiguous_or_invalid_input(message):
    with pytest.raises(ValueError): parse_confirmation(message)


def test_confirmation_grammar_and_numeric_date_candidates():
    assert parse_confirmation(' confirm ') is None
    assert parse_confirmation('confirm 2026-09-10') == date(2026,9,10)
    assert printed_date_candidates('9/10/26') == {date(2026,9,10),date(2026,10,9)}
    assert printed_date_candidates('2026-02-30') == set()


class Harness:
    def __init__(self,store):
        self.store=store
        self.vision=FakeVision(extraction(ConfidenceScore.MEDIUM))
        self.storage=AsyncMock(wraps=FakeStorage())
        self.notifier=FakeNotifier()
        self.pipeline=ReceiptPipeline(store=store,vision=self.vision,storage=self.storage,notifier=self.notifier)
        self.ids=count(10000)

    async def photo(self,chat=42,color='white'):
        event=self.store.create_webhook_event(update_id=next(self.ids),chat_id=chat,kind='photo')
        await self.pipeline.process_photo(event_id=event.id,chat_id=chat,image_bytes=image_bytes(color))
        return event.id

    async def reply(self,text,chat=42):
        event=self.store.create_webhook_event(update_id=next(self.ids),chat_id=chat,kind='text',text=text)
        await self.pipeline.process_category_reply(event_id=event.id,chat_id=chat,category=text)
        return event.id


@pytest.mark.asyncio
async def test_unknown_vendor_review_then_category_learns_only_after_confirmation(store,session_factory):
    h=Harness(store)
    event_id=await h.photo()
    user=store.get_or_create_user(42)
    pending=store.get_open_pending(user)
    assert pending.status == ReceiptStatus.NEEDS_REVIEW
    assert 'Please review' in h.notifier.messages[-1]
    assert '10 September 2026' in h.notifier.messages[-1]
    await h.reply('Dining')  # A category cannot accidentally confirm a review.
    assert store.get_open_pending(user).status == ReceiptStatus.NEEDS_REVIEW
    assert store.find_vendor_category(user,'ACMESUPPLIES') is None
    with pytest.raises(LookupError): store.resolve_category(user,pending.receipt_id,'Dining')
    await h.reply('CONFIRM')
    assert store.get_open_pending(user).status == ReceiptStatus.PENDING_CATEGORY
    assert store.find_vendor_category(user,'ACMESUPPLIES') is None
    await h.reply('CONFIRM')  # A repeated confirmation cannot become category memory.
    assert store.get_open_pending(user).status == ReceiptStatus.PENDING_CATEGORY
    await h.reply('Dining')
    assert store.get_open_pending(user) is None
    assert store.find_vendor_category(user,'ACMESUPPLIES') == 'Dining'
    with session_factory() as session:
        r=session.get(Receipt,pending.receipt_id)
        assert r.status == ReceiptStatus.COMPLETED and r.completed_at is not None
        assert r.review_reason is None
        assert session.get(WebhookEvent,event_id).status == 'COMPLETED'
        assert session.scalar(select(func.count(Receipt.id))) == 1


@pytest.mark.asyncio
async def test_keigo_high_confidence_ambiguous_date_requires_explicit_correction(store,session_factory):
    h=Harness(store)
    h.vision.result=extraction().model_copy(update={
        'vendor_name':'Keigo Ramen & Katsu','total_amount':Decimal('536'),
        'receipt_date':date(2026,10,9),'raw_date_text':'9/10/26','vat_amount':None,
    })
    event_id=await h.photo()
    user=store.get_or_create_user(42)
    pending=store.get_open_pending(user)
    assert pending.status == ReceiptStatus.NEEDS_REVIEW
    assert '09 October 2026' in h.notifier.messages[-1]
    await h.reply('CONFIRM')
    assert 'specify the correct date' in h.notifier.messages[-1]
    await h.reply('CONFIRM 2026-10-09')
    assert 'future' in h.notifier.messages[-1]
    await h.reply('CONFIRM 2026-09-10')
    await h.reply('Dining')
    with session_factory() as session:
        r=session.get(Receipt,pending.receipt_id)
        assert r.receipt_date == date(2026,9,10) and r.total_amount == Decimal('536')
        assert r.raw_date_text == '9/10/26' and r.status == ReceiptStatus.COMPLETED
        assert session.get(WebhookEvent,event_id).completed_at is not None


@pytest.mark.asyncio
async def test_known_vendor_medium_requires_confirmation_and_recovers_after_restart(store,session_factory):
    from tests.test_repository import create_pending
    user,old=create_pending(store,42,9900)
    store.resolve_category(user,old,'Meals')
    h=Harness(store)
    h.vision.result=extraction(ConfidenceScore.MEDIUM).model_copy(update={'total_amount':Decimal('200')})
    await h.photo()
    pending=store.get_open_pending(user)
    assert pending.category == 'Meals' and pending.status == ReceiptStatus.NEEDS_REVIEW
    # New service objects retrieve the durable review, without calling Vision.
    h.pipeline=ReceiptPipeline(store=store,vision=AsyncMock(),storage=h.storage,notifier=h.notifier)
    await h.reply('REVIEW')
    assert 'Please review' in h.notifier.messages[-1]
    await h.reply('CONFIRM')
    assert store.get_open_pending(user) is None
    assert 'Receipt recorded' in h.notifier.messages[-1]
    await h.reply('CONFIRM')
    with session_factory() as session:
        assert session.scalar(select(func.count(Receipt.id))) == 2
        assert session.get(Receipt,pending.receipt_id).status == 'COMPLETED'


@pytest.mark.asyncio
async def test_vat_above_total_cannot_be_confirmed_and_retry_removes_only_draft(store,session_factory):
    h=Harness(store)
    h.vision.result=extraction().model_copy(update={'vat_amount':Decimal('999')})
    original=await h.photo()
    user=store.get_or_create_user(42)
    pending=store.get_open_pending(user)
    await h.reply('CONFIRM')
    assert 'VAT exceeds' in h.notifier.messages[-1]
    assert store.get_open_pending(user).receipt_id == pending.receipt_id
    await h.reply('RETRY')
    assert store.get_open_pending(user) is None
    with session_factory() as session:
        assert session.get(Receipt,pending.receipt_id) is None
        assert session.get(WebhookEvent,original).error_code == 'USER_RETRY'
        assert session.scalar(select(func.count(UserVendorMemory.user_id))) == 0
        assert session.scalar(select(func.count(PendingConversation.id))) == 0
    # Same image can be retried because the unconfirmed draft was explicitly discarded.
    h.vision.result=extraction()
    await h.photo()
    assert store.get_open_pending(user).status == ReceiptStatus.PENDING_CATEGORY


@pytest.mark.asyncio
async def test_review_is_user_scoped_and_new_photos_do_not_overwrite_pending(store,session_factory):
    h=Harness(store)
    await h.photo()
    a=store.get_or_create_user(42)
    b=store.get_or_create_user(43)
    pending=store.get_open_pending(a)
    with pytest.raises(LookupError): store.confirm_review(b,pending.receipt_id)
    with pytest.raises(LookupError): store.retry_review(b,pending.receipt_id)
    await h.reply('CONFIRM',chat=43)
    assert store.get_open_pending(a) == pending
    await h.photo(color='gray')
    assert 'Please review' in h.notifier.messages[-1]
    assert h.storage.upload_receipt.await_count == 1
    assert store.get_open_pending(a) == pending
    await h.photo()  # Same bytes still get duplicate detection.
    assert 'Duplicate' in h.notifier.messages[-1]
    await h.photo(chat=43)
    assert store.get_open_pending(b).status == 'NEEDS_REVIEW'
    with session_factory() as session:
        assert session.scalar(select(func.count(Receipt.id))) == 2


@pytest.mark.asyncio
async def test_corrected_date_duplicate_keeps_review_pending(store,session_factory):
    user=store.get_or_create_user(42)
    event=store.create_webhook_event(update_id=9950,chat_id=42,kind='photo')
    existing=store.create_receipt(draft(event.id,user))
    h=Harness(store)
    h.vision.result=extraction().model_copy(update={'receipt_date':date(2026,10,9),'raw_date_text':'9/10/26'})
    await h.photo()
    pending=store.get_open_pending(user)
    await h.reply('CONFIRM 2026-09-10')
    assert 'already saved' in h.notifier.messages[-1]
    assert store.get_open_pending(user).receipt_id == pending.receipt_id
    with session_factory() as session:
        assert session.get(Receipt,pending.receipt_id).receipt_date == date(2026,10,9)
        assert session.get(Receipt,existing).receipt_date == date(2026,9,10)
    await h.reply('RETRY')
    with pytest.raises(LookupError): store.retry_review(user,existing)
    with session_factory() as session:
        assert session.get(Receipt,existing) is not None


def test_create_receipt_atomically_opens_review_and_rejects_second_pending(store,session_factory):
    user=store.get_or_create_user(42)
    first=store.create_webhook_event(update_id=9980,chat_id=42,kind='photo')
    second=store.create_webhook_event(update_id=9981,chat_id=42,kind='photo')
    r=store.create_receipt(replace(draft(first.id,user),status='NEEDS_REVIEW',review_reason='MEDIUM_CONFIDENCE'))
    assert store.get_open_pending(user).receipt_id == r
    with pytest.raises(PendingReceiptError):
        store.create_receipt(replace(draft(second.id,user),image_sha256='b'*64,total_amount=Decimal('200')))
    with session_factory() as session:
        assert session.scalar(select(func.count(Receipt.id))) == 1
        assert session.get(WebhookEvent,first.id).status == 'NEEDS_REVIEW'
        assert session.get(WebhookEvent,first.id).completed_at is None


@pytest.mark.asyncio
async def test_invalid_extraction_requests_retry_without_receipt(store,session_factory):
    h=Harness(store)
    h.pipeline._vision=AsyncMock()
    h.pipeline._vision.extract.side_effect=VisionExtractionError('invalid schema')
    event=await h.photo()
    with session_factory() as session:
        assert session.scalar(select(Receipt)) is None
        assert session.get(WebhookEvent,event).status == 'RETRY_REQUESTED'


@pytest.mark.parametrize('overrides', [
    {'Vendor_Name':''}, {'Total_Amount':'-1'}, {'Total_Amount':'0'},
    {'Date':'31/02/2026'}, {'VAT_Amount':'-1'}, {'Date_Text':'x'*81},
])
def test_invalid_fields_never_reach_review(overrides):
    import json
    from app.vision import parse_vision_json
    from tests.test_extraction import valid_payload
    with pytest.raises(VisionExtractionError): parse_vision_json(json.dumps(valid_payload(**overrides)))


def test_extraction_preserves_printed_date_instead_of_normalizing_it():
    import json
    from app.vision import parse_vision_json
    from tests.test_extraction import valid_payload
    result=parse_vision_json(json.dumps(valid_payload(Date='09/10/2026',Date_Text='9/10/26')))
    assert result.raw_date_text == '9/10/26'
    assert ReviewReason.AMBIGUOUS_DATE in review_reasons(result)


def test_pending_insert_failure_rolls_back_receipt_and_event(store,session_factory):
    from sqlalchemy import event as sa_event
    user=store.get_or_create_user(42)
    event=store.create_webhook_event(update_id=9990,chat_id=42,kind='photo')
    def fail_pending(session,*args):
        if any(isinstance(row,PendingConversation) for row in session.new):
            raise RuntimeError('simulated pending insert failure')
    sa_event.listen(session_factory,'before_flush',fail_pending)
    try:
        with pytest.raises(RuntimeError):
            store.create_receipt(replace(draft(event.id,user),status='NEEDS_REVIEW'))
    finally:
        sa_event.remove(session_factory,'before_flush',fail_pending)
    with session_factory() as session:
        assert session.scalar(select(func.count(Receipt.id))) == 0
        assert session.get(WebhookEvent,event.id).status == 'RECEIVED'


@pytest.mark.asyncio
async def test_review_prompt_failure_leaves_durable_pending(store,session_factory):
    from app.telegram import TelegramError
    h=Harness(store)
    h.pipeline._notifier=AsyncMock()
    h.pipeline._notifier.send.side_effect=TelegramError('delivery failed')
    with pytest.raises(TelegramError): await h.photo()
    user=store.get_or_create_user(42)
    assert store.get_open_pending(user).status == 'NEEDS_REVIEW'
    h.pipeline._notifier=h.notifier
    await h.reply('REVIEW')
    assert 'Please review' in h.notifier.messages[-1]
    await h.reply('CONFIRM')
    assert store.get_open_pending(user).status == 'PENDING_CATEGORY'
