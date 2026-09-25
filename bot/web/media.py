"""Анимации и превью подарков для мини-аппа: скачиваются из Telegram один раз и кэшируются на диске."""
import asyncio
import gzip
import logging
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Gift

from bot.services.gifts import GiftCatalog

log = logging.getLogger(__name__)

CONTENT_TYPES = {"json": "application/json", "webm": "video/webm", "webp": "image/webp"}


def media_kind(gift: Gift) -> str:
    """json — Lottie (из .tgs), webm — видео-стикер, webp — статичный."""
    if gift.sticker.is_animated:
        return "json"
    if gift.sticker.is_video:
        return "webm"
    return "webp"


class GiftMedia:
    def __init__(self, bot: Bot, catalog: GiftCatalog, cache_dir: Path) -> None:
        self.bot = bot
        self.catalog = catalog
        self.cache_dir = cache_dir
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, gift_id: str, thumb: bool = False) -> tuple[bytes, str] | None:
        gift = await self.catalog.any(gift_id)
        if gift is None:
            return None
        if thumb:
            if gift.sticker.thumbnail is None:
                return None
            kind, file_id = "webp", gift.sticker.thumbnail.file_id
        else:
            kind, file_id = media_kind(gift), gift.sticker.file_id

        safe_id = "".join(c for c in gift_id if c.isalnum())
        path = self.cache_dir / f"{safe_id}{'_thumb' if thumb else ''}.{kind}"
        lock = self._locks.setdefault(str(path), asyncio.Lock())
        async with lock:
            if not path.exists():
                try:
                    data = (await self.bot.download(file_id)).read()
                except TelegramAPIError as e:
                    log.warning("Не удалось скачать стикер подарка %s: %s", gift_id, e)
                    return None
                if kind == "json" and not thumb:
                    data = gzip.decompress(data)  # .tgs — это Lottie JSON в gzip
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            return path.read_bytes(), CONTENT_TYPES[kind]
