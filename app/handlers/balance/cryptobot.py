import html

import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.keyboards.topup_amounts import get_topup_amount_keyboard
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


@error_handler
async def start_cryptobot_payment(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = html.escape(
            getattr(db_user, 'restriction_reason', None)
            or texts.t('TOPUP_RESTRICTION_DEFAULT_REASON', 'This action is restricted by the administrator')
        )
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append(
                [types.InlineKeyboardButton(text=texts.USER_RESTRICTION_APPEAL_BUTTON, url=support_url)]
            )
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await callback.message.edit_text(
            texts.USER_RESTRICTION_TOPUP_BLOCKED.format(reason=reason),
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        await callback.answer()
        return

    if not settings.is_cryptobot_enabled():
        await callback.answer(
            texts.t('CRYPTOBOT_UNAVAILABLE_ALERT', '❌ Crypto payment is temporarily unavailable'), show_alert=True
        )
        return

    available_assets = settings.get_cryptobot_assets()
    assets_text = ', '.join(available_assets)

    message_text = texts.t(
        'CRYPTOBOT_TOPUP_PROMPT',
        '🪙 <b>Top up with crypto</b>\n\n'
        'Enter an amount between $1 and $1000:\n\n'
        '💰 Available assets: {assets}\n'
        '⚡ Instant balance credit\n'
        '🔒 Secure payment via CryptoBot',
    ).format(assets=assets_text)

    keyboard = await get_topup_amount_keyboard('cryptobot', db_user.language, back_callback='back_to_menu')

    await callback.message.edit_text(message_text, reply_markup=keyboard, parse_mode='HTML')

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(
        payment_method='cryptobot',
        cryptobot_prompt_message_id=callback.message.message_id,
        cryptobot_prompt_chat_id=callback.message.chat.id,
    )
    await callback.answer()


@error_handler
async def process_cryptobot_payment_amount(
    message: types.Message, db_user: User, db: AsyncSession, amount_kopeks: int, state: FSMContext
):
    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = html.escape(
            getattr(db_user, 'restriction_reason', None)
            or texts.t('TOPUP_RESTRICTION_DEFAULT_REASON', 'This action is restricted by the administrator')
        )
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append(
                [types.InlineKeyboardButton(text=texts.USER_RESTRICTION_APPEAL_BUTTON, url=support_url)]
            )
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await message.answer(
            texts.USER_RESTRICTION_TOPUP_BLOCKED.format(reason=reason),
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode='HTML',
        )
        await state.clear()
        return

    texts = get_texts(db_user.language)

    if not settings.is_cryptobot_enabled():
        await message.answer(texts.t('CRYPTOBOT_UNAVAILABLE_ALERT', '❌ Crypto payment is temporarily unavailable'))
        return

    amount_usd = round(amount_kopeks / 100, 2)

    if amount_usd < 1:
        await message.answer(
            texts.t('CRYPTOBOT_MIN_AMOUNT_ERROR', '❌ Minimum top-up amount: $1'),
            reply_markup=get_back_keyboard(db_user.language),
        )
        return

    if amount_usd > 1000:
        await message.answer(
            texts.t('CRYPTOBOT_MAX_AMOUNT_ERROR', '❌ Maximum top-up amount: $1000'),
            reply_markup=get_back_keyboard(db_user.language),
        )
        return

    try:
        payment_service = PaymentService(message.bot)

        payment_result = await payment_service.create_cryptobot_payment(
            db=db,
            user_id=db_user.id,
            amount_usd=amount_usd,
            asset=settings.CRYPTOBOT_DEFAULT_ASSET,
            description=settings.get_balance_payment_description(
                amount_kopeks,
                telegram_user_id=db_user.telegram_id,
                user_db_id=db_user.id,
            ),
            payload=f'balance_{db_user.id}_{amount_kopeks}',
        )

        if not payment_result:
            await message.answer(
                texts.t(
                    'CRYPTOBOT_PAYMENT_CREATE_ERROR',
                    '❌ Error creating the payment. Please try again later or contact support.',
                )
            )
            await state.clear()
            return

        bot_invoice_url = payment_result.get('bot_invoice_url')
        mini_app_invoice_url = payment_result.get('mini_app_invoice_url')

        payment_url = bot_invoice_url or mini_app_invoice_url

        if not payment_url:
            await message.answer(
                texts.t('CRYPTOBOT_PAYMENT_LINK_ERROR', '❌ Error retrieving the payment link. Please contact support.')
            )
            await state.clear()
            return

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.t('CRYPTOBOT_PAY_BUTTON', '🪙 Pay'), url=payment_url)],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CRYPTOBOT_CHECK_STATUS_BUTTON', '📊 Check status'),
                        callback_data=f'check_cryptobot_{payment_result["local_payment_id"]}',
                    )
                ],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')],
            ]
        )

        state_data = await state.get_data()
        prompt_message_id = state_data.get('cryptobot_prompt_message_id')
        prompt_chat_id = state_data.get('cryptobot_prompt_chat_id', message.chat.id)

        try:
            await message.delete()
        except Exception as delete_error:  # pragma: no cover - depends on bot rights
            logger.warning('Не удалось удалить сообщение с суммой CryptoBot', delete_error=delete_error)

        if prompt_message_id and prompt_message_id != message.message_id:
            try:
                await message.bot.delete_message(prompt_chat_id, prompt_message_id)
            except Exception as delete_error:  # pragma: no cover - diagnostics
                logger.warning('Не удалось удалить сообщение с запросом суммы CryptoBot', delete_error=delete_error)

        invoice_message = await message.answer(
            texts.t(
                'CRYPTOBOT_INVOICE_MESSAGE',
                '🪙 <b>Crypto payment</b>\n\n'
                '💰 Amount to credit: {amount}\n'
                '💵 To pay: {amount_usd} USD\n'
                '🪙 Asset: {asset}\n'
                '🆔 Payment ID: {invoice_id}...\n\n'
                '📱 <b>Instructions:</b>\n'
                "1. Tap the 'Pay' button\n"
                '2. Choose a convenient asset\n'
                '3. Transfer the specified amount\n'
                '4. Funds will be credited to your balance automatically\n\n'
                '🔒 Payment is processed via the secure CryptoBot system\n'
                '⚡ Supported assets: USDT, TON, BTC, ETH\n\n'
                '❓ If you run into any issues, contact {support}',
            ).format(
                amount=settings.format_price(amount_kopeks),
                amount_usd=f'{amount_usd:.2f}',
                asset=payment_result['asset'],
                invoice_id=payment_result['invoice_id'][:8],
                support=settings.get_support_contact_display_html(),
            ),
            reply_markup=keyboard,
            parse_mode='HTML',
        )

        await state.update_data(
            cryptobot_invoice_message_id=invoice_message.message_id,
            cryptobot_invoice_chat_id=invoice_message.chat.id,
        )

        await state.clear()

        logger.info(
            'Создан CryptoBot платеж',
            telegram_id=db_user.telegram_id,
            amount_usd=round(amount_usd, 2),
            payment_result=payment_result['invoice_id'],
        )

    except Exception as e:
        logger.error('Ошибка создания CryptoBot платежа', error=e)
        await message.answer(
            texts.t(
                'CRYPTOBOT_PAYMENT_CREATE_ERROR',
                '❌ Error creating the payment. Please try again later or contact support.',
            )
        )
        await state.clear()


