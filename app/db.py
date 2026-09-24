from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, Numeric, String, Text, UniqueConstraint, create_engine, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from app.config import get_settings
from app.statuses import EVENT_STATUSES, RECEIPT_STATUSES, EventStatus, PendingStatus


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("id", "telegram_chat_id", name="users_id_chat_key"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=EventStatus.RECEIVED)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    receipt: Mapped[Receipt | None] = relationship(back_populates="event", uselist=False)
    attempts: Mapped[list[ProcessingAttempt]] = relationship(back_populates="event")


class Receipt(Base):
    __tablename__ = "receipts"
    __table_args__ = (
        UniqueConstraint("event_id", name="receipts_event_id_key"),
        UniqueConstraint("chat_id", "vendor_normalized", "receipt_date", "total_amount", name="receipts_exact_duplicate_key"),
        UniqueConstraint("user_id", "vendor_normalized", "receipt_date", "total_amount", name="receipts_user_exact_duplicate_key"),
        UniqueConstraint("id", "user_id", name="receipts_id_user_key"),
        ForeignKeyConstraint(["user_id", "chat_id"], ["users.id", "users.telegram_chat_id"], name="receipts_user_chat_fkey"),
        CheckConstraint(f"status IN {RECEIPT_STATUSES}", name="receipts_valid_status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("webhook_events.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
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

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(Text)

    event: Mapped[WebhookEvent] = relationship(back_populates="receipt")
    pending_conversation: Mapped[PendingConversation | None] = relationship(
        back_populates="receipt", uselist=False,
        primaryjoin="Receipt.id == PendingConversation.receipt_id",
        foreign_keys="PendingConversation.receipt_id",
    )


class VendorMemory(Base):
    """Legacy global memory, retained for migration history only."""
    __tablename__ = "vendor_memory"

    normalized_name: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class UserVendorMemory(Base):
    __tablename__ = "user_vendor_memory"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    normalized_name: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PendingConversation(Base):
    __tablename__ = "pending_conversations"
    __table_args__ = (
        CheckConstraint("status IN ('OPEN', 'RESOLVED')", name="pending_conversations_valid_status"),
        ForeignKeyConstraint(["user_id", "chat_id"], ["users.id", "users.telegram_chat_id"], name="pending_conversations_user_chat_fkey"),
        ForeignKeyConstraint(["receipt_id", "user_id"], ["receipts.id", "receipts.user_id"], name="pending_conversations_receipt_user_fkey"),
        Index("pending_conversations_one_open_per_chat", "chat_id", unique=True,
              postgresql_where=text("status = 'OPEN'"), sqlite_where=text("status = 'OPEN'")),
        Index("pending_conversations_one_open_per_user", "user_id", unique=True,
              postgresql_where=text("status = 'OPEN'"), sqlite_where=text("status = 'OPEN'")),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    receipt_id: Mapped[UUID] = mapped_column(ForeignKey("receipts.id", ondelete="CASCADE"), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PendingStatus.OPEN)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    receipt: Mapped[Receipt] = relationship(
        back_populates="pending_conversation",
        primaryjoin="Receipt.id == PendingConversation.receipt_id",
        foreign_keys=[receipt_id],
    )


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
