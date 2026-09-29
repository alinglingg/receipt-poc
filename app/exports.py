"""Bounded, read-only CSV export of the owner's completed receipts."""
import csv
from dataclasses import dataclass
from io import BytesIO, StringIO
from uuid import UUID

from sqlalchemy import select
from app.db import Receipt
from app.expenses import month_bounds
from app.statuses import ReceiptStatus

MAX_ROWS = 10000
MAX_BYTES = 5 * 1024 * 1024
HEADERS = ('date', 'vendor', 'category', 'total', 'VAT', 'status')


@dataclass(frozen=True)
class ReceiptExport:
    filename: str
    content: bytes
    count: int


def spreadsheet_text(value):
    text = value or ''
    # CSV quoting alone does not prevent spreadsheet formulas.
    if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')):
        return "'" + text
    return text


def export_receipts(session_factory, user_id, period):
    if not isinstance(user_id, UUID):
        raise ValueError('A valid user ID is required.')
    filters = [Receipt.user_id == user_id, Receipt.status == ReceiptStatus.COMPLETED]
    if period != 'all':
        start, end = month_bounds(period)
        filters += [Receipt.receipt_date >= start, Receipt.receipt_date < end]
    buffer = BytesIO()
    buffer.write(b'\xef\xbb\xbf')
    def write_row(values):
        line = StringIO(newline='')
        csv.writer(line, quoting=csv.QUOTE_ALL).writerow(values)
        encoded = line.getvalue().encode('utf-8')
        if buffer.tell() + len(encoded) > MAX_BYTES:
            raise ValueError('Export is too large. Try a single month with /export YYYY-MM.')
        buffer.write(encoded)
    write_row(HEADERS)
    count = 0
    with session_factory() as session:
        query = select(Receipt.receipt_date, Receipt.vendor_name, Receipt.category,
                       Receipt.total_amount, Receipt.vat_amount, Receipt.status).where(*filters).order_by(
                           Receipt.receipt_date, Receipt.id).limit(MAX_ROWS + 1)
        for receipt_date, vendor, category, total, vat, status in session.execute(query.execution_options(yield_per=500)):
            count += 1
            if count > MAX_ROWS:
                raise ValueError('Export exceeds 10,000 receipts. Try a single month with /export YYYY-MM.')
            write_row((receipt_date.isoformat(), spreadsheet_text(vendor), spreadsheet_text(category),
                       format(total, '.2f'), format(vat, '.2f') if vat is not None else '', status))
    return ReceiptExport(f'expenses_{period}.csv', buffer.getvalue(), count)
