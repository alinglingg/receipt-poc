"""Postgres-backed persistence adapter for the receipt workflow."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.corrections import recent_receipts, correct_receipt
from app.db import PendingConversation, ProcessingAttempt, Receipt, User, UserVendorMemory, WebhookEvent
from app.pipeline import DuplicateReceiptError, PendingReceiptError, PendingReceipt, ReceiptDraft
from app.review import validate_confirmation
from app.statuses import EventStatus, ReceiptStatus, PendingStatus, EVENT_TERMINAL_STATUSES


def _set_event_status(event: WebhookEvent, status: str, error_code: str | None = None) -> None:
    status = EventStatus(status)
    now = datetime.now(timezone.utc)
    if status == EventStatus.PROCESSING and event.processing_started_at is None:
        event.processing_started_at = now
    if status in EVENT_TERMINAL_STATUSES:
        if event.status != status or event.completed_at is None:
            event.completed_at = now
    else:
        event.completed_at = None
    event.status = status
    event.error_code = error_code
    event.updated_at = now



class SqlAlchemyReceiptStore:
    """One short database transaction per workflow operation."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def recent_receipts(self, user_id: UUID):
        return recent_receipts(self._session_factory, user_id)

    def correct_receipt(self, user_id: UUID, correction):
        return correct_receipt(self._session_factory, user_id, correction)

    def get_or_create_user(self, chat_id: int) -> UUID:
        """Resolve Telegram identity, including simultaneous first messages."""
        with self._session_factory() as session:
            existing = session.scalar(select(User.id).where(User.telegram_chat_id == chat_id))
            if existing is not None:
                return existing
            user = User(telegram_chat_id=chat_id)
            session.add(user)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.scalar(select(User.id).where(User.telegram_chat_id == chat_id))
                if existing is None:
                    raise
                return existing
            return user.id

    def create_webhook_event(self, *, update_id: int, chat_id: int, kind: str, file_id: str | None = None, text: str | None = None) -> WebhookEvent | None:
        with self._session_factory() as session:
            event = WebhookEvent(update_id=update_id, chat_id=chat_id, kind=kind, file_id=file_id, text=text)
            session.add(event)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return None  # Telegram retry: the update has already been accepted.
            session.refresh(event)
            session.expunge(event)
            return event

    def mark_event(self, event_id: UUID, status: str, error_code: str | None = None) -> None:
        with self._session_factory() as session:
            event = session.get(WebhookEvent, event_id)
            if event is None:
                raise LookupError(f"Webhook event {event_id} was not found.")
            _set_event_status(event, status, error_code)
            session.commit()

    def record_attempt(self, event_id: UUID, stage: str, success: bool, error_code: str | None = None) -> None:
        with self._session_factory() as session:
            attempt_number = session.scalar(
                select(func.count(ProcessingAttempt.id)).where(
                    ProcessingAttempt.event_id == event_id,
                    ProcessingAttempt.stage == stage,
                )
            )
            session.add(
                ProcessingAttempt(
                    event_id=event_id,
                    stage=stage,
                    attempt_number=int(attempt_number or 0) + 1,
                    success=success,
                    error_code=error_code,
                )
            )
            session.commit()

    def find_vendor_category(self, user_id: UUID, normalized_vendor: str) -> str | None:
        with self._session_factory() as session:
            return session.scalar(
                select(UserVendorMemory.category).where(
                    UserVendorMemory.user_id == user_id,
                    UserVendorMemory.normalized_name == normalized_vendor,
                )
            )

    def is_duplicate(self, user_id: UUID, normalized_vendor: str, receipt_date: date, total_amount: Decimal) -> bool:
        with self._session_factory() as session:
            return session.scalar(
                select(Receipt.id).where(
                    Receipt.user_id == user_id,
                    Receipt.vendor_normalized == normalized_vendor,
                    Receipt.receipt_date == receipt_date,
                    Receipt.total_amount == total_amount,
                )
            ) is not None

    def is_duplicate_image(self, user_id: UUID, image_sha256: str) -> bool:
        if not image_sha256:
            return False
        with self._session_factory() as session:
            return session.scalar(select(Receipt.id).where(
                Receipt.user_id == user_id,
                Receipt.image_sha256 == image_sha256,
            ).limit(1)) is not None

    def create_receipt(self, draft: ReceiptDraft) -> UUID:
        with self._session_factory() as session:
            owner = session.scalar(select(User.id).where(
                User.id == draft.user_id, User.telegram_chat_id == draft.chat_id,
            ).with_for_update())
            event = session.get(WebhookEvent, draft.event_id)
            if owner is None or event is None or event.chat_id != draft.chat_id:
                raise LookupError("Receipt owner does not match its Telegram event.")
            # Serialize saves per owner so concurrent OCR results cannot bypass
            # the image check. The early pipeline check alone is not atomic.
            if draft.image_sha256 and session.scalar(select(Receipt.id).where(
                Receipt.user_id == draft.user_id,
                Receipt.image_sha256 == draft.image_sha256,
            ).limit(1)) is not None:
                raise DuplicateReceiptError("This receipt image is already saved.")
            if session.scalar(select(PendingConversation.id).where(
                PendingConversation.user_id == draft.user_id,
                PendingConversation.status == PendingStatus.OPEN,
            )) is not None:
                raise PendingReceiptError("Finish the current receipt first.")
            saved_at = datetime.now(timezone.utc)
            receipt = Receipt(
                event_id=draft.event_id,
                chat_id=draft.chat_id,
                user_id=draft.user_id,
                vendor_name=draft.vendor_name,
                vendor_normalized=draft.vendor_normalized,
                receipt_date=draft.receipt_date,
                total_amount=draft.total_amount,
                vat_amount=draft.vat_amount,
                category=draft.category,
                confidence=draft.confidence,
                status=draft.status,
                image_path=draft.image_path,
                image_sha256=draft.image_sha256,
                raw_date_text=draft.raw_date_text,
                review_reason=draft.review_reason,
                failure_reason=draft.failure_reason,
                processing_started_at=event.processing_started_at,
                updated_at=saved_at,
                completed_at=saved_at if draft.status == ReceiptStatus.COMPLETED else None,
            )
            session.add(receipt)
            try:
                session.flush()
                if draft.status in (ReceiptStatus.PENDING_CATEGORY, ReceiptStatus.NEEDS_REVIEW):
                    session.add(PendingConversation(user_id=draft.user_id, chat_id=draft.chat_id, receipt_id=receipt.id))
                if draft.status in (ReceiptStatus.PENDING_CATEGORY, ReceiptStatus.NEEDS_REVIEW, ReceiptStatus.COMPLETED):
                    _set_event_status(event, draft.status)
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                # Do not disguise foreign-key/check failures as duplicates.
                constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
                duplicate_constraints = {
                    "receipts_user_exact_duplicate_key", "receipts_exact_duplicate_key",
                    # PostgreSQL truncates the autogenerated name from 001_init.sql.
                    "receipts_chat_id_vendor_normalized_receipt_date_total_amoun_key",
                    "receipts_event_id_key",
                }
                sqlite_duplicate = str(exc.orig).startswith("UNIQUE constraint failed: receipts.")
                if constraint in duplicate_constraints or sqlite_duplicate:
                    raise DuplicateReceiptError("The receipt violates a duplicate rule.") from exc
                raise
            return receipt.id

    def create_pending_conversation(self, user_id: UUID, receipt_id: UUID) -> None:
        with self._session_factory() as session:
            receipt = session.scalar(select(Receipt).where(Receipt.id == receipt_id, Receipt.user_id == user_id))
            if receipt is None:
                raise LookupError("Receipt was not found for this user.")
            # Receipt creation now attaches its pending conversation atomically.
            # Keep this legacy entry point idempotent for existing callers.
            if session.scalar(select(PendingConversation.id).where(PendingConversation.receipt_id == receipt_id)) is not None:
                return
            session.add(PendingConversation(user_id=user_id, chat_id=receipt.chat_id, receipt_id=receipt_id))
            session.commit()

    def get_open_pending(self, user_id: UUID) -> PendingReceipt | None:
        with self._session_factory() as session:
            row = session.execute(
                select(PendingConversation, Receipt)
                .join(Receipt, PendingConversation.receipt_id == Receipt.id)
                .where(PendingConversation.user_id == user_id, Receipt.user_id == user_id,
                       PendingConversation.status == PendingStatus.OPEN)
            ).first()
            if row is None:
                return None
            pending, receipt = row
            return PendingReceipt(
                receipt_id=pending.receipt_id,
                user_id=user_id,
                vendor_name=receipt.vendor_name,
                vendor_normalized=receipt.vendor_normalized,
                receipt_date=receipt.receipt_date,
                total_amount=receipt.total_amount,
                vat_amount=receipt.vat_amount,
                confidence=receipt.confidence,
                image_path=receipt.image_path,
                category=receipt.category,
                status=receipt.status,
                review_reason=receipt.review_reason,
                raw_date_text=receipt.raw_date_text,
            )

    def resolve_category(self, user_id: UUID, receipt_id: UUID, category: str) -> None:
        with self._session_factory() as session:
            # Serialize category writes for this user, including vendor-memory creation.
            owner = session.scalar(select(User).where(User.id == user_id).with_for_update())
            if owner is None:
                raise LookupError("User was not found.")
            pending = session.scalar(
                select(PendingConversation).where(
                    PendingConversation.user_id == user_id,
                    PendingConversation.receipt_id == receipt_id,
                    PendingConversation.status == PendingStatus.OPEN,
                )
            )
            if pending is None:
                raise LookupError("The pending category request no longer exists.")
            receipt = session.scalar(select(Receipt).where(Receipt.id == receipt_id, Receipt.user_id == user_id))
            if receipt is None:
                raise LookupError("Receipt was not found for this user.")
            if receipt.status != ReceiptStatus.PENDING_CATEGORY:
                raise LookupError("This receipt requires review before assigning its category.")
            receipt.category = category
            receipt.status = ReceiptStatus.COMPLETED
            receipt.completed_at = datetime.now(timezone.utc)
            receipt.updated_at = receipt.completed_at
            receipt.review_reason = None
            receipt.failure_reason = None
            pending.status = PendingStatus.RESOLVED
            pending.resolved_at = datetime.now(timezone.utc)

            memory = session.get(UserVendorMemory, (user_id, receipt.vendor_normalized))
            if memory is None:
                session.add(UserVendorMemory(user_id=user_id, normalized_name=receipt.vendor_normalized,
                                             display_name=receipt.vendor_name, category=category))
            else:
                memory.display_name = receipt.vendor_name
                memory.category = category

            original_event = session.get(WebhookEvent, receipt.event_id)
            if original_event is not None:
                _set_event_status(original_event, EventStatus.COMPLETED)
            session.commit()

    def _open_review(self, session, user_id: UUID, receipt_id: UUID):
        owner = session.scalar(select(User.id).where(User.id == user_id).with_for_update())
        if owner is None:
            raise LookupError("User was not found.")
        row = session.execute(select(PendingConversation, Receipt).join(
            Receipt, PendingConversation.receipt_id == Receipt.id,
        ).where(
            PendingConversation.user_id == user_id, Receipt.user_id == user_id,
            Receipt.id == receipt_id, PendingConversation.status == PendingStatus.OPEN,
            Receipt.status == ReceiptStatus.NEEDS_REVIEW,
        )).first()
        if row is None:
            raise LookupError("This review has already been resolved or is not yours.")
        return row

    def confirm_review(self, user_id: UUID, receipt_id: UUID, date_override: date | None = None) -> PendingReceipt:
        with self._session_factory() as session:
            pending, receipt = self._open_review(session, user_id, receipt_id)
            confirmed_date = validate_confirmation(receipt.receipt_date, receipt.total_amount,
                                                    receipt.vat_amount, receipt.review_reason, date_override)
            duplicate = session.scalar(select(Receipt.id).where(
                Receipt.user_id == user_id, Receipt.id != receipt_id,
                Receipt.vendor_normalized == receipt.vendor_normalized,
                Receipt.receipt_date == confirmed_date, Receipt.total_amount == receipt.total_amount,
            ).limit(1))
            if duplicate is not None:
                raise DuplicateReceiptError("The confirmed date matches an existing receipt.")
            event = session.get(WebhookEvent, receipt.event_id)
            receipt.receipt_date = confirmed_date
            receipt.review_reason = None
            receipt.updated_at = datetime.now(timezone.utc)
            receipt.status = ReceiptStatus.COMPLETED if receipt.category else ReceiptStatus.PENDING_CATEGORY
            if receipt.status == ReceiptStatus.COMPLETED:
                receipt.completed_at = receipt.updated_at
                pending.status = PendingStatus.RESOLVED
                pending.resolved_at = receipt.updated_at
            _set_event_status(event, receipt.status)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                # External SQL writers may race the check despite our per-user lock.
                if getattr(exc.orig, 'sqlstate', None) == '23505' or str(exc.orig).startswith('UNIQUE constraint failed: receipts.'):
                    raise DuplicateReceiptError("The confirmed receipt already exists.") from exc
                raise
            return PendingReceipt(receipt.id, receipt.user_id, receipt.vendor_name,
                                  receipt.vendor_normalized, receipt.receipt_date, receipt.total_amount,
                                  receipt.vat_amount, receipt.confidence, receipt.image_path,
                                  receipt.category, receipt.status, receipt.review_reason, receipt.raw_date_text)

    def retry_review(self, user_id: UUID, receipt_id: UUID) -> None:
        """Explicit RETRY discards only the caller's unconfirmed review draft."""
        with self._session_factory() as session:
            pending, receipt = self._open_review(session, user_id, receipt_id)
            event = session.get(WebhookEvent, receipt.event_id)
            _set_event_status(event, EventStatus.RETRY_REQUESTED, 'USER_RETRY')
            session.delete(pending)
            session.flush()
            session.delete(receipt)
            # Keep the webhook, attempt history and private image; no completed
            # receipt or learned vendor memory can be removed by this operation.
            session.commit()

    def unfinished_photo_events(self) -> list[WebhookEvent]:
        """Events recovered when a server restarted mid-processing."""
        with self._session_factory() as session:
            events = list(
                session.scalars(
                    select(WebhookEvent).where(
                        WebhookEvent.kind == "photo",
                        WebhookEvent.status.in_((EventStatus.RECEIVED, EventStatus.PROCESSING)),
                    )
                )
            )
            for event in events:
                session.expunge(event)
            return events
