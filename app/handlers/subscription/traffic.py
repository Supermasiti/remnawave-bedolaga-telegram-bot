import math
from datetime import UTC, datetime

from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PERIOD_PRICES, settings
from app.database.crud.subscription import (
    add_subscription_traffic,
    reactivate_subscription,
)
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import TransactionType, User
from app.keyboards.inline import (
    _get_days_word,
    get_add_traffic_keyboard,
    get_add_traffic_keyboard_from_tariff,
    get_back_keyboard,
    get_countries_keyboard,
    get_devices_keyboard,
    get_insufficient_balance_keyboard,
    get_reset_traffic_confirm_keyboard,
)
from app.localization.texts import get_texts
from app.services.pricing_engine import PricingEngine
from app.services.remnawave_service import RemnaWaveService
from app.services.subscription_service import SubscriptionService
from app.services.user_cart_service import user_cart_service
from app.states import SubscriptionStates
from app.utils.pricing_utils import (
    calculate_prorated_price,
)

from .common import (
    _get_period_hint_from_subscription,
    get_confirm_switch_traffic_keyboard,
    get_traffic_switch_keyboard,
    logger,
)
from .countries import (
    _build_countries_selection_text,
    _get_available_countries,
    _get_preselected_free_countries,
    _should_show_countries_management,
)
from .summary import present_subscription_summary


async def _resolve_subscription(callback, db_user, db, state=None):
    """Resolve subscription — delegates to shared resolve_subscription_from_context."""
    from .common import resolve_subscription_from_context

    return await resolve_subscription_from_context(callback, db_user, db, state)


async def handle_add_traffic(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None):
    from app.config import settings
    from app.database.crud.subscription import get_active_subscriptions_by_user_id
    from app.database.crud.tariff import get_tariff_by_id

    texts = get_texts(db_user.language)

    # В режиме мульти-тарифов без явного sub_id в callback — показываем выбор подписки.
    if settings.is_multi_tariff_enabled() and callback.data == 'buy_traffic':
        active_subs = await get_active_subscriptions_by_user_id(db, db_user.id)
        if len(active_subs) > 1:
            from app.database.crud.tariff import get_tariff_by_id as _get_tariff

            keyboard = []
            for sub in sorted(active_subs, key=lambda s: s.id):
                if sub.is_trial:
                    continue
                tariff_name = ''
                if sub.tariff_id:
                    _t = await _get_tariff(db, sub.tariff_id)
                    tariff_name = _t.name if _t else f'#{sub.id}'
                else:
                    tariff_name = texts.t('SUBSCRIPTION_HASH_LABEL', 'Subscription #{id}').format(id=sub.id)
                days_left = max(0, (sub.end_date - datetime.now(UTC)).days) if sub.end_date else 0
                keyboard.append(
                    [
                        types.InlineKeyboardButton(
                            text=f'📊 {tariff_name} ({days_left} {_get_days_word(days_left, db_user.language)})',
                            callback_data=f'st:{sub.id}',
                        )
                    ]
                )
            keyboard.append([types.InlineKeyboardButton(text=texts.BACK_BUTTON, callback_data='back_to_menu')])
            await callback.message.edit_text(
                texts.t('ADD_TRAFFIC_CHOOSE_SUBSCRIPTION_MESSAGE', '📊 <b>Add traffic</b>\n\nChoose a subscription:'),
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
            )
            await callback.answer()
            return

    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return

    if not subscription or subscription.is_trial:
        await callback.answer(
            texts.t('PAID_FEATURE_ONLY', '⚠ Эта функция доступна только для платных подписок'),
            show_alert=True,
        )
        return

    if subscription.traffic_limit_gb == 0:
        await callback.answer(
            texts.t('TRAFFIC_ALREADY_UNLIMITED', '⚠ У вас уже безлимитный трафик'),
            show_alert=True,
        )
        return

    # Режим тарифов - проверяем настройки тарифа
    if settings.is_tariffs_mode() and subscription.tariff_id:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        if not tariff or not tariff.can_topup_traffic():
            await callback.answer(
                texts.t(
                    'TARIFF_TRAFFIC_TOPUP_DISABLED',
                    '⚠️ На вашем тарифе докупка трафика недоступна',
                ),
                show_alert=True,
            )
            return

        # Показываем пакеты из тарифа
        current_traffic = subscription.traffic_limit_gb
        packages = tariff.get_traffic_topup_packages()

        period_hint_days = _get_period_hint_from_subscription(subscription)
        traffic_discount_percent = PricingEngine.get_addon_discount_percent(
            db_user,
            'traffic',
            period_hint_days,
        )

        prompt_text = texts.t(
            'ADD_TRAFFIC_PROMPT',
            (
                '📈 <b>Добавить трафик к подписке</b>\n\n'
                'Текущий лимит: {current_traffic}\n'
                'Выберите дополнительный трафик:'
            ),
        ).format(current_traffic=texts.format_traffic(current_traffic))

        await callback.message.edit_text(
            prompt_text,
            reply_markup=get_add_traffic_keyboard_from_tariff(
                db_user.language,
                packages,
                subscription.end_date,
                traffic_discount_percent,
                sub_id=sub_id,
            ),
            parse_mode='HTML',
        )

        await callback.answer()
        return

    # Стандартный режим - проверяем глобальные настройки
    if not settings.is_traffic_topup_enabled():
        await callback.answer(
            texts.t(
                'TRAFFIC_TOPUP_DISABLED',
                '⚠️ Функция докупки трафика отключена',
            ),
            show_alert=True,
        )
        return

    if settings.is_traffic_topup_blocked():
        await callback.answer(
            texts.t(
                'TRAFFIC_FIXED_MODE',
                '⚠️ В текущем режиме трафик фиксированный и не может быть изменен',
            ),
            show_alert=True,
        )
        return

    current_traffic = subscription.traffic_limit_gb
    period_hint_days = _get_period_hint_from_subscription(subscription)
    traffic_discount_percent = PricingEngine.get_addon_discount_percent(
        db_user,
        'traffic',
        period_hint_days,
    )

    prompt_text = texts.t(
        'ADD_TRAFFIC_PROMPT',
        ('📈 <b>Добавить трафик к подписке</b>\n\nТекущий лимит: {current_traffic}\nВыберите дополнительный трафик:'),
    ).format(current_traffic=texts.format_traffic(current_traffic))

    await callback.message.edit_text(
        prompt_text,
        reply_markup=get_add_traffic_keyboard(
            db_user.language,
            subscription.end_date,
            traffic_discount_percent,
            sub_id=sub_id,
        ),
        parse_mode='HTML',
    )

    await callback.answer()


