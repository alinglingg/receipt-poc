from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, event
from sqlalchemy.exc import IntegrityError

from app.corrections import Correction
from app.db import Receipt, ReceiptEvent
from app.pipeline import DuplicateReceiptError
from tests.test_corrections import saved


def events(session_factory, receipt_id):
    with session_factory() as session:
        return list(session.scalars(select(ReceiptEvent).where(ReceiptEvent.receipt_id == receipt_id)
                    .order_by(ReceiptEvent.created_at, ReceiptEvent.id)))


def test_creation_and_correction_values_and_noops(store, session_factory):
    user, receipt_id = saved(store)
    initial = events(session_factory, receipt_id)
    assert len(initial) == 1 and initial[0].event_type == 'CREATED'
    assert initial[0].old_value is None
    assert initial[0].new_value['total_amount'] == '125.50'
    assert initial[0].new_value['receipt_date'] == '2026-09-10'
    assert 'image_path' not in initial[0].new_value
    store.correct_receipt(user, Correction(receipt_id, 'total', '450'))
    store.correct_receipt(user, Correction(receipt_id, 'total', '450.00'))
    rows = events(session_factory, receipt_id)
    assert len(rows) == 2
    assert rows[1].old_value == {'total_amount':'125.50'}
    assert rows[1].new_value == {'total_amount':'450.00'}
    assert '125.50 → 450.00' in store.receipt_history(user, receipt_id)


def test_review_and_assignment_atomic_history(store, session_factory):
    user, receipt_id = saved(store, status='NEEDS_REVIEW', category=None, review_reason='MEDIUM_CONFIDENCE')
    store.confirm_review(user, receipt_id, date(2026, 9, 11))
    store.resolve_category(user, receipt_id, 'Dining')
    rows = events(session_factory, receipt_id)
    assert [row.event_type for row in rows] == ['CREATED', 'REVIEW_CONFIRMED', 'CATEGORY_ASSIGNED']
    assert rows[1].old_value['receipt_date'] == '2026-09-10'
    assert rows[1].new_value['status'] == 'PENDING_CATEGORY'
    assert rows[2].old_value['category'] is None
    assert rows[2].new_value['category'] == 'Dining'
    with pytest.raises(LookupError):
        store.resolve_category(user, receipt_id, 'Dining')
    assert len(events(session_factory, receipt_id)) == 3


def test_rejected_corrections_and_owner_isolation(store, session_factory):
    user, receipt_id = saved(store)
    saved(store, update=2, total_amount=Decimal('450'))
    other = store.get_or_create_user(99)
    with pytest.raises(DuplicateReceiptError):
        store.correct_receipt(user, Correction(receipt_id, 'total', '450'))
    with pytest.raises(LookupError):
        store.correct_receipt(other, Correction(receipt_id, 'total', '200'))
    for target in (receipt_id, uuid4()):
        with pytest.raises(LookupError):
            store.receipt_history(other, target)
    assert len(events(session_factory, receipt_id)) == 1
    with session_factory() as session:
        session.add(ReceiptEvent(receipt_id=receipt_id, user_id=other, event_type='CORRECTED', new_value={}))
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize('operation', ['create', 'correct', 'confirm', 'category'])
def test_audit_failure_rolls_back_receipt_change(store, session_factory, operation):
    user, receipt_id = saved(store, status='NEEDS_REVIEW' if operation == 'confirm' else
                             ('PENDING_CATEGORY' if operation == 'category' else 'COMPLETED'),
                             category=None if operation == 'category' else 'Maintenance',
                             review_reason='MEDIUM_CONFIDENCE' if operation == 'confirm' else None)
    def fail(*args):
        raise RuntimeError('Audit insert failed')
    event.listen(ReceiptEvent, 'before_insert', fail)
    try:
        with pytest.raises(RuntimeError):
            if operation == 'create':
                saved(store, update=2, total_amount=Decimal('450'))
            elif operation == 'correct':
                store.correct_receipt(user, Correction(receipt_id, 'total', '450'))
            elif operation == 'confirm':
                store.confirm_review(user, receipt_id)
            else:
                store.resolve_category(user, receipt_id, 'Dining')
    finally:
        event.remove(ReceiptEvent, 'before_insert', fail)
    assert len(events(session_factory, receipt_id)) == 1
    with session_factory() as session:
        rows = list(session.scalars(select(Receipt)))
        assert len(rows) == 1
        assert rows[0].total_amount == Decimal('125.50')
        if operation == 'confirm':
            assert rows[0].status == 'NEEDS_REVIEW'
        if operation == 'category':
            assert rows[0].category is None
            assert store.get_open_pending(user) is not None
            assert store.find_vendor_category(user, 'ACMESUPPLIES') is None


def test_retry_preserves_existing_behavior(store, session_factory):
    user, receipt_id = saved(store, status='NEEDS_REVIEW', review_reason='MEDIUM_CONFIDENCE')
    store.retry_review(user, receipt_id)
    assert events(session_factory, receipt_id) == []
    assert store.get_open_pending(user) is None


def test_history_pagination_and_escaping(store):
    user, receipt_id = saved(store)
    for number in range(6):
        store.correct_receipt(user, Correction(receipt_id, 'category', f'*Cat_{number}'))
    first = store.receipt_history(user, receipt_id)
    assert f'/history {receipt_id} 2' in first
    assert '\\*Cat\\_5' in first
    second = store.receipt_history(user, receipt_id, 2)
    assert 'Created' in second and 'Next:' not in second
    with pytest.raises(ValueError):
        store.receipt_history(user, receipt_id, 0)


@pytest.mark.asyncio
async def test_history_command_does_not_resolve_pending(store):
    from types import SimpleNamespace
    from app.commands import process_command
    user, receipt_id = saved(store, status='PENDING_CATEGORY', category=None)
    messages = []
    async def send(chat, message):
        messages.append(message)
    event_row = store.create_webhook_event(update_id=10, chat_id=42, kind='text')
    assert await process_command(store, SimpleNamespace(send=send), event_id=event_row.id,
                                 chat_id=42, text=f'/history {receipt_id}')
    assert 'Created' in messages[0]
    assert store.get_open_pending(user).receipt_id == receipt_id
