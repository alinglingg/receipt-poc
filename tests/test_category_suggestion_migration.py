from tests.test_user_migration import legacy_connection, MIGRATIONS, add_receipt, migrate


def test_suggestions_migration_preserves_existing_pending_receipts(legacy_connection):
    c = legacy_connection
    add_receipt(c, 42, 900, category=None, status='PENDING_CATEGORY')
    migrate(c)
    for name in ('003_receipt_lifecycle.sql', '004_receipt_review.sql',
                 '005_receipt_audit.sql', '006_vendor_aliases.sql', '007_web_dashboard.sql'):
        c.execute((MIGRATIONS / name).read_text())
    before = c.execute('SELECT to_jsonb(r) FROM receipts r').fetchall()
    c.execute((MIGRATIONS / '008_category_suggestions.sql').read_text())
    assert c.execute("SELECT to_jsonb(r) - 'suggested_category' FROM receipts r").fetchall() == before
    assert c.execute('SELECT suggested_category FROM receipts').fetchone() == (None,)
