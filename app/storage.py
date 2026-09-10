"""Private Supabase Storage adapter for receipt originals."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx


class StorageError(RuntimeError):
    """A receipt image could not be safely stored or shared."""


class SupabaseStorage:
    def __init__(
        self,
        *,
        supabase_url: str,
        service_role_key: str,
        bucket: str = "receipts",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = supabase_url.rstrip("/")
        self._bucket = bucket
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._owns_client = client is None
        self._headers = {
            "Authorization": f"Bearer {service_role_key}",
            "apikey": service_role_key,
        }

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def upload_receipt(self, *, object_path: str, content: bytes) -> str:
        """Upload a JPEG to a private bucket and return its storage path."""
        self._validate_path(object_path)
        if not content:
            raise StorageError("Refusing to upload an empty receipt image.")

        url = f"{self._base_url}/storage/v1/object/{quote(self._bucket, safe='')}/{quote(object_path, safe='/')}"
        headers = {**self._headers, "Content-Type": "image/jpeg", "x-upsert": "false"}
        try:
            response = await self._client.post(url, headers=headers, content=content)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise StorageError("Receipt image upload failed.") from exc
        return object_path

    async def create_signed_url(self, *, object_path: str, expires_in_seconds: int = 900) -> str:
        """Create a short-lived URL; never expose a permanent public object URL."""
        self._validate_path(object_path)
        if not 60 <= expires_in_seconds <= 86_400:
            raise ValueError("Signed URL expiry must be between 60 seconds and 24 hours.")

        url = f"{self._base_url}/storage/v1/object/sign/{quote(self._bucket, safe='')}/{quote(object_path, safe='/')}"
        try:
            response = await self._client.post(url, headers=self._headers, json={"expiresIn": expires_in_seconds})
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise StorageError("Could not create a secure receipt link.") from exc

        signed_path = payload.get("signedURL") or payload.get("signedUrl")
        if not isinstance(signed_path, str) or not signed_path:
            raise StorageError("Storage returned an invalid signed URL.")
        if signed_path.startswith("http://") or signed_path.startswith("https://"):
            return signed_path
        return f"{self._base_url}/storage/v1{signed_path if signed_path.startswith('/') else '/' + signed_path}"

    @staticmethod
    def _validate_path(object_path: str) -> None:
        if not object_path or object_path.startswith("/") or ".." in object_path.split("/"):
            raise ValueError("Storage path must be a relative path without traversal.")
