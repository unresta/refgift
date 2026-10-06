"""Юзербот для НФТ подарков: на каждое сообщение пользователя (в том числе платное) отвечает текстом из админки.

Сессия — StringSession в файле рядом с базой; создаётся один раз командой `python -m bot.userbot_login`.
"""
import asyncio
import logging
import time
from enum import StrEnum
from pathlib import Path

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl import functions, types

from bot.config import Config
from bot.database import Database
from bot.settings import Settings
from bot.utils import esc, render_template

log = logging.getLogger(__name__)
logging.getLogger("telethon").setLevel(logging.WARNING)

DEVICE = "RefGift userbot"
SERVICE_ID = 777000   # служебные уведомления Telegram
COOLDOWN = 3.0        # сек: на альбом из нескольких фото — один ответ


def session_path(db_path: str) -> Path:
    return Path(db_path).parent / "userbot.session"


class UbStatus(StrEnum):
    NO_API = "no_api"          # не заданы USERBOT_API_ID / USERBOT_API_HASH
    NO_SESSION = "no_session"  # не выполнен вход
    CONNECTING = "connecting"
    ONLINE = "online"
    ERROR = "error"


class Userbot:
    def __init__(self, config: Config, db: Database, settings: Settings) -> None:
        self.api_id = config.userbot_api_id
        self.api_hash = config.userbot_api_hash
        self.path = session_path(config.db_path)
        self.db = db
        self.settings = settings
        self.client: TelegramClient | None = None
        self.status = UbStatus.NO_API
        self.error = ""
        self.me: types.User | None = None
        self.paid_stars: int | None = None  # цена сообщения от не-контактов: 0 — бесплатно, None — неизвестно
        self._last: dict[int, float] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    @property
    def online(self) -> bool:
        return self.status is UbStatus.ONLINE and self.client is not None and self.client.is_connected()

    @property
    def username(self) -> str | None:
        return self.me.username if self.me else None

    def launch(self) -> None:
        """Подключение в фоне — запуск бота не ждёт Telegram."""
        self._task = asyncio.create_task(self.start())

    async def start(self) -> None:
        async with self._lock:
            await self._disconnect()
            self.error = ""
            if not (self.api_id and self.api_hash):
                self.status = UbStatus.NO_API
                return
            session = self.path.read_text().strip() if self.path.exists() else ""
            if not session:
                self.status = UbStatus.NO_SESSION
                return
            self.status = UbStatus.CONNECTING
            client = TelegramClient(StringSession(session), self.api_id, self.api_hash, device_model=DEVICE)
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise RuntimeError("сессия недействительна — выполните вход заново")
                self.me = await client.get_me()
                client.add_event_handler(self.on_message, events.NewMessage(incoming=True,
                                                                            func=lambda e: e.is_private))
                self.client = client
                self.status = UbStatus.ONLINE
            except Exception as e:  # сеть, отозванная сессия, неверные api_id/api_hash
                log.warning("Юзербот не подключился: %s", e)
                self.status, self.error = UbStatus.ERROR, str(e) or type(e).__name__
                await client.disconnect()
                return
            log.info("Юзербот подключён: %s (@%s)", self.me.first_name, self.me.username)
        await self.refresh_paid()

    async def refresh_paid(self) -> None:
        if not self.online:
            return
        try:
            privacy = await self.client(functions.account.GetGlobalPrivacySettingsRequest())
            self.paid_stars = privacy.noncontact_peers_paid_stars or 0
        except Exception as e:
            log.debug("Не удалось получить цену сообщений: %s", e)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        async with self._lock:
            await self._disconnect()

    async def _disconnect(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

    async def on_message(self, event) -> None:
        if not self.settings.flag("userbot_enabled"):
            return
        sender = await event.get_sender()
        if (not isinstance(sender, types.User) or sender.bot or sender.is_self or sender.contact
                or sender.support or sender.id == SERVICE_ID):
            return
        stars = int(getattr(event.message, "paid_message_stars", None) or 0)
        if self.settings.flag("userbot_paid_only") and not stars:
            return
        await self.db.log_userbot_message(sender.id, stars)

        t = time.monotonic()
        if t - self._last.get(sender.id, 0.0) < COOLDOWN:
            return
        if len(self._last) > 5000:
            self._last = {uid: ts for uid, ts in self._last.items() if t - ts < COOLDOWN}
        self._last[sender.id] = t

        text = render_template(self.settings.get("userbot_reply"), name=esc(sender.first_name or ""))
        try:
            await event.respond(text, parse_mode="html", link_preview=False)
        except Exception as e:
            log.warning("Юзербот не ответил %s: %s", sender.id, e)
