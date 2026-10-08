"""Баннер над экранами меню: каждый экран — фото баннера с текстом в подписи."""
import io
import logging
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardMarkup, InputFile,
                           InputMediaPhoto, Message)

from bot.settings import Settings
from bot.utils import icon_safe, show, strip_tags

log = logging.getLogger(__name__)

DEFAULT_BANNER = Path(__file__).resolve().parent.parent / "assets" / "banner.webp"
CAPTION_LIMIT = 1024


def _as_jpeg(path: Path) -> BufferedInputFile:
    from PIL import Image
    buf = io.BytesIO()
    Image.open(path).convert("RGB").save(buf, "JPEG", quality=90)
    return BufferedInputFile(buf.getvalue(), filename="banner.jpg")


class Banner:
    def __init__(self, bot: Bot, settings: Settings) -> None:
        self.bot = bot
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return self.settings.flag("banner_enabled") and bool(self.settings.get("banner_file_id")
                                                             or DEFAULT_BANNER.exists())

    def _media(self) -> str | InputFile:
        return self.settings.get("banner_file_id") or FSInputFile(DEFAULT_BANNER, filename="banner.webp")

    async def remember(self, msg: Message | bool | None) -> None:
        """Запоминает file_id загруженного баннера, чтобы не загружать файл каждый раз."""
        if isinstance(msg, Message) and msg.photo and not self.settings.get("banner_file_id"):
            await self.set_photo(msg.photo[-1].file_id, msg.photo[-1].file_unique_id)

    async def set_photo(self, file_id: str, unique_id: str, custom: bool = False) -> None:
        await self.settings.set("banner_file_id", file_id)
        await self.settings.set("banner_unique_id", unique_id)
        await self.settings.set("banner_custom", custom)

    async def reset(self) -> None:
        """Возврат к баннеру по умолчанию из bot/assets."""
        for key in ("banner_file_id", "banner_unique_id", "banner_custom"):
            await self.settings.reset(key)

    def _is_banner(self, msg: Message) -> bool:
        unique = self.settings.get("banner_unique_id")
        return bool(msg.photo and unique and msg.photo[-1].file_unique_id == unique)

    async def _send(self, chat_id: int, text: str, kb: InlineKeyboardMarkup | None) -> Message | None:
        async def send(media: str | InputFile) -> Message:
            return await icon_safe(lambda m: self.bot.send_photo(chat_id, media, caption=text, reply_markup=m), kb)

        media = self._media()
        try:
            sent = await send(media)
        except TelegramBadRequest as e:
            if isinstance(media, str):  # сохранённый file_id испорчен — загрузим файл заново
                log.warning("Баннер %s не отправился (%s) — загружаю заново", media, e)
                await self.reset()
                return await self._send(chat_id, text, kb) if DEFAULT_BANNER.exists() else None
            log.warning("WebP-баннер не принят (%s) — отправляю JPEG", e)
            try:
                sent = await send(_as_jpeg(DEFAULT_BANNER))
            except TelegramBadRequest:
                log.exception("Не удалось отправить баннер")
                return None
        await self.remember(sent)
        return sent

    async def show(self, event: Message | CallbackQuery, text: str,
                   kb: InlineKeyboardMarkup | None = None) -> Message | None:
        """Как utils.show(), но с баннером: подпись редактируется, сообщение без баннера заменяется."""
        if not self.enabled or len(strip_tags(text)) > CAPTION_LIMIT:
            return await show(event, text, kb)
        if isinstance(event, Message):
            return await self._send(event.chat.id, text, kb) or await show(event, text, kb)

        msg = event.message
        if isinstance(msg, Message) and msg.photo:
            try:
                if self._is_banner(msg):
                    await icon_safe(lambda m: msg.edit_caption(caption=text, reply_markup=m), kb)
                    return msg
                if media := self.settings.get("banner_file_id"):
                    edited = await icon_safe(lambda m: msg.edit_media(
                        InputMediaPhoto(media=media, caption=text), reply_markup=m), kb)
                    return edited if isinstance(edited, Message) else msg
            except TelegramBadRequest as e:
                if "message is not modified" in str(e):
                    return msg
        sent = await self._send(event.from_user.id, text, kb)
        if sent is None:
            return await show(event, text, kb)
        if isinstance(msg, Message):
            try:
                await msg.delete()
            except TelegramBadRequest:
                pass
        return sent
