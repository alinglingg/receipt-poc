"""Small Telegram Bot API adapter used by the webhook and pipeline."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any

import httpx


class TelegramError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramUpdate:
    update_id: int
    chat_id: int
    kind: str
    file_id: str | None = None
    text: str | None = None


def parse_update(payload: dict[str, Any]) -> TelegramUpdate | None:
    """Extract only the fields the POC needs; ignore unrelated Telegram updates."""
    message = payload.get("message")
    if not isinstance(message, dict) or not isinstance(payload.get("update_id"), int):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict) or not isinstance(chat.get("id"), int):
        return None

    photos = message.get("photo")
    if isinstance(photos, list) and photos and isinstance(photos[-1], dict):
        file_id = photos[-1].get("file_id")
        if isinstance(file_id, str):
            return TelegramUpdate(payload["update_id"], chat["id"], "photo", file_id=file_id)
    text = message.get("text")
    if isinstance(text, str):
        return TelegramUpdate(payload["update_id"], chat["id"], "text", text=text)
    return TelegramUpdate(payload["update_id"], chat["id"], "other")


class TelegramBot:
    def __init__(self, token: str, webhook_secret: str, client: httpx.AsyncClient | None = None) -> None:
        self._token = token
        self._webhook_secret = webhook_secret
        self._api_base = f"https://api.telegram.org/bot{token}"
        self._file_base = f"https://api.telegram.org/file/bot{token}"
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._owns_client = client is None

    def is_valid_secret(self, supplied_secret: str | None) -> bool:
        return bool(supplied_secret) and hmac.compare_digest(supplied_secret, self._webhook_secret)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def download_photo(self, file_id: str) -> bytes:
        try:
            metadata = await self._client.get(f"{self._api_base}/getFile", params={"file_id": file_id})
            metadata.raise_for_status()
            file_path = metadata.json().get("result", {}).get("file_path")
            if not isinstance(file_path, str):
                raise TelegramError("Telegram did not return a photo file path.")
            download = await self._client.get(f"{self._file_base}/{file_path}")
            download.raise_for_status()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramError("Could not download the receipt image from Telegram.") from exc
        return download.content

    async def send(self, chat_id: int, markdown: str) -> None:
        try:
            response = await self._client.post(
                f"{self._api_base}/sendMessage",
                json={"chat_id": chat_id, "text": markdown, "parse_mode": "Markdown", "disable_web_page_preview": True},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise TelegramError("Could not send the Telegram response.") from exc