def _calculate_traffic_reset_price(subscription) -> int:
    """Рассчитывает цену сброса трафика в зависимости от настроек."""
    mode = settings.get_traffic_reset_price_mode()
    base_price = settings.get_traffic_reset_base_price()

    # Если базовая цена не задана, используем цену периода 30 дней
    if base_price == 0:
        base_price = PERIOD_PRICES.get(30, 0)

    if mode == 'period':
        # Старое поведение: фиксированная цена = стоимость периода
        return base_price

    if mode == 'traffic':
        # Цена = стоимость текущего пакета трафика
        traffic_price = settings.get_traffic_price(subscription.traffic_limit_gb)
        return max(traffic_price, base_price)

    if mode == 'traffic_with_purchased':
        # Цена = стоимость базового трафика + докупленного
        # Базовый трафик = текущий лимит - докупленный
        purchased_gb = getattr(subscription, 'purchased_traffic_gb', 0) or 0
        base_traffic_gb = subscription.traffic_limit_gb - purchased_gb

        # Получаем цену базового трафика
        base_traffic_price = settings.get_traffic_price(base_traffic_gb) if base_traffic_gb > 0 else 0

        # Получаем цену докупленного трафика
        purchased_traffic_price = settings.get_traffic_price(purchased_gb) if purchased_gb > 0 else 0

        total_price = base_traffic_price + purchased_traffic_price
        return max(total_price, base_price)

    # Fallback на базовую цену
    return base_price


async def handle_reset_traffic(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
):
    from app.config import settings

    texts = get_texts(db_user.language)

    if settings.is_traffic_topup_blocked():
        await callback.answer(
            texts.t('TRAFFIC_FIXED_MODE', '⚠️ In the current mode traffic is fixed and cannot be changed'),
            show_alert=True,
        )
        return

    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return

    if not subscription or subscription.is_trial:
        await callback.answer(
            texts.t('PAID_FEATURE_ONLY', '⚠ This feature is available only for paid subscriptions'),
            show_alert=True,
        )
        return

    if subscription.traffic_limit_gb == 0:
        await callback.answer(
            texts.t('TRAFFIC_ALREADY_UNLIMITED', '⚠ You already have unlimited traffic'),
            show_alert=True,
        )
        return

    reset_price = _calculate_traffic_reset_price(subscription)

    # Формируем информацию о расчете цены
    purchased_gb = getattr(subscription, 'purchased_traffic_gb', 0) or 0
    price_info = ''
    if purchased_gb > 0 and settings.get_traffic_reset_price_mode() == 'traffic_with_purchased':
        base_traffic_gb = subscription.traffic_limit_gb - purchased_gb
        price_info = texts.t(
            'TRAFFIC_RESET_PRICE_CALCULATION_INFO',
            '\n\n💡 <i>Price calculation:</i>\n• Base traffic: {base}\n• Purchased: {purchased}',
        ).format(
            base=texts.format_traffic(base_traffic_gb),
            purchased=texts.format_traffic(purchased_gb),
        )

    # Проверяем достаточно ли средств
    has_enough_balance = db_user.balance_kopeks >= reset_price
    missing_kopeks = max(0, reset_price - db_user.balance_kopeks)

    # Формируем текст о балансе
    balance_info = texts.t('BALANCE_LINE_SIMPLE', '\n\n💰 Balance: {balance}').format(
        balance=texts.format_price(db_user.balance_kopeks)
    )
    if not has_enough_balance:
        balance_info += texts.t('MISSING_AMOUNT_LINE', '\n⚠️ Missing: {missing}').format(
            missing=texts.format_price(missing_kopeks)
        )

    await callback.message.edit_text(
        texts.t(
            'TRAFFIC_RESET_CONFIRM_MESSAGE',
            (
                '🔄 <b>Traffic reset</b>\n\n'
                'Used: {used}\n'
                'Limit: {limit}\n\n'
                'Reset cost: {price}{price_info}{balance_info}\n\n'
                'After the reset, the used traffic counter will become 0.'
            ),
        ).format(
            used=texts.format_traffic(subscription.traffic_used_gb, is_limit=False),
            limit=texts.format_traffic(subscription.traffic_limit_gb),
            price=texts.format_price(reset_price),
            price_info=price_info,
            balance_info=balance_info,
        ),
        reply_markup=get_reset_traffic_confirm_keyboard(
            reset_price,
            db_user.language,
            has_enough_balance=has_enough_balance,
            missing_kopeks=missing_kopeks,
        ),
    )

    await callback.answer()


