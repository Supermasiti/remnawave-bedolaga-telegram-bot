"""
Multi-tariff subscription list handler.

Shows all user subscriptions with per-subscription management.
Only active when MULTI_TARIFF_ENABLED=True.
"""

from __future__ import annotations

import structlog
from aiogram import Router, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import (
    decrement_subscription_server_counts,
    get_all_subscriptions_by_user_id,
    get_subscription_by_id_for_user,
)
from app.database.models import Subscription, SubscriptionStatus, User
from app.localization.texts import get_texts
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)

router = Router()


def _status_emoji(sub) -> str:
    """Return status emoji based on subscription's actual status."""
    actual = sub.actual_status
    if actual in ('active', 'trial'):
        return '🟢'
    if actual == 'limited':
        return '🟡'
    return '🔴'


def _status_text(sub, texts) -> str:
    """Return localized status text based on subscription's actual status."""
    actual = sub.actual_status
    if actual == 'expired':
        return texts.t('MY_SUBS_STATUS_EXPIRED', '🔴 Expired')
    if actual == 'active':
        if sub.is_trial:
            return texts.t('MY_SUBS_STATUS_TRIAL', '🎯 Trial')
        return texts.t('MY_SUBS_STATUS_ACTIVE', '🟢 Active')
    if actual == 'disabled':
        return texts.t('MY_SUBS_STATUS_DISABLED', '⚫ Disabled')
    if actual == 'limited':
        return texts.t('MY_SUBS_STATUS_LIMITED', '⚠️ Traffic exhausted')
    if actual == 'trial':
        return texts.t('MY_SUBS_STATUS_TRIAL', '🎯 Trial')
    return texts.t('MY_SUBS_STATUS_UNKNOWN', '❓ Unknown')


def _status_label(sub, texts) -> str:
    """Return a short human-readable status label for non-active subscriptions."""
    actual = sub.actual_status
    if actual == 'expired':
        return ' ' + texts.t('MY_SUBS_STATUS_EXPIRED_SUFFIX', '(Expired)')
    if actual == 'disabled':
        return ' ' + texts.t('MY_SUBS_STATUS_DISABLED_SUFFIX', '(Disabled)')
    if actual == 'limited':
        return ' ' + texts.t('MY_SUBS_STATUS_LIMITED_SUFFIX', '(Limit reached)')
    return ''


def _format_subscription_line(sub, idx: int, texts) -> str:
    """Format a single subscription for the list view."""
    tariff_name = sub.tariff.name if sub.tariff else texts.t('MY_SUBS_DEFAULT_NAME', 'Subscription')
    emoji = _status_emoji(sub)
    label = _status_label(sub, texts)

    # Traffic info
    if sub.traffic_limit_gb == 0:
        traffic = '∞'
    else:
        used = f'{sub.traffic_used_gb:.1f}' if sub.traffic_used_gb else '0'
        traffic = f'{used}/{sub.traffic_limit_gb} {texts.t("TRAFFIC_UNIT_GB", "GB")}'

    # Devices
    devices = (
        texts.t('MY_SUBS_DEVICES_SHORT', '{count} dev.').format(count=sub.device_limit) if sub.device_limit else ''
    )

    # End date
    end_date = sub.end_date.strftime('%d.%m.%Y') if sub.end_date else '—'

    parts = [f'{emoji} <b>{idx}. {tariff_name}</b>{label}']
    parts.append(texts.t('MY_SUBS_TRAFFIC_LINE', '   📊 Traffic: {traffic}').format(traffic=traffic))
    if devices:
        parts.append(texts.t('MY_SUBS_DEVICES_LINE', '   📱 Devices: {devices}').format(devices=devices))
    parts.append(texts.t('MY_SUBS_UNTIL_LINE', '   📅 Until: {date}').format(date=end_date))

    return '\n'.join(parts)


def _build_subscriptions_keyboard(subscriptions: list, language: str) -> types.InlineKeyboardMarkup:
    """Build inline keyboard with per-subscription management buttons."""
    texts = get_texts(language)
    buttons = []
    for idx, sub in enumerate(subscriptions, 1):
        tariff_name = sub.tariff.name if sub.tariff else texts.t('MY_SUBS_DEFAULT_NAME_WITH_ID', 'Subscription #{id}').format(id=sub.id)
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=f'⚙️ {tariff_name}',
                    callback_data=f'sm:{sub.id}',
                )
            ]
        )

    # "Buy another tariff" button
    buy_text = getattr(texts, 'MENU_BUY_SUBSCRIPTION', None) or texts.t('MY_SUBS_BUY_ANOTHER_PLAN', 'Buy another plan')
    buttons.append(
        [
            types.InlineKeyboardButton(text=f'➕ {buy_text}', callback_data='menu_buy'),
        ]
    )
    # Back button
    buttons.append(
        [
            types.InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu'),
        ]
    )

    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


