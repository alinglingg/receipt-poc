"""Phase 4 migration and concurrency tests require a disposable PostgreSQL DB."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from threading import Barrier

import psycopg
import pytest
from sqlalchemy.orm import sessionmaker

from app.repository import SqlAlchemyReceiptStore
from app.pipeline import PendingReceiptError
from tests.test_repository import draft
from tests.test_user_migration import legacy_connection, MIGRATIONS, add_receipt, migrate


def migrate_review(connection):
    connection.execute((MIGRATIONS/'003_receipt_lifecycle.sql').read_text())
    connection.execute((MIGRATIONS/'004_receipt_review.sql').read_text())
    connection.execute((MIGRATIONS/'005_receipt_audit.sql').read_text())


def test_review_migration_preserves_receipts_and_pending_categories(legacy_connection,postgres_schema):
    c=legacy_connection
    receipt=add_receipt(c,42,900,category=None,status='PENDING_CATEGORY')
    c.execute('INSERT INTO pending_conversations (chat_id,receipt_id) VALUES (42,%s)',(receipt,))
    migrate(c)
    c.execute((MIGRATIONS/'003_receipt_lifecycle.sql').read_text())
    before=c.execute("SELECT to_jsonb(r) FROM receipts r").fetchall()
    pending_before=c.execute("SELECT to_jsonb(p) FROM pending_conversations p").fetchall()
    c.execute((MIGRATIONS/'004_receipt_review.sql').read_text())
    assert c.execute("SELECT to_jsonb(r) - 'raw_date_text' FROM receipts r").fetchall() == before
    assert c.execute("SELECT to_jsonb(p) FROM pending_conversations p").fetchall() == pending_before
    # Runtime repository requires the current additive audit schema.
    legacy_connection.execute((MIGRATIONS/'005_receipt_audit.sql').read_text())
    store=SqlAlchemyReceiptStore(sessionmaker(bind=postgres_schema,expire_on_commit=False))
    user=store.get_or_create_user(42)
    store.resolve_category(user,receipt,'Dining')
    assert c.execute('SELECT status,category FROM receipts WHERE id=%s',(receipt,)).fetchone() == ('COMPLETED','Dining')
    c.execute("UPDATE webhook_events SET status='NEEDS_REVIEW'")
    with pytest.raises(psycopg.errors.CheckViolation): c.execute("UPDATE webhook_events SET status='INVALID'")
    with pytest.raises(psycopg.errors.DuplicateColumn): c.execute((MIGRATIONS/'004_receipt_review.sql').read_text())
    c.execute('ROLLBACK')
    assert c.execute('SELECT count(*) FROM receipts').fetchone()[0] == 1


def test_empty_review_migration(legacy_connection):
    migrate(legacy_connection)
    migrate_review(legacy_connection)
    assert legacy_connection.execute('SELECT count(*) FROM receipts').fetchone()[0] == 0


def test_concurrent_reviews_create_one_pending_receipt(legacy_connection,postgres_schema):
    migrate(legacy_connection)
    migrate_review(legacy_connection)
    store=SqlAlchemyReceiptStore(sessionmaker(bind=postgres_schema,expire_on_commit=False))
    user=store.get_or_create_user(42)
    events=[store.create_webhook_event(update_id=n,chat_id=42,kind='photo') for n in (950,951)]
    barrier=Barrier(2)
    def save(index):
        barrier.wait(timeout=10)
        try:
            store.create_receipt(replace(draft(events[index].id,user),status='NEEDS_REVIEW',
                total_amount=Decimal(200+index),image_sha256=str(index)*64,review_reason='MEDIUM_CONFIDENCE'))
            return 'saved'
        except PendingReceiptError:
            return 'pending'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save,range(2))) == ['pending','saved']
    assert legacy_connection.execute('SELECT count(*) FROM receipts').fetchone()[0] == 1
    assert legacy_connection.execute("SELECT count(*) FROM pending_conversations WHERE status='OPEN'").fetchone()[0] == 1


def test_concurrent_confirmations_resolve_once(legacy_connection,postgres_schema):
    migrate(legacy_connection)
    migrate_review(legacy_connection)
    store=SqlAlchemyReceiptStore(sessionmaker(bind=postgres_schema,expire_on_commit=False))
    user=store.get_or_create_user(42)
    event=store.create_webhook_event(update_id=960,chat_id=42,kind='photo')
    receipt=store.create_receipt(replace(draft(event.id,user),status='NEEDS_REVIEW',review_reason='MEDIUM_CONFIDENCE'))
    barrier=Barrier(2)
    def confirm(_):
        barrier.wait(timeout=10)
        try:
            store.confirm_review(user,receipt)
            return 'confirmed'
        except LookupError:
            return 'resolved'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(confirm,range(2))) == ['confirmed','resolved']
    assert legacy_connection.execute('SELECT status FROM receipts WHERE id=%s',(receipt,)).fetchone()[0] == 'COMPLETED'
