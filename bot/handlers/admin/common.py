from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject

from bot.callbacks import A
from bot.services.admins import AdminRegistry
from bot.utils import show

Btn = InlineKeyboardButton


class IsAdmin(Filter):
    async def __call__(self, event: Message | CallbackQuery, admins: AdminRegistry) -> bool:
        return event.from_user is not None and admins.is_admin(event.from_user.id)


class ResetStateMiddleware(BaseMiddleware):
    """Любая кнопка админки отменяет незавершённый ввод (кроме помеченных keep_state)."""

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        state: FSMContext | None = data.get("state")
        if state is not None and not get_flag(data, "keep_state") and await state.get_state() is not None:
            await state.clear()
        return await handler(event, data)


class CommandResetsStateMiddleware(BaseMiddleware):
    """Outer: любая /команда прерывает ввод, чтобы /start не попал, например, в поиск пользователя."""

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        state: FSMContext | None = data.get("state")
        if (isinstance(event, Message) and event.text and event.text.startswith("/")
                and state is not None and await state.get_state() is not None):
            await state.clear()
        return await handler(event, data)


class Input(StatesGroup):
    channel = State()
    channel_link = State()
    goal = State()
    gift_text = State()
    text = State()
    user_search = State()
    user_message = State()
    admin_add = State()
    bc_message = State()
    bc_buttons = State()


def btn(text: str, s: str, a: str = "open", id: int = 0, p: int = 0, v: str = "",
        style: str | None = None) -> Btn:
    return Btn(text=text, style=style, callback_data=A(s=s, a=a, id=id, p=p, v=v).pack())


def kb(*rows: list[Btn]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def back(s: str = "home", a: str = "open", text: str = "« Назад", **kw: Any) -> list[Btn]:
    return [btn(text, s, a, **kw)]


def pager(s: str, a: str, page: int, pages: int, **kw: Any) -> list[Btn]:
    if pages <= 1:
        return []
    return [
        btn("◀️", s, a, p=(page - 1) % pages, **kw),
        btn(f"{page + 1} / {pages}", "noop"),
        btn("▶️", s, a, p=(page + 1) % pages, **kw),
    ]


def pages_count(total: int, per_page: int) -> int:
    return max(1, -(-total // per_page))


async def prompt(call: CallbackQuery, state: FSMContext, st: State, text: str,
                 cancel: list[Btn], **data: Any) -> None:
    """Переводит админа в режим ввода и показывает подсказку с кнопкой отмены."""
    await state.set_state(st)
    msg = await show(call, text, kb(cancel))
    await state.update_data(prompt_id=msg.message_id if msg else None, **data)


async def drop_prompt(message: Message, state: FSMContext) -> dict[str, Any]:
    """Удаляет сообщение-подсказку, возвращает данные FSM."""
    data = await state.get_data()
    if pid := data.get("prompt_id"):
        try:
            await message.bot.delete_message(message.chat.id, pid)
        except TelegramBadRequest:
            pass
    return data
