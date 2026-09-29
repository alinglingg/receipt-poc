"""Handle deterministic receipt commands before category/review replies."""
from uuid import UUID
from app.vendors import COMMANDS as VENDOR_COMMANDS, HELP as VENDOR_HELP, vendor_response
from app.corrections import HELP, parse_edit
from app.expense_commands import COMMANDS, HELP as EXPENSE_HELP, expense_response
from app.pipeline import DuplicateReceiptError
from app.review import escape_markdown
from app.statuses import EventStatus


def format_receipt(receipt):
    return (
        f'ID: `{receipt.id}`\n'
        f'Vendor: {escape_markdown(receipt.vendor_name)}\n'
        f'Date: {receipt.receipt_date:%d %B %Y}\n'
        f'Total: {receipt.total_amount:.2f}\n'
        f'Category: {escape_markdown(receipt.category or "Not assigned")}'
    )


async def process_command(store, notifier, *, event_id, chat_id, text, dashboard_url=""):
    text = text.strip()
    if not text.startswith('/'):
        return False
    # All slash commands are consumed so typos never become vendor categories.
    try:
        user_id = store.get_or_create_user(chat_id)
        if text.lower() in {'/help', '/start'}:
            response = 'View changes: `/history <receipt-id>`\n\n' + HELP + "\n\n" + EXPENSE_HELP + '\n\nYou can also ask: How much did I spend this month?\nFor pending categories, reply CATEGORY Dining (or your chosen category).'
            response += '\n\nOpen the web dashboard: /dashboard\n\n' + VENDOR_HELP + '\n\nExport CSV: `/export YYYY-MM` or `/export all`.'
        elif text.split()[0].lower() == '/dashboard':
            if text.lower() != '/dashboard':
                raise ValueError('Send /dashboard without any arguments.')
            if not dashboard_url:
                raise ValueError('The web dashboard is not configured yet.')
            token = store.dashboard.issue_login(user_id)
            response = f'[Open your dashboard]({dashboard_url}/dashboard/#token={token})\n\nThis private sign-in link expires in 10 minutes and works once. Do not share it.'
        elif text.split()[0].lower() == '/export':
            parts = text.split()
            if len(parts) != 2:
                raise ValueError('Use /export YYYY-MM or /export all.')
            export = store.export_receipts(user_id, parts[1].lower())
            if export.count:
                await notifier.send_document(chat_id, filename=export.filename, content=export.content,
                    caption=f'{export.count} completed receipts — {parts[1].lower()}. Dates are receipt dates; blank VAT means not shown.')
                store.mark_event(event_id, EventStatus.COMPLETED)
                return True
            response = 'No completed receipts found for this period. No CSV was sent.'
        elif text.split()[0].lower() in VENDOR_COMMANDS:
            response = vendor_response(store.vendors, user_id, text)
        elif text.split()[0].lower() == '/history':
            parts = text.split()
            if len(parts) not in (2, 3):
                raise ValueError('Use /history <receipt-id> [page].')
            try:
                receipt_id = UUID(parts[1])
                page = int(parts[2]) if len(parts) == 3 else 1
            except ValueError:
                raise ValueError('Use /history <receipt-id> [page], copying the complete ID from /receipts.') from None
            response = store.receipt_history(user_id, receipt_id, page)
        elif text.split()[0].lower() in COMMANDS:
            response = expense_response(store.expenses, user_id, text, format_receipt)
        elif text.lower() == '/receipts':
            receipts = store.recent_receipts(user_id)
            response = ('Recent saved receipts:\n\n' + '\n\n'.join(map(format_receipt, receipts))
                        if receipts else 'No completed receipts yet. Send a receipt photo to get started.')
            response += '\n\nSend /help for correction commands.'
        elif text.split()[0].lower() == '/edit':
            receipt = store.correct_receipt(user_id, parse_edit(text))
            response = '✅ Receipt corrected\n\n' + format_receipt(receipt)
            response += '\n\nThis changes only this receipt; future vendor categories stay as previously learned.'
        else:
            raise ValueError('Unknown command. Send /help for available commands.')
    except (ValueError, LookupError) as error:
        store.mark_event(event_id, EventStatus.RETRY_REQUESTED, 'INVALID_CORRECTION')
        await notifier.send(chat_id, escape_markdown(str(error)))
        return True
    except DuplicateReceiptError:
        store.mark_event(event_id, EventStatus.RETRY_REQUESTED, 'CORRECTION_DUPLICATE')
        await notifier.send(chat_id, 'That change would duplicate another receipt. Nothing was changed.')
        return True
    store.mark_event(event_id, EventStatus.COMPLETED)
    await send_response(notifier, chat_id, response)
    return True


async def send_response(notifier, chat_id, response):
    # Split only between complete receipt blocks, preserving Markdown and IDs.
    chunk = ''
    for block in response.split('\n\n'):
        candidate = chunk + ('\n\n' if chunk else '') + block
        if len(candidate.encode('utf-16-le')) // 2 > 3500:
            await notifier.send(chat_id, chunk)
            chunk = block
        else:
            chunk = candidate
    if chunk:
        await notifier.send(chat_id, chunk)
    return True
