import json
from datetime import date
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.assistant import ExpenseAssistant, QueryPlan, TOOL, validate_plan, execute_plan, process_assistant_text
from app.db import Receipt, WebhookEvent
from app.pipeline import ReceiptPipeline
from tests.helpers import FakeVision, FakeStorage, FakeNotifier
from tests.test_corrections import saved


def arguments(**changes):
    values = dict(action='summary', month='2026-09', second_month=None, category=None,
                  vendor=None, start=None, end=None, limit=None, receipt_id=None)
    values.update(changes)
    return values


def client_for(payload=None, **response_changes):
    response = NS(status='completed', output=[NS(type='function_call', name='query_expenses',
                  arguments=json.dumps(payload or arguments()))], output_text='Invented total: 999999')
    for key, value in response_changes.items():
        setattr(response, key, value)
    return NS(responses=NS(create=AsyncMock(return_value=response)))


@pytest.mark.asyncio
async def test_actual_sdk_shape_and_database_grounding(store, monkeypatch):
    user, _ = saved(store)
    saved(store, update=2, chat=99)
    monkeypatch.setattr('app.assistant.today', lambda: date(2026, 9, 25))
    client = client_for()
    assistant = ExpenseAssistant('fake', client=client)
    answer = await assistant.answer(store.expenses, user, 'How much did I spend this month?')
    assert '125.50' in answer and '999999' not in answer
    request = client.responses.create.call_args.kwargs
    assert request['store'] is False and request['parallel_tool_calls'] is False
    assert request['tools'][0]['strict'] is True
    assert '2026-09-25' in request['instructions']
    assert '125.50' not in json.dumps(request)  # Receipts never go to the model.
    schema = TOOL['parameters']
    assert set(schema['required']) == set(schema['properties'])
    assert schema['additionalProperties'] is False
    assert 'user_id' not in schema['properties']


@pytest.mark.parametrize('changes', [
    {'user_id': str(uuid4())}, {'action': 'delete'}, {'limit': True}, {'limit': 11},
    {'month': '2026-13'}, {'month': None}, {'vendor': 'hidden filter'},
    {'action': 'compare', 'second_month': None},
    {'action': 'search', 'month': None, 'start': '2026-02-30'},
    {'action': 'search', 'month': None, 'start': '2026-10-01', 'end': '2026-09-01'},
    {'action': 'receipt', 'month': None, 'receipt_id': 'bad'},
])
def test_untrusted_arguments_rejected(changes):
    with pytest.raises((ValueError, ValidationError)):
        validate_plan(QueryPlan.model_validate(arguments(**changes)))


@pytest.mark.asyncio
@pytest.mark.parametrize('output,status', [
    ([], 'completed'), ([NS(type='message', text='Total 100')], 'completed'),
    ([NS(type='function_call', name='delete', arguments='{}')], 'completed'),
    ([NS(type='function_call', name='query_expenses', arguments='{')], 'completed'),
    ([NS(type='function_call', name='query_expenses', arguments=json.dumps(arguments()))] * 2, 'completed'),
    ([], 'incomplete'),
])
async def test_invalid_provider_response_never_runs_query(output, status):
    client = client_for(output=output, status=status)
    with pytest.raises(ValueError):
        await ExpenseAssistant('fake', client=client).answer(NS(), uuid4(), 'total?')


@pytest.mark.asyncio
async def test_length_limit_before_provider_call():
    client = client_for()
    with pytest.raises(ValueError):
        await ExpenseAssistant('fake', client=client).answer(NS(), uuid4(), 'a' * 2001)
    client.responses.create.assert_not_called()


@pytest.mark.parametrize('plan,fragment', [
    ({'action':'categories'}, 'Maintenance'),
    ({'action':'compare', 'second_month':'2026-10'}, '-125.50'),
    ({'action':'search', 'month':None, 'vendor':'Acme', 'start':'2026-09-01', 'end':'2026-10-01'}, 'Acme'),
    ({'action':'largest', 'month':None, 'limit':1}, '125.50'),
    ({'action':'clarify', 'month':None}, 'Please ask one expense question'),
])
def test_read_only_dispatch(store, plan, fragment):
    user, _ = saved(store)
    assert fragment in execute_plan(store.expenses, user, QueryPlan.model_validate(arguments(**plan)))


