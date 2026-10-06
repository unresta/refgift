"""Юзербот для НФТ подарков: на каждое сообщение пользователя (в том числе платное) отвечает текстом из админки.

Сессия — StringSession в файле рядом с базой; создаётся один раз командой `python -m bot.userbot_login`.
"""
import asyncio
import logging
import time
from enum import StrEnum
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import RPCError
from telethon.extensions import html as tl_html
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
LINK_TITLE_MAX = 32   # название ссылки в списке «Ссылки на чат»


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
        self.link_errors: dict[int, str] = {}  # id НФТ подарка → почему не удалось создать ссылку на чат
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
        await self.sync_nft_links(only_missing=True)  # подарки, добавленные, пока юзербот был отключён

    async def refresh_paid(self) -> None:
        if not self.online:
            return
        try:
            privacy = await self.client(functions.account.GetGlobalPrivacySettingsRequest())
            self.paid_stars = privacy.noncontact_peers_paid_stars or 0
        except Exception as e:
            log.debug("Не удалось получить цену сообщений: %s", e)

    # ---------- ссылки на чат (Telegram Business) для НФТ подарков ----------

    def link_message(self, title: str) -> str:
        """HTML первого сообщения для ссылки подарка."""
        return render_template(self.settings.get("nft_link_message"), gift=esc(title))

    async def sync_nft_link(self, gift_id: int) -> str | None:
        """Создаёт или обновляет ссылку на чат для подарка. Возвращает текст ошибки или None."""
        gift = await self.db.get_nft_gift(gift_id)
        if gift is None:
            return "подарок не найден"
        if not self.online:
            return "юзербот не подключён"
        message, entities = tl_html.parse(self.link_message(gift["title"]))
        link = types.InputBusinessChatLink(message=message, entities=entities or None,
                                           title=f"💎 {gift['title']}"[:LINK_TITLE_MAX])
        try:
            result = None
            if gift["chat_link_slug"]:
                try:
                    result = await self.client(functions.account.EditBusinessChatLinkRequest(
                        slug=gift["chat_link_slug"], link=link))
                except RPCError as e:  # ссылку удалили в Telegram вручную — создадим новую
                    log.info("Ссылка %s не обновилась (%s), создаю новую", gift["chat_link_slug"], e)
            if result is None:
                result = await self.client(functions.account.CreateBusinessChatLinkRequest(link=link))
        except RPCError as e:
            error = "нужен Telegram Premium на аккаунте юзербота" if "PREMIUM" in str(e).upper() else str(e)
            self.link_errors[gift_id] = error
            log.warning("Не удалось создать ссылку на чат для подарка %s: %s", gift_id, e)
            return error
        self.link_errors.pop(gift_id, None)
        await self.db.update_nft_gift(gift_id, chat_link=result.link, chat_link_slug=result.link.rsplit("/", 1)[-1])
        return None

    async def sync_nft_links(self, only_missing: bool = False) -> tuple[int, int]:
        """Ссылки для всех подарков. Возвращает (успешно, с ошибкой)."""
        ok = failed = 0
        for gift in await self.db.nft_gifts():
            if only_missing and gift["chat_link"]:
                continue
            if await self.sync_nft_link(gift["id"]):
                failed += 1
            else:
                ok += 1
        return ok, failed

    async def delete_nft_link(self, slug: str | None) -> None:
        if not slug or not self.online:
            return
        try:
            await self.client(functions.account.DeleteBusinessChatLinkRequest(slug=slug))
        except RPCError as e:
            log.info("Ссылка %s не удалена: %s", slug, e)

    async def chat_link_views(self) -> dict[str, int]:
        """Сколько раз открыли каждую ссылку: slug → переходы."""
        if not self.online:
            return {}
        try:
            links = (await self.client(functions.account.GetBusinessChatLinksRequest())).links
        except RPCError:
            return {}
        return {link.link.rsplit("/", 1)[-1]: link.views for link in links}

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
