"""Explicit per-user category aliases. No fuzzy matching or receipt rewriting."""
from sqlalchemy import select

from app.db import User, UserVendorMemory, VendorAlias, PendingConversation, Receipt
from app.pipeline import normalize_vendor
from app.review import escape_markdown

HELP = ('Vendor commands:\n`/vendors` lists learned vendors.\n'
        '`/alias Starbucks #1234 | Starbucks` links an alternate name to a learned vendor.\n'
        '`/aliases` lists links; `/unalias Starbucks #1234` removes one.\n'
        'These links affect category suggestions for future receipts only.')
COMMANDS = {'/vendors', '/alias', '/aliases', '/unalias'}


def name_parts(value):
    value = ' '.join(value.split())
    key = normalize_vendor(value)
    if not 1 <= len(value) <= 200 or not key or '|' in value:
        raise ValueError('Vendor names must have 1–200 characters including a letter or number, and cannot contain |.')
    return value, key


class VendorAliases:
    def __init__(self, sessions):
        self._sessions = sessions

    def add(self, user_id, alternate, target):
        display, key = name_parts(alternate)
        _, target_key = name_parts(target)
        if key == target_key:
            raise ValueError('These names already match after normalizing punctuation and case.')
        with self._sessions() as session:
            owner = session.scalar(select(User.id).where(User.id == user_id).with_for_update())
            if owner is None:
                raise LookupError('User not found.')
            if session.get(VendorAlias, (user_id, target_key)) is not None:
                raise ValueError('Choose a learned vendor from /vendors, not another alias.')
            memory = session.get(UserVendorMemory, (user_id, target_key))
            if memory is None:
                raise ValueError('Target vendor has no learned category. Choose a name from /vendors.')
            if session.get(UserVendorMemory, (user_id, key)) is not None:
                raise ValueError('The alternate name already has its own learned category; it cannot be replaced by an alias.')
            existing = session.get(VendorAlias, (user_id, key))
            if existing is not None:
                if existing.target_normalized != target_key:
                    raise ValueError('This alias already points elsewhere. Remove it with /unalias first.')
                return existing.display_name, memory.display_name, memory.category
            pending = session.scalar(select(Receipt.id).join(PendingConversation,
                PendingConversation.receipt_id == Receipt.id).where(
                Receipt.user_id == user_id, Receipt.vendor_normalized == key,
                PendingConversation.user_id == user_id, PendingConversation.status == 'OPEN'))
            if pending is not None:
                raise ValueError('Finish the pending receipt for this alternate name before linking it.')
            session.add(VendorAlias(user_id=user_id, normalized_name=key,
                                    display_name=display, target_normalized=target_key))
            session.commit()
            return display, memory.display_name, memory.category

    def remove(self, user_id, alternate):
        _, key = name_parts(alternate)
        with self._sessions() as session:
            session.scalar(select(User.id).where(User.id == user_id).with_for_update())
            row = session.get(VendorAlias, (user_id, key))
            if row is None:
                raise LookupError('Alias not found. Use /aliases to see your links.')
            session.delete(row)
            session.commit()

    def listing(self, user_id, aliases=False, page=1):
        if type(page) is not int or not 1 <= page <= 10000:
            raise ValueError('Page must be between 1 and 10000.')
        with self._sessions() as session:
            if aliases:
                query = select(VendorAlias.display_name, UserVendorMemory.display_name, UserVendorMemory.category).join(
                    UserVendorMemory, (UserVendorMemory.user_id == VendorAlias.user_id) &
                    (UserVendorMemory.normalized_name == VendorAlias.target_normalized)).where(
                    VendorAlias.user_id == user_id).order_by(VendorAlias.normalized_name)
            else:
                query = select(UserVendorMemory.display_name, UserVendorMemory.category).where(
                    UserVendorMemory.user_id == user_id).order_by(UserVendorMemory.normalized_name)
            rows = list(session.execute(query.offset((page - 1) * 10).limit(11)))
        lines = [f'{"Aliases" if aliases else "Learned vendors"} — page {page}']
        for row in rows[:10]:
            lines.append(escape_markdown(f'{row[0]} → {row[1]}' + (f' (category: {row[2]})' if aliases else '')))
        if not rows:
            lines.append('No entries on this page.')
        if len(rows) > 10:
            lines.append(f'Next: `/{"aliases" if aliases else "vendors"} {page + 1}`')
        return '\n\n'.join(lines)


def vendor_response(vendors, user_id, text):
    parts = text.split(maxsplit=1)
    command = parts[0].lower()
    value = parts[1] if len(parts) > 1 else ''
    if command in {'/vendors', '/aliases'}:
        try:
            page = int(value) if value else 1
        except ValueError:
            raise ValueError('Use /vendors [page] or /aliases [page].') from None
        return vendors.listing(user_id, aliases=command == '/aliases', page=page)
    if command == '/unalias':
        vendors.remove(user_id, value)
        return 'Alias removed. Saved receipts and learned vendor categories are unchanged.'
    if command == '/alias':
        names = value.split('|')
        if len(names) != 2:
            raise ValueError('Use /alias alternate name | learned vendor name. See /vendors for targets.')
        alternate, target, category = vendors.add(user_id, *names)
        return ('Alias saved: ' + escape_markdown(f'{alternate} → {target}') +
                '\nFuture receipts matching this alternate name will use category: ' + escape_markdown(category) +
                '\nSaved receipts and duplicate rules are unchanged.')
    raise ValueError('Unknown vendor command.')