def test_single_receipt_owner_scope(store):
    user, receipt_id = saved(store)
    other = store.get_or_create_user(99)
    plan = QueryPlan.model_validate(arguments(action='receipt', month=None, receipt_id=str(receipt_id)))
    with pytest.raises(LookupError):
        execute_plan(store.expenses, other, plan)
    assert str(receipt_id) in execute_plan(store.expenses, user, plan)


@pytest.mark.asyncio
async def test_pending_category_requires_explicit_assignment(store, session_factory):
    user, receipt_id = saved(store, status='PENDING_CATEGORY', category=None)
    notifier = FakeNotifier()
    assistant = NS(answer=AsyncMock(return_value='Total: 0.00'))
    pipeline = ReceiptPipeline(store=store, vision=FakeVision(None), storage=FakeStorage(), notifier=notifier)
    services = NS(store=store, telegram=notifier, pipeline=pipeline, assistant=assistant)
    for number, message in enumerate(['How much did I spend?', 'Hello', 'Dining'], 10):
        event = store.create_webhook_event(update_id=number, chat_id=42, kind='text')
        await process_assistant_text(services, event_id=event.id, chat_id=42, text=message)
        assert store.get_open_pending(user).receipt_id == receipt_id
    event = store.create_webhook_event(update_id=20, chat_id=42, kind='text')
    await process_assistant_text(services, event_id=event.id, chat_id=42, text='CATEGORY Dining')
    assert assistant.answer.await_count == 3
    assert store.get_open_pending(user) is None
    with session_factory() as session:
        assert session.get(Receipt, receipt_id).category == 'Dining'


@pytest.mark.asyncio
async def test_review_and_slash_routing_without_model(store, monkeypatch):
    from app.config import Settings
    monkeypatch.setattr('app.config.get_settings', lambda: Settings.model_construct())
    from app.main import WebhookServices, _process_event
    from app.telegram import TelegramUpdate
    saved(store, status='NEEDS_REVIEW', review_reason='MEDIUM_CONFIDENCE')
    notifier = FakeNotifier()
    pipeline = NS(process_category_reply=AsyncMock())
    assistant = NS(answer=AsyncMock(return_value='From database'))
    services = WebhookServices(store, pipeline, notifier, assistant)
    for number, message in enumerate(['/summary 2026-09', 'CONFIRM', 'REVIEW', 'RETRY', 'CATEGORY Dining'], 10):
        event = store.create_webhook_event(update_id=number, chat_id=42, kind='text')
        await _process_event(services, TelegramUpdate(number, 42, 'text', text=message), event.id)
    assert pipeline.process_category_reply.await_count == 3
    assistant.answer.assert_not_called()
    event = store.create_webhook_event(update_id=30, chat_id=42, kind='text')
    await _process_event(services, TelegramUpdate(30, 42, 'text', text='How much this month?'), event.id)
    assistant.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_failure_sanitized_and_pending_unchanged(store, session_factory):
    user, receipt_id = saved(store, status='PENDING_CATEGORY', category=None)
    messages = []
    async def send(chat, message):
        messages.append(message)
    services = NS(store=store, telegram=NS(send=send), pipeline=NS(),
                  assistant=NS(answer=AsyncMock(side_effect=RuntimeError('secret provider payload'))))
    event = store.create_webhook_event(update_id=10, chat_id=42, kind='text')
    await process_assistant_text(services, event_id=event.id, chat_id=42, text='total?')
    assert 'secret' not in messages[0]
    assert store.get_open_pending(user).receipt_id == receipt_id
    with session_factory() as session:
        assert session.get(WebhookEvent, event.id).error_code == 'EXPENSE_QUERY_ERROR'
