"""Задания «Заработать звёзды»: подписка, буст канала или переход по ссылке → звёзды на баланс в боте."""
import logging
import time
from enum import StrEnum

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiosqlite import Row

from bot.database import Database
from bot.services.subscription import MEMBER_STATUSES

log = logging.getLogger(__name__)

KINDS = {"sub": "📢 Подписка", "boost": "🚀 Буст", "link": "🔗 Ссылка"}
LINK_DELAY = 5  # секунд с открытия задания-ссылки до проверки: перейти по ссылке нельзя мгновенно


class TaskCheck(StrEnum):
    DONE = "done"            # засчитано, награда начислена
    NOT_DONE = "not_done"    # условие не выполнено
    TOO_EARLY = "too_early"  # задание-ссылка: проверка раньше, чем можно было перейти
    ERROR = "error"          # бот не может проверить (не админ в канале и т.п.)
    GONE = "gone"            # задание выключено, лимит исчерпан или уже выполнено


def boost_url(username: str | None, chat_id: int) -> str:
    if username:
        return f"https://t.me/boost/{username}"
    return f"https://t.me/boost?c={str(chat_id).removeprefix('-100')}"


class TaskService:
    def __init__(self, bot: Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        self._opened: dict[tuple[int, int], float] = {}  # (user, task) → когда открыл задание-ссылку

    def opened(self, user_id: int, task_id: int) -> None:
        if len(self._opened) > 50_000:
            self._opened.clear()
        self._opened.setdefault((user_id, task_id), time.monotonic())

    async def check(self, task: Row, user_id: int) -> TaskCheck:
        if not task["is_active"] or (task["max_done"] and task["done"] >= task["max_done"]):
            return TaskCheck.GONE
        if task["kind"] == "link":
            opened = self._opened.get((user_id, task["id"]))
            if opened is None or time.monotonic() - opened < LINK_DELAY:
                self.opened(user_id, task["id"])
                return TaskCheck.TOO_EARLY
        else:
            try:
                ok = await (self._is_member if task["kind"] == "sub" else self._has_boost)(task["chat_id"], user_id)
            except TelegramAPIError as e:
                log.warning("Не удалось проверить задание %s (%s): %s", task["id"], task["chat_id"], e)
                return TaskCheck.ERROR
            if not ok:
                return TaskCheck.NOT_DONE
        if not await self.db.complete_task(task, user_id):
            return TaskCheck.GONE
        self._opened.pop((user_id, task["id"]), None)
        return TaskCheck.DONE

    async def _is_member(self, chat_id: int, user_id: int) -> bool:
        member = await self.bot.get_chat_member(chat_id, user_id)
        if member.status in MEMBER_STATUSES:
            return True
        if member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False):
            return True
        return await self.db.has_join_request(chat_id, user_id)

    async def _has_boost(self, chat_id: int, user_id: int) -> bool:
        return bool((await self.bot.get_user_chat_boosts(chat_id, user_id)).boosts)
