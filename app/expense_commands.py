"""Explicit Telegram interface for the read-only expense tools."""
from uuid import UUID
from app.review import escape_markdown

HELP = (
    'Expense commands (completed receipts only):\n'
    '`/summary 2026-09`\n'
    '`/categories 2026-09`\n'
    '`/category 2026-09 Dining`\n'
    '`/largest` or `/largest 2026-09`\n'
    '`/search Keigo`\n'
    '`/receipt <receipt-id>`\n'
    '`/compare 2026-08 2026-09`'
)
COMMANDS = {'/summary', '/categories', '/category', '/largest', '/search', '/receipt', '/compare'}


def expense_response(queries, user_id, text, format_receipt):
    parts = text.split()
    command = parts[0].lower()
    def usage():
        raise ValueError('Invalid expense command. Send /help for examples.')
    if command == '/search':
        if len(parts) < 2:
            usage()
        rows = queries.search_expenses(user_id, vendor=text.split(maxsplit=1)[1])
        return _listing('Vendor matches (up to 10, newest receipt date first)', rows, format_receipt)
    if command == '/receipt':
        if len(parts) != 2:
            usage()
        try:
            receipt_id = UUID(parts[1])
        except ValueError:
            raise ValueError('Copy the complete receipt ID from /receipts.') from None
        return format_receipt(queries.get_expense(user_id, receipt_id))
    if command == '/largest':
        if len(parts) > 2:
            usage()
        month = parts[1] if len(parts) == 2 else None
        rows = queries.get_largest_expenses(user_id, month=month)
        return _listing('Largest expenses: ' + (month or 'all dates') + ' (up to 5)', rows, format_receipt)
    if command in {'/summary', '/categories', '/category'}:
        if (command == '/category' and len(parts) < 3) or (command != '/category' and len(parts) != 2):
            usage()
        month = parts[1]
        if command == '/categories':
            rows = queries.get_category_summary(user_id, month)
            blocks = [f'{escape_markdown(category or "Unassigned")}: {summary.total:.2f} ({summary.count} receipts)' for category, summary in rows]
            return f'Categories — {month}\n\n' + ('\n\n'.join(blocks) or 'No completed receipts for this month.')
        category = text.split(maxsplit=2)[2] if command == '/category' else None
        result = queries.get_monthly_summary(user_id, month, category=category)
        return (f'Spending — {month}' + (f' / {escape_markdown(category)}' if category else '')
                + f'\nReceipts: {result.count}\nTotal: {result.total:.2f}\nBased on completed receipts and receipt dates.')
    if command == '/compare':
        if len(parts) != 3:
            usage()
        result = queries.compare_months(user_id, parts[1], parts[2])
        percent = f'{result.percent_change:+.2f}%' if result.percent_change is not None else 'N/A (first month total is zero)'
        return (f'{parts[1]}: {result.first.total:.2f} ({result.first.count} receipts)\n'
                f'{parts[2]}: {result.second.total:.2f} ({result.second.count} receipts)\n'
                f'Change (second minus first): {result.change:+.2f}\nPercentage change: {percent}')
    usage()


def _listing(title, rows, formatter):
    return title + '\n\n' + ('\n\n'.join(map(formatter, rows)) if rows else 'No matching completed receipts.')
