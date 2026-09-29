import csv
from datetime import date
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest

from app.commands import process_command
from app.db import WebhookEvent
from app.exports import HEADERS, spreadsheet_text
from app.telegram import TelegramBot, TelegramError
from tests.test_corrections import saved
from tests.test_expenses import expenses


def rows(export):
    return list(csv.DictReader(StringIO(export.content.decode('utf-8-sig'), newline='')))


def test_month_boundaries_statuses_and_owners(store, expenses):
    _, user, *_ = expenses
    result = store.export_receipts(user, '2026-09')
    data = rows(result)
    assert result.filename == 'expenses_2026-09.csv'
    assert result.count == 2
    assert list(data[0]) == list(HEADERS)
    assert [r['date'] for r in data] == ['2026-09-01', '2026-09-30']
    assert sum(Decimal(r['total']) for r in data) == Decimal('300.30')
    assert {r['status'] for r in data} == {'COMPLETED'}
    assert store.export_receipts(user, 'all').count == 4
    empty = store.export_receipts(store.get_or_create_user(888), 'all')
    assert empty.count == 0 and rows(empty) == []


def test_csv_roundtrip_unicode_quotes_newlines_and_vat(store):
    user, _ = saved(store, vendor_name='Café, "Hello"\nBranch', category='=HYPERLINK("bad")', vat_amount=None)
    data = rows(store.export_receipts(user, 'all'))
    assert data[0]['vendor'] == 'Café, "Hello"\nBranch'
    assert data[0]['category'] == '\'=HYPERLINK("bad")'
    assert data[0]['VAT'] == '' and data[0]['total'] == '125.50'
    saved(store, update=2, total_amount=Decimal('500'), vat_amount=Decimal('0'))
    assert any(
        r['VAT'] == '0.00' for r in rows(store.export_receipts(user, 'all')))


@pytest.mark.parametrize('value', ['=1+1', '+123', '-1+2', '@SUM(A1)', '  =1', '\ttext', '\rtext', '\ntext'])
def test_formula_cells_are_text(value):
    assert spreadsheet_text(value) == "'" + value


def test_export_is_not_limited_by_search_pagination(store):
    user = store.get_or_create_user(42)
    for number in range(1, 52):
        saved(store, update=number, total_amount=Decimal(number + 100))
    assert store.export_receipts(user, 'all').count == 51


def test_oversized_export_fails_instead_of_silently_truncating(store, monkeypatch):
    user, _ = saved(store)
    saved(store, update=2, total_amount=Decimal('500'))
    monkeypatch.setattr('app.exports.MAX_ROWS', 1)
    with pytest.raises(ValueError, match='exceeds'):
        store.export_receipts(user, 'all')
    monkeypatch.setattr('app.exports.MAX_ROWS', 10000)
    monkeypatch.setattr('app.exports.MAX_BYTES', 100)
    with pytest.raises(ValueError, match='too large'):
        store.export_receipts(user, 'all')


@pytest.mark.parametrize('period', ['2026-13', '../secret', 'September', '', '2026-9'])
def test_invalid_export_period(store, period):
    with pytest.raises(ValueError):
        store.export_receipts(store.get_or_create_user(42), period)


@pytest.mark.asyncio
async def test_command_sends_file_only_to_requesting_chat_and_preserves_pending(store, session_factory):
    user, _ = saved(store)
    _, pending = saved(store, update=2, status='PENDING_CATEGORY', category=None, total_amount=Decimal('500'))
    notifier = NS(send=AsyncMock(), send_document=AsyncMock())
    event = store.create_webhook_event(update_id=10, chat_id=42, kind='text')
    assert await process_command(store, notifier, event_id=event.id, chat_id=42, text='/export all')
    notifier.send_document.assert_awaited_once()
    args = notifier.send_document.call_args
    assert args.args == (42,)
    assert args.kwargs['filename'] == 'expenses_all.csv'
    assert '1 completed receipts' in args.kwargs['caption']
    assert store.get_open_pending(user).receipt_id == pending
    with session_factory() as session:
        assert session.get(WebhookEvent, event.id).status == 'COMPLETED'


@pytest.mark.asyncio
async def test_empty_and_invalid_export_send_no_document(store):
    notifier = NS(send=AsyncMock(), send_document=AsyncMock())
    for number, command in enumerate(['/export all', '/export', '/export 2026-13', '/export all extra'], 1):
        event = store.create_webhook_event(update_id=number, chat_id=42, kind='text')
        assert await process_command(store, notifier, event_id=event.id, chat_id=42, text=command)
    notifier.send_document.assert_not_called()
    assert 'No completed receipts' in notifier.send.call_args_list[0].args[1]


@pytest.mark.asyncio
async def test_document_multipart_upload():
    async def handler(request):
        assert request.url.path.endswith('/sendDocument')
        assert 'multipart/form-data' in request.headers['content-type']
        content = await request.aread()
        assert b'filename="expenses_all.csv"' in content
        assert b'Content-Type: text/csv' in content
        assert b'name="chat_id"\r\n\r\n42' in content
        assert b'date,vendor' in content
        return httpx.Response(200, json={'ok': True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = TelegramBot('fake', 'secret', client)
        await bot.send_document(42, filename='expenses_all.csv', content=b'date,vendor', caption='Export')


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [httpx.Response(500, text='secret'), httpx.Response(200, json={'ok':False, 'description':'secret'}), httpx.Response(200, text='secret')])
async def test_document_failure_is_sanitized(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        with pytest.raises(TelegramError, match='Could not send the CSV') as error:
            await TelegramBot('fake', 'secret', client).send_document(42, filename='expenses_all.csv', content=b'x', caption='Export')
        assert 'secret' not in str(error.value)


@pytest.mark.asyncio
async def test_failed_upload_is_not_marked_completed(store, session_factory, monkeypatch):
    from app.config import Settings
    monkeypatch.setattr('app.config.get_settings', lambda: Settings.model_construct())
    from app.main import _process_event, WebhookServices
    from app.telegram import TelegramUpdate
    saved(store)
    notifier = NS(send=AsyncMock(), send_document=AsyncMock(side_effect=TelegramError('failed')))
    services = WebhookServices(store, NS(), notifier)
    event = store.create_webhook_event(update_id=10, chat_id=42, kind='text')
    await _process_event(services, TelegramUpdate(10, 42, 'text', text='/export all'), event.id)
    with session_factory() as session:
        row = session.get(WebhookEvent, event.id)
        assert row.status == 'FAILED' and row.error_code == 'TELEGRAM_ERROR'
