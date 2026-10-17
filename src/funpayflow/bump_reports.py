"""Pure HTML rendering of successful bumps, before Telegram notification delivery."""

from html import escape


# Count serialized HTML in UTF-16 units: stricter than Telegram's rendered-text limit.
REPORT_MESSAGE_LIMIT = 3600
_HEADER = "🚀 <b>Лоты подняты!</b>"
_CONTINUATION = " (продолжение)"


def format_seconds(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600} ч {(seconds % 3600) // 60:02d} мин"
    if seconds >= 60:
        remainder = seconds % 60
        return f"{seconds // 60} мин {remainder:02d} сек" if remainder else f"{seconds // 60} мин"
    return f"{seconds} сек"


def _units(text):
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def _name_parts(name, budget):
    """Split source characters before escaping, never a tag/entity/surrogate pair."""
    parts, current, size = [], [], 0
    for char in name:
        cost = _units(escape(char))
        if current and size + cost > budget:
            parts.append(''.join(current)); current, size = [], 0
        current.append(char); size += cost
    parts.append(''.join(current))
    return parts


def build_bump_report_chunks(raised, next_check):
    """Preserve entry order and full names; oversized names get labelled continuations."""
    raised = list(raised)
    if not raised:
        return []
    footer = f"🔄 Следующая общая проверка: через {format_seconds(next_check)}"
    names = [str(name) for name, _, _ in raised]
    entries = [f"🎮 <b>{escape(name)}</b>\n✅ Лоты подняты\n"
               f"⏳ Повтор для этой игры: через {format_seconds(wait)}"
               for name, (_, _, wait) in zip(names, raised)]
    complete = _HEADER + '\n\n' + '\n\n'.join(entries) + '\n\n' + footer
    if _units(complete) <= REPORT_MESSAGE_LIMIT:
        return [complete]
    # Grow only the actual numbering width; do not reserve hypothetical pages.
    digits = 1
    while True:
        budget = REPORT_MESSAGE_LIMIT - _units(_HEADER + '\n\n') - (4 + 2 * digits)
        pages = _pack_pages(names, raised, footer, budget)
        total = len(pages)
        needed = len(str(total))
        if needed <= digits:
            return [(_HEADER + f" ({index + 1}/{total})" + '\n\n' + body)
                    for index, body in enumerate(pages)]
        digits = needed


def _pack_pages(names, raised, footer, budget):
    blocks = []
    for name, (_, _, wait) in zip(names, raised):
        tail = f"\n✅ Лоты подняты\n⏳ Повтор для этой игры: через {format_seconds(wait)}"
        whole = f"🎮 <b>{escape(name)}</b>" + tail
        if _units(whole) <= budget:
            blocks.append(whole)
            continue
        name_budget = budget - _units('🎮 <b></b>' + _CONTINUATION + tail)
        pieces = _name_parts(name, name_budget)
        for index, piece in enumerate(pieces):
            block = f"🎮 <b>{escape(piece)}</b>" + (_CONTINUATION if index else '')
            if index == len(pieces) - 1:
                block += tail
            blocks.append(block)
    pages, current, size = [], [], 0
    for block in blocks:
        cost = _units(block) + (2 if current else 0)
        if current and size + cost > budget:
            pages.append('\n\n'.join(current)); current, size = [], 0
            cost = _units(block)
        current.append(block); size += cost
    if current:
        pages.append('\n\n'.join(current))
    if _units(pages[-1] + '\n\n' + footer) <= budget:
        pages[-1] += '\n\n' + footer
    else:
        pages.append(footer)
    return pages
