from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy.orm import sessionmaker

from app.corrections import Correction
from app.repository import SqlAlchemyReceiptStore
from tests.test_user_migration import legacy_connection, MIGRATIONS, add_receipt, migrate
from tests.test_corrections import saved
from tests.test_audit import events


def test_additive_migration_preserves_data_and_enables_rls(legacy_connection, postgres_schema):
    c = legacy_connection
    receipt_id = add_receipt(c, 42, 900)
    migrate(c)
    for name in ('003_receipt_lifecycle.sql', '004_receipt_review.sql'):
        c.execute((MIGRATIONS / name).read_text())
    before = c.execute('SELECT to_jsonb(r) FROM receipts r').fetchall()
    c.execute((MIGRATIONS / '005_receipt_audit.sql').read_text())
    assert c.execute('SELECT to_jsonb(r) FROM receipts r').fetchall() == before
    assert c.execute('SELECT count(*) FROM receipt_events').fetchone()[0] == 0
    assert c.execute("SELECT relrowsecurity FROM pg_class WHERE oid='receipt_events'::regclass").fetchone()[0]
    store = SqlAlchemyReceiptStore(sessionmaker(bind=postgres_schema, expire_on_commit=False))
    user = store.get_or_create_user(42)
    store.correct_receipt(user, Correction(receipt_id, 'category', 'Dining'))
    assert 'Corrected' in store.receipt_history(user, receipt_id)


def test_concurrent_corrections_keep_a_contiguous_history(postgres_schema):
    from app.db import Base
    Base.metadata.create_all(postgres_schema)
    sessions = sessionmaker(bind=postgres_schema, expire_on_commit=False)
    store = SqlAlchemyReceiptStore(sessions)
    user, receipt_id = saved(store)
    barrier = Barrier(2)
    def change(value):
        barrier.wait(timeout=10)
        store.correct_receipt(user, Correction(receipt_id, 'total', value))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(change, ['200', '300']))
    rows = events(sessions, receipt_id)
    assert len(rows) == 3
    assert rows[1].old_value['total_amount'] == rows[0].new_value['total_amount']
    assert rows[2].old_value['total_amount'] == rows[1].new_value['total_amount']


def test_empty_migration_and_reapply_preserves_table(legacy_connection):
    import psycopg
    import pytest
    c = legacy_connection
    migrate(c)
    for name in ('003_receipt_lifecycle.sql', '004_receipt_review.sql', '005_receipt_audit.sql'):
        c.execute((MIGRATIONS / name).read_text())
    with pytest.raises(psycopg.errors.DuplicateTable):
        c.execute((MIGRATIONS / '005_receipt_audit.sql').read_text())
    c.execute('ROLLBACK')
    assert c.execute('SELECT count(*) FROM receipt_events').fetchone()[0] == 0
