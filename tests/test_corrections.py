from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.corrections import Correction, parse_edit
from app.db import Receipt, WebhookEvent
from app.pipeline import DuplicateReceiptError
from app.commands import process_command
from tests.test_repository import draft


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr('app.corrections.today', lambda: date(2026, 9, 25))


def saved(store, update=1, chat=42, **changes):
    user = store.get_or_create_user(chat)
    event = store.create_webhook_event(update_id=update, chat_id=chat, kind='photo')
    row = replace(draft(event.id, user, chat), image_sha256=str(update).zfill(64), **changes)
    return user, store.create_receipt(row)


@pytest.mark.parametrize('field,value', [
    ('total', 'NaN'), ('total', '-1'), ('total', '0'), ('total', '1.001'),
    ('total', '10000000000'), ('total', '1e3'), ('total', '1,000'),
    ('date', '09/10/26'), ('date', '2026-02-30'), ('date', '2026-10-09'),
    ('vendor', '***'), ('category', 'x'*101), ('status', 'COMPLETED'),
])
def test_invalid_command_values(field, value):
    with pytest.raises(ValueError):
        parse_edit(f'/edit {uuid4()} {field} {value}')


@pytest.mark.parametrize('field,value,expected', [
    ('total', '450.00', Decimal('450.00')),
    ('date', '22/09/2026', date(2026, 9, 22)),
    ('vendor', '  New   Vendor ', 'New Vendor'),
    ('category', 'Pet Care', 'Pet Care'),
])
def test_correction_persists_only_selected_field(store, session_factory, field, value, expected):
    user, receipt_id = saved(store)
    old = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with session_factory() as session:
        row = session.get(Receipt, receipt_id)
        row.updated_at = old
        session.commit()
        completed = row.completed_at
    result = store.correct_receipt(user, Correction(receipt_id, field, value))
    column = {'date':'receipt_date','total':'total_amount','vendor':'vendor_name','category':'category'}[field]
    assert getattr(result, column) == expected
    with session_factory() as session:
        row = session.get(Receipt, receipt_id)
        # SQLite returns naive timestamps, PostgreSQL returns aware values.
        actual_completed = row.completed_at
        if actual_completed.tzinfo is None:
            completed = completed.replace(tzinfo=None)
        assert actual_completed == completed
        assert row.status == 'COMPLETED'
        assert row.updated_at.replace(tzinfo=None) > old.replace(tzinfo=None)
        assert row.image_sha256 == '1'.zfill(64)
        if field == 'vendor':
            assert row.vendor_normalized == 'NEWVENDOR'
        assert store.find_vendor_category(user, row.vendor_normalized) is None


def test_owner_isolation_and_missing_ids(store):
    user, receipt_id = saved(store)
    other, _ = saved(store, update=2, chat=99)
    assert [r.id for r in store.recent_receipts(user)] == [receipt_id]
    for target in (receipt_id, uuid4()):
        with pytest.raises(LookupError):
            store.correct_receipt(other if target == receipt_id else user, Correction(target, 'total', '450'))


def test_duplicate_and_vat_rejections_are_atomic(store, session_factory):
    user, first = saved(store)
    _, second = saved(store, update=2, total_amount=Decimal('450'))
    with pytest.raises(DuplicateReceiptError):
        store.correct_receipt(user, Correction(first, 'total', '450'))
    with pytest.raises(ValueError):
        store.correct_receipt(user, Correction(first, 'total', '10'))
    with session_factory() as session:
        assert session.get(Receipt, first).total_amount == Decimal('125.50')
    # Saving the existing value is valid, not a duplicate of itself.
    store.correct_receipt(user, Correction(second, 'total', '450'))


def test_pending_receipt_is_preserved(store, session_factory):
    user, receipt_id = saved(store, status='PENDING_CATEGORY', category=None)
    assert store.recent_receipts(user) == []
    with pytest.raises(ValueError):
        store.correct_receipt(user, Correction(receipt_id, 'category', 'Dining'))
    assert store.get_open_pending(user).receipt_id == receipt_id
    with session_factory() as session:
        assert session.get(Receipt, receipt_id).category is None


@pytest.mark.asyncio
async def test_real_text_routing_and_confirmation(store, session_factory, monkeypatch):
    from app.config import Settings
    monkeypatch.setattr('app.config.get_settings', lambda: Settings.model_construct())
    from app.main import _process_event, WebhookServices
    from app.telegram import TelegramUpdate
    user, receipt_id = saved(store)
    sent, categories = [], []
    async def send(chat_id, message):
        sent.append(message)
    async def category_reply(**kwargs):
        categories.append(kwargs['category'])
    services = WebhookServices(store, SimpleNamespace(process_category_reply=category_reply), SimpleNamespace(send=send))
    for update_id, text in enumerate(['/receipts', f'/edit {receipt_id} total 450', '/typo', 'Dining'], 10):
        event = store.create_webhook_event(update_id=update_id, chat_id=42, kind='text', text=text)
        await _process_event(services, TelegramUpdate(update_id, 42, 'text', text=text), event.id)
    assert str(receipt_id) in sent[0]
    assert 'Receipt corrected' in sent[1] and '450.00' in sent[1]
    assert categories == ['Dining']
    with session_factory() as session:
        assert session.get(Receipt, receipt_id).total_amount == Decimal('450')
        event = session.scalar(select(WebhookEvent).where(WebhookEvent.update_id == 11))
        assert event.status == 'COMPLETED'


@pytest.mark.parametrize('field,existing,value', [
    ('date', {'receipt_date': date(2026, 9, 22)}, '2026-09-22'),
    ('vendor', {'vendor_name': 'Other', 'vendor_normalized': 'OTHER'}, 'Other'),
])
def test_other_duplicate_keys_blocked(store, field, existing, value):
    user, first = saved(store)
    saved(store, update=2, **existing)
    with pytest.raises(DuplicateReceiptError):
        store.correct_receipt(user, Correction(first, field, value))


@pytest.mark.asyncio
async def test_large_receipt_list_is_split_safely(store):
    for index in range(1, 11):
        user, _ = saved(store, update=index, vendor_name='*'*199 + 'A',
                        category='_'*100, total_amount=Decimal(index + 100))
    event = store.create_webhook_event(update_id=100, chat_id=42, kind='text')
    sent = []
    async def send(chat, message):
        sent.append(message)
    await process_command(store, SimpleNamespace(send=send), event_id=event.id, chat_id=42, text='/receipts')
    assert len(sent) > 1
    assert all(len(message) <= 3500 for message in sent)
    assert sum(message.count('ID:') for message in sent) == 10


def test_category_correction_does_not_change_vendor_memory(store, session_factory):
    from app.db import UserVendorMemory
    user, receipt_id = saved(store)
    with session_factory() as session:
        session.add(UserVendorMemory(user_id=user, normalized_name='ACMESUPPLIES',
                                     display_name='Acme Supplies', category='Maintenance'))
        session.commit()
    store.correct_receipt(user, Correction(receipt_id, 'category', 'Dining'))
    assert store.find_vendor_category(user, 'ACMESUPPLIES') == 'Maintenance'
