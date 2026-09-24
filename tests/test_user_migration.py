"""Run the actual SQL migrations only against an explicitly selected test database."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import psycopg
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.db import Receipt, User
from app.pipeline import DuplicateReceiptError
from app.repository import SqlAlchemyReceiptStore


MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


@pytest.fixture
def legacy_connection(postgres_schema):
    pooled = postgres_schema.raw_connection()
    connection = pooled.driver_connection
    connection.autocommit = True
    try:
        baseline = (MIGRATIONS / "001_init.sql").read_text()
        if not connection.execute("SELECT 1 FROM pg_available_extensions WHERE name='pgcrypto'").fetchone():
            # Minimal PostgreSQL distributions omit contrib extensions. All table DDL
            # still runs unchanged; these schemas only need the built-in UUID function.
            assert connection.execute("SELECT pg_catalog.gen_random_uuid()").fetchone()[0]
            baseline = baseline.replace("CREATE EXTENSION IF NOT EXISTS pgcrypto;", "")
        connection.execute(baseline)
        yield connection
    finally:
        connection.execute("ROLLBACK")
        pooled.close()


def migrate(connection):
    connection.execute((MIGRATIONS / "002_user_scoping.sql").read_text())


def add_receipt(connection, chat_id, update_id, *, vendor="ACME", category="Meals",
                total="125.50", status="COMPLETED"):
    event_id = connection.execute(
        "INSERT INTO webhook_events (update_id,chat_id,kind,status) VALUES (%s,%s,'photo',%s) RETURNING id",
        (update_id, chat_id, status),
    ).fetchone()[0]
    return connection.execute(
        """INSERT INTO receipts (event_id,chat_id,vendor_name,vendor_normalized,receipt_date,
               total_amount,vat_amount,category,confidence,status,image_path,image_sha256)
           VALUES (%s,%s,%s,%s,'2026-09-10',%s,15.50,%s,'High',%s,%s,%s) RETURNING id""",
        (event_id, chat_id, vendor, vendor, Decimal(total), category, status,
         f"{chat_id}/{event_id}.jpg", "a" * 64),
    ).fetchone()[0]


def snapshot(connection, table):
    # table names are fixed test constants, never supplied by a user.
    return [row[0] for row in connection.execute(f"SELECT to_jsonb(t) - 'user_id' FROM {table} t ORDER BY 1")]


def test_migration_backfills_ownership_without_changing_legacy_data(legacy_connection):
    connection = legacy_connection
    add_receipt(connection, 42, 1)
    pending_id = add_receipt(connection, 43, 2, category=None, status="PENDING_CATEGORY")
    connection.execute("INSERT INTO pending_conversations (chat_id,receipt_id) VALUES (43,%s)", (pending_id,))
    connection.execute("INSERT INTO webhook_events (update_id,chat_id,kind) VALUES (3,-10044,'text')")
    connection.execute("INSERT INTO vendor_memory (normalized_name,display_name,category) VALUES ('LEGACY','Legacy','Global')")
    connection.execute("INSERT INTO processing_attempts (event_id,stage,success) SELECT event_id,'vision',true FROM receipts")
    tables = ("receipts", "pending_conversations", "webhook_events", "vendor_memory", "processing_attempts")
    before = {table: snapshot(connection, table) for table in tables}

    migrate(connection)

    assert {table: snapshot(connection, table) for table in tables} == before
    assert connection.execute("SELECT count(*) FROM users").fetchone()[0] == 3
    assert connection.execute("SELECT count(*) FROM receipts r JOIN users u ON u.id=r.user_id WHERE r.chat_id=u.telegram_chat_id").fetchone()[0] == 2
    assert connection.execute("SELECT p.user_id=r.user_id FROM pending_conversations p JOIN receipts r ON r.id=p.receipt_id").fetchone()[0]


def test_migration_seeds_only_unambiguous_owned_vendor_categories(legacy_connection):
    connection = legacy_connection
    add_receipt(connection, 42, 10, category="Meals")
    add_receipt(connection, 43, 11, category="Client Entertainment")
    add_receipt(connection, 42, 12, vendor="AMBIGUOUS", category="Meals")
    add_receipt(connection, 42, 13, vendor="AMBIGUOUS", category="Travel", total="126.00")
    add_receipt(connection, 42, 14, vendor="PENDING", category="Unconfirmed", status="PENDING_CATEGORY")
    add_receipt(connection, 42, 15, vendor="BLANK", category=" ")
    connection.execute("INSERT INTO vendor_memory (normalized_name,display_name,category) VALUES ('UNSEEN','Unseen','Global')")
    migrate(connection)
    memories = connection.execute("""SELECT u.telegram_chat_id,m.normalized_name,m.category
        FROM user_vendor_memory m JOIN users u ON u.id=m.user_id ORDER BY 1,2""").fetchall()
    assert memories == [(42, "ACME", "Meals"), (43, "ACME", "Client Entertainment")]
    assert connection.execute("SELECT count(*) FROM vendor_memory").fetchone()[0] == 1


@pytest.mark.parametrize("mismatch", ["event", "pending"])
def test_inconsistent_legacy_ownership_aborts_without_partial_migration(legacy_connection, mismatch):
    connection = legacy_connection
    receipt_id = add_receipt(connection, 42, 20)
    if mismatch == "event":
        connection.execute("UPDATE webhook_events SET chat_id=43")
    else:
        connection.execute("INSERT INTO pending_conversations (chat_id,receipt_id) VALUES (43,%s)", (receipt_id,))
    before = snapshot(connection, "receipts")
    with pytest.raises(psycopg.errors.RaiseException, match="Ownership backfill aborted"):
        migrate(connection)
    connection.execute("ROLLBACK")
    assert snapshot(connection, "receipts") == before
    assert connection.execute("SELECT to_regclass('users')").fetchone()[0] is None
    assert connection.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='receipts' AND column_name='user_id'").fetchone()[0] == 0


def test_empty_migration_is_safe_and_new_tables_are_backend_only(legacy_connection):
    connection = legacy_connection
    migrate(connection)
    assert connection.execute("SELECT count(*) FROM users").fetchone()[0] == 0
    flags = connection.execute("""SELECT relname,relrowsecurity FROM pg_class
        WHERE relnamespace=current_schema()::regnamespace AND relname IN ('users','user_vendor_memory') ORDER BY relname""").fetchall()
    assert flags == [("user_vendor_memory", True), ("users", True)]
    with pytest.raises(psycopg.errors.DuplicateTable):
        migrate(connection)
    connection.execute("ROLLBACK")
    assert connection.execute("SELECT count(*) FROM users").fetchone()[0] == 0


def test_migrated_database_enforces_owner_and_pending_constraints(legacy_connection):
    connection = legacy_connection
    receipt_a = add_receipt(connection, 42, 30, category=None, status="PENDING_CATEGORY")
    receipt_b = add_receipt(connection, 43, 31, total="128.00", category=None, status="PENDING_CATEGORY")
    second_a = add_receipt(connection, 42, 32, total="126.00", category=None, status="PENDING_CATEGORY")
    migrate(connection)
    user_a = connection.execute("SELECT user_id FROM receipts WHERE id=%s", (receipt_a,)).fetchone()[0]
    user_b = connection.execute("SELECT user_id FROM receipts WHERE id=%s", (receipt_b,)).fetchone()[0]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        connection.execute("UPDATE receipts SET user_id=%s WHERE id=%s", (user_b, receipt_a))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        connection.execute("INSERT INTO pending_conversations (chat_id,user_id,receipt_id) VALUES (43,%s,%s)", (user_b, receipt_a))
    with pytest.raises(psycopg.errors.NotNullViolation):
        connection.execute("UPDATE receipts SET user_id=NULL WHERE id=%s", (receipt_a,))
    connection.execute("INSERT INTO pending_conversations (chat_id,user_id,receipt_id) VALUES (42,%s,%s)", (user_a, receipt_a))
    connection.execute("INSERT INTO pending_conversations (chat_id,user_id,receipt_id) VALUES (43,%s,%s)", (user_b, receipt_b))
    with pytest.raises(psycopg.errors.UniqueViolation):
        connection.execute("INSERT INTO pending_conversations (chat_id,user_id,receipt_id) VALUES (42,%s,%s)", (user_a, second_a))


def test_existing_pending_receipt_resumes_after_migration(legacy_connection, postgres_schema):
    receipt_id = add_receipt(legacy_connection, 42, 40, category=None, status="PENDING_CATEGORY")
    legacy_connection.execute("INSERT INTO pending_conversations (chat_id,receipt_id) VALUES (42,%s)", (receipt_id,))
    migrate(legacy_connection)
    legacy_connection.execute((MIGRATIONS / "003_receipt_lifecycle.sql").read_text())
    legacy_connection.execute((MIGRATIONS / "004_receipt_review.sql").read_text())
    store = SqlAlchemyReceiptStore(sessionmaker(bind=postgres_schema, expire_on_commit=False))
    user_id = store.get_or_create_user(42)
    assert store.get_open_pending(user_id).receipt_id == receipt_id
    store.resolve_category(user_id, receipt_id, "Meals")
    assert store.get_open_pending(user_id) is None
    assert store.find_vendor_category(user_id, "ACME") == "Meals"
    assert legacy_connection.execute("SELECT status FROM receipts WHERE id=%s", (receipt_id,)).fetchone()[0] == "COMPLETED"


def test_concurrent_first_messages_create_one_user(legacy_connection, postgres_schema):
    migrate(legacy_connection)
    legacy_connection.execute((MIGRATIONS / "003_receipt_lifecycle.sql").read_text())
    legacy_connection.execute((MIGRATIONS / "004_receipt_review.sql").read_text())
    factory = sessionmaker(bind=postgres_schema, expire_on_commit=False)
    store = SqlAlchemyReceiptStore(factory)
    barrier = Barrier(8)

    def resolve(_):
        barrier.wait(timeout=10)
        return store.get_or_create_user(42)

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(resolve, range(8)))
    assert len(set(ids)) == 1
    with factory() as session:
        assert session.scalar(select(func.count(User.id))) == 1


@pytest.mark.parametrize("changed_total", [False, True])
def test_concurrent_receipts_are_deduplicated_on_migrated_database(legacy_connection, postgres_schema, changed_total):
    # Use a backfilled receipt as the template, with a new date to avoid its duplicate key.
    original = add_receipt(legacy_connection, 42, 50)
    migrate(legacy_connection)
    legacy_connection.execute((MIGRATIONS / "003_receipt_lifecycle.sql").read_text())
    legacy_connection.execute((MIGRATIONS / "004_receipt_review.sql").read_text())
    factory = sessionmaker(bind=postgres_schema, expire_on_commit=False)
    store = SqlAlchemyReceiptStore(factory)
    from app.pipeline import ReceiptDraft
    from datetime import date

    user_id = store.get_or_create_user(42)
    events = [store.create_webhook_event(update_id=n, chat_id=42, kind="photo") for n in (51, 52)]
    draft = ReceiptDraft(event_id=events[0].id, chat_id=42, user_id=user_id,
                         vendor_name="ACME", vendor_normalized="ACME", receipt_date=date(2026, 9, 11),
                         total_amount=Decimal("125.50"), vat_amount=None, category="Meals",
                         confidence="High", status="COMPLETED", image_path="42/new.jpg", image_sha256="b" * 64)
    barrier = Barrier(2)

    def save(event):
        barrier.wait(timeout=10)
        try:
            store.create_receipt(replace(draft, event_id=event.id, total_amount=Decimal("7500") if changed_total and event.id == events[1].id else draft.total_amount))
            return "saved"
        except DuplicateReceiptError:
            return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, events))
    assert sorted(results) == ["duplicate", "saved"]
    with factory() as session:
        assert session.scalar(select(func.count(Receipt.id)).where(Receipt.id != original)) == 1
