from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.dashboard_store import digest, dashboard_origin
from app.db import BrowserToken, Receipt
from tests.test_corrections import saved

ORIGIN = 'https://receipts.example.com'
COOKIE = '__Host-receipt_session'


@pytest.fixture
def web(store, monkeypatch):
    from app.config import Settings
    monkeypatch.setattr('app.config.get_settings', lambda: Settings.model_construct())
    from app.main import create_app, WebhookServices
    services = WebhookServices(store, NS(), NS(), storage=NS(create_signed_url=AsyncMock(return_value='https://storage.example/image')), dashboard_url=ORIGIN)
    app = create_app(services)
    return app, services


def test_tokens_are_hashed_one_time_expiring_and_revocable(store, session_factory):
    user = store.get_or_create_user(42)
    token = store.dashboard.issue_login(user)
    with session_factory() as session:
        row = session.scalar(select(BrowserToken))
        assert row.token_hash == digest(token) and row.token_hash != token
    session_token = store.dashboard.exchange_login(token)
    assert store.dashboard.authenticate(session_token) == user
    for invalid in [token, 'wrong', None]:
        with pytest.raises(LookupError):
            store.dashboard.authenticate(invalid)
    with pytest.raises(LookupError):
        store.dashboard.exchange_login(token)
    store.dashboard.logout(session_token)
    with pytest.raises(LookupError):
        store.dashboard.authenticate(session_token)
    expired = store.dashboard.issue_login(user)
    with session_factory() as session:
        session.get(BrowserToken, digest(expired)).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    with pytest.raises(LookupError):
        store.dashboard.exchange_login(expired)


def test_new_link_invalidates_old_and_groups_are_denied(store):
    user = store.get_or_create_user(42)
    first = store.dashboard.issue_login(user)
    store.dashboard.issue_login(user)
    with pytest.raises(LookupError):
        store.dashboard.exchange_login(first)
    with pytest.raises(ValueError):
        store.dashboard.issue_login(store.get_or_create_user(-10042))


@pytest.mark.parametrize('url', ['http://example.com', 'https://user:pass@example.com', 'https://example.com/path', 'https://example.com?a=1', 'https://example.com#x'])
def test_origin_config_rejects_unsafe_values(url):
    with pytest.raises(ValueError):
        dashboard_origin(url)


@pytest.mark.asyncio
async def test_login_cookie_headers_replay_origin_and_logout(web, store):
    app, _ = web
    user = store.get_or_create_user(42)
    token = store.dashboard.issue_login(user)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        assert (await client.get('/api/dashboard/session')).status_code == 401
        bad = await client.post('/api/dashboard/login', json={'token':token}, headers={'Origin':'https://evil.example'})
        assert bad.status_code == 403
        good = await client.post('/api/dashboard/login', json={'token':token}, headers={'Origin':ORIGIN})
        assert good.status_code == 200
        cookie = good.headers['set-cookie']
        assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=strict' in cookie
        assert good.headers['cache-control'] == 'no-store'
        assert good.headers['referrer-policy'] == 'no-referrer'
        assert (await client.get('/api/dashboard/session')).status_code == 200
        assert (await client.post('/api/dashboard/login', json={'token':token}, headers={'Origin':ORIGIN})).status_code == 401
        assert (await client.post('/api/dashboard/logout', json={})).status_code == 403
        assert (await client.post('/api/dashboard/logout', json={}, headers={'Origin':ORIGIN})).status_code == 200
        assert (await client.get('/api/dashboard/session')).status_code == 401


def session_cookie(store, user):
    return {COOKIE: store.dashboard.exchange_login(store.dashboard.issue_login(user))}


@pytest.mark.asyncio
async def test_reads_edits_exports_images_and_ownership(web, store, session_factory):
    app, services = web
    user, receipt_id = saved(store)
    _, foreign = saved(store, update=2, chat=99)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN, cookies=session_cookie(store,user), headers={'Origin':ORIGIN}) as client:
        summary = (await client.get('/api/dashboard/summary?month=2026-09')).json()
        assert summary['count'] == 1 and summary['total'] == '125.50'
        listing = (await client.get('/api/dashboard/receipts?month=2026-09')).json()
        assert [r['id'] for r in listing['items']] == [str(receipt_id)]
        for path in [f'/receipts/{foreign}', f'/receipts/{foreign}/image', f'/receipts/{foreign}/history']:
            assert (await client.get('/api/dashboard'+path)).status_code == 404
        services.storage.create_signed_url.assert_not_called()
        assert (await client.post(f'/api/dashboard/receipts/{foreign}/edit', json={'field':'total','value':'300'})).status_code == 404
        assert (await client.get(f'/api/dashboard/receipts/{receipt_id}/image')).status_code == 200
        services.storage.create_signed_url.assert_awaited_once()
        edit = await client.post(f'/api/dashboard/receipts/{receipt_id}/edit', json={'field':'total','value':'450.00'})
        assert edit.status_code == 200 and edit.json()['total'] == '450.00'
        history = (await client.get(f'/api/dashboard/receipts/{receipt_id}/history')).json()['text']
        assert '125.50 → 450.00' in history
        export = await client.get('/api/dashboard/export?period=2026-09')
        assert export.status_code == 200 and '450.00' in export.text
        assert 'text/csv' in export.headers['content-type']
        assert (await client.post(f'/api/dashboard/receipts/{receipt_id}/edit',json={'field':'total','value':'0'})).status_code == 400
        assert (await client.get('/api/dashboard/receipts?page=0')).status_code == 400
        assert (await client.get('/api/dashboard/summary?month=oops')).status_code == 400


