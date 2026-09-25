import asyncio
import logging
import time
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup

from bot.database import Database

log = logging.getLogger(__name__)

RATE_DELAY = 1 / 25  # ~25 сообщений в секунду — ниже лимита Telegram


@dataclass(slots=True)
class BroadcastStats:
    total: int
    sent: int = 0
    blocked: int = 0
    failed: int = 0
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    cancelled: bool = False

    @property
    def done(self) -> int:
        return self.sent + self.blocked + self.failed

    @property
    def elapsed(self) -> float:
        return (self.finished or time.monotonic()) - self.started

    @property
    def eta(self) -> float:
        if not self.done:
            return 0
        return self.elapsed / self.done * (self.total - self.done)


class Broadcaster:
    """Одна рассылка за раз, выполняется в фоне."""

    def __init__(self, bot: Bot, db: Database) -> None:
        self.bot = bot
        self.db = db
        self.stats: BroadcastStats | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def stop(self) -> None:
        self._stop.set()

    def is_done(self) -> bool:
        return self._task is None or self._task.done()

    async def wait(self, timeout: float) -> None:
        if self._task is not None and not self._task.done():
            await asyncio.wait({self._task}, timeout=timeout)

    def start(self, user_ids: list[int], from_chat_id: int, message_id: int,
              markup: InlineKeyboardMarkup | None) -> BroadcastStats:
        self._stop.clear()
        self.stats = BroadcastStats(total=len(user_ids))
        self._task = asyncio.create_task(self._run(user_ids, from_chat_id, message_id, markup))
        return self.stats

    async def _run(self, user_ids: list[int], from_chat_id: int, message_id: int,
                   markup: InlineKeyboardMarkup | None) -> None:
        stats = self.stats
        assert stats is not None
        blocked: list[int] = []
        try:
            for uid in user_ids:
                if self._stop.is_set():
                    stats.cancelled = True
                    break
                result = await self._send(uid, from_chat_id, message_id, markup)
                if result == "ok":
                    stats.sent += 1
                elif result == "blocked":
                    stats.blocked += 1
                    blocked.append(uid)
                else:
                    stats.failed += 1
                await asyncio.sleep(RATE_DELAY)
        except Exception:
            log.exception("Рассылка упала")
        finally:
            stats.finished = time.monotonic()
            if blocked:
                await self.db.mark_blocked_many(blocked)

    async def _send(self, uid: int, from_chat_id: int, message_id: int, markup, attempt: int = 0) -> str:
        try:
            await self.bot.copy_message(uid, from_chat_id, message_id, reply_markup=markup)
            return "ok"
        except TelegramRetryAfter as e:
            if attempt >= 3:
                return "failed"
            await asyncio.sleep(e.retry_after + 1)
            return await self._send(uid, from_chat_id, message_id, markup, attempt + 1)
        except TelegramForbiddenError:
            return "blocked"
        except TelegramAPIError as e:
            if "chat not found" in str(e).lower() or "deactivated" in str(e).lower():
                return "blocked"
            return "failed"
