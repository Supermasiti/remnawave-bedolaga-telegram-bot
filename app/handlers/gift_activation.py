"""Handler for gift subscription activation via inline callback button."""

import html as html_mod

import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import InaccessibleMessage
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import GuestPurchase
from app.keyboards.inline import _get_days_word
from app.localization.texts import get_texts
from app.services.guest_purchase_service import GuestPurchaseError, activate_purchase


logger = structlog.get_logger(__name__)


async def handle_gift_activate(callback: types.CallbackQuery) -> None:
    """Handle gift_activate:{purchase_id} callback from Telegram notification."""
    default_texts = get_texts(settings.DEFAULT_LANGUAGE)
    gift_not_found = default_texts.t('GIFT_NOT_FOUND_OR_UNAVAILABLE', 'Gift not found or unavailable.')

    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer(
            default_texts.t('GIFT_MESSAGE_EXPIRED', 'Message expired. Please try /start.'), show_alert=True
        )
        return

    if not callback.data:
        return

    parts = callback.data.split(':', 1)
    if len(parts) != 2:
        await callback.answer(gift_not_found, show_alert=True)
        return

    try:
        purchase_id = int(parts[1])
    except ValueError:
        await callback.answer(gift_not_found, show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text(default_texts.t('GIFT_ACTIVATING', '⏳ Activating gift...'), parse_mode=None)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(GuestPurchase)
            .options(selectinload(GuestPurchase.user), selectinload(GuestPurchase.tariff))
            .where(GuestPurchase.id == purchase_id)
        )
        purchase = result.scalars().first()

        if not purchase or purchase.user_id is None or purchase.user is None:
            await callback.message.edit_text(gift_not_found, parse_mode=None)
            return

        texts = get_texts(purchase.user.language)
        gift_not_found = texts.t('GIFT_NOT_FOUND_OR_UNAVAILABLE', 'Gift not found or unavailable.')

        # Verify the callback sender is the actual recipient
        if purchase.user.telegram_id != callback.from_user.id:
            await callback.message.edit_text(gift_not_found, parse_mode=None)
            return

        # Resolve tariff info inside session (selectin-loaded relationships)
        tariff_name = html_mod.escape(purchase.tariff.name) if purchase.tariff and purchase.tariff.name else ''
        period_days = purchase.period_days

        try:
            await activate_purchase(db, purchase.token, skip_notification=True)
        except GuestPurchaseError as exc:
            logger.warning(
                'Gift activation via callback failed',
                purchase_id=purchase_id,
                telegram_id=callback.from_user.id,
                error=exc.message,
            )
            if exc.status_code >= 500:
                await callback.message.edit_text(
                    texts.t('GIFT_ACTIVATION_ERROR_RETRY', 'An error occurred during activation. Please try again later.'),
                    parse_mode=None,
                )
            else:
                await callback.message.edit_text(
                    texts.t('GIFT_ACTIVATION_FAILED', 'Failed to activate the gift: {error}').format(
                        error=html_mod.escape(exc.message)
                    ),
                    parse_mode=None,
                )
            return
        except Exception:
            logger.exception(
                'Unexpected error during gift activation via callback',
                purchase_id=purchase_id,
                telegram_id=callback.from_user.id,
            )
            await callback.message.edit_text(
                texts.t('GIFT_ACTIVATION_ERROR_RETRY', 'An error occurred during activation. Please try again later.'),
                parse_mode=None,
            )
            return

    period_text = (
        f'{period_days} {_get_days_word(period_days, purchase.user.language)}' if period_days else ''
    )
    tariff_text = f'{tariff_name} — {period_text}' if tariff_name else period_text

    await callback.message.edit_text(
        texts.t('GIFT_ACTIVATED_TITLE', '✅ <b>Gift activated!</b>\n{tariff_text}\n\nYour subscription has been updated.').format(
            tariff_text=tariff_text
        ),
    )


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(handle_gift_activate, F.data.startswith('gift_activate:'))
