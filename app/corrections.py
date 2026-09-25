"""Explicit, owner-scoped corrections to completed receipts."""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import Receipt, User
from app.pipeline import DuplicateReceiptError, normalize_vendor
from app.review import today
from app.statuses import ReceiptStatus

HELP = (
    "Use /receipts to see the latest 10 saved receipts.\n"
    "To correct one, copy its ID into:\n"
    "`/edit <receipt-id> total 450.00`\n"
    "`/edit <receipt-id> date 2026-09-10`\n"
    "`/edit <receipt-id> vendor Starbucks`\n"
    "`/edit <receipt-id> category Transportation`\n"
    "Dates also accept DD/MM/YYYY. Corrections affect only that receipt."
)


@dataclass(frozen=True)
class Correction:
    receipt_id: UUID
    field: str
    value: str


@dataclass(frozen=True)
class SavedReceipt:
    id: UUID
    vendor_name: str
    receipt_date: date
    total_amount: Decimal
    category: str | None


def snapshot(row):
    return SavedReceipt(row.id, row.vendor_name, row.receipt_date, row.total_amount, row.category)


def parse_edit(message: str) -> Correction:
    parts = message.strip().split(maxsplit=3)
    if len(parts) != 4 or parts[0].lower() != '/edit':
        raise ValueError('Use /edit <receipt-id> <field> <value>. Send /help for examples.')
    try:
        receipt_id = UUID(parts[1])
    except ValueError:
        raise ValueError('Copy the complete receipt ID from /receipts.') from None
    correction = Correction(receipt_id, parts[2].lower(), parts[3])
    validated_value(correction.field, correction.value)
    return correction


def validated_value(field: str, value: str):
    value = ' '.join(value.split())
    if field == 'total':
        if not re.fullmatch(r'\d{1,10}(?:\.\d{1,2})?', value) or Decimal(value) <= 0:
            raise ValueError('Total must be positive, with up to 10 digits and 2 decimal places; for example 450.00.')
        return Decimal(value)
    if field == 'date':
        try:
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                result = date.fromisoformat(value)
            elif re.fullmatch(r'\d{2}/\d{2}/\d{4}', value):
                result = datetime.strptime(value, '%d/%m/%Y').date()
            else:
                raise ValueError
        except ValueError:
            raise ValueError('Use a valid date: YYYY-MM-DD or DD/MM/YYYY.') from None
        if result > today():
            raise ValueError('The receipt date cannot be in the future.')
        return result
    if field in {'vendor', 'category'}:
        limit = 200 if field == 'vendor' else 100
        if not value or len(value) > limit or (field == 'vendor' and not normalize_vendor(value)):
            raise ValueError(f'{field.title()} must contain 1–{limit} characters' + (' including a letter or number.' if field == 'vendor' else '.'))
        return value
    raise ValueError('Choose one field: date, total, vendor, or category.')


def recent_receipts(session_factory, user_id):
    with session_factory() as session:
        rows = session.scalars(select(Receipt).where(
            Receipt.user_id == user_id, Receipt.status == ReceiptStatus.COMPLETED,
        ).order_by(Receipt.created_at.desc(), Receipt.id.desc()).limit(10))
        return [snapshot(row) for row in rows]


def correct_receipt(session_factory, user_id, correction):
    # Validate here as well: callers cannot bypass command validation.
    value = validated_value(correction.field, correction.value)
    with session_factory() as session:
        owner = session.scalar(select(User).where(User.id == user_id).with_for_update())
        if owner is None:
            raise LookupError('Receipt not found. Copy an ID from /receipts.')
        row = session.scalar(select(Receipt).where(
            Receipt.id == correction.receipt_id, Receipt.user_id == user_id,
        ).with_for_update())
        if row is None:
            raise LookupError('Receipt not found. Copy an ID from /receipts.')
        if row.status != ReceiptStatus.COMPLETED:
            raise ValueError('Finish this receipt’s review/category conversation before editing it.')
        vendor = normalize_vendor(value) if correction.field == 'vendor' else row.vendor_normalized
        receipt_date = value if correction.field == 'date' else row.receipt_date
        total = value if correction.field == 'total' else row.total_amount
        if correction.field == 'total' and row.vat_amount is not None and total < row.vat_amount:
            raise ValueError('Total cannot be less than the recorded VAT.')
        duplicate = session.scalar(select(Receipt.id).where(
            Receipt.user_id == user_id, Receipt.id != row.id,
            Receipt.vendor_normalized == vendor, Receipt.receipt_date == receipt_date,
            Receipt.total_amount == total,
        ))
        if duplicate is not None:
            raise DuplicateReceiptError('This correction would duplicate another receipt.')
        column = {'vendor': 'vendor_name', 'date': 'receipt_date', 'total': 'total_amount', 'category': 'category'}[correction.field]
        setattr(row, column, value)
        row.vendor_normalized = vendor
        row.updated_at = datetime.now(timezone.utc)
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            code = getattr(error.orig, 'sqlstate', None)
            if code == '23505' or 'UNIQUE constraint failed' in str(error.orig):
                raise DuplicateReceiptError('This correction would duplicate another receipt.') from error
            raise
        return snapshot(row)
