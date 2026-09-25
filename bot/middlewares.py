import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from bot.utils import show
from bot.views import subscribe_screen

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


async def _deny(event: TelegramObject, text: str) -> None:
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
    elif isinstance(event, Message):
        await event.answer(text)


class UserMiddleware(BaseMiddleware):
    """Регистрирует/обновляет пользователя, отсекает банов и режим техработ."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: User | None = data.get("event_from_user")
        chat = data.get("event_chat")
        if tg_user is None or tg_user.is_bot or chat is None or chat.type != "private":
            return await handler(event, data)

        db = data["db"]
        user, is_new = await db.upsert_user(tg_user.id, tg_user.username, tg_user.full_name)
        is_admin = data["admins"].is_admin(tg_user.id)
        data.update(user=user, is_new=is_new, is_admin=is_admin)

        if not is_admin:
            if user["is_banned"]:
                return await _deny(event, "🚫 Доступ к боту ограничен.")
            if data["settings"].flag("maintenance"):
                return await _deny(event, "🛠 Бот на техническом обслуживании. Загляни чуть позже!")
        return await handler(event, data)


class ThrottlingMiddleware(BaseMiddleware):
    """Гасит флуд кнопками и сообщениями (для админов не действует)."""

    def __init__(self, rate: float = 0.5) -> None:
        self.rate = rate
        self._last: dict[int, float] = {}

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: User | None = data.get("event_from_user")
        if tg_user is None or data["admins"].is_admin(tg_user.id):
            return await handler(event, data)
        now = time.monotonic()
        if now - self._last.get(tg_user.id, 0) < self.rate:
            if isinstance(event, CallbackQuery):
                await event.answer("⏳ Не так быстро…")
            return None
        self._last[tg_user.id] = now
        if len(self._last) > 50_000:
            self._last = {k: v for k, v in self._last.items() if now - v < 60}
        return await handler(event, data)


class SubscriptionGate(BaseMiddleware):
    """Перед любым действием пользователя проверяет обязательную подписку."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        if get_flag(data, "skip_sub") or "user" not in data:
            return await handler(event, data)

        user = data["user"]
        missing = await data["subs"].missing(user["user_id"])
        if not missing:
            if user["verified_at"] is None:
                await data["rewards"].complete_verification(user["user_id"])
                data["user"] = await data["db"].get_user(user["user_id"])
            return await handler(event, data)

        text, kb = subscribe_screen(data["settings"], user["full_name"], missing)
        if isinstance(event, CallbackQuery):
            answer = data.get("callback_answer")
            if answer is not None:
                answer.text = "📢 Сначала подпишись на каналы"
        await show(event, text, kb)
        return None
