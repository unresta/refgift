"""Вход в аккаунт юзербота (один раз).

Локально:   python -m bot.userbot_login
В Docker:   docker compose run --rm bot python -m bot.userbot_login

Спросит телефон, код из Telegram и пароль 2FA, сохранит сессию рядом с базой.
После этого в админке: 💎 НФТ подарки → 🤖 Юзербот → 🔄 Переподключить.
"""
import asyncio
import os
import sys
from getpass import getpass

from telethon import TelegramClient
from telethon.sessions import StringSession

import bot.config  # noqa: F401 — подгружает .env
from bot.services.userbot import DEVICE, session_path


async def main() -> int:
    api_id = int(os.getenv("USERBOT_API_ID", "").strip() or 0)
    api_hash = os.getenv("USERBOT_API_HASH", "").strip()
    if not (api_id and api_hash):
        print("❌ Заполните USERBOT_API_ID и USERBOT_API_HASH в .env\n"
              "   Взять: https://my.telegram.org → API development tools")
        return 1

    path = session_path(os.getenv("DB_PATH", "data/bot.db"))
    client = TelegramClient(StringSession(), api_id, api_hash, device_model=DEVICE)
    await client.start(
        phone=lambda: input("📱 Телефон аккаунта (например +79991234567): "),
        code_callback=lambda: input("🔑 Код из Telegram: "),
        password=lambda: getpass("🔒 Пароль 2FA: "),
    )
    me = await client.get_me()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(client.session.save())
    path.chmod(0o600)
    await client.disconnect()
    print(f"\n✅ Вход выполнен: {me.first_name} (@{me.username or '—'})\n"
          f"   Сессия сохранена: {path} — это доступ к аккаунту, никому её не передавайте.\n"
          "   В админке: 💎 НФТ подарки → 🤖 Юзербот → 🔄 Переподключить")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
