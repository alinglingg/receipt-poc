"""Suggested expense categories; custom category names remain supported."""
PRESET_CATEGORIES = (
    'Groceries', 'Dining', 'Transportation', 'Utilities', 'Housing', 'Health',
    'Pet Care', 'Shopping', 'Household', 'Entertainment', 'Travel', 'Education',
    'Work', 'Subscriptions', 'Personal Care', 'Gifts & Donations', 'Other',
)


def category_choices(saved=()):
    return sorted(set(PRESET_CATEGORIES).union(name for name in saved if name), key=lambda name: (name.casefold(), name))


def category_list_message():
    return ('Available category suggestions:\n\n' + '\n'.join(PRESET_CATEGORIES)
            + '\n\nWhen the bot asks for a category, reply CATEGORY Groceries (or your choice). '
            'Custom category names are also welcome. Photo captions do not assign categories yet.')


def category_suggestion(value: str) -> str | None:
    """Only expose recognized presets as model suggestions; keep custom choices separate."""
    return next((name for name in PRESET_CATEGORIES
                 if name.casefold() == value.strip().casefold()), None)
