from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import UserVendorMemory, VendorAlias, Receipt
from app.pipeline import ReceiptPipeline
from app.commands import process_command
from tests.helpers import extraction, FakeVision, FakeStorage, FakeNotifier, image_bytes
from tests.test_corrections import saved


def learn(store, sessions, chat=42, name='STARBUCKS', display='Starbucks', category='Dining'):
    user = store.get_or_create_user(chat)
    with sessions() as session:
        session.add(UserVendorMemory(user_id=user, normalized_name=name, display_name=display, category=category))
        session.commit()
    return user


def test_exact_alias_is_private_and_removable(store, session_factory):
    user = learn(store, session_factory)
    other = learn(store, session_factory, chat=99, category='Business')
    store.vendors.add(user, 'Starbucks #1234', 'Starbucks')
    store.vendors.add(user, 'STARBUCKS 1234', 'starbucks')  # Idempotent normalized name.
    assert store.find_vendor_category(user, 'STARBUCKS1234') == 'Dining'
    assert store.find_vendor_category(user, 'STARBUCKS123') is None
    assert store.find_vendor_category(other, 'STARBUCKS1234') is None
    store.vendors.add(other, 'Starbucks #1234', 'Starbucks')
    assert store.find_vendor_category(other, 'STARBUCKS1234') == 'Business'
    store.vendors.remove(user, 'Starbucks 1234')
    assert store.find_vendor_category(user, 'STARBUCKS1234') is None
    assert store.find_vendor_category(user, 'STARBUCKS') == 'Dining'
    assert store.find_vendor_category(other, 'STARBUCKS1234') == 'Business'


def test_alias_follows_target_category_without_copying_memory(store, session_factory):
    user = learn(store, session_factory)
    store.vendors.add(user, 'SB branch', 'Starbucks')
    with session_factory() as session:
        session.get(UserVendorMemory, (user, 'STARBUCKS')).category = 'Meals'
        session.commit()
        assert session.get(UserVendorMemory, (user, 'SBBRANCH')) is None
    assert store.find_vendor_category(user, 'SBBRANCH') == 'Meals'


def test_reject_conflicts_chains_self_and_unknown_target(store, session_factory):
    user = learn(store, session_factory)
    learn(store, session_factory, name='KEIGO', display='Keigo')
    store.vendors.add(user, 'SB', 'Starbucks')
    for alternate, target in [('Starbucks!', 'STARBUCKS'), ('New', 'Missing'),
                               ('Starbucks', 'Keigo'), ('Other', 'SB'), ('SB', 'Keigo')]:
        with pytest.raises(ValueError):
            store.vendors.add(user, alternate, target)
    assert store.find_vendor_category(user, 'SB') == 'Dining'
    other = store.get_or_create_user(99)
    with pytest.raises(ValueError):
        store.vendors.add(other, 'SB', 'Starbucks')
    with session_factory() as session:
        session.add(VendorAlias(user_id=other, normalized_name='SB', display_name='SB', target_normalized='STARBUCKS'))
        with pytest.raises(IntegrityError):
            session.commit()


def test_pending_receipt_cannot_be_reclassified_by_alias(store, session_factory):
    user = learn(store, session_factory)
    _, receipt_id = saved(store, status='PENDING_CATEGORY', category=None)
    with pytest.raises(ValueError):
        store.vendors.add(user, 'Acme Supplies', 'Starbucks')
    assert store.get_open_pending(user).receipt_id == receipt_id


@pytest.mark.asyncio
async def test_pipeline_inherits_alias_category_preserves_vendor_and_duplicate_guard(store, session_factory):
    user = learn(store, session_factory)
    store.vendors.add(user, 'Starbucks #1234', 'Starbucks')
    vision = FakeVision(extraction().model_copy(update={'vendor_name':'Starbucks #1234'}))
    pipeline = ReceiptPipeline(store=store, vision=vision, storage=FakeStorage(), notifier=FakeNotifier())
    for number in (1, 2):
        event = store.create_webhook_event(update_id=number, chat_id=42, kind='photo')
        await pipeline.process_photo(event_id=event.id, chat_id=42, image_bytes=image_bytes())
    with session_factory() as session:
        rows = list(session.scalars(select(Receipt)))
        assert len(rows) == 1
        assert (rows[0].vendor_name, rows[0].vendor_normalized, rows[0].category, rows[0].status) == (
            'Starbucks #1234', 'STARBUCKS1234', 'Dining', 'COMPLETED')
        receipt_id = rows[0].id
    store.vendors.remove(user, 'Starbucks #1234')
    with session_factory() as session:
        assert session.get(Receipt, receipt_id).category == 'Dining'


@pytest.mark.asyncio
async def test_commands_and_markdown_escaping(store, session_factory):
    user = learn(store, session_factory)
    sent = []
    async def send(chat, message):
        sent.append(message)
    for number, text in enumerate(['/vendors', '/alias SB_* | Starbucks', '/aliases', '/unalias SB_*', '/alias bad', '/vendors nope'], 10):
        event = store.create_webhook_event(update_id=number, chat_id=42, kind='text')
        assert await process_command(store, SimpleNamespace(send=send), event_id=event.id, chat_id=42, text=text)
    assert 'Starbucks' in sent[0] and 'Alias saved' in sent[1]
    assert 'SB\\_\\*' in sent[2]
    assert store.find_vendor_category(user, 'SB') is None
    assert store.vendors.listing(store.get_or_create_user(99)) == 'Learned vendors — page 1\n\nNo entries on this page.'


@pytest.mark.parametrize('value', ['', '***', 'x' * 201, 'A|B'])
def test_invalid_names(store, session_factory, value):
    user = learn(store, session_factory)
    with pytest.raises(ValueError):
        store.vendors.add(user, value, 'Starbucks')


def test_database_rejects_foreign_target_and_lists_pages(store, session_factory):
    user = learn(store, session_factory)
    other = store.get_or_create_user(99)
    with session_factory() as session:
        session.add(VendorAlias(user_id=other, normalized_name='SB', display_name='SB', target_normalized='STARBUCKS'))
        with pytest.raises(IntegrityError):
            session.commit()
    for index in range(11):
        store.vendors.add(user, f'Branch {index:02}', 'Starbucks')
    assert '/aliases 2' in store.vendors.listing(user, aliases=True)
    second = store.vendors.listing(user, aliases=True, page=2)
    assert 'Branch 10' in second and 'Next:' not in second
