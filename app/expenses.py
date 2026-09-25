"""Read-only, owner-scoped expense tools. Aggregation happens in SQL."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re
from uuid import UUID

from sqlalchemy import case, func, select

from app.corrections import snapshot
from app.db import Receipt
from app.statuses import ReceiptStatus


@dataclass(frozen=True)
class Summary:
    count: int
    total: Decimal


@dataclass(frozen=True)
class MonthComparison:
    first: Summary
    second: Summary
    change: Decimal
    percent_change: Decimal | None


def month_bounds(month: str) -> tuple[date, date]:
    if not isinstance(month, str) or not re.fullmatch(r'\d{4}-\d{2}', month):
        raise ValueError('Use a month in YYYY-MM format, for example 2026-09.')
    year, number = map(int, month.split('-'))
    if not 1 <= year <= 9998 or not 1 <= number <= 12:
        raise ValueError('Use a valid month with a year between 0001 and 9998.')
    return date(year, number, 1), date(year + (number == 12), 1 if number == 12 else number + 1, 1)


def text_filter(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f'Search text must contain 1–{limit} characters.')
    return value.strip()


class ExpenseQueries:
    def __init__(self, session_factory):
        self._sessions = session_factory

    def _filters(self, user_id, start=None, end=None, category=None, vendor=None):
        if not isinstance(user_id, UUID):
            raise ValueError('A valid user ID is required.')
        for value in (start, end):
            if value is not None and type(value) is not date:
                raise ValueError('Date filters must be dates.')
        if start and end and start >= end:
            raise ValueError('The end date must be later than the start date.')
        filters = [Receipt.user_id == user_id, Receipt.status == ReceiptStatus.COMPLETED]
        if start:
            filters.append(Receipt.receipt_date >= start)
        if end:
            filters.append(Receipt.receipt_date < end)
        if category is not None:
            filters.append(func.lower(Receipt.category) == text_filter(category, 100).lower())
        if vendor is not None:
            # Escape SQL LIKE wildcards: user text is always a literal substring.
            value = text_filter(vendor, 200).replace('/', '//').replace('%', '/%').replace('_', '/_')
            filters.append(Receipt.vendor_name.ilike('%' + value + '%', escape='/'))
        return filters

    def search_expenses(self, user_id, *, vendor=None, category=None, start=None, end=None, limit=10, offset=0, largest=False):
        if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or offset < 0:
            raise ValueError('Limit must be 1–50 and offset must be nonnegative.')
        if type(largest) is not bool:
            raise ValueError('Largest must be a boolean.')
        filters = self._filters(user_id, start, end, category, vendor)
        ordering = [Receipt.total_amount.desc()] if largest else []
        ordering += [Receipt.receipt_date.desc(), Receipt.id.desc()]
        with self._sessions() as session:
            return [snapshot(row) for row in session.scalars(select(Receipt).where(*filters)
                    .order_by(*ordering).limit(limit).offset(offset))]

    def get_expense(self, user_id, receipt_id):
        filters = self._filters(user_id)
        if not isinstance(receipt_id, UUID):
            raise ValueError('Use the complete receipt ID from /receipts.')
        with self._sessions() as session:
            row = session.scalar(select(Receipt).where(*filters, Receipt.id == receipt_id))
            if row is None:
                raise LookupError('Completed receipt not found.')
            return snapshot(row)

    def get_monthly_summary(self, user_id, month, *, category=None):
        start, end = month_bounds(month)
        filters = self._filters(user_id, start, end, category)
        with self._sessions() as session:
            count, total = session.execute(select(func.count(Receipt.id), func.sum(Receipt.total_amount)).where(*filters)).one()
            return Summary(count, total if total is not None else Decimal('0.00'))

    def get_category_summary(self, user_id, month):
        start, end = month_bounds(month)
        filters = self._filters(user_id, start, end)
        total = func.sum(Receipt.total_amount)
        with self._sessions() as session:
            return [(category, Summary(count, amount)) for category, count, amount in session.execute(
                select(Receipt.category, func.count(Receipt.id), total).where(*filters)
                .group_by(Receipt.category).order_by(total.desc(), Receipt.category.asc()))]

    def get_largest_expenses(self, user_id, *, month=None, limit=5):
        start, end = month_bounds(month) if month is not None else (None, None)
        return self.search_expenses(user_id, start=start, end=end, limit=limit, largest=True)

    def compare_months(self, user_id, first_month, second_month):
        first_start, first_end = month_bounds(first_month)
        second_start, second_end = month_bounds(second_month)
        # One statement provides a consistent snapshot for both totals.
        filters = self._filters(user_id)
        first = (Receipt.receipt_date >= first_start) & (Receipt.receipt_date < first_end)
        second = (Receipt.receipt_date >= second_start) & (Receipt.receipt_date < second_end)
        with self._sessions() as session:
            a_count, a_total, b_count, b_total = session.execute(select(
                func.count(case((first, Receipt.id))), func.sum(case((first, Receipt.total_amount))),
                func.count(case((second, Receipt.id))), func.sum(case((second, Receipt.total_amount))),
            ).where(*filters, first | second)).one()
        a, b = Summary(a_count, a_total or Decimal('0.00')), Summary(b_count, b_total or Decimal('0.00'))
        change = b.total - a.total
        percent = (change / a.total * 100).quantize(Decimal('0.01')) if a.total else None
        return MonthComparison(a, b, change, percent)
