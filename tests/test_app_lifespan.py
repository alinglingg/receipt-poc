import asyncio
import importlib
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def main(monkeypatch):
    from app.config import Settings
    monkeypatch.setattr('app.config.get_settings', lambda: Settings.model_construct())
    module = importlib.import_module('app.main')
    monkeypatch.setattr(module, 'build_services', lambda settings: None)
    return module


@pytest.mark.asyncio
async def test_unconfigured_app_starts_and_health_works(main):
    app = main.create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/health')).json() == {'status': 'ok'}


@pytest.mark.asyncio
async def test_recovery_runs_at_startup_and_is_cancelled_on_shutdown(main, monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()
    calls = []
    async def process(services, update, event_id):
        calls.append((update, event_id))
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    monkeypatch.setattr(main, '_process_event', process)
    events = [NS(id='event-1', update_id=1, chat_id=42, file_id='photo'),
              NS(id='event-2', update_id=2, chat_id=42, file_id=None)]
    store = NS(unfinished_photo_events=Mock(return_value=events))
    app = main.create_app(NS(store=store))
    store.unfinished_photo_events.assert_not_called()
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), timeout=1)
        assert len(calls) == 1
        update, event_id = calls[0]
        assert (update.kind, update.file_id, update.chat_id, event_id) == ('photo', 'photo', 42, 'event-1')
        assert not stopped.is_set()
    assert stopped.is_set()
    store.unfinished_photo_events.assert_called_once()
