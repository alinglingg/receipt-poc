from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.repository import SqlAlchemyReceiptStore
from tests.test_user_migration import legacy_connection, MIGRATIONS, add_receipt, migrate


def test_dashboard_migration_preserves_data_and_enables_rls(legacy_connection, postgres_schema):
    c = legacy_connection
    add_receipt(c, 42, 900)
    migrate(c)
    for name in ('003_receipt_lifecycle.sql', '004_receipt_review.sql', '005_receipt_audit.sql', '006_vendor_aliases.sql'):
        c.execute((MIGRATIONS / name).read_text())
    before = c.execute('SELECT to_jsonb(r) FROM receipts r').fetchall()
    c.execute((MIGRATIONS / '007_web_dashboard.sql').read_text())
    assert c.execute('SELECT to_jsonb(r) FROM receipts r').fetchall() == before
    assert c.execute("SELECT relrowsecurity FROM pg_class WHERE oid='browser_tokens'::regclass").fetchone()[0]
    store = SqlAlchemyReceiptStore(sessionmaker(bind=postgres_schema, expire_on_commit=False))
    user = store.get_or_create_user(42)
    assert store.dashboard.authenticate(store.dashboard.exchange_login(store.dashboard.issue_login(user))) == user


def test_concurrent_login_has_exactly_one_winner(postgres_schema):
    Base.metadata.create_all(postgres_schema)
    store = SqlAlchemyReceiptStore(sessionmaker(bind=postgres_schema, expire_on_commit=False))
    token = store.dashboard.issue_login(store.get_or_create_user(42))
    barrier = Barrier(2)
    def exchange(_):
        barrier.wait(timeout=10)
        try:
            store.dashboard.exchange_login(token)
            return 'accepted'
        except LookupError:
            return 'rejected'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(exchange, range(2))) == ['accepted', 'rejected']
