"""Shared formatting utilities for traffic, price, and period display."""

import html


def safe_html_name(name: str | None) -> str:
    """HTML-escape a display name for Telegram HTML messages."""
    return html.escape(name or '')


def user_html_link(user) -> str:
    """Build an HTML-safe clickable user link for Telegram messages."""
    safe = safe_html_name(user.full_name)
    if getattr(user, 'telegram_id', None):
        return f'<a href="tg://user?id={user.telegram_id}">{safe}</a>'
    return f'<b>{safe}</b>'


def format_traffic(gb: int, language: str | None = None) -> str:
    """Форматирует трафик."""
    if gb == 0:
        if language in ('es', 'pt'):
            return 'Ilimitado'
        if language == 'ru':
            return 'Безлимит'
        return 'Unlimited'
    unit = 'ГБ' if language == 'ru' else 'GB'
    return f'{gb} {unit}'


def format_price_kopeks(kopeks: int, compact: bool = False) -> str:
    """Форматирует цену из центов в доллары."""
    rubles = kopeks / 100
    from app.config import settings as _settings
    symbol = _settings.CURRENCY_SYMBOL
    if compact:
        # Компактный формат - округляем до целых долларов
        return f'{symbol}{int(round(rubles))}'
    if rubles == int(rubles):
        return f'{symbol}{int(rubles)}'
    return f'{symbol}{rubles:.2f}'


def format_period(days: int, language: str | None = None) -> str:
    """Форматирует период."""
    if language == 'es':
        word = 'día' if days == 1 else 'días'
    elif language == 'pt':
        word = 'dia' if days == 1 else 'dias'
    elif language == 'ru':
        mod100 = days % 100
        mod10 = days % 10
        if 11 <= mod100 <= 19:
            word = 'дней'
        elif mod10 == 1:
            word = 'день'
        elif 2 <= mod10 <= 4:
            word = 'дня'
        else:
            word = 'дней'
    else:
        word = 'day' if days == 1 else 'days'
    return f'{days} {word}'
