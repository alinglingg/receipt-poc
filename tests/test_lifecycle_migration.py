"""Phase 3 migration checks against a disposable PostgreSQL schema only."""
import psycopg
import pytest

from tests.test_user_migration import legacy_connection, MIGRATIONS, add_receipt, migrate


def lifecycle(connection):
    connection.execute((MIGRATIONS / '003_receipt_lifecycle.sql').read_text())


def test_lifecycle_migration_preserves_values_and_backfills_evidence(legacy_connection):
    c = legacy_connection
    completed = add_receipt(c,42,800)
    other = add_receipt(c,42,802,total='200.00')
    pending = add_receipt(c,43,801,category=None,status='PENDING_CATEGORY')
    c.execute("INSERT INTO pending_conversations (chat_id,receipt_id) VALUES (43,%s)",(pending,))
    c.execute("INSERT INTO processing_attempts (event_id,stage,success,created_at) SELECT event_id,'vision',true,created_at FROM receipts WHERE id=%s",(completed,))
    migrate(c)
    before = c.execute('SELECT id,event_id,chat_id,user_id,vendor_name,vendor_normalized,receipt_date,total_amount,vat_amount,category,confidence,status,image_path,image_sha256,created_at FROM receipts ORDER BY id').fetchall()
    lifecycle(c)
    assert c.execute('SELECT id,event_id,chat_id,user_id,vendor_name,vendor_normalized,receipt_date,total_amount,vat_amount,category,confidence,status,image_path,image_sha256,created_at FROM receipts ORDER BY id').fetchall() == before
    row = c.execute('SELECT r.processing_started_at,r.completed_at,r.updated_at,e.updated_at FROM receipts r JOIN webhook_events e ON e.id=r.event_id WHERE r.id=%s',(completed,)).fetchone()
    assert row[0] is not None and row[1] == row[3] and row[2] >= row[3]
    row = c.execute('SELECT processing_started_at,completed_at,review_reason,failure_reason FROM receipts WHERE id=%s',(pending,)).fetchone()
    assert row == (None,None,None,None)
    assert c.execute('SELECT count(*) FROM pending_conversations').fetchone()[0] == 1
    # Existing ownership and exact-duplicate constraints remain intact.
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        c.execute('UPDATE receipts SET chat_id=999 WHERE id=%s',(completed,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        c.execute("UPDATE receipts SET total_amount=125.50 WHERE id=%s",(other,))


def test_lifecycle_status_check_accepts_new_states_and_rejects_unknown(legacy_connection):
    c=legacy_connection
    receipt=add_receipt(c,42,810)
    migrate(c)
    lifecycle(c)
    for status in ('PROCESSING','PENDING_CATEGORY','NEEDS_REVIEW','COMPLETED','DUPLICATE','FAILED'):
        c.execute('UPDATE receipts SET status=%s WHERE id=%s',(status,receipt))
    with pytest.raises(psycopg.errors.CheckViolation):
        c.execute("UPDATE receipts SET status='UNKNOWN' WHERE id=%s",(receipt,))
    # Reapplying fails atomically, preserving the first migration's columns/data.
    with pytest.raises(psycopg.errors.DuplicateColumn):
        lifecycle(c)
    c.execute('ROLLBACK')
    assert c.execute('SELECT status FROM receipts WHERE id=%s',(receipt,)).fetchone()[0] == 'FAILED'


def test_empty_lifecycle_migration(legacy_connection):
    migrate(legacy_connection)
    lifecycle(legacy_connection)
    assert legacy_connection.execute('SELECT count(*) FROM receipts').fetchone()[0] == 0
