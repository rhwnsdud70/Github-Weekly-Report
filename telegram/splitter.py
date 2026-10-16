from config.rules import TELEGRAM_LIMIT
from models.domain import TelegramMessage


class OversizedSection(ValueError):
    pass


def units(text):
    # Conservative UTF-16 units also account for emoji/surrogate pairs.
    return len(text.encode('utf-16-le')) // 2


def split_message(message, limit=TELEGRAM_LIMIT):
    if units(message.text) <= limit:
        return [message]
    # Reserve enough for numbering, then verify exact final lengths.
    capacity = limit - units(message.header) - 32
    if capacity <= 0:
        raise OversizedSection('Message header exceeds Telegram limit')
    groups = []
    current = []
    def add(block, force=False):
        nonlocal current
        if current and (force or units('\n\n'.join([*current, block])) > capacity):
            groups.append(current)
            current = []
        if units(block) > capacity:
            raise OversizedSection('Single paragraph exceeds Telegram limit; regenerate shorter content')
        current.append(block)
    for index, section in enumerate(message.sections):
        preferred_break = message.kind == 'top' and section.startswith(
            ('👨‍💻 개발자에게', '💬 개발자들은', '🚨 주의할 점'))
        if units(section) <= capacity:
            add(section, preferred_break)
            continue
        paragraphs = section.split('\n\n')
        title = paragraphs[0]
        if len(paragraphs) == 1:
            raise OversizedSection('Single paragraph exceeds Telegram limit')
        for number, paragraph in enumerate(paragraphs[1:]):
            add(title + '\n\n' + paragraph, preferred_break and number == 0)
    if current:
        groups.append(current)
    result = [TelegramMessage(message.kind, f'{message.header} ({index}/{len(groups)})', sections,
                              message.rank, index) for index, sections in enumerate(groups, 1)]
    if any(units(item.text) > limit for item in result):
        raise OversizedSection('Numbered message exceeds Telegram limit')
    return result


def split_messages(messages, limit=TELEGRAM_LIMIT):
    return [part for message in messages for part in split_message(message, limit)]
