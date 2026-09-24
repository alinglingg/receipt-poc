"""Shared fakes and receipt fixtures; no external API calls."""
from io import BytesIO
from uuid import UUID, uuid4

from PIL import Image

from app.pipeline import PendingReceipt, ReceiptDraft
from app.vision import ConfidenceScore, ReceiptExtraction


def image_bytes(color="white") -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 32), color).save(output, format="JPEG")
    return output.getvalue()


def extraction(
    confidence: ConfidenceScore = ConfidenceScore.HIGH,
) -> ReceiptExtraction:
    return ReceiptExtraction(
        Vendor_Name="Acme Supplies",
        Date="10/09/2026",
        Date_Text="10 September 2026",
        Total_Amount="125.50",
        VAT_Amount="15.50",
        Category="Maintenance",
        Confidence_Score=confidence,
    )


class FakeStore:
    def __init__(
        self,
        *,
        category: str | None = "Maintenance",
        duplicate: bool = False,
    ) -> None:
        self.category = category
        self.duplicate = duplicate
        self.events: list[tuple[str, str, str | None]] = []
        self.drafts: list[ReceiptDraft] = []
        self.pending: PendingReceipt | None = None

    def get_or_create_user(self, chat_id: int) -> UUID:
        return UUID(int=chat_id)

    def mark_event(
        self,
        event_id: UUID,
        status: str,
        error_code: str | None = None,
    ) -> None:
        self.events.append((str(event_id), status, error_code))

    def record_attempt(self, *args: object, **kwargs: object) -> None:
        pass

    def find_vendor_category(
        self,
        user_id: UUID,
        normalized_vendor: str,
    ) -> str | None:
        return self.category

    def is_duplicate(self, *args: object) -> bool:
        return self.duplicate

    def is_duplicate_image(self, user_id, image_sha256):
        return any(d.user_id == user_id and d.image_sha256 == image_sha256 for d in self.drafts)

    def create_receipt(self, draft: ReceiptDraft) -> UUID:
        self.drafts.append(draft)
        receipt_id = uuid4()

        if draft.status in ("PENDING_CATEGORY", "NEEDS_REVIEW"):
            self.pending = PendingReceipt(
                receipt_id=receipt_id,
                user_id=draft.user_id,
                vendor_name=draft.vendor_name,
                vendor_normalized=draft.vendor_normalized,
                receipt_date=draft.receipt_date,
                total_amount=draft.total_amount,
                vat_amount=draft.vat_amount,
                confidence=draft.confidence,
                image_path=draft.image_path,
                category=draft.category,
                status=draft.status,
                review_reason=draft.review_reason,
                raw_date_text=draft.raw_date_text,
            )

        self.mark_event(draft.event_id, draft.status)
        return receipt_id

    def create_pending_conversation(
        self,
        user_id: UUID,
        receipt_id: UUID,
    ) -> None:
        pass

    def get_open_pending(
        self,
        user_id: UUID,
    ) -> PendingReceipt | None:
        return self.pending

    def resolve_category(
        self,
        user_id: UUID,
        receipt_id: UUID,
        category: str,
    ) -> None:
        self.category = category
        self.pending = None


class FakeVision:
    def __init__(self, result: ReceiptExtraction) -> None:
        self.result = result

    async def extract(
        self,
        jpeg_bytes: bytes,
    ) -> ReceiptExtraction:
        return self.result


class FakeStorage:
    async def upload_receipt(
        self,
        *,
        object_path: str,
        content: bytes,
    ) -> str:
        return object_path

    async def create_signed_url(
        self,
        *,
        object_path: str,
        expires_in_seconds: int = 900,
    ) -> str:
        return f"https://storage.test/{object_path}?temporary=true"


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(
        self,
        chat_id: int,
        markdown: str,
    ) -> None:
        self.messages.append(markdown)