def _build_subscription_detail_keyboard(sub_id: int, language: str, sub=None) -> types.InlineKeyboardMarkup:
    """Build keyboard for single subscription management.

    For expired/disabled subscriptions, only 'Renew' and 'Back' are shown —
    connection link and traffic/device management are irrelevant.
    """
    texts = get_texts(language)
    is_inactive = sub is not None and sub.actual_status in ('expired', 'disabled')

    buttons = []

    if not is_inactive:
        buttons.append(
            [types.InlineKeyboardButton(text=texts.t('MY_SUBS_CONNECTION_LINK_BUTTON', '🔗 Connection link'), callback_data=f'sl:{sub_id}')]
        )

    buttons.append([types.InlineKeyboardButton(text=texts.t('MY_SUBS_RENEW_BUTTON', '🔄 Renew'), callback_data=f'se:{sub_id}')])

    if not is_inactive:
        buttons.append(
            [types.InlineKeyboardButton(text=texts.t('MY_SUBS_AUTOPAY_BUTTON', '💳 Autopay'), callback_data='subscription_autopay')]
        )
        buttons.append(
            [types.InlineKeyboardButton(text=texts.t('MY_SUBS_TRAFFIC_BUTTON', '📊 Traffic'), callback_data=f'st:{sub_id}')]
        )
        buttons.append(
            [types.InlineKeyboardButton(text=texts.t('MY_SUBS_DEVICES_BUTTON', '📱 Devices'), callback_data=f'sd:{sub_id}')]
        )

    if is_inactive:
        buttons.append(
            [types.InlineKeyboardButton(text=texts.t('MY_SUBS_DELETE_BUTTON', '🗑 Delete subscription'), callback_data=f'sub_del:{sub_id}')]
        )

    if not is_inactive and settings.is_subscription_revoke_enabled():
        buttons.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('MY_SUBS_REISSUE_BUTTON', '🔄 Reissue'),
                    callback_data=f'sr:{sub_id}',
                )
            ]
        )

    buttons.append(
        [types.InlineKeyboardButton(text=texts.t('MY_SUBS_BACK_TO_LIST_BUTTON', '◀️ Back to subscriptions'), callback_data='my_subscriptions')]
    )

    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


async def show_my_subscriptions(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext | None = None,
) -> None:
    """Show list of all user subscriptions."""
    if not settings.is_multi_tariff_enabled():
        # Fallback to legacy single subscription view
        return

    texts = get_texts(db_user.language)
    subscriptions = await get_all_subscriptions_by_user_id(db, db_user.id)

    if not subscriptions:
        text = texts.t('MY_SUBS_LIST_TITLE', '📋 <b>My subscriptions</b>') + '\n\n' + texts.t(
            'MY_SUBS_EMPTY_NOTICE', 'You have no subscriptions.'
        )
        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.t('MY_SUBS_BUY_BUTTON', '🛒 Buy subscription'), callback_data='menu_buy')],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')],
            ]
        )
    else:
        lines = [texts.t('MY_SUBS_LIST_TITLE', '📋 <b>My subscriptions</b>') + '\n']
        for idx, sub in enumerate(subscriptions, 1):
            lines.append(_format_subscription_line(sub, idx, texts))
            lines.append('')  # empty line between subscriptions
        text = '\n'.join(lines)
        keyboard = _build_subscriptions_keyboard(subscriptions, db_user.language)

    if callback.message:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


