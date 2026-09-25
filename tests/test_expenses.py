from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.commands import process_command
from app.db import Receipt
from app.expenses import month_bounds
from tests.test_corrections import saved


@pytest.fixture
def expenses(store, session_factory):
    user, a = saved(store, update=1, vendor_name='Keigo', receipt_date=date(2026, 9, 1),
                    total_amount=Decimal('100.10'), category='Dining')
    _, b = saved(store, update=2, vendor_name='100%_Market', receipt_date=date(2026, 9, 30),
                 total_amount=Decimal('200.20'), category='Groceries')
    saved(store, update=3, receipt_date=date(2026, 8, 31), total_amount=Decimal('50.00'), category='Dining')
    saved(store, update=4, receipt_date=date(2026, 10, 1), total_amount=Decimal('900.00'))
    other, foreign = saved(store, update=5, chat=99, total_amount=Decimal('999.00'))
    for number, status in enumerate(['NEEDS_REVIEW', 'PENDING_CATEGORY', 'PROCESSING', 'FAILED', 'DUPLICATE'], 6):
        _, receipt_id = saved(store, update=number, total_amount=Decimal(number * 100))
        with session_factory() as session:
            session.get(Receipt, receipt_id).status = status
            session.commit()
    return store.expenses, user, a, b, foreign


def test_monthly_category_totals_and_zero(expenses):
    queries, user, *_ = expenses
    summary = queries.get_monthly_summary(user, '2026-09')
    assert (summary.count, summary.total) == (2, Decimal('300.30'))
    assert queries.get_monthly_summary(user, '2026-09', category='dining').total == Decimal('100.10')
    assert queries.get_monthly_summary(user, '2025-01').total == Decimal('0.00')
    assert queries.get_monthly_summary(user, '2025-01').count == 0
    categories = queries.get_category_summary(user, '2026-09')
    assert [(name, result.total) for name, result in categories] == [('Groceries', Decimal('200.20')), ('Dining', Decimal('100.10'))]


def test_search_literal_filters_pagination_and_ownership(expenses):
    queries, user, a, b, foreign = expenses
    assert [r.id for r in queries.search_expenses(user, vendor='keigo')] == [a]
    assert [r.id for r in queries.search_expenses(user, vendor='%_')] == [b]
    assert queries.search_expenses(user, vendor="' OR 1=1 --") == []
    assert queries.search_expenses(user, vendor='nobody') == []
    rows = queries.search_expenses(user, start=date(2026, 9, 1), end=date(2026, 10, 1))
    assert [r.id for r in rows] == [b, a]
    assert queries.search_expenses(user, start=date(2026, 9, 1), end=date(2026, 10, 1), offset=1, limit=1) == [rows[1]]
    assert queries.get_expense(user, a).total_amount == Decimal('100.10')
    with pytest.raises(LookupError):
        queries.get_expense(user, foreign)
    with pytest.raises(LookupError):
        queries.get_expense(user, uuid4())


def test_largest_and_month_comparison(expenses):
    queries, user, a, b, _ = expenses
    assert [r.id for r in queries.get_largest_expenses(user, month='2026-09')] == [b, a]
    assert queries.get_largest_expenses(user, limit=1)[0].total_amount == Decimal('900.00')
    result = queries.compare_months(user, '2026-08', '2026-09')
    assert result.change == Decimal('250.30')
    assert result.percent_change == Decimal('500.60')
    assert queries.compare_months(user, '2026-09', '2026-08').change == Decimal('-250.30')
    assert queries.compare_months(user, '2025-01', '2026-09').percent_change is None
    assert queries.compare_months(user, '2026-09', '2026-09').change == 0


@pytest.mark.parametrize('month', ['2026-13', '2026-00', '26-09', '0000-01', '9999-12', '2026-9', None])
def test_invalid_month(month):
    with pytest.raises(ValueError):
        month_bounds(month)


def test_leap_year_and_year_rollover():
    assert month_bounds('2024-02') == (date(2024, 2, 1), date(2024, 3, 1))
    assert month_bounds('2026-12') == (date(2026, 12, 1), date(2027, 1, 1))


@pytest.mark.parametrize('arguments', [
    {'limit': 0}, {'limit': 51}, {'limit': True}, {'offset': -1}, {'vendor': ''},
    {'category': 'x' * 101}, {'start': '2026-01-01'},
    {'start': date(2026, 10, 1), 'end': date(2026, 9, 1)},
])
def test_invalid_tool_arguments(store, arguments):
    with pytest.raises(ValueError):
        store.expenses.search_expenses(uuid4(), **arguments)


@pytest.mark.asyncio
async def test_commands_preserve_pending_conversation(store, expenses):
    queries, user, a, _, _ = expenses
    _, pending_id = saved(store, update=30, status='PENDING_CATEGORY', category=None, total_amount=Decimal('123.45'))
    sent = []
    async def send(chat_id, text):
        sent.append(text)
    messages = ['/summary 2026-09', '/categories 2026-09', '/category 2026-09 Dining',
                '/search Keigo', '/largest 2026-09', '/compare 2026-08 2026-09',
                f'/receipt {a}', '/summary nonsense', '/search', '/compare 2026-08', '/help']
    for number, message in enumerate(messages, 100):
        event = store.create_webhook_event(update_id=number, chat_id=42, kind='text')
        assert await process_command(store, SimpleNamespace(send=send), event_id=event.id, chat_id=42, text=message)
    assert '300.30' in sent[0] and '100.10' in sent[2]
    assert str(a) in sent[3] and '500.60%' in sent[5]
    assert '/summary' in sent[-1]
    assert store.get_open_pending(user).receipt_id == pending_id


def test_summary_reflects_corrections(store, expenses):
    from app.corrections import Correction
    queries, user, a, *_ = expenses
    store.correct_receipt(user, Correction(a, 'total', '150.25'))
    assert queries.get_monthly_summary(user, '2026-09').total == Decimal('350.45')
    store.correct_receipt(user, Correction(a, 'category', 'Travel'))
    assert queries.get_monthly_summary(user, '2026-09', category='Dining').count == 0
    assert queries.get_monthly_summary(user, '2026-09', category='Travel').total == Decimal('150.25')


def test_equal_totals_have_stable_order_and_empty_owner(store):
    user, a = saved(store, update=1, receipt_date=date(2026, 9, 1))
    _, b = saved(store, update=2, receipt_date=date(2026, 9, 2))
    assert [r.id for r in store.expenses.get_largest_expenses(user)] == [b, a]
    unknown = uuid4()
    assert store.expenses.get_category_summary(unknown, '2026-09') == []
    assert store.expenses.search_expenses(unknown) == []
    comparison = store.expenses.compare_months(unknown, '2026-08', '2026-09')
    assert comparison.change == 0 and comparison.percent_change is None
