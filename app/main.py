from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, status

from app.config import Settings, get_settings
from app.db import create_session_factory
from app.pipeline import ReceiptPipeline
from app.repository import SqlAlchemyReceiptStore
from app.storage import SupabaseStorage
from app.telegram import TelegramBot, TelegramError, TelegramUpdate, parse_update
from app.vision import OpenAIVisionExtractor


@dataclass
class WebhookServices:
    store: SqlAlchemyReceiptStore
    pipeline: ReceiptPipeline
    telegram: TelegramBot


def build_services(settings: Settings) -> WebhookServices | None:
    """Return None in an unconfigured local checkout so /health still works."""
    required = (
        settings.database_url,
        settings.telegram_bot_token,
        settings.telegram_webhook_secret,
        settings.openai_api_key,
        settings.supabase_url,
        settings.supabase_service_role_key,
    )
    if not all(required):
        return None
    telegram = TelegramBot(
        settings.telegram_bot_token.get_secret_value(),
        settings.telegram_webhook_secret.get_secret_value(),
    )
    storage = SupabaseStorage(
        supabase_url=settings.supabase_url,
        service_role_key=settings.supabase_service_role_key.get_secret_value(),
        bucket=settings.supabase_receipts_bucket,
    )
    pipeline = ReceiptPipeline(
        store=SqlAlchemyReceiptStore(create_session_factory()),
        vision=OpenAIVisionExtractor(settings.openai_api_key.get_secret_value(), settings.openai_model),
        storage=storage,
        notifier=telegram,
    )
    return WebhookServices(store=SqlAlchemyReceiptStore(create_session_factory()), pipeline=pipeline, telegram=telegram)


def create_app(services: WebhookServices | None = None) -> FastAPI:
    app = FastAPI(title="Receipt POC", version="0.1.0")
    app.state.services = services if services is not None else build_services(get_settings())

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/telegram", status_code=status.HTTP_200_OK)
    async def telegram_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> dict[str, bool]:
        services: WebhookServices | None = app.state.services
        if services is None:
            raise HTTPException(status_code=503, detail="Telegram integration is not configured.")
        if not services.telegram.is_valid_secret(x_telegram_bot_api_secret_token):
            raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret.")
        payload: dict[str, Any] = await request.json()
        update = parse_update(payload)
        if update is None:
            return {"accepted": True}
        event = services.store.create_webhook_event(
            update_id=update.update_id,
            chat_id=update.chat_id,
            kind=update.kind,
            file_id=update.file_id,
            text=update.text,
        )
        if event is None:
            return {"accepted": True}
        background_tasks.add_task(_process_event, services, update, event.id)
        return {"accepted": True}

    @app.on_event("startup")
    async def recover_unfinished_receipts() -> None:
        services: WebhookServices | None = app.state.services
        if services is None:
            return
        for event in services.store.unfinished_photo_events():
            if event.file_id:
                update = TelegramUpdate(event.update_id, event.chat_id, "photo", event.file_id)
                asyncio.create_task(_process_event(services, update, event.id))

    return app


async def _process_event(services: WebhookServices, update: TelegramUpdate, event_id) -> None:
    try:
        if update.kind == "photo" and update.file_id:
            image_bytes = await services.telegram.download_photo(update.file_id)
            await services.pipeline.process_photo(event_id=event_id, chat_id=update.chat_id, image_bytes=image_bytes)
        elif update.kind == "text" and update.text is not None:
            await services.pipeline.process_category_reply(event_id=event_id, chat_id=update.chat_id, category=update.text)
        else:
            services.store.mark_event(event_id, "COMPLETED")
            await services.telegram.send(update.chat_id, "Please send a receipt image, or reply with a category when asked.")
    except TelegramError:
        services.store.mark_event(event_id, "FAILED", "TELEGRAM_ERROR")


app = create_app()
