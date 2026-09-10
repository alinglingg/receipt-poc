from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, create_engine, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from app.config import get_settings


EVENT_STATUSES = (
    "RECEIVED", "PROCESSING", "PENDING_CATEGORY", "COMPLETED", "DUPLICATE", "RETRY_REQUESTED", "FAILED"
)
RECEIPT_STATUSES = ("PENDING_CATEGORY", "COMPLETED")


class Base(DeclarativeBase):
    pass


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (
        CheckConstraint(f"status IN {EVENT_STATUSES}", name="webhook_events_valid_status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    update_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    file_id: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RECEIVED")
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    receipt: Mapped[Receipt | None] = relationship(back_populates="event", uselist=False)
    attempts: Mapped[list[ProcessingAttempt]] = relationship(back_populates="event")


class Receipt(Base):
    __tablename__ = "receipts"
    __table_args__ = (
        UniqueConstraint("event_id", name="receipts_event_id_key"),
        UniqueConstraint("chat_id", "vendor_normalized", "receipt_date", "total_amount", name="receipts_exact_duplicate_key"),
        CheckConstraint(f"status IN {RECEIPT_STATUSES}", name="receipts_valid_status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("webhook_events.id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    vendor_name: Mapped[str] = mapped_column(Text, nullable=False)
    vendor_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    receipt_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    vat_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    category: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    image_path: Mapped[str] = mapped_column(Text, nullable=False)
    image_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    event: Mapped[WebhookEvent] = relationship(back_populates="receipt")
    pending_conversation: Mapped[PendingConversation | None] = relationship(back_populates="receipt", uselist=False)


class VendorMemory(Base):
    __tablename__ = "vendor_memory"

    normalized_name: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PendingConversation(Base):
    __tablename__ = "pending_conversations"
    __table_args__ = (CheckConstraint("status IN ('OPEN', 'RESOLVED')", name="pending_conversations_valid_status"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    receipt_id: Mapped[UUID] = mapped_column(ForeignKey("receipts.id", ondelete="CASCADE"), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    receipt: Mapped[Receipt] = relationship(back_populates="pending_conversation")


class ProcessingAttempt(Base):
    __tablename__ = "processing_attempts"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("webhook_events.id", ondelete="CASCADE"), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    event: Mapped[WebhookEvent] = relationship(back_populates="attempts")


def create_session_factory():
    engine = create_engine(get_settings().require_database_url(), pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False)