async def show_subscription_detail(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Show detail view for a single subscription (IDOR protected)."""
    texts = get_texts(db_user.language)
    parts = callback.data.split(':')
    if len(parts) < 2:
        await callback.answer(texts.t('INVALID_FORMAT_ALERT', 'Invalid format'), show_alert=True)
        return

    sub_id = int(parts[1])
    subscription = await get_subscription_by_id_for_user(db, sub_id, db_user.id)

    if not subscription:
        await callback.answer(texts.t('SUBSCRIPTION_NOT_FOUND_ALERT', 'Subscription not found'), show_alert=True)
        return

    # Persist active sub_id so downstream handlers without sub_id in callback_data
    # (e.g. 'subscription_autopay') can resolve the right subscription via FSM.
    await state.update_data(active_subscription_id=sub_id)

    tariff_name = subscription.tariff.name if subscription.tariff else texts.t('MY_SUBS_DEFAULT_NAME', 'Subscription')

    # Traffic
    if subscription.traffic_limit_gb == 0:
        traffic = texts.t('MY_SUBS_UNLIMITED_TRAFFIC', '∞ GB')
    else:
        used = f'{subscription.traffic_used_gb:.1f}' if subscription.traffic_used_gb else '0'
        traffic = f'{used} / {subscription.traffic_limit_gb} {texts.t("TRAFFIC_UNIT_GB", "GB")}'

    end_date = subscription.end_date.strftime('%d.%m.%Y %H:%M') if subscription.end_date else '—'
    status = _status_text(subscription, texts)

    text = (
        f'📋 <b>{tariff_name}</b>\n\n'
        + texts.t('MY_SUBS_STATUS_LINE', 'Status: {status}').format(status=status)
        + '\n'
        + texts.t('MY_SUBS_TRAFFIC_LINE_PLAIN', '📊 Traffic: {traffic}').format(traffic=traffic)
        + '\n'
        + texts.t('MY_SUBS_DEVICES_LINE_PLAIN', '📱 Devices: {devices}').format(devices=subscription.device_limit)
        + '\n'
        + texts.t('MY_SUBS_UNTIL_LINE_PLAIN', '📅 Until: {date}').format(date=end_date)
        + '\n'
    )

    if subscription.subscription_url and not settings.should_hide_subscription_link():
        text += f'\n🔗 <code>{subscription.subscription_url}</code>'

    keyboard = _build_subscription_detail_keyboard(sub_id, db_user.language, sub=subscription)

    if callback.message:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


async def _resolve_and_store_sub(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> Subscription | None:
    """Extract sub_id from callback, validate ownership, store in FSM state."""
    texts = get_texts(db_user.language)
    sub_id = _extract_sub_id(callback)
    if sub_id is None:
        await callback.answer(texts.t('INVALID_FORMAT_ALERT', 'Invalid format'), show_alert=True)
        return None

    subscription = await get_subscription_by_id_for_user(db, sub_id, db_user.id)
    if not subscription:
        await callback.answer(texts.t('SUBSCRIPTION_NOT_FOUND_ALERT', 'Subscription not found'), show_alert=True)
        return None

    # Store in FSM state so downstream handlers can use it
    await state.update_data(active_subscription_id=sub_id)
    return subscription


async def handle_subscription_link(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Delegation: sl:{sub_id} → connect subscription link handler."""
    subscription = await _resolve_and_store_sub(callback, db_user, db, state)
    if not subscription:
        return

    from .links import handle_connect_subscription

    await handle_connect_subscription(callback, db_user, db, state)


async def handle_subscription_extend(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Delegation: se:{sub_id} → extend/renew subscription handler."""
    subscription = await _resolve_and_store_sub(callback, db_user, db, state)
    if not subscription:
        return

    from .purchase import handle_extend_subscription

    await handle_extend_subscription(callback, db_user, db, state)


async def handle_subscription_traffic(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Delegation: st:{sub_id} → traffic management handler."""
    subscription = await _resolve_and_store_sub(callback, db_user, db, state)
    if not subscription:
        return

    from .traffic import handle_add_traffic

    await handle_add_traffic(callback, db_user, db, state)


async def handle_subscription_devices(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Delegation: sd:{sub_id} → devices menu with buy + manage options."""
    texts = get_texts(db_user.language)
    subscription = await _resolve_and_store_sub(callback, db_user, db, state)
    if not subscription:
        return

    sub_id = subscription.id

    # Проверяем доступность докупки устройств
    can_buy_devices = False
    if subscription.tariff_id:
        from app.database.crud.tariff import get_tariff_by_id

        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        tariff_device_price = getattr(tariff, 'device_price_kopeks', None) if tariff else None
        can_buy_devices = bool(tariff_device_price and tariff_device_price > 0)
    else:
        can_buy_devices = settings.is_devices_selection_enabled()

    current_devices = subscription.device_limit or 0
    text = texts.t(
        'MY_SUBS_DEVICES_MENU_TEXT', '📱 <b>Devices</b>\n\nCurrent limit: {count} devices\n\nChoose an action:'
    ).format(count=current_devices)

    keyboard = []
    if can_buy_devices:
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('MY_SUBS_BUY_MORE_DEVICES_BUTTON', '➕ Buy more devices'),
                    callback_data=f'change_devices_menu:{sub_id}',
                )
            ]
        )
    keyboard.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('MY_SUBS_MANAGE_DEVICES_BUTTON', '📱 Manage devices'), callback_data=f'device_management:{sub_id}'
            )
        ]
    )
    keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data=f'sm:{sub_id}')])

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


async def handle_change_devices_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Delegation: change_devices_menu:{sub_id} → buy/change device limit."""
    subscription = await _resolve_and_store_sub(callback, db_user, db, state)
    if not subscription:
        return

    from .devices import handle_change_devices

    await handle_change_devices(callback, db_user, db, state)


async def handle_device_management_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Delegation: device_management:{sub_id} → manage connected devices."""
    subscription = await _resolve_and_store_sub(callback, db_user, db, state)
    if not subscription:
        return

    from .devices import handle_device_management

    await handle_device_management(callback, db_user, db, state)


async def handle_subscription_delete_confirm(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Show delete confirmation for an expired/disabled subscription."""
    texts = get_texts(db_user.language)
    sub_id = _extract_sub_id(callback)
    if sub_id is None:
        await callback.answer(texts.t('INVALID_FORMAT_ALERT', 'Invalid format'), show_alert=True)
        return

    subscription = await get_subscription_by_id_for_user(db, sub_id, db_user.id)
    if not subscription:
        await callback.answer(texts.t('SUBSCRIPTION_NOT_FOUND_ALERT', 'Subscription not found'), show_alert=True)
        return

    if subscription.actual_status not in ('expired', 'disabled'):
        await callback.answer(
            texts.t('MY_SUBS_DELETE_ONLY_INACTIVE_ALERT', 'Only an expired or disabled subscription can be deleted'),
            show_alert=True,
        )
        return

    tariff_name = subscription.tariff.name if subscription.tariff else texts.t('MY_SUBS_DEFAULT_NAME', 'Subscription')

    text = texts.t(
        'MY_SUBS_DELETE_CONFIRM_MESSAGE',
        '🗑 <b>Delete subscription "{name}"?</b>\n\n'
        '⚠️ The subscription will be deleted permanently.\n'
        'All data, devices, and settings will be lost.\n'
        'This action cannot be undone.',
    ).format(name=tariff_name)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('MY_SUBS_DELETE_CONFIRM_YES_BUTTON', '🗑 Yes, delete'),
                    callback_data=f'sub_del_yes:{sub_id}',
                )
            ],
            [types.InlineKeyboardButton(text=texts.t('CANCEL_BUTTON', '◀️ Cancel'), callback_data=f'sm:{sub_id}')],
        ]
    )

    if callback.message:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


