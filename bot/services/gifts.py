"""Каталог подарков Telegram и картинки чеков со значком конкретного подарка.

Inline-результаты с фото Telegram показывает сеткой без подписей, поэтому для каждого подарка
генерируем свой вариант картинки чека — чтобы админ видел, какой подарок выбирает.
"""
import asyncio
import io
import logging
import math
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, Gift
from PIL import Image, ImageDraw, ImageFont

from bot.database import Database
from bot.services.admins import AdminRegistry
from bot.settings import Settings

log = logging.getLogger(__name__)

CATALOG_TTL = 600
MAX_SIDE = 1280


def gift_emoji(gift: Gift) -> str:
    return gift.sticker.emoji or "🎁"


class GiftCatalog:
    """Доступные боту подарки (с кэшем). Premium-only и распроданные исключены."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._gifts: list[Gift] = []
        self._loaded_at = 0.0

    async def gifts(self) -> list[Gift]:
        if time.monotonic() - self._loaded_at > CATALOG_TTL or not self._gifts:
            try:
                raw = (await self.bot.get_available_gifts()).gifts
            except TelegramAPIError as e:
                log.warning("get_available_gifts failed: %s", e)
                return self._gifts
            self._gifts = [g for g in raw if not g.is_premium and g.remaining_count != 0]
            self._loaded_at = time.monotonic()
        return self._gifts

    async def get(self, gift_id: str) -> Gift | None:
        return next((g for g in await self.gifts() if g.id == gift_id), None)


def _star(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, fill) -> None:
    points = []
    for i in range(10):
        radius = r if i % 2 == 0 else r * 0.45
        angle = math.pi / 2 + i * math.pi / 5
        points.append((cx + radius * math.cos(angle), cy - radius * math.sin(angle)))
    draw.polygon(points, fill=fill)


def compose(base: bytes, thumbnail: bytes, price: int) -> bytes:
    """Картинка чека + плашка в правом нижнем углу: превью подарка и цена в звёздах."""
    image = Image.open(io.BytesIO(base)).convert("RGBA")
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    width, height = image.size
    side = min(width, height)
    badge_h = int(side * 0.3)
    pad = max(4, int(side * 0.035))

    thumb = Image.open(io.BytesIO(thumbnail)).convert("RGBA")
    thumb.thumbnail((int(badge_h * 0.8), int(badge_h * 0.8)))

    font = ImageFont.load_default(size=max(12, int(badge_h * 0.36)))
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    text = str(price)
    text_w = draw.textlength(text, font=font)
    star_r = badge_h * 0.16
    inner = pad // 2
    badge_w = int(inner + thumb.width + inner + text_w + inner // 2 + star_r * 2 + inner * 2)

    x0, y0 = width - badge_w - pad, height - badge_h - pad
    draw.rounded_rectangle((x0, y0, x0 + badge_w, y0 + badge_h), radius=badge_h // 3, fill=(255, 255, 255, 235))
    overlay.alpha_composite(thumb, (x0 + inner, y0 + (badge_h - thumb.height) // 2))
    cy = y0 + badge_h / 2
    text_x = x0 + inner + thumb.width + inner
    draw.text((text_x, cy), text, font=font, fill=(25, 25, 25, 255), anchor="lm")
    _star(draw, text_x + text_w + inner // 2 + star_r, cy, star_r, (255, 184, 0, 255))

    out = io.BytesIO()
    Image.alpha_composite(image, overlay).convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()


class GiftImages:
    """file_id картинок чека для каждого подарка: генерирует, заливает в Telegram, кэширует в БД."""

    def __init__(self, bot: Bot, db: Database, settings: Settings, catalog: GiftCatalog,
                 admins: AdminRegistry) -> None:
        self.bot = bot
        self.db = db
        self.settings = settings
        self.catalog = catalog
        self.admins = admins
        self._running: set[tuple[str, str]] = set()
        self._tasks: set[asyncio.Task] = set()

    async def file_id(self, gift: Gift) -> str | None:
        """Готовая картинка для подарка; если её ещё нет — ставит генерацию в фон и отдаёт базовую."""
        base = self.settings.get("check_photo")
        if not base:
            return None
        cached = await self.db.get_gift_image(base, gift.id)
        if cached:
            return cached
        self.schedule([gift])
        return base

    def schedule(self, gifts: list[Gift] | None = None) -> None:
        task = asyncio.create_task(self._generate_all(gifts))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _generate_all(self, gifts: list[Gift] | None) -> None:
        base = self.settings.get("check_photo")
        if not base:
            return
        for gift in gifts if gifts is not None else await self.catalog.gifts():
            key = (base, gift.id)
            if key in self._running or await self.db.get_gift_image(base, gift.id):
                continue
            self._running.add(key)
            try:
                await self._generate(base, gift)
            except Exception:
                log.exception("Не удалось сгенерировать картинку чека для подарка %s", gift.id)
            finally:
                self._running.discard(key)

    async def _generate(self, base: str, gift: Gift) -> None:
        thumb = gift.sticker.thumbnail
        if thumb is None:
            return
        base_bytes = (await self.bot.download(base)).read()
        thumb_bytes = (await self.bot.download(thumb.file_id)).read()
        image = await asyncio.to_thread(compose, base_bytes, thumb_bytes, gift.star_count)

        # Чтобы получить file_id, картинку нужно куда-то отправить: шлём главному админу без звука и удаляем.
        chat_id = next(iter(self.admins.super_admins))
        sent = await self.bot.send_photo(chat_id, BufferedInputFile(image, f"check_{gift.id}.jpg"),
                                         disable_notification=True)
        await self.db.save_gift_image(base, gift.id, sent.photo[-1].file_id)
        try:
            await self.bot.delete_message(chat_id, sent.message_id)
        except TelegramAPIError:
            pass
