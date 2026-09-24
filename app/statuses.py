"""Persisted lifecycle values shared by models and workflow code."""
from enum import StrEnum


class EventStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    PENDING_CATEGORY = "PENDING_CATEGORY"
    COMPLETED = "COMPLETED"
    DUPLICATE = "DUPLICATE"
    RETRY_REQUESTED = "RETRY_REQUESTED"
    FAILED = "FAILED"


class ReceiptStatus(StrEnum):
    PROCESSING = "PROCESSING"
    PENDING_CATEGORY = "PENDING_CATEGORY"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    COMPLETED = "COMPLETED"
    DUPLICATE = "DUPLICATE"
    FAILED = "FAILED"


class PendingStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


EVENT_STATUSES = tuple(status.value for status in EventStatus)
RECEIPT_STATUSES = tuple(status.value for status in ReceiptStatus)
EVENT_TERMINAL_STATUSES = frozenset({
    EventStatus.COMPLETED, EventStatus.DUPLICATE, EventStatus.RETRY_REQUESTED, EventStatus.FAILED,
})
