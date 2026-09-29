"""Database-backed browser sessions and owner-scoped dashboard reads."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import delete, select, func

from app.db import BrowserToken, User, Receipt, UserVendorMemory, VendorAlias
from app.expenses import month_bounds, text_filter
from app.statuses import RECEIPT_STATUSES

SESSION_SECONDS = 86400


def dashboard_origin(value):
    if not value:
        return ''
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('DASHBOARD_URL must be an HTTPS origin, for example https://your-app.onrender.com')
    return f'https://{parsed.netloc}'


def digest(token):
    if not isinstance(token, str) or len(token) != 43:
        return ''
    return hashlib.sha256(token.encode()).hexdigest()


def receipt_data(row):
    return dict(id=str(row.id), vendor=row.vendor_name, date=row.receipt_date.isoformat(),
                total=format(row.total_amount, '.2f'), vat=format(row.vat_amount, '.2f') if row.vat_amount is not None else None,
                category=row.category, status=row.status, confidence=row.confidence,
                review_reason=row.review_reason, raw_date=row.raw_date_text)


class DashboardStore:
    def __init__(self, sessions):
        self._sessions = sessions

    def issue_login(self, user_id):
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        with self._sessions() as session:
            user = session.scalar(select(User).where(User.id == user_id).with_for_update())
            if user is None or user.telegram_chat_id <= 0:
                raise ValueError('Open a private chat with the bot to request a dashboard link.')
            session.execute(delete(BrowserToken).where(BrowserToken.user_id == user_id,
                            (BrowserToken.kind == 'LOGIN') | (BrowserToken.expires_at <= now)))
            session.add(BrowserToken(token_hash=digest(token), user_id=user_id, kind='LOGIN', expires_at=now + timedelta(minutes=10)))
            session.commit()
        return token

    def exchange_login(self, token):
        now = datetime.now(timezone.utc)
        replacement = secrets.token_urlsafe(32)
        with self._sessions() as session:
            # Atomic consume, including two simultaneous requests for the same link.
            user_id = session.execute(delete(BrowserToken).where(
                BrowserToken.token_hash == digest(token), BrowserToken.kind == 'LOGIN',
                BrowserToken.expires_at > now).returning(BrowserToken.user_id)).scalar_one_or_none()
            if user_id is None:
                raise LookupError('This sign-in link has expired or was already used. Send /dashboard to the bot for a new one.')
            session.add(BrowserToken(token_hash=digest(replacement), user_id=user_id, kind='SESSION',
                                     expires_at=now + timedelta(seconds=SESSION_SECONDS)))
            session.commit()
        return replacement

    def authenticate(self, token):
        with self._sessions() as session:
            user_id = session.scalar(select(BrowserToken.user_id).where(
                BrowserToken.token_hash == digest(token), BrowserToken.kind == 'SESSION',
                BrowserToken.expires_at > datetime.now(timezone.utc)))
            if user_id is None:
                raise LookupError('Please sign in using a fresh /dashboard link from Telegram.')
            return user_id

    def logout(self, token):
        with self._sessions() as session:
            session.execute(delete(BrowserToken).where(BrowserToken.token_hash == digest(token), BrowserToken.kind == 'SESSION'))
            session.commit()

    def receipts(self, user_id, month=None, vendor=None, status=None, page=1):
        if not 1 <= page <= 10000:
            raise ValueError('Invalid page.')
        filters = [Receipt.user_id == user_id]
        if month:
            start, end = month_bounds(month)
            filters += [Receipt.receipt_date >= start, Receipt.receipt_date < end]
        if vendor:
            value = text_filter(vendor, 200).replace('/', '//').replace('%', '/%').replace('_', '/_')
            filters.append(Receipt.vendor_name.ilike('%' + value + '%', escape='/'))
        if status:
            if status not in RECEIPT_STATUSES:
                raise ValueError('Unknown receipt status.')
            filters.append(Receipt.status == status)
        with self._sessions() as session:
            rows = list(session.scalars(select(Receipt).where(*filters).order_by(
                Receipt.receipt_date.desc(), Receipt.id.desc()).offset((page - 1) * 20).limit(21)))
            return dict(items=[receipt_data(row) for row in rows[:20]], has_more=len(rows) > 20)

    def receipt(self, user_id, receipt_id, image=False):
        with self._sessions() as session:
            row = session.scalar(select(Receipt).where(Receipt.id == receipt_id, Receipt.user_id == user_id))
            if row is None:
                raise LookupError('Receipt not found.')
            return row.image_path if image else receipt_data(row)

    def pending(self, user_id):
        with self._sessions() as session:
            rows = session.scalars(select(Receipt).where(Receipt.user_id == user_id,
                 Receipt.status.in_(['NEEDS_REVIEW', 'PENDING_CATEGORY'])).order_by(Receipt.created_at).limit(20))
            return [receipt_data(row) for row in rows]

    def vendors(self, user_id, page=1):
        if not 1 <= page <= 10000:
            raise ValueError('Invalid page.')
        with self._sessions() as session:
            rows = list(session.scalars(select(UserVendorMemory).where(UserVendorMemory.user_id == user_id)
                .order_by(UserVendorMemory.normalized_name).offset((page - 1) * 20).limit(21)))
            aliases = list(session.scalars(select(VendorAlias).where(VendorAlias.user_id == user_id)
                .order_by(VendorAlias.normalized_name).offset((page - 1) * 20).limit(21)))
            return dict(items=[dict(name=r.display_name, category=r.category) for r in rows[:20]],
                        aliases=[dict(name=r.display_name, target=r.target_normalized) for r in aliases[:20]],
                        has_more=len(rows) > 20 or len(aliases) > 20)
