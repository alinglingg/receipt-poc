"""Postgres-backed persistence adapter for the receipt workflow."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import PendingConversation, ProcessingAttempt, Receipt, VendorMemory, WebhookEvent
from app.pipeline import DuplicateReceiptError, PendingReceipt, ReceiptDraft


class SqlAlchemyReceiptStore:
    """One short database transaction per workflow operation."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

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

    def find_vendor_category(self, normalized_vendor: str) -> str | None:
        with self._session_factory() as session:
            return session.scalar(
                select(VendorMemory.category).where(VendorMemory.normalized_name == normalized_vendor)
            )

    def is_duplicate(self, chat_id: int, normalized_vendor: str, receipt_date: date, total_amount: Decimal) -> bool:
        with self._session_factory() as session:
            return session.scalar(
                select(Receipt.id).where(
                    Receipt.chat_id == chat_id,
                    Receipt.vendor_normalized == normalized_vendor,
                    Receipt.receipt_date == receipt_date,
                    Receipt.total_amount == total_amount,
                )
            ) is not None

    def create_receipt(self, draft: ReceiptDraft) -> UUID:
        with self._session_factory() as session:
            receipt = Receipt(
                event_id=draft.event_id,
                chat_id=draft.chat_id,
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
                raise DuplicateReceiptError("The receipt violates the exact duplicate rule.") from exc
            return receipt.id

    def create_pending_conversation(self, chat_id: int, receipt_id: UUID) -> None:
        with self._session_factory() as session:
            session.add(PendingConversation(chat_id=chat_id, receipt_id=receipt_id))
            session.commit()

    def get_open_pending(self, chat_id: int) -> PendingReceipt | None:
        with self._session_factory() as session:
            row = session.execute(
                select(PendingConversation, Receipt)
                .join(Receipt, PendingConversation.receipt_id == Receipt.id)
                .where(PendingConversation.chat_id == chat_id, PendingConversation.status == "OPEN")
            ).first()
            if row is None:
                return None
            pending, receipt = row
            return PendingReceipt(
                receipt_id=pending.receipt_id,
                vendor_name=receipt.vendor_name,
                vendor_normalized=receipt.vendor_normalized,
                receipt_date=receipt.receipt_date,
                total_amount=receipt.total_amount,
                vat_amount=receipt.vat_amount,
                confidence=receipt.confidence,
                image_path=receipt.image_path,
            )

    def resolve_category(self, chat_id: int, receipt_id: UUID, category: str, normalized_vendor: str, display_vendor: str) -> None:
        with self._session_factory() as session:
            pending = session.scalar(
                select(PendingConversation).where(
                    PendingConversation.chat_id == chat_id,
                    PendingConversation.receipt_id == receipt_id,
                    PendingConversation.status == "OPEN",
                )
            )
            if pending is None:
                raise LookupError("The pending category request no longer exists.")
            receipt = session.get(Receipt, receipt_id)
            assert receipt is not None
            receipt.category = category
            receipt.status = "COMPLETED"
            pending.status = "RESOLVED"
            pending.resolved_at = datetime.now(timezone.utc)

            memory = session.get(VendorMemory, normalized_vendor)
            if memory is None:
                session.add(VendorMemory(normalized_name=normalized_vendor, display_name=display_vendor, category=category))
            else:
                memory.display_name = display_vendor
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
