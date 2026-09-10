from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.pipeline import DuplicateReceiptError, ReceiptDraft
from app.repository import SqlAlchemyReceiptStore


@pytest.fixture
def store() -> SqlAlchemyReceiptStore:
    engine = create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return SqlAlchemyReceiptStore(sessionmaker(bind=engine, expire_on_commit=False))


def draft(event_id, chat_id=42) -> ReceiptDraft:
    return ReceiptDraft(
        event_id=event_id,
        chat_id=chat_id,
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

    store.create_receipt(draft(first_event.id))
    with pytest.raises(DuplicateReceiptError):
        store.create_receipt(draft(second_event.id))
