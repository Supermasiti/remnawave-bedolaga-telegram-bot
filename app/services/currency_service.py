"""Converts USD-based prices to a local display currency for LatAm users.

Actual payment execution (Telegram Stars, CryptoBot) always happens in USD —
this service only affects how a price is *displayed* next to the USD amount.
Rates are fetched from a free, no-key exchange rate API and cached in memory,
with a periodic background refresh and a static fallback table in case the
API is unreachable.
"""

import asyncio
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


# Approximate safety-net rates (USD -> target), used only until the live API
# has returned a successful response at least once, or if it stays down.
# Not the source of truth — just keeps prices roughly sane in the meantime.
_FALLBACK_RATES: dict[str, Decimal] = {
    'MXN': Decimal('18.5'),
    'BRL': Decimal('5.5'),
    'ARS': Decimal('1000'),
    'COP': Decimal('4000'),
}

CURRENCY_SYMBOLS: dict[str, str] = {
    'MXN': 'MX$',
    'BRL': 'R$',
    'ARS': 'AR$',
    'COP': 'COL$',
    'USD': '$',
}


class CurrencyService:
    def __init__(self) -> None:
        self._rates: dict[str, Decimal] = {}
        self._task: asyncio.Task[None] | None = None
        self._last_updated: datetime | None = None

    async def start(self) -> None:
        if not settings.DISPLAY_CURRENCY_ENABLED:
            return
        await self._refresh()
        self._task = asyncio.create_task(self._periodic_loop())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _periodic_loop(self) -> None:
        while True:
            await asyncio.sleep(max(1, settings.DISPLAY_CURRENCY_REFRESH_HOURS) * 3600)
            await self._refresh()

    async def _refresh(self) -> None:
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(settings.DISPLAY_CURRENCY_API_URL) as resp,
            ):
                if resp.status != 200:
                    logger.warning('Currency rate fetch failed: HTTP', resp_status=resp.status)
                    return
                data = await resp.json(content_type=None)

            rates = data.get('rates') or {}
            parsed = {code: Decimal(str(value)) for code, value in rates.items() if value}
            if parsed:
                self._rates = parsed
                self._last_updated = datetime.now(UTC)
                logger.info('💱 Курсы валют обновлены', currency_count=len(parsed))
        except Exception as e:
            logger.warning('⚠️ Не удалось обновить курсы валют, используем предыдущие/резервные значения', error=e)

    def get_rate(self, currency_code: str) -> Decimal | None:
        """Return the USD -> currency_code rate, or None if unavailable."""
        if currency_code == 'USD':
            return Decimal('1')
        return self._rates.get(currency_code) or _FALLBACK_RATES.get(currency_code)

    def convert_usd(self, usd_amount: Decimal, currency_code: str) -> Decimal | None:
        rate = self.get_rate(currency_code)
        if rate is None:
            return None
        return (usd_amount * rate).quantize(Decimal('1'), rounding=ROUND_HALF_UP)

    def get_status(self) -> dict:
        return {
            'enabled': settings.DISPLAY_CURRENCY_ENABLED,
            'currencies_loaded': len(self._rates),
            'last_updated': self._last_updated.isoformat() if self._last_updated else None,
            'running': self._task is not None and not self._task.done(),
        }


currency_service = CurrencyService()


def get_display_currency_for_language(language: str) -> str:
    """Map an interface language to a display currency code. 'USD' means no conversion."""
    if not settings.DISPLAY_CURRENCY_ENABLED:
        return 'USD'
    language_code = (language or '').split('-')[0].lower()
    if language_code == 'es':
        return settings.DISPLAY_CURRENCY_ES
    if language_code == 'pt':
        return settings.DISPLAY_CURRENCY_PT
    return 'USD'


def get_currency_symbol(currency_code: str) -> str:
    return CURRENCY_SYMBOLS.get(currency_code, f'{currency_code} ')
