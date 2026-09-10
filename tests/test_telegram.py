import httpx
import pytest

from app.telegram import TelegramBot, parse_update


def test_parse_photo_uses_largest_telegram_photo_variant() -> None:
    update = parse_update({"update_id": 5, "message": {"chat": {"id": 7}, "photo": [{"file_id": "small"}, {"file_id": "large"}]}})

    assert update is not None
    assert update.kind == "photo"
    assert update.file_id == "large"


def test_parse_text_and_ignore_non_message_update() -> None:
    update = parse_update({"update_id": 6, "message": {"chat": {"id": 7}, "text": "Utilities"}})
    assert update is not None and update.text == "Utilities"
    assert parse_update({"update_id": 7, "callback_query": {}}) is None


@pytest.mark.asyncio
async def test_telegram_secret_and_file_download() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"result": {"file_path": "photos/receipt.jpg"}})
        if request.url.path.endswith("/photos/receipt.jpg"):
            return httpx.Response(200, content=b"receipt-bytes")
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bot = TelegramBot("token", "secret", client=client)

    assert bot.is_valid_secret("secret")
    assert not bot.is_valid_secret("wrong")
    assert await bot.download_photo("file-id") == b"receipt-bytes"
    await client.aclose()
