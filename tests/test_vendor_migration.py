from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg
import pytest
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.repository import SqlAlchemyReceiptStore
from tests.test_user_migration import legacy_connection, MIGRATIONS, add_receipt, migrate
from tests.test_vendors import learn


def test_migration_preserves_receipts_memory_and_enables_rls(legacy_connection, postgres_schema):
    c = legacy_connection
    add_receipt(c, 42, 900)
    migrate(c)
    for name in ('003_receipt_lifecycle.sql', '004_receipt_review.sql', '005_receipt_audit.sql'):
        c.execute((MIGRATIONS / name).read_text())
    before = c.execute('SELECT to_jsonb(r) FROM receipts r').fetchall()
    memory = c.execute('SELECT to_jsonb(m) FROM user_vendor_memory m').fetchall()
    c.execute((MIGRATIONS / '006_vendor_aliases.sql').read_text())
    assert c.execute('SELECT to_jsonb(r) FROM receipts r').fetchall() == before
    assert c.execute('SELECT to_jsonb(m) FROM user_vendor_memory m').fetchall() == memory
    assert c.execute("SELECT relrowsecurity FROM pg_class WHERE oid='vendor_aliases'::regclass").fetchone()[0]
    assert c.execute('SELECT count(*) FROM vendor_aliases').fetchone()[0] == 0
    store = SqlAlchemyReceiptStore(sessionmaker(bind=postgres_schema, expire_on_commit=False))
    user = store.get_or_create_user(42)
    store.vendors.add(user, 'ACME Branch', 'ACME')
    assert store.find_vendor_category(user, 'ACMEBRANCH') == 'Meals'
    with pytest.raises(psycopg.errors.DuplicateTable):
        c.execute((MIGRATIONS / '006_vendor_aliases.sql').read_text())
    c.execute('ROLLBACK')
    assert c.execute('SELECT count(*) FROM vendor_aliases').fetchone()[0] == 1


def test_concurrent_alias_conflict_does_not_overwrite(postgres_schema):
    Base.metadata.create_all(postgres_schema)
    sessions = sessionmaker(bind=postgres_schema, expire_on_commit=False)
    store = SqlAlchemyReceiptStore(sessions)
    user = learn(store, sessions)
    learn(store, sessions, name='KEIGO', display='Keigo', category='Meals')
    barrier = Barrier(2)
    def add(target):
        barrier.wait(timeout=10)
        try:
            store.vendors.add(user, 'Branch', target)
            return 'saved'
        except ValueError:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(add, ['Starbucks', 'Keigo'])) == ['conflict', 'saved']
    assert store.find_vendor_category(user, 'BRANCH') in {'Dining', 'Meals'}
