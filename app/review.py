"""Deterministic review decisions and deliberately small confirmation grammar."""
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
import re

from app.vision import ConfidenceScore, ReceiptExtraction


class ReviewReason(StrEnum):
    MEDIUM_CONFIDENCE = 'MEDIUM_CONFIDENCE'
    AMBIGUOUS_DATE = 'AMBIGUOUS_DATE'
    DATE_UNVERIFIED = 'DATE_UNVERIFIED'
    FUTURE_DATE = 'FUTURE_DATE'
    VAT_EXCEEDS_TOTAL = 'VAT_EXCEEDS_TOTAL'


DATE_REASONS = {ReviewReason.AMBIGUOUS_DATE, ReviewReason.DATE_UNVERIFIED, ReviewReason.FUTURE_DATE}
REASON_LABELS = {
    ReviewReason.MEDIUM_CONFIDENCE: 'The extraction needs your review.',
    ReviewReason.AMBIGUOUS_DATE: 'The printed date can mean two different dates.',
    ReviewReason.DATE_UNVERIFIED: 'The date could not be verified against the printed text.',
    ReviewReason.FUTURE_DATE: 'The extracted date is in the future.',
    ReviewReason.VAT_EXCEEDS_TOTAL: 'VAT is greater than the total.',
}


def today() -> date:
    return datetime.now(timezone(timedelta(hours=8))).date()


def printed_date_candidates(raw: str | None) -> set[date]:
    if not raw:
        return set()
    raw = raw.strip()
    # A four-digit leading year makes the ordering explicit (YYYY-MM-DD).
    match = re.fullmatch(r'(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})', raw)
    if match:
        try:
            return {date(*map(int, match.groups()))}
        except ValueError:
            return set()
    match = re.fullmatch(r'(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})', raw)
    if match:
        a, b, year = map(int, match.groups())
        year = year + 2000 if year < 100 else year
        candidates = set()
        for month, day in ((a,b),(b,a)):
            try:
                candidates.add(date(year,month,day))
            except ValueError:
                pass
        return candidates
    clean = ' '.join(raw.replace(',', ' ').split())
    for fmt in ('%d %B %Y', '%d %b %Y', '%B %d %Y', '%b %d %Y'):
        try:
            return {datetime.strptime(clean, fmt).date()}
        except ValueError:
            pass
    return set()


def review_reasons(extraction: ReceiptExtraction, *, current_date: date | None = None) -> list[ReviewReason]:
    reasons = []
    if extraction.confidence_score == ConfidenceScore.MEDIUM:
        reasons.append(ReviewReason.MEDIUM_CONFIDENCE)
    candidates = printed_date_candidates(extraction.raw_date_text)
    if len(candidates) > 1:
        reasons.append(ReviewReason.AMBIGUOUS_DATE)
    if extraction.receipt_date not in candidates:
        reasons.append(ReviewReason.DATE_UNVERIFIED)
    if extraction.receipt_date > (current_date or today()):
        reasons.append(ReviewReason.FUTURE_DATE)
    if extraction.vat_amount is not None and extraction.vat_amount > extraction.total_amount:
        reasons.append(ReviewReason.VAT_EXCEEDS_TOTAL)
    return reasons


def parse_confirmation(message: str) -> date | None:
    """None means confirm the displayed date; date overrides must be ISO dates."""
    parts = message.strip().split()
    if len(parts) == 1 and parts[0].upper() == 'CONFIRM':
        return None
    if len(parts) == 2 and parts[0].upper() == 'CONFIRM' and re.fullmatch(r'\d{4}-\d{2}-\d{2}', parts[1]):
        return date.fromisoformat(parts[1])
    raise ValueError('Reply CONFIRM or CONFIRM YYYY-MM-DD.')


def validate_confirmation(receipt_date: date, total, vat, reason: str | None, override: date | None) -> date:
    reasons = set((reason or '').split(','))
    if reasons.intersection(DATE_REASONS) and override is None:
        raise ValueError('Please specify the correct date: CONFIRM YYYY-MM-DD.')
    confirmed_date = override or receipt_date
    if confirmed_date > today():
        raise ValueError('The date is in the future. Use CONFIRM YYYY-MM-DD with the receipt date.')
    if vat is not None and vat > total:
        raise ValueError('VAT exceeds the total. Reply RETRY to discard this unconfirmed draft and send a clearer photo.')
    return confirmed_date


def escape_markdown(value: str) -> str:
    # Telegram's legacy Markdown mode is used by the existing bot.
    return re.sub(r'([_*`\[])', r'\\\1', value)
