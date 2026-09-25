import html
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message


def now() -> int:
    return int(time.time())


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def fmt_num(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def fmt_dt(ts: int | None, tz: ZoneInfo) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz).strftime("%d.%m.%Y %H:%M")


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def progress_bar(current: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "▱" * width
    filled = round(width * max(0, min(current, total)) / total)
    return "▰" * filled + "▱" * (width - filled)


def percent(part: int, whole: int) -> int:
    return round(part * 100 / whole) if whole else 0


def user_link(user_id: int, name: str | None) -> str:
    return f'<a href="tg://user?id={user_id}">{esc(name) or user_id}</a>'


def render_template(template: str, **values: object) -> str:
    """Подставляет {placeholder}; незнакомые фигурные скобки оставляет как есть."""
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return template


async def show(event: Message | CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> Message | None:
    """Единая точка вывода «экрана»: редактирует сообщение с кнопкой или отправляет новое."""
    if isinstance(event, Message):
        return await event.answer(text, reply_markup=kb)

    msg = event.message
    if isinstance(msg, Message) and msg.text is not None:
        try:
            return await msg.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return msg
    sent = await event.bot.send_message(event.from_user.id, text, reply_markup=kb)
    if isinstance(msg, Message):
        try:
            await msg.delete()
        except TelegramBadRequest:
            pass
    return sent
