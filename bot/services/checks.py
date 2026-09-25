import secrets
import string
import time
from dataclasses import dataclass, field
from enum import StrEnum

from aiogram import Bot
from aiogram.types import Gift, InlineKeyboardButton, InlineKeyboardMarkup
from aiosqlite import Row

from bot.database import Database
from bot.services.gifts import gift_emoji
from bot.services.rewards import ClaimResult, RewardService
from bot.services.subscription import SubscriptionService
from bot.settings import Settings
from bot.utils import esc, render_template


CHECK_PREFIX = "c_"
MAX_ACTIVATIONS = 10_000


class CheckStatus(StrEnum):
    SENT = "sent"            # подарок отправлен
    PENDING = "pending"      # подарок в очереди на выдачу
    NEED_SUB = "need_sub"    # сначала нужна подписка
    NOT_FOUND = "not_found"
    INACTIVE = "inactive"
    EXHAUSTED = "exhausted"
    ALREADY = "already"


@dataclass(slots=True)
class Activation:
    status: CheckStatus
    check: Row | None = None
    missing: list[Row] = field(default_factory=list)


def _get(row: Row | dict, key: str):
    return row[key] if key in row.keys() else None


def random_code() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(10))


class CheckService:
    def __init__(self, bot: Bot, db: Database, settings: Settings, subs: SubscriptionService,
                 rewards: RewardService, bot_username: str) -> None:
        self.bot = bot
        self.db = db
        self.settings = settings
        self.subs = subs
        self.rewards = rewards
        self.bot_username = bot_username
        self._last_cleanup = 0.0

    # ---------- оформление ----------
    def url(self, code: str) -> str:
        return f"https://t.me/{self.bot_username}?start={CHECK_PREFIX}{code}"

    def emoji(self, check: Row | dict) -> str:
        return _get(check, "gift_emoji") or self.settings.get("gift_emoji")

    def price(self, check: Row | dict) -> int:
        return _get(check, "gift_price") or self.settings.get_int("gift_price")

    def caption(self, check: Row | dict) -> str:
        template = esc(check["caption"]) if check["caption"] else self.settings.get("check_caption")
        return render_template(template, gift=self.emoji(check), count=check["total"])

    def keyboard(self, check: Row | dict) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"🎁 Забрать {self.emoji(check)}", url=self.url(check["code"]))
        ]])

    # ---------- создание ----------
    async def get_or_create_draft(self, admin_id: int, total: int, caption: str | None, gift: Gift) -> Row:
        with_photo = bool(self.settings.get("check_photo") or await self.db.get_gift_banner(gift.id))
        draft = await self.db.find_draft_check(admin_id, total, caption, with_photo, gift.id)
        if draft:
            return await self.db.get_check(draft["id"])
        if time.monotonic() - self._last_cleanup > 3600:
            self._last_cleanup = time.monotonic()
            await self.db.cleanup_check_drafts()
        code = random_code()
        while await self.db.get_check_by_code(code):
            code = random_code()
        check_id = await self.db.create_check(code, total, caption, with_photo, admin_id,
                                              gift.id, gift_emoji(gift), gift.star_count)
        check = await self.db.get_check(check_id)
        assert check is not None
        return check

    # ---------- активация ----------
    async def activate(self, user_id: int, code: str) -> Activation:
        check = await self.db.get_check_by_code(code)
        if not check:
            await self.db.set_pending_check(user_id, None)
            return Activation(CheckStatus.NOT_FOUND)
        if await self.db.has_activated(check["id"], user_id):
            await self.db.set_pending_check(user_id, None)
            return Activation(CheckStatus.ALREADY, check)
        if not check["is_active"]:
            await self.db.set_pending_check(user_id, None)
            return Activation(CheckStatus.INACTIVE, check)
        if check["used"] >= check["total"]:
            await self.db.set_pending_check(user_id, None)
            return Activation(CheckStatus.EXHAUSTED, check)

        # Подписку проверяем всегда заново, без кэша — прямо перед активацией.
        missing = await self.subs.missing(user_id, use_cache=False)
        if missing:
            await self.db.set_pending_check(user_id, code)
            return Activation(CheckStatus.NEED_SUB, check, missing)
        await self.rewards.complete_verification(user_id)

        activated = await self.db.try_activate_check(check["id"], user_id)
        await self.db.set_pending_check(user_id, None)
        if not activated:
            if await self.db.has_activated(check["id"], user_id):
                return Activation(CheckStatus.ALREADY, check)
            fresh = await self.db.get_check(check["id"])
            status = CheckStatus.INACTIVE if fresh and not fresh["is_active"] else CheckStatus.EXHAUSTED
            return Activation(status, check)

        user = await self.db.get_user(user_id)
        assert user is not None
        result = await self.rewards.grant(user, check_id=check["id"], origin=f"🎟 чек <code>{check['code']}</code>",
                                          gift_id=check["gift_id"])

        return Activation(CheckStatus.SENT if result is ClaimResult.SENT else CheckStatus.PENDING, check)
