import html
import logging
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Awaitable, Callable, TypeVar
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LinkPreviewOptions, Message

log = logging.getLogger(__name__)
T = TypeVar("T")


def now() -> int:
    return int(time.time())


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def fmt_num(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def fmt_stars(cents: int) -> str:
    """Баланс бота хранится в сотых долях звезды: 25 → «0.25», 500 → «5»."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole}" + (f".{frac:02d}".rstrip("0") if frac else "")


def parse_stars(raw: str, max_value: int = 1_000_000) -> int | None:
    """«2», «0.25», «0,5 ⭐» → сотые доли звезды; None — не число, больше двух знаков после точки или вне 0…max."""
    raw = raw.strip().rstrip("⭐ ").replace(",", ".").replace(" ", "")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if not value.is_finite() or value < 0 or value > max_value or value != value.quantize(Decimal("0.01")):
        return None
    return int(value * 100)


def fmt_duration(seconds: int) -> str:
    hours, rest = divmod(max(0, seconds), 3600)
    minutes = -(-rest // 60)
    if minutes == 60:
        hours, minutes = hours + 1, 0
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    return f"{hours} ч" if hours else f"{max(1, minutes)} мин"


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


def strip_tags(html_text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))


# ---------- премиум-эмодзи на кнопках ----------

ICON_FALLBACK: dict[str, str] = {}  # id премиум-эмодзи → обычный эмодзи, если Telegram иконку не примет


def icon_text(text: str, icon_id: str | None, fallback: str) -> tuple[str, str | None]:
    """Текст кнопки и id иконки: с иконкой — без эмодзи в тексте (его заменит иконка)."""
    if icon_id:
        ICON_FALLBACK[icon_id] = fallback
        return text, icon_id
    return (f"{fallback} {text}" if fallback else text), None


def has_icons(kb: InlineKeyboardMarkup | None) -> bool:
    return kb is not None and any(b.icon_custom_emoji_id for row in kb.inline_keyboard for b in row)


def without_icons(kb: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Клавиатура без премиум-эмодзи: они доступны, только если у владельца бота есть Telegram Premium."""
    rows = []
    for row in kb.inline_keyboard:
        new_row = []
        for b in row:
            if b.icon_custom_emoji_id:
                fallback = ICON_FALLBACK.get(b.icon_custom_emoji_id, "")
                b = b.model_copy(update={"icon_custom_emoji_id": None,
                                         "text": f"{fallback} {b.text}" if fallback else b.text})
            new_row.append(b)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def icon_safe(send: Callable[[InlineKeyboardMarkup | None], Awaitable[T]],
                    kb: InlineKeyboardMarkup | None) -> T:
    """Отправка с клавиатурой; если Telegram не принял премиум-эмодзи — повтор без них."""
    try:
        return await send(kb)
    except TelegramBadRequest as e:
        if not has_icons(kb) or "message is not modified" in str(e):
            raise
        log.warning("Кнопки с премиум-эмодзи не приняты (%s) — показываю без них", e)
        return await send(without_icons(kb))


async def show(event: Message | CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> Message | None:
    """Единая точка вывода «экрана»: редактирует сообщение с кнопкой или отправляет новое."""
    if isinstance(event, Message):
        return await icon_safe(lambda m: event.answer(text, reply_markup=m), kb)

    msg = event.message
    if isinstance(msg, Message) and msg.text is not None:
        try:
            return await icon_safe(lambda m: msg.edit_text(text, reply_markup=m), kb)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return msg
    sent = await icon_safe(lambda m: event.bot.send_message(event.from_user.id, text, reply_markup=m), kb)
    if isinstance(msg, Message):
        try:
            await msg.delete()
        except TelegramBadRequest:
            pass
    return sent


async def send_card(bot: Bot, chat_id: int, text: str, kb: InlineKeyboardMarkup | None = None,
                    photo: str | None = None, preview_url: str | None = None) -> Message:
    """Карточка: фото с подписью, иначе текст с большим превью ссылки (или без превью)."""
    if photo:
        try:
            return await bot.send_photo(chat_id, photo, caption=text, reply_markup=kb)
        except TelegramBadRequest:  # слишком длинная подпись или битый file_id — покажем текстом
            pass
    preview = (LinkPreviewOptions(url=preview_url, prefer_large_media=True, show_above_text=True)
               if preview_url else None)
    return await bot.send_message(chat_id, text, reply_markup=kb, link_preview_options=preview)


async def show_card(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None,
                    photo: str | None = None, preview_url: str | None = None) -> None:
    """Как show(), но для карточек с картинкой/превью: текстовый экран редактирует, иначе присылает заново."""
    msg = call.message
    if not photo and isinstance(msg, Message) and msg.text is not None:
        preview = (LinkPreviewOptions(url=preview_url, prefer_large_media=True, show_above_text=True)
                   if preview_url else None)
        try:
            await msg.edit_text(text, reply_markup=kb, link_preview_options=preview)
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return
    await send_card(call.bot, call.from_user.id, text, kb, photo, preview_url)
    if isinstance(msg, Message):
        try:
            await msg.delete()
        except TelegramBadRequest:
            pass
