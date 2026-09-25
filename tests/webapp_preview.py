"""Локальный предпросмотр мини-аппа без Telegram: фейковый API, демо-данные, подписанный initData.

Запуск:  .venv/bin/python -m tests.webapp_preview   → откройте напечатанную ссылку в браузере.
"""
import asyncio
import json
import os
import tempfile
import time
from urllib.parse import quote
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetAvailableGifts, GetFile
from aiogram.types import Gifts
from aiohttp import web

from bot.__main__ import build
from bot.config import Config
from bot.web.server import create_app
from tests.smoke_test import BOT_ID, FakeSession, init_data

PORT = 8090
USER_ID = 42
GIFTS = [("🏆", 100), ("🌹", 25), ("🎂", 50), ("💝", 15), ("🧸", 15), ("🎁", 25), ("💐", 50), ("💍", 100), ("💎", 100)]
THEME = {"bg_color": "#17212b", "secondary_bg_color": "#232e3c", "text_color": "#f5f5f5", "hint_color": "#708499",
         "link_color": "#6ab3f3", "button_color": "#5288c1", "button_text_color": "#ffffff",
         "accent_text_color": "#6ab2f2", "section_bg_color": "#17212b"}


class PreviewSession(FakeSession):
    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetAvailableGifts):
            sticker = {"file_id": "x", "file_unique_id": "x", "type": "regular", "width": 512, "height": 512,
                       "is_animated": True, "is_video": False}
            return Gifts(gifts=[{"id": f"g{i}", "star_count": price, "sticker": {**sticker, "emoji": emoji}}
                                for i, (emoji, price) in enumerate(GIFTS)])
        if isinstance(method, GetFile):  # без реального Telegram показываем эмодзи вместо анимаций
            raise TelegramBadRequest(method=method, message="preview")
        return await super().make_request(bot, method, timeout)


async def main() -> None:
    tmp = tempfile.mkdtemp()
    config = Config("0:fake", frozenset({1}), os.path.join(tmp, "p.db"), ZoneInfo("Europe/Moscow"))
    bot = Bot(f"{BOT_ID}:fake", session=PreviewSession(), default=DefaultBotProperties(parse_mode="HTML"))
    dp, db, _ = await build(config, bot)
    await dp["roulette"].seed_defaults()

    # немного истории для вкладок «Топ» и «Профиль»
    case = (await db.roulette_cases())[0]
    for uid, name in ((USER_ID, "Павел Дуров"), (7, "Анна Смирнова"), (8, "Макс"), (9, "Olga K")):
        await db.upsert_user(uid, None, name)
        for prize in (await db.roulette_prizes(case["id"]))[: 3 if uid == USER_ID else 2]:
            spin_id = await db.create_spin(uid, case)
            await db.mark_spin_paid(spin_id, f"c{spin_id}", prize)
            await db.set_spin_status(spin_id, "sent" if spin_id % 3 else "pending")

    runner = web.AppRunner(create_app(dp["web"]))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    url = (f"http://127.0.0.1:{PORT}/#tgWebAppData={quote(init_data(USER_ID, bot.token, int(time.time())))}"
           f"&tgWebAppVersion=8.0&tgWebAppPlatform=ios&tgWebAppThemeParams={quote(json.dumps(THEME))}")
    print(url, flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
