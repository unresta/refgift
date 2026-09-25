import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    super_admins: frozenset[int]
    db_path: str
    tz: ZoneInfo


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN не задан. Скопируйте .env.example в .env и заполните.")

    raw_admins = os.getenv("ADMIN_IDS", "").replace(" ", "")
    admins = frozenset(int(x) for x in raw_admins.split(",") if x.lstrip("-").isdigit())
    if not admins:
        raise RuntimeError("ADMIN_IDS не задан — укажите хотя бы одного администратора.")

    return Config(
        bot_token=token,
        super_admins=admins,
        db_path=os.getenv("DB_PATH", "data/bot.db"),
        tz=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")),
    )
