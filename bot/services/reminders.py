"""Напоминания «забери подарок» тем, кто нажал /start, но не прошёл обязательную подписку."""
import asyncio
import json
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiosqlite import Row

from bot.callbacks import U
from bot.database import Database
from bot.settings import DEFAULTS, Settings
from bot.utils import esc, now, render_template

log = logging.getLogger(__name__)

TICK_SECONDS = 15
BATCH = 200
MAX_VARIANTS = 10
MAX_COUNT = 10


class ReminderService:
    def __init__(self, bot: Bot, db: Database, settings: Settings) -> None:
        self.bot = bot
        self.db = db
        self.settings = settings
        self._task: asyncio.Task | None = None

    # ---------- настройки ----------
    @property
    def enabled(self) -> bool:
        return self.settings.flag("remind_enabled")

    @property
    def count(self) -> int:
        return max(1, min(MAX_COUNT, self.settings.get_int("remind_count")))

    @property
    def interval(self) -> int:
        """Интервал в секундах."""
        return max(1, self.settings.get_int("remind_interval")) * 60

    def texts(self) -> list[str]:
        try:
            texts = json.loads(self.settings.get("remind_texts"))
        except ValueError:
            texts = []
        return [t for t in texts if isinstance(t, str) and t.strip()] or json.loads(DEFAULTS["remind_texts"])

    async def set_texts(self, texts: list[str]) -> None:
        await self.settings.set("remind_texts", json.dumps(texts[:MAX_VARIANTS], ensure_ascii=False))

    # ---------- сообщение ----------
    def render(self, step: int, name: str) -> tuple[str, InlineKeyboardMarkup]:
        """step — номер напоминания с 1; варианты текста идут по очереди и повторяются по кругу."""
        texts = self.texts()
        text = render_template(texts[(step - 1) % len(texts)], name=esc(name),
                               gift=self.settings.get("gift_emoji"))
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=self.settings.get("remind_button") or DEFAULTS["remind_button"],
            style="success", callback_data=U(a="gift_cta").pack(),
        )]])
        return text, kb

    async def send(self, chat_id: int, step: int, name: str) -> None:
        text, kb = self.render(step, name)
        photo = self.settings.get("remind_photo")
        if photo:
            await self.bot.send_photo(chat_id, photo, caption=text, reply_markup=kb)
        else:
            await self.bot.send_message(chat_id, text, reply_markup=kb)

    # ---------- расписание ----------
    async def on_start(self, user: Row) -> None:
        """Пользователь нажал /start и не прошёл подписку — запускаем цепочку (один раз на пользователя)."""
        if self.enabled and user["verified_at"] is None:
            await self.db.schedule_first_reminder(user["user_id"], now() + self.interval)

    async def tick(self) -> int:
        """Отправляет все созревшие напоминания. Возвращает число отправленных."""
        if not self.enabled or self.settings.flag("maintenance"):
            return 0
        sent = 0
        for user in await self.db.due_reminders(now(), BATCH):
            step = user["remind_step"] + 1
            if step > self.count:
                await self.db.cancel_reminder(user["user_id"])
                continue
            try:
                await self.send(user["user_id"], step, user["full_name"])
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                break  # остальных отправим на следующем тике
            except TelegramForbiddenError:
                await self.db.set_blocked(user["user_id"])
                await self.db.cancel_reminder(user["user_id"])
                continue
            except TelegramAPIError as e:
                log.debug("Напоминание %s не отправлено: %s", user["user_id"], e)
                await self.db.cancel_reminder(user["user_id"])
                continue
            next_at = now() + self.interval if step < self.count else None
            await self.db.reminder_sent(user["user_id"], step, next_at)
            sent += 1
            await asyncio.sleep(1 / 25)
        return sent

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Ошибка в цикле напоминаний")
            await asyncio.sleep(TICK_SECONDS)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