async def handle_subscription_delete_execute(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Actually delete an expired/disabled subscription."""
    texts = get_texts(db_user.language)
    sub_id = _extract_sub_id(callback)
    if sub_id is None:
        await callback.answer(texts.t('INVALID_FORMAT_ALERT', 'Invalid format'), show_alert=True)
        return

    subscription = await get_subscription_by_id_for_user(db, sub_id, db_user.id)
    if not subscription:
        await callback.answer(texts.t('SUBSCRIPTION_NOT_FOUND_ALERT', 'Subscription not found'), show_alert=True)
        return

    deletable_statuses = {SubscriptionStatus.EXPIRED.value, SubscriptionStatus.DISABLED.value}
    if getattr(subscription, 'actual_status', subscription.status) not in deletable_statuses:
        await callback.answer(
            texts.t('MY_SUBS_DELETE_ONLY_INACTIVE_ALERT', 'Only an expired or disabled subscription can be deleted'),
            show_alert=True,
        )
        return

    # Delete from RemnaWave panel (stops webhooks / phantom notifications)
    if subscription.remnawave_uuid:
        try:
            from app.services.remnawave_webhook_service import RemnaWaveWebhookService

            # Suppress the self-inflicted user.deleted webhook so its sibling-expiry
            # sweep never touches the user's other (still-active) subscriptions.
            RemnaWaveWebhookService.mark_intentional_panel_deletion(panel_uuids=[subscription.remnawave_uuid])
            service = SubscriptionService()
            await service.delete_remnawave_user(subscription.remnawave_uuid)
        except Exception as e:
            logger.warning('Failed to delete RemnaWave user on subscription delete', error=e)

    # Decrement server counts
    await decrement_subscription_server_counts(db, subscription)

    # Hard delete from DB
    await db.delete(subscription)
    await db.commit()

    logger.info(
        'Subscription deleted by user via bot',
        subscription_id=sub_id,
        user_id=db_user.id,
    )

    await callback.answer(texts.t('MY_SUBS_DELETED_ALERT', 'Subscription deleted'), show_alert=True)

    # Return to subscriptions list
    await show_my_subscriptions(callback, db_user, db, state)


def _extract_sub_id(callback: types.CallbackQuery) -> int | None:
    """Extract subscription ID from callback_data format 'prefix:sub_id'."""
    parts = (callback.data or '').split(':')
    if len(parts) >= 2:
        try:
            return int(parts[1])
        except (ValueError, TypeError):
            return None
    return None
