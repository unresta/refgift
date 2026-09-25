import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat, InlineKeyboardMarkup

from bot.database import Database

log = logging.getLogger(__name__)

USER_COMMANDS = [BotCommand(command="start", description="🧸 Главное меню")]
ADMIN_COMMANDS = USER_COMMANDS + [BotCommand(command="admin", description="🛠 Админ-панель")]


class AdminRegistry:
    def __init__(self, bot: Bot, db: Database, super_admins: frozenset[int]) -> None:
        self.bot = bot
        self.db = db
        self.super_admins = super_admins
        self.extra: set[int] = set()

    async def load(self) -> None:
        self.extra = set(await self.db.admin_ids())

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.super_admins or user_id in self.extra

    def is_super(self, user_id: int) -> bool:
        return user_id in self.super_admins

    @property
    def all_ids(self) -> list[int]:
        return [*self.super_admins, *(a for a in self.extra if a not in self.super_admins)]

    async def add(self, user_id: int, added_by: int) -> None:
        await self.db.add_admin(user_id, added_by)
        self.extra.add(user_id)
        await self.set_commands(user_id)

    async def remove(self, user_id: int) -> None:
        await self.db.remove_admin(user_id)
        self.extra.discard(user_id)
        try:
            await self.bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=user_id))
        except TelegramAPIError:
            pass

    async def set_commands(self, user_id: int) -> None:
        try:
            await self.bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=user_id))
        except TelegramAPIError:
            pass  # админ ещё не запускал бота

    async def setup_commands(self) -> None:
        await self.bot.set_my_commands(USER_COMMANDS)
        await asyncio.gather(*(self.set_commands(uid) for uid in self.all_ids))

    async def notify(self, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
        for uid in self.all_ids:
            try:
                await self.bot.send_message(uid, text, reply_markup=kb)
            except TelegramAPIError as e:
                log.debug("Не удалось уведомить админа %s: %s", uid, e)