@pytest.mark.asyncio
async def test_review_confirm_category_and_retry_preserve_scope(web, store, session_factory):
    app, _ = web
    user, receipt_id = saved(store, status='NEEDS_REVIEW', category=None, review_reason='MEDIUM_CONFIDENCE')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN,cookies=session_cookie(store,user),headers={'Origin':ORIGIN}) as client:
        assert len((await client.get('/api/dashboard/review')).json()['items']) == 1
        endpoint = f'/api/dashboard/receipts/{receipt_id}/review'
        assert (await client.post(endpoint,json={'action':'category','value':'Dining'})).status_code == 404
        assert (await client.post(endpoint,json={'action':'confirm','value':'2026-09-10'})).status_code == 200
        assert (await client.post(endpoint,json={'action':'category','value':'Dining'})).status_code == 200
        assert (await client.get('/api/dashboard/review')).json()['items'] == []
        assert (await client.post(endpoint,json={'action':'retry'})).status_code == 404
        with session_factory() as session:
            assert session.get(Receipt,receipt_id).category == 'Dining'
        # A genuine review draft can still be discarded through the owned flow.
        _, draft = saved(store, update=2, status='NEEDS_REVIEW', review_reason='MEDIUM_CONFIDENCE', vendor_normalized='OTHER')
        assert (await client.post(f'/api/dashboard/receipts/{draft}/review',json={'action':'retry'})).status_code == 200


@pytest.mark.asyncio
async def test_vendor_aliases_and_disabled_dashboard(web, store):
    from tests.test_vendors import learn
    app, services = web
    user = learn(store, store._session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN,cookies=session_cookie(store,user),headers={'Origin':ORIGIN}) as client:
        assert (await client.post('/api/dashboard/aliases',json={'action':'add','name':'SB Branch','target':'Starbucks'})).status_code == 200
        vendors = (await client.get('/api/dashboard/vendors')).json()
        assert vendors['aliases'][0]['name'] == 'SB Branch'
        assert (await client.post('/api/dashboard/aliases',json={'action':'remove','name':'SB Branch'})).status_code == 200
        services.dashboard_url = ''
        assert (await client.get('/api/dashboard/session')).status_code == 503
        shell = await client.get('/dashboard/')
        assert shell.status_code == 200 and 'Your receipts.' in shell.text
        assert 'frame-ancestors' in shell.headers['content-security-policy']
        assert (await client.get('/dashboard/assets/dashboard.js')).status_code == 200
        assert (await client.get('/dashboard/assets/config.py')).status_code == 404


@pytest.mark.asyncio
async def test_telegram_login_command_only_private_and_configured(store):
    from app.commands import process_command
    notifier = NS(send=AsyncMock())
    event = store.create_webhook_event(update_id=1,chat_id=42,kind='text')
    await process_command(store,notifier,event_id=event.id,chat_id=42,text='/dashboard',dashboard_url=ORIGIN)
    assert ORIGIN+'/dashboard/#token=' in notifier.send.call_args.args[1]
    event = store.create_webhook_event(update_id=2,chat_id=-42,kind='text')
    await process_command(store,notifier,event_id=event.id,chat_id=-42,text='/dashboard',dashboard_url=ORIGIN)
    assert '#token=' not in notifier.send.call_args.args[1]


def test_browser_session_expires(store, session_factory):
    user = store.get_or_create_user(42)
    token = store.dashboard.exchange_login(store.dashboard.issue_login(user))
    with session_factory() as session:
        session.get(BrowserToken, digest(token)).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    with pytest.raises(LookupError):
        store.dashboard.authenticate(token)


@pytest.mark.asyncio
async def test_cross_origin_edit_cannot_change_receipt(web, store):
    app, _ = web
    user, receipt_id = saved(store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN, cookies=session_cookie(store,user)) as client:
        response = await client.post(f'/api/dashboard/receipts/{receipt_id}/edit', json={'field':'total','value':'999'}, headers={'Origin':'https://other.example'})
        assert response.status_code == 403
        receipt = (await client.get(f'/api/dashboard/receipts/{receipt_id}')).json()
        assert receipt['total'] == '125.50'
