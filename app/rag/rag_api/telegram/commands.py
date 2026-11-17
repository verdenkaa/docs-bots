"""Service replies for the bot; questions are accepted through Mini App only."""

import re


def _command_name(text):
    if not isinstance(text, str) or not text.lstrip().startswith('/'):
        return None
    # Keep command payloads separate, including /start deep-link arguments.
    token = text.split(maxsplit=1)[0]
    match = re.fullmatch(r'(/[A-Za-z0-9_]+)(?:@[A-Za-z0-9_]+)?', token)
    return (match[1] if match else token).casefold()


_COMMAND_REPLIES = {
    '/start': (
        'Здравствуйте! Чтобы задать вопрос по документации или обратиться '
        'в поддержку, откройте мини-приложение через меню бота.'
    ),
    '/help': (
        'Вопросы по документации и обращения в поддержку доступны '
        'в мини-приложении. Откройте его через меню бота.'
    ),
}

_NAVIGATION_REPLY = (
    'Чат с помощником и обращения в поддержку доступны в мини-приложении. '
    'Откройте его через меню бота.'
)