@error_handler
async def check_cryptobot_payment_status(callback: types.CallbackQuery, db: AsyncSession, db_user: User):
    texts = get_texts(db_user.language)
    try:
        local_payment_id = int(callback.data.split('_')[-1])

        from app.database.crud.cryptobot import get_cryptobot_payment_by_id

        payment = await get_cryptobot_payment_by_id(db, local_payment_id)

        if not payment:
            await callback.answer(texts.t('CRYPTOBOT_PAYMENT_NOT_FOUND', '❌ Payment not found'), show_alert=True)
            return

        status_emoji = {'active': '⏳', 'paid': '✅', 'expired': '❌'}

        status_labels = {
            'active': texts.t('CRYPTOBOT_STATUS_PENDING', 'Awaiting payment'),
            'paid': texts.t('CRYPTOBOT_STATUS_PAID', 'Paid'),
            'expired': texts.t('CRYPTOBOT_STATUS_EXPIRED', 'Expired'),
        }

        emoji = status_emoji.get(payment.status, '❓')
        status = status_labels.get(payment.status, texts.t('CRYPTOBOT_STATUS_UNKNOWN', 'Unknown'))

        message_text = texts.t(
            'CRYPTOBOT_STATUS_MESSAGE',
            '🪙 Payment status:\n\n'
            '🆔 ID: {invoice_id}...\n'
            '💰 Amount: {amount} {asset}\n'
            '📊 Status: {emoji} {status}\n'
            '📅 Created: {created_at}\n',
        ).format(
            invoice_id=payment.invoice_id[:8],
            amount=payment.amount,
            asset=payment.asset,
            emoji=emoji,
            status=status,
            created_at=payment.created_at.strftime('%d.%m.%Y %H:%M'),
        )

        if payment.is_paid:
            message_text += '\n' + texts.t(
                'CRYPTOBOT_STATUS_PAID_NOTE', '✅ Payment completed successfully!\n\nFunds have been credited to your balance.'
            )
        elif payment.is_pending:
            message_text += '\n' + texts.t(
                'CRYPTOBOT_STATUS_PENDING_NOTE', "⏳ Payment is awaiting completion. Tap the 'Pay' button above."
            )
        elif payment.is_expired:
            message_text += '\n' + texts.t('CRYPTOBOT_STATUS_EXPIRED_NOTE', '❌ The payment has expired. Contact {support}').format(
                support=settings.get_support_contact_display()
            )

        await callback.answer(message_text, show_alert=True)

    except Exception as e:
        logger.error('Ошибка проверки статуса CryptoBot платежа', error=e)
        await callback.answer(texts.t('CRYPTOBOT_STATUS_CHECK_ERROR', '❌ Error checking status'), show_alert=True)
