from datetime import date
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db import PendingConversation, Receipt, User, UserVendorMemory, VendorMemory
from app.pipeline import DuplicateReceiptError, ReceiptDraft
from app.repository import SqlAlchemyReceiptStore


def draft(event_id, user_id, chat_id=42) -> ReceiptDraft:
    return ReceiptDraft(
        event_id=event_id,
        chat_id=chat_id,
        user_id=user_id,
        vendor_name="Acme Supplies",
        vendor_normalized="ACMESUPPLIES",
        receipt_date=date(2026, 9, 10),
        total_amount=Decimal("125.50"),
        vat_amount=Decimal("15.50"),
        category="Maintenance",
        confidence="High",
        status="COMPLETED",
        image_path="42/receipt.jpg",
        image_sha256="a" * 64,
    )


def test_webhook_update_id_is_idempotent(store: SqlAlchemyReceiptStore) -> None:
    first = store.create_webhook_event(update_id=100, chat_id=42, kind="photo", file_id="file-1")
    second = store.create_webhook_event(update_id=100, chat_id=42, kind="photo", file_id="file-1")

    assert first is not None
    assert second is None


def test_database_constraint_catches_duplicate_race(store: SqlAlchemyReceiptStore) -> None:
    first_event = store.create_webhook_event(update_id=101, chat_id=42, kind="photo")
    second_event = store.create_webhook_event(update_id=102, chat_id=42, kind="photo")
    assert first_event is not None and second_event is not None

    user_id = store.get_or_create_user(42)
    store.create_receipt(draft(first_event.id, user_id))
    with pytest.raises(DuplicateReceiptError):
        store.create_receipt(draft(second_event.id, user_id))


def test_users_are_created_once_per_chat_including_group_chats(store, session_factory):
    first = store.get_or_create_user(42)
    assert store.get_or_create_user(42) == first
    assert store.get_or_create_user(-10042) != first
    with session_factory() as session:
        assert session.scalar(select(func.count(User.id))) == 2


def create_pending(store, chat_id, update_id, **overrides):
    user_id = store.get_or_create_user(chat_id)
    event = store.create_webhook_event(update_id=update_id, chat_id=chat_id, kind="photo")
    receipt_id = store.create_receipt(replace(
        draft(event.id, user_id, chat_id), category=None, status="PENDING_CATEGORY", **overrides,
    ))
    store.create_pending_conversation(user_id, receipt_id)
    return user_id, receipt_id


def test_same_receipt_is_allowed_for_different_users(store):
    user_a, _ = create_pending(store, 42, 200)
    user_b = store.get_or_create_user(43)
    args = ("ACMESUPPLIES", date(2026, 9, 10), Decimal("125.50"))
    assert store.is_duplicate(user_a, *args)
    assert not store.is_duplicate(user_b, *args)
    create_pending(store, 43, 201)
    assert store.is_duplicate(user_b, *args)


def test_vendor_learning_and_pending_receipts_are_isolated(store, session_factory):
    user_a, receipt_a = create_pending(store, 42, 210)
    user_b, receipt_b = create_pending(store, 43, 211)
    assert store.get_open_pending(user_a).receipt_id == receipt_a
    assert store.get_open_pending(user_b).receipt_id == receipt_b
    assert store.get_open_pending(user_a).user_id == user_a

    store.resolve_category(user_a, receipt_a, "Meals")
    assert store.get_open_pending(user_a) is None
    assert store.get_open_pending(user_b).receipt_id == receipt_b
    assert store.find_vendor_category(user_a, "ACMESUPPLIES") == "Meals"
    assert store.find_vendor_category(user_b, "ACMESUPPLIES") is None
    store.resolve_category(user_b, receipt_b, "Client Entertainment")
    assert store.find_vendor_category(user_b, "ACMESUPPLIES") == "Client Entertainment"
    assert store.find_vendor_category(user_a, "ACMESUPPLIES") == "Meals"

    # Existing memory updates remain scoped too.
    _, next_receipt = create_pending(store, 42, 212, total_amount=Decimal("126.00"))
    store.resolve_category(user_a, next_receipt, "Travel")
    assert store.find_vendor_category(user_a, "ACMESUPPLIES") == "Travel"
    assert store.find_vendor_category(user_b, "ACMESUPPLIES") == "Client Entertainment"
    with session_factory() as session:
        assert session.get(Receipt, receipt_a).status == "COMPLETED"
        assert session.scalar(select(func.count()).select_from(UserVendorMemory)) == 2


def test_legacy_global_vendor_memory_is_never_used(store, session_factory):
    with session_factory() as session:
        session.add(VendorMemory(normalized_name="ACMESUPPLIES", display_name="Acme Supplies", category="Global"))
        session.commit()
    user_id = store.get_or_create_user(42)
    assert store.find_vendor_category(user_id, "ACMESUPPLIES") is None


def test_another_user_cannot_resolve_or_attach_a_pending_receipt(store, session_factory):
    user_a, receipt_a = create_pending(store, 42, 220)
    user_b = store.get_or_create_user(43)
    assert store.get_open_pending(user_b) is None
    with pytest.raises(LookupError):
        store.resolve_category(user_b, receipt_a, "Wrong")
    with pytest.raises(LookupError):
        store.create_pending_conversation(user_b, receipt_a)
    assert store.get_open_pending(user_a).receipt_id == receipt_a
    with session_factory() as session:
        assert session.get(Receipt, receipt_a).category is None
        assert session.scalar(select(func.count()).select_from(UserVendorMemory)) == 0


def test_receipt_owner_must_match_chat_and_event(store):
    user_a = store.get_or_create_user(42)
    user_b = store.get_or_create_user(43)
    event_a = store.create_webhook_event(update_id=230, chat_id=42, kind="photo")
    with pytest.raises(LookupError):
        store.create_receipt(draft(event_a.id, user_b, 42))
    with pytest.raises(LookupError):
        store.create_receipt(draft(event_a.id, user_a, 43))
    with pytest.raises(LookupError):
        store.create_receipt(draft(event_a.id, user_b, 43))


def test_database_rejects_pending_owner_mismatch(store, session_factory):
    user_a = store.get_or_create_user(42)
    user_b = store.get_or_create_user(43)
    event_a = store.create_webhook_event(update_id=240, chat_id=42, kind="photo")
    receipt_a = store.create_receipt(draft(event_a.id, user_a))
    with session_factory() as session, pytest.raises(IntegrityError):
        session.add(PendingConversation(user_id=user_b, chat_id=43, receipt_id=receipt_a))
        session.commit()


def test_database_rejects_receipt_user_chat_mismatch(store, session_factory):
    user_a = store.get_or_create_user(42)
    store.get_or_create_user(43)
    event_a = store.create_webhook_event(update_id=241, chat_id=42, kind="photo")
    receipt_a = store.create_receipt(draft(event_a.id, user_a))
    with session_factory() as session, pytest.raises(IntegrityError):
        session.get(Receipt, receipt_a).chat_id = 43
        session.commit()


def test_non_duplicate_constraint_failure_is_not_misreported(store):
    user_a = store.get_or_create_user(42)
    event_a = store.create_webhook_event(update_id=250, chat_id=42, kind="photo")
    with pytest.raises(IntegrityError):
        store.create_receipt(replace(draft(event_a.id, user_a), status="INVALID"))
