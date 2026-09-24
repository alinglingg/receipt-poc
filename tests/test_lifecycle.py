from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.db import Receipt, WebhookEvent
from app.pipeline import ReceiptPipeline
from app.statuses import EventStatus, ReceiptStatus
from app.telegram import TelegramError, TelegramUpdate
from app.vision import ConfidenceScore
from tests.helpers import FakeVision, FakeStorage, FakeNotifier, extraction, image_bytes


def test_event_timestamps_preserve_first_start_and_record_retry_reason(store, session_factory):
    event = store.create_webhook_event(update_id=700, chat_id=42, kind='photo')
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    with session_factory() as session:
        row = session.get(WebhookEvent, event.id)
        row.status = EventStatus.PROCESSING
        row.processing_started_at = old
        session.commit()
    store.mark_event(event.id, EventStatus.PROCESSING)
    store.mark_event(event.id, EventStatus.RETRY_REQUESTED, 'LOW_CONFIDENCE')
    with session_factory() as session:
        row = session.get(WebhookEvent, event.id)
        assert row.processing_started_at.replace(tzinfo=timezone.utc) == old
        assert row.completed_at >= row.processing_started_at
        assert row.error_code == 'LOW_CONFIDENCE'
    store.mark_event(event.id, EventStatus.PROCESSING)
    with session_factory() as session:
        row = session.get(WebhookEvent, event.id)
        assert row.completed_at is None and row.error_code is None
        assert row.processing_started_at.replace(tzinfo=timezone.utc) == old


@pytest.mark.asyncio
async def test_pending_receipt_completion_updates_original_event(store, session_factory):
    photo = store.create_webhook_event(update_id=710, chat_id=42, kind='photo')
    pipeline = ReceiptPipeline(store=store, vision=FakeVision(extraction()), storage=FakeStorage(), notifier=FakeNotifier())
    await pipeline.process_photo(event_id=photo.id, chat_id=42, image_bytes=image_bytes())
    with session_factory() as session:
        receipt = session.scalar(select(Receipt))
        assert receipt.status == ReceiptStatus.PENDING_CATEGORY
        assert receipt.processing_started_at is not None
        assert receipt.completed_at is None
        start = receipt.processing_started_at
        created = receipt.created_at
        assert session.get(WebhookEvent, photo.id).completed_at is None
    reply = store.create_webhook_event(update_id=711, chat_id=42, kind='text')
    await pipeline.process_category_reply(event_id=reply.id, chat_id=42, category='Pet Care')
    with session_factory() as session:
        receipt = session.scalar(select(Receipt))
        assert receipt.status == ReceiptStatus.COMPLETED
        assert receipt.processing_started_at == start
        assert receipt.created_at == created
        assert receipt.completed_at >= start
        assert receipt.updated_at >= receipt.completed_at
        assert receipt.review_reason is None and receipt.failure_reason is None
        original = session.get(WebhookEvent, photo.id)
        assert original.status == EventStatus.COMPLETED
        assert original.completed_at >= receipt.completed_at


@pytest.mark.asyncio
async def test_low_confidence_has_terminal_event_but_no_receipt(store, session_factory):
    event = store.create_webhook_event(update_id=720, chat_id=42, kind='photo')
    pipeline = ReceiptPipeline(store=store, vision=FakeVision(extraction(ConfidenceScore.LOW)), storage=FakeStorage(), notifier=FakeNotifier())
    await pipeline.process_photo(event_id=event.id, chat_id=42, image_bytes=image_bytes())
    with session_factory() as session:
        assert session.scalar(select(Receipt)) is None
        event = session.get(WebhookEvent, event.id)
        assert event.status == EventStatus.RETRY_REQUESTED
        assert event.error_code == 'LOW_CONFIDENCE'
        assert event.processing_started_at is not None and event.completed_at is not None


@pytest.fixture
def process_event(monkeypatch):
    # Importing main builds the app; explicitly prevent reading the local .env.
    import importlib
    import app.config
    monkeypatch.setattr(app.config, 'get_settings', lambda: app.config.Settings.model_construct())
    return importlib.import_module('app.main')._process_event


@pytest.mark.asyncio
@pytest.mark.parametrize('error,code', [(TelegramError('private details'), 'TELEGRAM_ERROR'), (RuntimeError('private details'), 'PROCESSING_ERROR')])
async def test_processing_failure_records_safe_reason(process_event, store, session_factory, error, code):
    event = store.create_webhook_event(update_id=730, chat_id=42, kind='photo', file_id='test')
    telegram = AsyncMock()
    telegram.download_photo.side_effect = error
    await process_event(SimpleNamespace(store=store, telegram=telegram, pipeline=AsyncMock()), TelegramUpdate(730,42,'photo','test'), event.id)
    with session_factory() as session:
        event = session.get(WebhookEvent, event.id)
        assert event.status == EventStatus.FAILED and event.error_code == code
        assert event.processing_started_at is not None and event.completed_at is not None
        assert session.scalar(select(Receipt)) is None


@pytest.mark.asyncio
async def test_notification_failure_preserves_completed_receipt(process_event, store, session_factory):
    from tests.test_repository import create_pending
    user, receipt_id = create_pending(store, 42, 740)
    store.resolve_category(user, receipt_id, 'Pet Care')
    event = store.create_webhook_event(update_id=741, chat_id=42, kind='photo', file_id='test')
    notifier = AsyncMock()
    notifier.send.side_effect = TelegramError('delivery unavailable')
    telegram = AsyncMock()
    telegram.download_photo.return_value = image_bytes()
    pipeline = ReceiptPipeline(store=store, vision=FakeVision(extraction().model_copy(update={'receipt_date': datetime(2026,9,11).date(), 'raw_date_text': '11 September 2026'})), storage=FakeStorage(), notifier=notifier)
    await process_event(SimpleNamespace(store=store, telegram=telegram, pipeline=pipeline), TelegramUpdate(741,42,'photo','test'), event.id)
    with session_factory() as session:
        receipt = session.scalar(select(Receipt).where(Receipt.event_id == event.id))
        assert receipt.status == ReceiptStatus.COMPLETED
        assert receipt.completed_at is not None and receipt.failure_reason is None
        assert receipt.updated_at >= receipt.completed_at
        assert session.get(WebhookEvent, event.id).status == EventStatus.FAILED


@pytest.mark.parametrize('status,reason_field', [(ReceiptStatus.NEEDS_REVIEW,'review_reason'),(ReceiptStatus.FAILED,'failure_reason')])
def test_receipt_can_persist_review_and_failure_metadata(store,session_factory,status,reason_field):
    from dataclasses import replace
    from tests.test_repository import draft
    user = store.get_or_create_user(42)
    event = store.create_webhook_event(update_id=750,chat_id=42,kind='photo')
    store.mark_event(event.id, EventStatus.PROCESSING)
    receipt_id = store.create_receipt(replace(draft(event.id,user),status=status,**{reason_field:'VALIDATION_REQUIRED'}))
    with session_factory() as session:
        receipt=session.get(Receipt,receipt_id)
        assert receipt.status == status
        assert getattr(receipt,reason_field) == 'VALIDATION_REQUIRED'
        assert receipt.processing_started_at is not None
        assert receipt.updated_at is not None and receipt.completed_at is None
