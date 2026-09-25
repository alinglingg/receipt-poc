"""Transaction-local receipt history; no provider payloads or image links."""
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select

from app.db import Receipt, ReceiptEvent
from app.review import escape_markdown

FIELDS = ('vendor_name', 'receipt_date', 'total_amount', 'vat_amount', 'category', 'status', 'review_reason')


def receipt_values(receipt):
    result = {}
    for field in FIELDS:
        value = getattr(receipt, field)
        if isinstance(value, Decimal):
            value = format(value, '.2f')
        elif field == 'receipt_date':
            value = value.isoformat()
        result[field] = value
    return result


def record_event(session, receipt, event_type, before=None):
    after = receipt_values(receipt)
    if before is not None:
        keys = [key for key in after if after[key] != before[key]]
        if not keys:
            return
        old = {key: before[key] for key in keys}
        new = {key: after[key] for key in keys}
    else:
        old, new = None, after
    session.add(ReceiptEvent(receipt_id=receipt.id, user_id=receipt.user_id,
                            event_type=event_type, old_value=old, new_value=new,
                            created_at=datetime.now(timezone.utc)))


def receipt_history(session_factory, user_id, receipt_id, page=1):
    if not isinstance(receipt_id, UUID) or type(page) is not int or not 1 <= page <= 10000:
        raise ValueError('Use /history <receipt-id> [page], with page between 1 and 10000.')
    with session_factory() as session:
        receipt = session.scalar(select(Receipt.id).where(Receipt.id == receipt_id, Receipt.user_id == user_id))
        if receipt is None:
            raise LookupError('Receipt not found. Copy an ID from /receipts.')
        rows = list(session.scalars(select(ReceiptEvent).where(
            ReceiptEvent.receipt_id == receipt_id, ReceiptEvent.user_id == user_id,
        ).order_by(ReceiptEvent.created_at.desc(), ReceiptEvent.id.desc()).offset((page - 1) * 5).limit(6)))
        blocks = [f'History for `{receipt_id}` — page {page} (UTC)']
        for row in rows[:5]:
            stamp = row.created_at
            if stamp.tzinfo is not None:
                stamp = stamp.astimezone(timezone.utc)
            lines = [f'{stamp:%Y-%m-%d %H:%M:%S} — {row.event_type.replace("_", " ").title()}']
            for field, value in row.new_value.items():
                label = field.replace('_', ' ').title()
                if row.old_value is None:
                    lines.append(f'{label}: {value if value is not None else "Not set"}')
                else:
                    old = row.old_value.get(field)
                    lines.append(f'{label}: {old if old is not None else "Not set"} → {value if value is not None else "Not set"}')
            blocks.append(escape_markdown('\n'.join(lines)))
        if not rows:
            blocks.append('No recorded history on this page. Tracking began with the audit-history deployment.')
        if len(rows) > 5:
            blocks.append(f'Next: `/history {receipt_id} {page + 1}`')
        return '\n\n'.join(blocks)
