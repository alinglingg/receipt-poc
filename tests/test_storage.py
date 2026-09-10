import json

import httpx
import pytest

from app.storage import StorageError, SupabaseStorage


@pytest.mark.asyncio
async def test_upload_uses_private_bucket_and_returns_object_path() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["body"] = request.content
        return httpx.Response(200, json={"Key": "receipts/chat/receipt.jpg"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    storage = SupabaseStorage(supabase_url="https://demo.supabase.co", service_role_key="secret", client=client)

    path = await storage.upload_receipt(object_path="123/abc.jpg", content=b"jpeg-data")

    assert path == "123/abc.jpg"
    assert captured["url"] == "https://demo.supabase.co/storage/v1/object/receipts/123/abc.jpg"
    assert captured["body"] == b"jpeg-data"
    assert captured["headers"]["x-upsert"] == "false"  # type: ignore[index]
    await client.aclose()


@pytest.mark.asyncio
async def test_signed_url_is_expiring_and_not_a_public_object_url() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/storage/v1/object/sign/receipts/123/abc.jpg"
        assert json.loads(request.content) == {"expiresIn": 900}
        return httpx.Response(200, json={"signedURL": "/object/sign/receipts/123/abc.jpg?token=temporary"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    storage = SupabaseStorage(supabase_url="https://demo.supabase.co", service_role_key="secret", client=client)

    url = await storage.create_signed_url(object_path="123/abc.jpg", expires_in_seconds=900)

    assert url == "https://demo.supabase.co/storage/v1/object/sign/receipts/123/abc.jpg?token=temporary"
    await client.aclose()


@pytest.mark.asyncio
async def test_storage_rejects_path_traversal_and_provider_failure() -> None:
    storage = SupabaseStorage(supabase_url="https://demo.supabase.co", service_role_key="secret")
    with pytest.raises(ValueError, match="without traversal"):
        await storage.upload_receipt(object_path="../secret.jpg", content=b"image")
    await storage.aclose()

    async def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(failing_handler))
    storage = SupabaseStorage(supabase_url="https://demo.supabase.co", service_role_key="secret", client=client)
    with pytest.raises(StorageError, match="upload failed"):
        await storage.upload_receipt(object_path="123/abc.jpg", content=b"image")
    await client.aclose()