async def confirm_reset_traffic(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
):
    from app.config import settings

    texts = get_texts(db_user.language)

    if settings.is_traffic_topup_blocked():
        await callback.answer(
            texts.t('TRAFFIC_FIXED_MODE', '⚠️ In the current mode traffic is fixed and cannot be changed'),
            show_alert=True,
        )
        return

    if settings.is_multi_tariff_enabled():
        _state_data = await state.get_data() if state else {}
        if not _state_data.get('active_subscription_id'):
            await callback.answer(
                texts.t('SELECT_SUBSCRIPTION_VIA_MY_SUBSCRIPTIONS', 'Select a subscription via "My subscriptions"'),
                show_alert=True,
            )
            return

    from app.database.crud.user import lock_user_for_pricing

    db_user = await lock_user_for_pricing(db, db_user.id)

    texts = get_texts(db_user.language)
    # Re-resolve after lock since db_user was refreshed
    subscription, _ = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return

    reset_price = _calculate_traffic_reset_price(subscription)

    if reset_price > 0 and db_user.balance_kopeks < reset_price:
        missing_kopeks = reset_price - db_user.balance_kopeks
        message_text = texts.t(
            'ADDON_INSUFFICIENT_FUNDS_MESSAGE',
            (
                '⚠️ <b>Недостаточно средств</b>\n\n'
                'Стоимость услуги: {required}\n'
                'На балансе: {balance}\n'
                'Не хватает: {missing}\n\n'
                'Выберите способ пополнения. Сумма подставится автоматически.'
            ),
        ).format(
            required=texts.format_price(reset_price, round_kopeks=False),
            balance=texts.format_price(db_user.balance_kopeks, round_kopeks=False),
            missing=texts.format_price(missing_kopeks, round_kopeks=False),
        )

        await callback.message.edit_text(
            message_text,
            reply_markup=get_insufficient_balance_keyboard(
                db_user.language,
                amount_kopeks=missing_kopeks,
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    try:
        success = await subtract_user_balance(
            db, db_user, reset_price, texts.t('TRAFFIC_RESET_TX_DESCRIPTION', 'Traffic reset')
        )

        if not success:
            await callback.answer(
                texts.t('PAYMENT_CHARGE_ERROR', '⚠️ Failed to charge the payment'), show_alert=True
            )
            return

        subscription.traffic_used_gb = 0.0
        subscription.updated_at = datetime.now(UTC)
        await db.commit()

        SubscriptionService()
        remnawave_service = RemnaWaveService()

        user = db_user
        remnawave_uuid = getattr(subscription, 'remnawave_uuid', None) or user.remnawave_uuid
        if remnawave_uuid:
            async with remnawave_service.get_api_client() as api:
                await api.reset_user_traffic(remnawave_uuid)

        await create_transaction(
            db=db,
            user_id=db_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=reset_price,
            description=texts.t('TRAFFIC_RESET_TX_DESCRIPTION', 'Traffic reset'),
        )

        await db.refresh(db_user)
        await db.refresh(subscription)

        await callback.message.edit_text(
            texts.t(
                'TRAFFIC_RESET_SUCCESS_MESSAGE',
                '✅ Traffic successfully reset!\n\n🔄 Used traffic has been zeroed\n📊 Limit: {limit}',
            ).format(limit=texts.format_traffic(subscription.traffic_limit_gb)),
            reply_markup=get_back_keyboard(db_user.language),
        )

        logger.info('✅ Пользователь сбросил трафик', telegram_id=db_user.telegram_id)

    except Exception as e:
        logger.error('Ошибка сброса трафика', error=e)
        await callback.message.edit_text(texts.ERROR, reply_markup=get_back_keyboard(db_user.language))

    await callback.answer()


async def refresh_traffic_config():
    try:
        from app.config import refresh_traffic_prices

        refresh_traffic_prices()

        packages = settings.get_traffic_packages()
        enabled_count = sum(1 for pkg in packages if pkg['enabled'])

        logger.info('🔄 Конфигурация трафика обновлена: активных пакетов', enabled_count=enabled_count)
        for pkg in packages:
            if pkg['enabled']:
                gb_text = '♾️ Безлимит' if pkg['gb'] == 0 else f'{pkg["gb"]} ГБ'
                logger.info('📦 ₽', gb_text=gb_text, pkg=pkg['price'] / 100)

        return True

    except Exception as e:
        logger.error('⚠️ Ошибка обновления конфигурации трафика', error=e)
        return False


async def get_traffic_packages_info() -> str:
    try:
        packages = settings.get_traffic_packages()

        info_lines = ['📦 Настроенные пакеты трафика:']

        enabled_packages = [pkg for pkg in packages if pkg['enabled']]
        disabled_packages = [pkg for pkg in packages if not pkg['enabled']]

        if enabled_packages:
            info_lines.append('\n✅ Активные:')
            for pkg in enabled_packages:
                gb_text = '♾️ Безлимит' if pkg['gb'] == 0 else f'{pkg["gb"]} ГБ'
                info_lines.append(f'   • {gb_text}: ${pkg["price"] // 100}')

        if disabled_packages:
            info_lines.append('\n❌ Отключенные:')
            for pkg in disabled_packages:
                gb_text = '♾️ Безлимит' if pkg['gb'] == 0 else f'{pkg["gb"]} ГБ'
                info_lines.append(f'   • {gb_text}: ${pkg["price"] // 100}')

        info_lines.append(f'\n📊 Всего пакетов: {len(packages)}')
        info_lines.append(f'🟢 Активных: {len(enabled_packages)}')
        info_lines.append(f'🔴 Отключенных: {len(disabled_packages)}')

        return '\n'.join(info_lines)

    except Exception as e:
        return f'⚠️ Ошибка получения информации: {e}'


async def select_traffic(callback: types.CallbackQuery, state: FSMContext, db_user: User):
    traffic_gb = int(callback.data.split('_')[1])
    texts = get_texts(db_user.language)

    data = await state.get_data()
    data['traffic_gb'] = traffic_gb

    traffic_price = settings.get_traffic_price(traffic_gb)
    data['total_price'] += traffic_price

    await state.set_data(data)

    if await _should_show_countries_management(db_user):
        countries = await _get_available_countries(db_user.promo_group_id)
        # Автоматически предвыбираем бесплатные серверы
        preselected = _get_preselected_free_countries(countries)
        data['countries'] = preselected
        await state.set_data(data)
        # Формируем текст с описаниями сквадов
        selection_text = _build_countries_selection_text(countries, texts.SELECT_COUNTRIES)
        await callback.message.edit_text(
            selection_text,
            reply_markup=get_countries_keyboard(countries, preselected, db_user.language),
            parse_mode='HTML',
        )
        await state.set_state(SubscriptionStates.selecting_countries)
        await callback.answer()
        return

    countries = await _get_available_countries(db_user.promo_group_id)
    available_countries = [c for c in countries if c.get('is_available', True)]
    data['countries'] = [available_countries[0]['uuid']] if available_countries else []
    await state.set_data(data)

    if settings.is_devices_selection_enabled():
        selected_devices = data.get('devices', settings.DEFAULT_DEVICE_LIMIT)

        await callback.message.edit_text(
            texts.SELECT_DEVICES, reply_markup=get_devices_keyboard(selected_devices, db_user.language)
        )
        await state.set_state(SubscriptionStates.selecting_devices)
        await callback.answer()
        return

    if await present_subscription_summary(callback, state, db_user, texts):
        await callback.answer()


async def add_traffic(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None):
    from app.database.crud.tariff import get_tariff_by_id

    traffic_gb = int(callback.data.split('_')[2])
    texts = get_texts(db_user.language)
    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return

    # Получаем цену: из тарифа или из глобальных настроек
    base_price = 0
    tariff = None

    if settings.is_tariffs_mode() and subscription and subscription.tariff_id:
        # Режим тарифов - берем цену из тарифа
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        if tariff and tariff.can_topup_traffic():
            base_price = tariff.get_traffic_topup_price(traffic_gb) or 0
        else:
            await callback.answer(
                texts.t('TARIFF_TRAFFIC_TOPUP_DISABLED', '⚠️ Traffic top-up is not available on your plan'),
                show_alert=True,
            )
            return
    else:
        # Стандартный режим
        if settings.is_traffic_topup_blocked():
            await callback.answer(
                texts.t('TRAFFIC_FIXED_MODE', '⚠️ In the current mode traffic is fixed and cannot be changed'),
                show_alert=True,
            )
            return
        base_price = settings.get_traffic_topup_price(traffic_gb)

    if base_price == 0 and traffic_gb != 0:
        await callback.answer(
            texts.t('TRAFFIC_PACKAGE_PRICE_NOT_CONFIGURED', '⚠️ The price for this package is not configured'),
            show_alert=True,
        )
        return

    # Lock user BEFORE price computation to prevent TOCTOU on group discount
    from app.database.crud.user import lock_user_for_pricing

    db_user = await lock_user_for_pricing(db, db_user.id)
    # Re-resolve after lock since db_user was refreshed
    subscription, _ = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return

    period_hint_days = _get_period_hint_from_subscription(subscription)
    discounted_per_month, discount_per_month, traffic_discount_pct = PricingEngine.calculate_traffic_discount(
        base_price,
        db_user,
        period_hint_days,
    )
    charged_days = 30

    # На тарифах пакеты трафика покупаются на 1 месяц (30 дней),
    # цена в тарифе уже месячная — не умножаем на оставшиеся месяцы подписки.
    # Пропорциональный расчёт применяем только в классическом режиме.
    is_tariff_mode = settings.is_tariffs_mode() and subscription and subscription.tariff_id

    if is_tariff_mode:
        price = discounted_per_month
    elif subscription:
        price, charged_days = calculate_prorated_price(
            discounted_per_month,
            subscription.end_date,
        )
    else:
        price = discounted_per_month

    total_discount_value = int(discount_per_month * charged_days / 30)

    if price > 0 and db_user.balance_kopeks < price:
        missing_kopeks = price - db_user.balance_kopeks

        # Save cart for auto-purchase after balance top-up
        cart_data = {
            'cart_mode': 'add_traffic',
            'subscription_id': subscription.id,
            'traffic_gb': traffic_gb,
            'price_kopeks': price,
            'base_price_kopeks': discounted_per_month,
            'discount_percent': traffic_discount_pct,
            'source': 'bot',
            'description': texts.t('TRAFFIC_PURCHASE_TX_DESCRIPTION', 'Extra traffic purchase: {gb} GB').format(
                gb=traffic_gb
            ),
        }
        try:
            await user_cart_service.save_user_cart(db_user.id, cart_data)
            logger.info(
                'Cart saved for traffic purchase (bot) user +', telegram_id=db_user.telegram_id, traffic_gb=traffic_gb
            )
        except Exception as e:
            logger.error('Error saving cart for traffic purchase (bot)', error=e)

        message_text = texts.t(
            'ADDON_INSUFFICIENT_FUNDS_MESSAGE',
            (
                '⚠️ <b>Недостаточно средств</b>\n\n'
                'Стоимость услуги: {required}\n'
                'На балансе: {balance}\n'
                'Не хватает: {missing}\n\n'
                'Выберите способ пополнения. Сумма подставится автоматически.'
            ),
        ).format(
            required=texts.format_price(price, round_kopeks=False),
            balance=texts.format_price(db_user.balance_kopeks, round_kopeks=False),
            missing=texts.format_price(missing_kopeks, round_kopeks=False),
        )

        await callback.message.edit_text(
            message_text,
            reply_markup=get_insufficient_balance_keyboard(
                db_user.language,
                amount_kopeks=missing_kopeks,
            ),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    # Сохраняем старое значение трафика для уведомления
    old_traffic_limit = subscription.traffic_limit_gb

    try:
        success = await subtract_user_balance(
            db,
            db_user,
            price,
            texts.t('ADD_TRAFFIC_TX_DESCRIPTION', 'Adding {gb} GB of traffic').format(gb=traffic_gb),
        )

        if not success:
            await callback.answer(
                texts.t('PAYMENT_CHARGE_ERROR', '⚠️ Failed to charge the payment'), show_alert=True
            )
            return

        if traffic_gb == 0:
            subscription.traffic_limit_gb = 0
            # При переходе на безлимит сбрасываем все докупки
            from sqlalchemy import delete

            from app.database.models import TrafficPurchase

            await db.execute(delete(TrafficPurchase).where(TrafficPurchase.subscription_id == subscription.id))
            subscription.purchased_traffic_gb = 0
            subscription.traffic_reset_at = None
        else:
            # add_subscription_traffic уже создаёт TrafficPurchase и обновляет все необходимые поля
            await add_subscription_traffic(db, subscription, traffic_gb)

        # Реактивируем подписку если она была DISABLED/EXPIRED (например, после LIMITED/EXPIRED в RemnaWave)
        await reactivate_subscription(db, subscription)

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        # Явно включаем пользователя на панели (PATCH может не снять LIMITED-статус)
        _en_uuid = (
            subscription.remnawave_uuid
            if settings.is_multi_tariff_enabled() and subscription.remnawave_uuid
            else db_user.remnawave_uuid
        )
        if _en_uuid and subscription.status == 'active':
            await subscription_service.enable_remnawave_user(_en_uuid)

        await create_transaction(
            db=db,
            user_id=db_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=price,
            description=texts.t('ADD_TRAFFIC_TX_DESCRIPTION', 'Adding {gb} GB of traffic').format(gb=traffic_gb),
        )

        await db.refresh(db_user)
        await db.refresh(subscription)

        # Отправляем уведомление админам о докупке трафика
        try:
            from app.services.admin_notification_service import AdminNotificationService

            notification_service = AdminNotificationService(callback.bot)
            await notification_service.send_subscription_update_notification(
                db, db_user, subscription, 'traffic', old_traffic_limit, subscription.traffic_limit_gb, price
            )
        except Exception as e:
            logger.error('Ошибка отправки уведомления о докупке трафика', error=e)

        success_text = texts.t('ADD_TRAFFIC_SUCCESS_TITLE', '✅ Traffic successfully added!\n\n')
        if traffic_gb == 0:
            success_text += texts.t('TRAFFIC_NOW_UNLIMITED_LINE', '🎉 You now have unlimited traffic!')
        else:
            success_text += texts.t('ADDED_GB_LINE', '📈 Added: {gb} GB\n').format(gb=traffic_gb)
            success_text += texts.t('NEW_LIMIT_LINE', 'New limit: {limit}').format(
                limit=texts.format_traffic(subscription.traffic_limit_gb)
            )

        if price > 0:
            success_text += texts.t('CHARGED_AMOUNT_LINE', '\n💰 Charged: {amount}').format(
                amount=texts.format_price(price)
            )
            if total_discount_value > 0:
                success_text += texts.t('DISCOUNT_SUFFIX_LINE', ' (discount {percent}%: -{amount})').format(
                    percent=traffic_discount_pct, amount=texts.format_price(total_discount_value)
                )

        await callback.message.edit_text(success_text, reply_markup=get_back_keyboard(db_user.language))

        logger.info('✅ Пользователь добавил ГБ трафика', telegram_id=db_user.telegram_id, traffic_gb=traffic_gb)

    except Exception as e:
        logger.error('Ошибка добавления трафика', error=e)
        await callback.message.edit_text(texts.ERROR, reply_markup=get_back_keyboard(db_user.language))

    await callback.answer()


async def handle_no_traffic_packages(callback: types.CallbackQuery, db_user: User):
    texts = get_texts(db_user.language)
    await callback.answer(
        texts.t(
            'NO_TRAFFIC_PACKAGES_ALERT',
            '⚠️ There are currently no traffic packages available. Please contact support for details.',
        ),
        show_alert=True,
    )


async def handle_switch_traffic(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
):
    from app.config import settings

    texts = get_texts(db_user.language)

    if settings.is_traffic_topup_blocked():
        await callback.answer(
            texts.t('TRAFFIC_FIXED_MODE', '⚠️ In the current mode traffic is fixed and cannot be changed'),
            show_alert=True,
        )
        return

    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return

    if not subscription or subscription.is_trial:
        await callback.answer(
            texts.t('PAID_FEATURE_ONLY', '⚠ This feature is available only for paid subscriptions'),
            show_alert=True,
        )
        return

    # Проверяем настройку тарифа
    if subscription.tariff_id:
        from app.database.crud.tariff import get_tariff_by_id

        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        if tariff and not tariff.allow_traffic_topup:
            await callback.answer(
                texts.t('TARIFF_TRAFFIC_SWITCH_DISABLED', '⚠️ Traffic switching is not available on your plan'),
                show_alert=True,
            )
            return

    current_traffic = subscription.traffic_limit_gb
    # Вычисляем базовый трафик (без докупленного) для корректного расчёта цен
    purchased_traffic = getattr(subscription, 'purchased_traffic_gb', 0) or 0
    base_traffic = current_traffic - purchased_traffic

    period_hint_days = _get_period_hint_from_subscription(subscription)
    traffic_discount_percent = PricingEngine.get_addon_discount_percent(
        db_user,
        'traffic',
        period_hint_days,
    )

    # Показываем информацию о докупленном трафике, если он есть
    purchased_info = ''
    if purchased_traffic > 0:
        purchased_info = texts.t(
            'TRAFFIC_PURCHASED_INFO_LINE',
            '\n📦 Base package: {base}\n➕ Purchased: {purchased}',
        ).format(base=texts.format_traffic(base_traffic), purchased=texts.format_traffic(purchased_traffic))

    await callback.message.edit_text(
        texts.t(
            'TRAFFIC_SWITCH_PROMPT_MESSAGE',
            (
                '🔄 <b>Switch traffic limit</b>\n\n'
                'Current limit: {current}{purchased_info}\n'
                'Choose a new traffic limit:\n\n'
                '💡 <b>Important:</b>\n'
                '• When increasing - you pay the difference\n'
                '• When decreasing - no refund is issued\n'
                '• Purchased traffic will be reset'
            ),
        ).format(current=texts.format_traffic(current_traffic), purchased_info=purchased_info),
        reply_markup=get_traffic_switch_keyboard(
            current_traffic,
            db_user.language,
            subscription.end_date,
            traffic_discount_percent,
            base_traffic_gb=base_traffic,
            back_callback=f'sm:{sub_id}' if settings.is_multi_tariff_enabled() and sub_id else 'subscription_settings',
        ),
    )

    await callback.answer()


async def confirm_switch_traffic(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
):
    new_traffic_gb = int(callback.data.split('_')[2])
    texts = get_texts(db_user.language)
    subscription, sub_id = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return

    current_traffic = subscription.traffic_limit_gb

    # Вычисляем базовый трафик (без докупленного) для корректного расчёта цены
    purchased_traffic = getattr(subscription, 'purchased_traffic_gb', 0) or 0
    base_traffic = current_traffic - purchased_traffic

    if new_traffic_gb == current_traffic:
        await callback.answer(
            texts.t('TRAFFIC_LIMIT_UNCHANGED_ALERT', 'ℹ️ Traffic limit unchanged'), show_alert=True
        )
        return

    # Используем базовый трафик для определения текущей цены пакета
    old_price_per_month = settings.get_traffic_price(base_traffic)
    new_price_per_month = settings.get_traffic_price(new_traffic_gb)

    now = datetime.now(UTC)
    days_remaining = max(1, math.ceil((subscription.end_date - now).total_seconds() / 86400))
    period_hint_days = days_remaining if days_remaining > 0 else None
    traffic_discount_percent = PricingEngine.get_addon_discount_percent(
        db_user,
        'traffic',
        period_hint_days,
    )

    discounted_old_per_month = PricingEngine.apply_discount(
        old_price_per_month,
        traffic_discount_percent,
    )
    discounted_new_per_month = PricingEngine.apply_discount(
        new_price_per_month,
        traffic_discount_percent,
    )
    price_difference_per_month = discounted_new_per_month - discounted_old_per_month
    discount_savings_per_month = (new_price_per_month - old_price_per_month) - price_difference_per_month

    if price_difference_per_month > 0:
        total_price_difference = int(price_difference_per_month * days_remaining / 30)
        total_price_difference = max(100, total_price_difference)

        if total_price_difference > 0 and db_user.balance_kopeks < total_price_difference:
            missing_kopeks = total_price_difference - db_user.balance_kopeks
            message_text = texts.t(
                'ADDON_INSUFFICIENT_FUNDS_MESSAGE',
                (
                    '⚠️ <b>Недостаточно средств</b>\n\n'
                    'Стоимость услуги: {required}\n'
                    'На балансе: {balance}\n'
                    'Не хватает: {missing}\n\n'
                    'Выберите способ пополнения. Сумма подставится автоматически.'
                ),
            ).format(
                required=texts.t('AMOUNT_FOR_DAYS_SUFFIX', '{price} (for {days} days)').format(
                    price=texts.format_price(total_price_difference), days=days_remaining
                ),
                balance=texts.format_price(db_user.balance_kopeks, round_kopeks=False),
                missing=texts.format_price(missing_kopeks, round_kopeks=False),
            )

            await callback.message.edit_text(
                message_text,
                reply_markup=get_insufficient_balance_keyboard(
                    db_user.language,
                    amount_kopeks=missing_kopeks,
                ),
                parse_mode='HTML',
            )
            await callback.answer()
            return

        action_text = texts.t('INCREASE_TO_LABEL', 'increase to {limit}').format(
            limit=texts.format_traffic(new_traffic_gb)
        )
        cost_text = texts.t('SURCHARGE_FOR_DAYS_LINE', 'Surcharge: {price} (for {days} days)').format(
            price=texts.format_price(total_price_difference), days=days_remaining
        )
        if discount_savings_per_month > 0:
            total_discount_savings = int(discount_savings_per_month * days_remaining / 30)
            cost_text += texts.t('DISCOUNT_SUFFIX_LINE', ' (discount {percent}%: -{amount})').format(
                percent=traffic_discount_percent, amount=texts.format_price(total_discount_savings)
            )
    else:
        total_price_difference = 0
        action_text = texts.t('DECREASE_TO_LABEL', 'decrease to {limit}').format(
            limit=texts.format_traffic(new_traffic_gb)
        )
        cost_text = texts.t('DEVICE_CHANGE_NO_REFUND', 'Payments are not refunded')

    confirm_text = texts.t('TRAFFIC_SWITCH_CONFIRM_TITLE', '🔄 <b>Confirm traffic switch</b>\n\n')
    confirm_text += texts.t('CURRENT_LIMIT_LINE', 'Current limit: {limit}\n').format(
        limit=texts.format_traffic(current_traffic)
    )
    confirm_text += texts.t('NEW_LIMIT_DOUBLE_LINE', 'New limit: {limit}\n\n').format(
        limit=texts.format_traffic(new_traffic_gb)
    )
    confirm_text += texts.t('ACTION_LINE', 'Action: {action}\n').format(action=action_text)
    confirm_text += f'💰 {cost_text}\n\n'
    confirm_text += texts.t('CONFIRM_SWITCH_QUESTION', 'Confirm the switch?')

    await callback.message.edit_text(
        confirm_text,
        reply_markup=get_confirm_switch_traffic_keyboard(
            new_traffic_gb,
            total_price_difference,
            db_user.language,
            back_callback=f'sm:{sub_id}' if settings.is_multi_tariff_enabled() and sub_id else 'subscription_settings',
        ),
        parse_mode='HTML',
    )

    await callback.answer()


async def execute_switch_traffic(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
):
    callback_parts = callback.data.split('_')
    new_traffic_gb = int(callback_parts[3])

    from app.database.crud.user import lock_user_for_pricing

    db_user = await lock_user_for_pricing(db, db_user.id)

    texts = get_texts(db_user.language)
    # Re-resolve after lock since db_user was refreshed
    subscription, _ = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        return
    current_traffic = subscription.traffic_limit_gb

    # Recompute price under lock (callback-baked value may be stale)
    purchased_traffic = getattr(subscription, 'purchased_traffic_gb', 0) or 0
    base_traffic = current_traffic - purchased_traffic
    old_price_per_month = settings.get_traffic_price(base_traffic)
    new_price_per_month = settings.get_traffic_price(new_traffic_gb)
    days_remaining = max(1, math.ceil((subscription.end_date - datetime.now(UTC)).total_seconds() / 86400))
    traffic_discount_percent = PricingEngine.get_addon_discount_percent(
        db_user,
        'traffic',
        days_remaining,
    )
    discounted_old = PricingEngine.apply_discount(old_price_per_month, traffic_discount_percent)
    discounted_new = PricingEngine.apply_discount(new_price_per_month, traffic_discount_percent)
    price_diff_per_month = discounted_new - discounted_old
    if price_diff_per_month > 0:
        price_difference = int(price_diff_per_month * days_remaining / 30)
        price_difference = max(100, price_difference)
    else:
        price_difference = 0

    try:
        if price_difference > 0:
            success = await subtract_user_balance(
                db,
                db_user,
                price_difference,
                texts.t('TRAFFIC_SWITCH_TX_DESCRIPTION', 'Traffic switch from {old}GB to {new}GB').format(
                    old=current_traffic, new=new_traffic_gb
                ),
            )

            if not success:
                await callback.answer(
                    texts.t('PAYMENT_CHARGE_ERROR', '⚠️ Failed to charge the payment'), show_alert=True
                )
                return

            days_remaining = max(1, math.ceil((subscription.end_date - datetime.now(UTC)).total_seconds() / 86400))
            await create_transaction(
                db=db,
                user_id=db_user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=price_difference,
                description=texts.t(
                    'TRAFFIC_SWITCH_TX_DESCRIPTION_WITH_DAYS',
                    'Traffic switch from {old}GB to {new}GB over {days} days',
                ).format(old=current_traffic, new=new_traffic_gb, days=days_remaining),
            )

        subscription.traffic_limit_gb = new_traffic_gb
        # Сбрасываем все докупки трафика при переключении пакета
        from sqlalchemy import delete

        from app.database.models import TrafficPurchase

        await db.execute(delete(TrafficPurchase).where(TrafficPurchase.subscription_id == subscription.id))
        subscription.purchased_traffic_gb = 0
        subscription.traffic_reset_at = None  # Сбрасываем дату сброса трафика
        subscription.updated_at = datetime.now(UTC)

        await db.commit()

        # Реактивируем подписку если она была DISABLED/EXPIRED (например, после LIMITED/EXPIRED в RemnaWave)
        await reactivate_subscription(db, subscription)

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        # Явно включаем пользователя на панели (PATCH может не снять LIMITED-статус)
        _en_uuid = (
            subscription.remnawave_uuid
            if settings.is_multi_tariff_enabled() and subscription.remnawave_uuid
            else db_user.remnawave_uuid
        )
        if _en_uuid and subscription.status == 'active':
            await subscription_service.enable_remnawave_user(_en_uuid)

        await db.refresh(db_user)
        await db.refresh(subscription)

        try:
            from app.services.admin_notification_service import AdminNotificationService

            notification_service = AdminNotificationService(callback.bot)
            await notification_service.send_subscription_update_notification(
                db, db_user, subscription, 'traffic', current_traffic, new_traffic_gb, price_difference
            )
        except Exception as e:
            logger.error('Ошибка отправки уведомления об изменении трафика', error=e)

        if new_traffic_gb > current_traffic:
            success_text = texts.t('TRAFFIC_LIMIT_INCREASED_SUCCESS', '✅ Traffic limit increased!\n\n')
            success_text += texts.t('DEVICE_CHANGE_RESULT_LINE', '📱 Was: {old} → Now: {new}\n').format(
                old=texts.format_traffic(current_traffic), new=texts.format_traffic(new_traffic_gb)
            )
            if price_difference > 0:
                success_text += texts.t('DEVICE_CHANGE_CHARGED', '💰 Charged: {amount}').format(
                    amount=texts.format_price(price_difference)
                )
        elif new_traffic_gb < current_traffic:
            success_text = texts.t('TRAFFIC_LIMIT_DECREASED_SUCCESS', '✅ Traffic limit decreased!\n\n')
            success_text += texts.t('DEVICE_CHANGE_RESULT_LINE', '📱 Was: {old} → Now: {new}\n').format(
                old=texts.format_traffic(current_traffic), new=texts.format_traffic(new_traffic_gb)
            )
            success_text += texts.t('DEVICE_CHANGE_NO_REFUND_INFO', 'ℹ️ Payments are not refunded')

        await callback.message.edit_text(success_text, reply_markup=get_back_keyboard(db_user.language))

        logger.info(
            '✅ Пользователь переключил трафик с на доплата: ₽',
            telegram_id=db_user.telegram_id,
            current_traffic=current_traffic,
            new_traffic_gb=new_traffic_gb,
            price_difference=price_difference / 100,
        )

    except Exception as e:
        logger.error('Ошибка переключения трафика', error=e)
        await callback.message.edit_text(texts.ERROR, reply_markup=get_back_keyboard(db_user.language))

    await callback.answer()
