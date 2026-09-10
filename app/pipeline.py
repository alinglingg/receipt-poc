"""Business workflow for receipt processing and category follow-up."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from app.imaging import InvalidReceiptImage, PreparedImage, prepare_receipt_image
from app.storage import StorageError
from app.vision import ReceiptExtraction, VisionExtractionError, is_usable_extraction


class DuplicateReceiptError(RuntimeError):
    """Raised by the database adapter when its unique duplicate rule fires."""


@dataclass(frozen=True)
class ReceiptDraft:
    event_id: UUID
    chat_id: int
    vendor_name: str
    vendor_normalized: str
    receipt_date: date
    total_amount: Decimal
    vat_amount: Decimal | None
    category: str | None
    confidence: str
    status: str
    image_path: str
    image_sha256: str


@dataclass(frozen=True)
class PendingReceipt:
    receipt_id: UUID
    vendor_name: str
    vendor_normalized: str
    receipt_date: date
    total_amount: Decimal
    vat_amount: Decimal | None
    confidence: str
    image_path: str


class ReceiptStore(Protocol):
    def mark_event(self, event_id: UUID, status: str, error_code: str | None = None) -> None: ...
    def record_attempt(self, event_id: UUID, stage: str, success: bool, error_code: str | None = None) -> None: ...
    def find_vendor_category(self, normalized_vendor: str) -> str | None: ...
    def is_duplicate(self, chat_id: int, normalized_vendor: str, receipt_date: date, total_amount: Decimal) -> bool: ...
    def create_receipt(self, draft: ReceiptDraft) -> UUID: ...
    def create_pending_conversation(self, chat_id: int, receipt_id: UUID) -> None: ...
    def get_open_pending(self, chat_id: int) -> PendingReceipt | None: ...
    def resolve_category(self, chat_id: int, receipt_id: UUID, category: str, normalized_vendor: str, display_vendor: str) -> None: ...


class ReceiptNotifier(Protocol):
    async def send(self, chat_id: int, markdown: str) -> None: ...


class ReceiptStorage(Protocol):
    async def upload_receipt(self, *, object_path: str, content: bytes) -> str: ...
    async def create_signed_url(self, *, object_path: str, expires_in_seconds: int = 900) -> str: ...


class ReceiptVision(Protocol):
    async def extract(self, jpeg_bytes: bytes) -> ReceiptExtraction: ...


def normalize_vendor(name: str) -> str:
    return "".join(character for character in name.upper() if character.isalnum())


class ReceiptPipeline:
    def __init__(self, *, store: ReceiptStore, vision: ReceiptVision, storage: ReceiptStorage, notifier: ReceiptNotifier) -> None:
        self._store = store
        self._vision = vision
        self._storage = storage
        self._notifier = notifier

    async def process_photo(self, *, event_id: UUID, chat_id: int, image_bytes: bytes) -> None:
        self._store.mark_event(event_id, "PROCESSING")
        try:
            prepared = prepare_receipt_image(image_bytes)
            self._store.record_attempt(event_id, "imaging", True)
        except InvalidReceiptImage:
            await self._request_retry(event_id, chat_id, "INVALID_IMAGE")
            return

        try:
            extraction = await self._vision.extract(prepared.content)
            self._store.record_attempt(event_id, "vision", True)
        except VisionExtractionError:
            self._store.record_attempt(event_id, "vision", False, "VISION_ERROR")
            await self._request_retry(event_id, chat_id, "VISION_ERROR")
            return

        if not is_usable_extraction(extraction):
            await self._request_retry(event_id, chat_id, "LOW_CONFIDENCE")
            return

        vendor_normalized = normalize_vendor(extraction.vendor_name)
        if self._store.is_duplicate(chat_id, vendor_normalized, extraction.receipt_date, extraction.total_amount):
            self._store.mark_event(event_id, "DUPLICATE")
            await self._notifier.send(chat_id, "⚠️ *Duplicate receipt detected*\n\nI found the same vendor, date, and total already recorded. No new entry was created.")
            return

        category = self._store.find_vendor_category(vendor_normalized)
        try:
            image_path = await self._storage.upload_receipt(
                object_path=f"{chat_id}/{event_id}.jpg", content=prepared.content
            )
            receipt_id = self._store.create_receipt(
                ReceiptDraft(
                    event_id=event_id,
                    chat_id=chat_id,
                    vendor_name=extraction.vendor_name,
                    vendor_normalized=vendor_normalized,
                    receipt_date=extraction.receipt_date,
                    total_amount=extraction.total_amount,
                    vat_amount=extraction.vat_amount,
                    category=category,
                    confidence=extraction.confidence_score.value,
                    status="COMPLETED" if category else "PENDING_CATEGORY",
                    image_path=image_path,
                    image_sha256=prepared.sha256,
                )
            )
            self._store.record_attempt(event_id, "storage_and_save", True)
        except DuplicateReceiptError:
            self._store.mark_event(event_id, "DUPLICATE")
            await self._notifier.send(chat_id, "⚠️ *Duplicate receipt detected*\n\nNo new entry was created.")
            return
        except StorageError:
            self._store.record_attempt(event_id, "storage_and_save", False, "STORAGE_ERROR")
            await self._request_retry(event_id, chat_id, "STORAGE_ERROR")
            return

        if category is None:
            self._store.create_pending_conversation(chat_id, receipt_id)
            self._store.mark_event(event_id, "PENDING_CATEGORY")
            await self._notifier.send(chat_id, f"❓ *Unrecognized vendor:* {extraction.vendor_name}\n\nWhich expense category should I assign this to?")
            return

        self._store.mark_event(event_id, "COMPLETED")
        await self._send_summary(chat_id, extraction, category, image_path)

    async def process_category_reply(self, *, event_id: UUID, chat_id: int, category: str) -> None:
        pending = self._store.get_open_pending(chat_id)
        category = " ".join(category.split())
        if pending is None:
            self._store.mark_event(event_id, "COMPLETED")
            await self._notifier.send(chat_id, "Send me a receipt image first, then I can record its category.")
            return
        if not category or len(category) > 100:
            self._store.mark_event(event_id, "RETRY_REQUESTED", "INVALID_CATEGORY")
            await self._notifier.send(chat_id, "Please reply with a short expense category, for example `Food Supplies`.")
            return

        self._store.resolve_category(chat_id, pending.receipt_id, category, pending.vendor_normalized, pending.vendor_name)
        self._store.mark_event(event_id, "COMPLETED")
        await self._notifier.send(chat_id, f"✅ Category saved: *{category}*\n\nFuture receipts from *{pending.vendor_name}* will use this category.")
        extraction = ReceiptExtraction(
            Vendor_Name=pending.vendor_name,
            Date=pending.receipt_date.strftime("%d/%m/%Y"),
            Total_Amount=pending.total_amount,
            VAT_Amount=pending.vat_amount,
            Category=category,
            Confidence_Score=pending.confidence,
        )
        await self._send_summary(chat_id, extraction, category, pending.image_path)

    async def _request_retry(self, event_id: UUID, chat_id: int, error_code: str) -> None:
        self._store.mark_event(event_id, "RETRY_REQUESTED", error_code)
        await self._notifier.send(chat_id, "⚠️ I couldn’t read that receipt reliably. Please send a clearer, well-lit photo showing the full receipt.")

    async def _send_summary(self, chat_id: int, extraction: ReceiptExtraction, category: str, image_path: str) -> None:
        signed_url = await self._storage.create_signed_url(object_path=image_path)
        vat = f"{extraction.vat_amount:.2f}" if extraction.vat_amount is not None else "Not shown"
        await self._notifier.send(
            chat_id,
            "✅ *Receipt recorded*\n\n"
            f"*Vendor:* {extraction.vendor_name}\n"
            f"*Date:* {extraction.receipt_date.strftime('%d/%m/%Y')}\n"
            f"*Total:* {extraction.total_amount:.2f}\n"
            f"*VAT:* {vat}\n"
            f"*Category:* {category}\n"
            f"*Confidence:* {extraction.confidence_score.value}\n\n"
            f"[View receipt securely]({signed_url}) _(link expires in 15 minutes)_",
        )
