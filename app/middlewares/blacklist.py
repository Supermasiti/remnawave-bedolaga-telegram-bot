from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery, TelegramObject, User as TgUser

from app.services.blacklist_service import blacklist_service


logger = structlog.get_logger(__name__)


class BlacklistMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: TgUser | None = None
        if isinstance(event, (Message, CallbackQuery, PreCheckoutQuery)):
            user = event.from_user

        if not user or user.is_bot:
            return await handler(event, data)

        is_blacklisted, reason = await blacklist_service.is_user_blacklisted(user.id, user.username)

        if not is_blacklisted:
            return await handler(event, data)

        logger.warning('🚫 Пользователь из черного списка', user_id=user.id, username=user.username, reason=reason)

        block_text = f'🚫 Access denied\n\nReason: {reason}\n\nIf you believe this is a mistake, contact support.'

        try:
            if isinstance(event, Message):
                await event.answer(block_text)
            elif isinstance(event, CallbackQuery):
                await event.answer(block_text, show_alert=True)
            elif isinstance(event, PreCheckoutQuery):
                await event.answer(ok=False, error_message='Access denied')
        except Exception as e:
            logger.error('Ошибка отправки сообщения о блокировке пользователю', user_id=user.id, error=e)

        return None
