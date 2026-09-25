import asyncio
import logging
import time

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiosqlite import Row

from bot.database import Database
from bot.settings import Settings

log = logging.getLogger(__name__)

MEMBER_STATUSES = {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER}


class SubscriptionService:
    """Проверка обязательной подписки с кэшем положительных результатов."""

    def __init__(self, bot: Bot, db: Database, settings: Settings) -> None:
        self.bot = bot
        self.db = db
        self.settings = settings
        self._ok_until: dict[int, float] = {}
        self.broken: dict[int, str] = {}  # chat_id -> ошибка проверки (бот не админ и т.п.)

    def reset_cache(self, user_id: int | None = None) -> None:
        if user_id is None:
            self._ok_until.clear()
        else:
            self._ok_until.pop(user_id, None)

    async def missing(self, user_id: int, use_cache: bool = True) -> list[Row]:
        """Каналы, на которые пользователь ещё не подписан."""
        if use_cache and self._ok_until.get(user_id, 0) > time.monotonic():
            return []
        channels = await self.db.channels(only_active=True)
        if not channels:
            return []
        results = await asyncio.gather(*(self._is_member(ch["chat_id"], user_id) for ch in channels))
        missing = [ch for ch, ok in zip(channels, results, strict=True) if not ok]
        if missing:
            self._ok_until.pop(user_id, None)
        else:
            self._ok_until[user_id] = time.monotonic() + self.settings.get_int("sub_cache_ttl")
        return missing

    async def _is_member(self, chat_id: int, user_id: int) -> bool:
        try:
            member = await self.bot.get_chat_member(chat_id, user_id)
        except TelegramAPIError as e:
            # Ошибка конфигурации канала не должна блокировать пользователей — пропускаем канал.
            if chat_id not in self.broken:
                log.warning("Не удалось проверить подписку на %s: %s", chat_id, e)
            self.broken[chat_id] = str(e)
            return True
        self.broken.pop(chat_id, None)
        if member.status in MEMBER_STATUSES:
            return True
        if member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False):
            return True
        # Для приватных каналов с заявками — поданная заявка считается подпиской.
        return await self.db.has_join_request(chat_id, user_id)
