"""Postgres-backed persistence adapter for the receipt workflow."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import PendingConversation, ProcessingAttempt, Receipt, User, UserVendorMemory, WebhookEvent
from app.pipeline import DuplicateReceiptError, PendingReceipt, ReceiptDraft


class SqlAlchemyReceiptStore:
    """One short database transaction per workflow operation."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

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
            event.status = status
            event.error_code = error_code
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
            event_chat = session.scalar(select(WebhookEvent.chat_id).where(WebhookEvent.id == draft.event_id))
            if owner is None or event_chat != draft.chat_id:
                raise LookupError("Receipt owner does not match its Telegram event.")
            # Serialize saves per owner so concurrent OCR results cannot bypass
            # the image check. The early pipeline check alone is not atomic.
            if draft.image_sha256 and session.scalar(select(Receipt.id).where(
                Receipt.user_id == draft.user_id,
                Receipt.image_sha256 == draft.image_sha256,
            ).limit(1)) is not None:
                raise DuplicateReceiptError("This receipt image is already saved.")
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
            )
            session.add(receipt)
            try:
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
            session.add(PendingConversation(user_id=user_id, chat_id=receipt.chat_id, receipt_id=receipt_id))
            session.commit()

    def get_open_pending(self, user_id: UUID) -> PendingReceipt | None:
        with self._session_factory() as session:
            row = session.execute(
                select(PendingConversation, Receipt)
                .join(Receipt, PendingConversation.receipt_id == Receipt.id)
                .where(PendingConversation.user_id == user_id, Receipt.user_id == user_id,
                       PendingConversation.status == "OPEN")
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
                    PendingConversation.status == "OPEN",
                )
            )
            if pending is None:
                raise LookupError("The pending category request no longer exists.")
            receipt = session.scalar(select(Receipt).where(Receipt.id == receipt_id, Receipt.user_id == user_id))
            if receipt is None:
                raise LookupError("Receipt was not found for this user.")
            receipt.category = category
            receipt.status = "COMPLETED"
            pending.status = "RESOLVED"
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
                original_event.status = "COMPLETED"
            session.commit()

    def unfinished_photo_events(self) -> list[WebhookEvent]:
        """Events recovered when a server restarted mid-processing."""
        with self._session_factory() as session:
            events = list(
                session.scalars(
                    select(WebhookEvent).where(
                        WebhookEvent.kind == "photo",
                        WebhookEvent.status.in_(("RECEIVED", "PROCESSING")),
                    )
                )
            )
            for event in events:
                session.expunge(event)
            return events
