"""Handle deterministic receipt commands before category/review replies."""
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


async def process_command(store, notifier, *, event_id, chat_id, text):
    text = text.strip()
    if not text.startswith('/'):
        return False
    # All slash commands are consumed so typos never become vendor categories.
    try:
        user_id = store.get_or_create_user(chat_id)
        if text.lower() in {'/help', '/start'}:
            response = HELP + "\n\n" + EXPENSE_HELP + '\n\nYou can also ask: How much did I spend this month?\nFor pending categories, reply CATEGORY Dining (or your chosen category).'
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
