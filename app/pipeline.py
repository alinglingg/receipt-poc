"""Business workflow for receipt processing and category follow-up."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from app.imaging import InvalidReceiptImage, prepare_receipt_image
from app.storage import StorageError
from app.statuses import EventStatus, ReceiptStatus
from app.review import review_reasons, parse_confirmation, escape_markdown, REASON_LABELS, DATE_REASONS
from app.vision import ReceiptExtraction, VisionExtractionError, is_usable_extraction


class PendingReceiptError(RuntimeError):
    """A user must finish the existing receipt conversation first."""


class DuplicateReceiptError(RuntimeError):
    """Raised by the database adapter when its unique duplicate rule fires."""


@dataclass(frozen=True)
class ReceiptDraft:
    event_id: UUID
    chat_id: int
    user_id: UUID
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
    raw_date_text: str | None = None
    review_reason: str | None = None
    failure_reason: str | None = None


@dataclass(frozen=True)
class PendingReceipt:
    receipt_id: UUID
    user_id: UUID
    vendor_name: str
    vendor_normalized: str
    receipt_date: date
    total_amount: Decimal
    vat_amount: Decimal | None
    confidence: str
    image_path: str
    category: str | None = None
    status: str = ReceiptStatus.PENDING_CATEGORY
    review_reason: str | None = None
    raw_date_text: str | None = None


class ReceiptStore(Protocol):
    def get_or_create_user(self, chat_id: int) -> UUID: ...
    def mark_event(self, event_id: UUID, status: str, error_code: str | None = None) -> None: ...
    def record_attempt(self, event_id: UUID, stage: str, success: bool, error_code: str | None = None) -> None: ...
    def find_vendor_category(self, user_id: UUID, normalized_vendor: str) -> str | None: ...
    def is_duplicate(self, user_id: UUID, normalized_vendor: str, receipt_date: date, total_amount: Decimal) -> bool: ...
    def is_duplicate_image(self, user_id: UUID, image_sha256: str) -> bool: ...
    def create_receipt(self, draft: ReceiptDraft) -> UUID: ...
    def create_pending_conversation(self, user_id: UUID, receipt_id: UUID) -> None: ...
    def get_open_pending(self, user_id: UUID) -> PendingReceipt | None: ...
    def resolve_category(self, user_id: UUID, receipt_id: UUID, category: str) -> None: ...
    def confirm_review(self, user_id: UUID, receipt_id: UUID, date_override: date | None = None) -> PendingReceipt: ...
    def retry_review(self, user_id: UUID, receipt_id: UUID) -> None: ...


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
        user_id = self._store.get_or_create_user(chat_id)
        self._store.mark_event(event_id, EventStatus.PROCESSING)
        try:
            prepared = prepare_receipt_image(image_bytes)
            self._store.record_attempt(event_id, "imaging", True)
        except InvalidReceiptImage:
            await self._request_retry(event_id, chat_id, "INVALID_IMAGE")
            return

        if self._store.is_duplicate_image(user_id, prepared.sha256):
            self._store.mark_event(event_id, EventStatus.DUPLICATE)
            await self._notifier.send(chat_id, "⚠️ *Duplicate receipt detected*\n\nThis image is already saved. No new entry was created.")
            return

        pending = self._store.get_open_pending(user_id)
        if pending is not None:
            self._store.mark_event(event_id, EventStatus.RETRY_REQUESTED, "PENDING_RECEIPT")
            await self._send_pending_prompt(chat_id, pending)
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
        if not vendor_normalized:
            await self._request_retry(event_id, chat_id, "INVALID_VENDOR")
            return
        if self._store.is_duplicate(user_id, vendor_normalized, extraction.receipt_date, extraction.total_amount):
            self._store.mark_event(event_id, EventStatus.DUPLICATE)
            await self._notifier.send(chat_id, "⚠️ *Duplicate receipt detected*\n\nI found the same vendor, date, and total already recorded. No new entry was created.")
            return

        category = self._store.find_vendor_category(user_id, vendor_normalized)
        reasons = review_reasons(extraction)
        receipt_status = (ReceiptStatus.NEEDS_REVIEW if reasons else
                          ReceiptStatus.COMPLETED if category else ReceiptStatus.PENDING_CATEGORY)
        try:
            image_path = await self._storage.upload_receipt(
                object_path=f"{chat_id}/{event_id}.jpg", content=prepared.content
            )
            receipt_id = self._store.create_receipt(
                ReceiptDraft(
                    event_id=event_id,
                    chat_id=chat_id,
                    user_id=user_id,
                    vendor_name=extraction.vendor_name,
                    vendor_normalized=vendor_normalized,
                    receipt_date=extraction.receipt_date,
                    total_amount=extraction.total_amount,
                    vat_amount=extraction.vat_amount,
                    category=category,
                    confidence=extraction.confidence_score.value,
                    status=receipt_status,
                    review_reason=",".join(reasons) or None,
                    raw_date_text=extraction.raw_date_text,
                    image_path=image_path,
                    image_sha256=prepared.sha256,
                )
            )
            self._store.record_attempt(event_id, "storage_and_save", True)
        except DuplicateReceiptError:
            self._store.mark_event(event_id, EventStatus.DUPLICATE)
            await self._notifier.send(chat_id, "⚠️ *Duplicate receipt detected*\n\nNo new entry was created.")
            return
        except PendingReceiptError:
            self._store.mark_event(event_id, EventStatus.RETRY_REQUESTED, "PENDING_RECEIPT")
            pending = self._store.get_open_pending(user_id)
            if pending is not None:
                await self._send_pending_prompt(chat_id, pending)
            else:
                await self._notifier.send(chat_id, "The earlier receipt was just resolved. Please resend this photo.")
            return
        except StorageError:
            self._store.record_attempt(event_id, "storage_and_save", False, "STORAGE_ERROR")
            await self._request_retry(event_id, chat_id, "STORAGE_ERROR")
            return

        # The receipt, event status and pending conversation were committed together.
        # Do not overwrite a status that another worker may already have resolved.
        if reasons or category is None:
            pending = self._store.get_open_pending(user_id)
            if pending is None or pending.receipt_id != receipt_id:
                return
            if reasons:
                await self._send_pending_prompt(chat_id, pending)
            else:
                await self._notifier.send(chat_id, f"❓ *Unrecognized vendor:* {escape_markdown(extraction.vendor_name)}\n\nWhich expense category should I assign this to?")
            return

        await self._send_summary(chat_id, extraction, category, image_path)

    async def process_category_reply(self, *, event_id: UUID, chat_id: int, category: str) -> None:
        user_id = self._store.get_or_create_user(chat_id)
        pending = self._store.get_open_pending(user_id)
        category = " ".join(category.split())
        if pending is None:
            self._store.mark_event(event_id, EventStatus.COMPLETED)
            await self._notifier.send(chat_id, "Send me a receipt image first, then I can record its category.")
            return
        if pending.status == ReceiptStatus.NEEDS_REVIEW:
            await self._process_review_reply(event_id, chat_id, pending, category)
            return
        if category.upper().split(" ", 1)[0] in {"CONFIRM", "REVIEW", "RETRY"}:
            self._store.mark_event(event_id, EventStatus.RETRY_REQUESTED, "CATEGORY_REQUIRED")
            await self._send_pending_prompt(chat_id, pending)
            return
        if not category or len(category) > 100:
            self._store.mark_event(event_id, EventStatus.RETRY_REQUESTED, "INVALID_CATEGORY")
            await self._notifier.send(chat_id, "Please reply with a short expense category, for example `Food Supplies`.")
            return

        try:
            self._store.resolve_category(user_id, pending.receipt_id, category)
        except LookupError:
            self._store.mark_event(event_id, EventStatus.COMPLETED)
            await self._notifier.send(chat_id, "That category request has already changed. Please check the latest bot message.")
            return
        self._store.mark_event(event_id, EventStatus.COMPLETED)
        await self._notifier.send(chat_id, f"✅ Category saved: *{escape_markdown(category)}*\n\nFuture receipts from *{escape_markdown(pending.vendor_name)}* will use this category.")
        extraction = ReceiptExtraction(
            Vendor_Name=pending.vendor_name,
            Date=pending.receipt_date.strftime("%d/%m/%Y"),
            Total_Amount=pending.total_amount,
            VAT_Amount=pending.vat_amount,
            Category=category,
            Confidence_Score=pending.confidence,
        )
        await self._send_summary(chat_id, extraction, category, pending.image_path)

    async def _send_pending_prompt(self, chat_id: int, pending: PendingReceipt) -> None:
        vendor = escape_markdown(pending.vendor_name)
        if pending.status != ReceiptStatus.NEEDS_REVIEW:
            await self._notifier.send(chat_id, f"Please finish the category for *{vendor}* before sending another receipt. Reply with an expense category.")
            return
        reasons = (pending.review_reason or '').split(',')
        reason_text = '\n'.join(REASON_LABELS.get(reason, 'Please check the extracted values.') for reason in reasons)
        vat = f'{pending.vat_amount:.2f}' if pending.vat_amount is not None else 'Not shown'
        category = escape_markdown(pending.category) if pending.category else 'Not assigned (asked after confirmation)'
        date_help = ('The date needs clarification. Reply `CONFIRM YYYY-MM-DD` with the correct date.'
                     if set(reasons).intersection(DATE_REASONS) else
                     'Reply `CONFIRM` if everything is correct, or `CONFIRM YYYY-MM-DD` to confirm with a corrected date.')
        signed_url = await self._storage.create_signed_url(object_path=pending.image_path)
        await self._notifier.send(chat_id,
            '⚠️ *Please review this receipt*\n\n'
            f'*Vendor:* {vendor}\n*Date:* {pending.receipt_date.strftime("%d %B %Y")}\n'
            f'*Printed date:* {escape_markdown(pending.raw_date_text or "Not available")}\n'
            f'*Total:* {pending.total_amount:.2f}\n*VAT:* {vat}\n*Category:* {category}\n\n'
            f'{reason_text}\n\n{date_help}\n'
            'For example, `CONFIRM 2026-09-10` means 10 September 2026.\n'
            'If other values are wrong, reply `RETRY` to discard this unconfirmed draft and send a clearer photo.\n\n'
            f'[View receipt securely]({signed_url}) _(link expires in 15 minutes)_')

    async def _process_review_reply(self, event_id: UUID, chat_id: int, pending: PendingReceipt, message: str) -> None:
        try:
            if message.upper() == 'REVIEW':
                await self._send_pending_prompt(chat_id, pending)
            elif message.upper() == 'RETRY':
                self._store.retry_review(pending.user_id, pending.receipt_id)
                await self._notifier.send(chat_id, 'Unconfirmed draft discarded. Please send a clearer photo of the receipt.')
            else:
                override = parse_confirmation(message)
                confirmed = self._store.confirm_review(pending.user_id, pending.receipt_id, override)
                if confirmed.status == ReceiptStatus.PENDING_CATEGORY:
                    await self._notifier.send(chat_id, f'✅ Receipt details confirmed.\n\nWhich expense category should I assign to *{escape_markdown(confirmed.vendor_name)}*?')
                else:
                    extraction = ReceiptExtraction(
                        Vendor_Name=confirmed.vendor_name, Date=confirmed.receipt_date,
                        Date_Text=confirmed.raw_date_text, Total_Amount=confirmed.total_amount,
                        VAT_Amount=confirmed.vat_amount, Category=confirmed.category,
                        Confidence_Score=confirmed.confidence,
                    )
                    await self._send_summary(chat_id, extraction, confirmed.category, confirmed.image_path)
        except ValueError as exc:
            self._store.mark_event(event_id, EventStatus.RETRY_REQUESTED, 'INVALID_CONFIRMATION')
            await self._notifier.send(chat_id, str(exc))
            return
        except DuplicateReceiptError:
            self._store.mark_event(event_id, EventStatus.RETRY_REQUESTED, 'REVIEW_DUPLICATE')
            await self._notifier.send(chat_id, 'That date matches a receipt already saved for you. This draft remains unconfirmed. Reply RETRY to discard it, or CONFIRM YYYY-MM-DD with the correct date.')
            return
        except LookupError:
            await self._notifier.send(chat_id, 'That review has already changed. Please check the latest bot message.')
        self._store.mark_event(event_id, EventStatus.COMPLETED)

    async def _request_retry(self, event_id: UUID, chat_id: int, error_code: str) -> None:
        self._store.mark_event(event_id, EventStatus.RETRY_REQUESTED, error_code)
        await self._notifier.send(chat_id, "⚠️ I couldn’t read that receipt reliably. Please send a clearer, well-lit photo showing the full receipt.")

    async def _send_summary(self, chat_id: int, extraction: ReceiptExtraction, category: str, image_path: str) -> None:
        signed_url = await self._storage.create_signed_url(object_path=image_path)
        vat = f"{extraction.vat_amount:.2f}" if extraction.vat_amount is not None else "Not shown"
        await self._notifier.send(
            chat_id,
            "✅ *Receipt recorded*\n\n"
            f"*Vendor:* {escape_markdown(extraction.vendor_name)}\n"
            f"*Date:* {extraction.receipt_date.strftime('%d/%m/%Y')}\n"
            f"*Total:* {extraction.total_amount:.2f}\n"
            f"*VAT:* {vat}\n"
            f"*Category:* {escape_markdown(category)}\n"
            f"*Confidence:* {extraction.confidence_score.value}\n\n"
            f"[View receipt securely]({signed_url}) _(link expires in 15 minutes)_",
        )
