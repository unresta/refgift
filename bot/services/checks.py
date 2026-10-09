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
PASSWORD_MAX = 64
PASSWORD_ATTEMPTS = 5      # неверных попыток подряд…
PASSWORD_LOCK = 600        # …и пауза в секундах — защита от перебора


class CheckStatus(StrEnum):
    SENT = "sent"            # подарок отправлен
    PENDING = "pending"      # подарок в очереди на выдачу
    NEED_SUB = "need_sub"    # сначала нужна подписка
    NOT_FOUND = "not_found"
    INACTIVE = "inactive"
    EXHAUSTED = "exhausted"
    ALREADY = "already"
    NEED_PASSWORD = "need_password"
    WRONG_PASSWORD = "wrong_password"
    LOCKED = "locked"        # слишком много неверных паролей


@dataclass(slots=True)
class Activation:
    status: CheckStatus
    check: Row | None = None
    missing: list[Row] = field(default_factory=list)
    available: bool = False  # чек ещё можно получить (для текста экрана подписки)
    attempts_left: int = 0   # для неверного пароля


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
        self._password_fails: dict[tuple[int, int], tuple[int, float]] = {}  # (user, чек) → (ошибок, когда)

    # ---------- оформление ----------
    def url(self, code: str) -> str:
        return f"https://t.me/{self.bot_username}?start={CHECK_PREFIX}{code}"

    def emoji(self, check: Row | dict) -> str:
        return _get(check, "gift_emoji") or self.settings.get("gift_emoji")

    def price(self, check: Row | dict) -> int:
        return _get(check, "gift_price") or self.settings.get_int("gift_price")

    def caption(self, check: Row | dict) -> str:
        if not check["caption"]:
            template = self.settings.get("check_caption")
        elif _get(check, "caption_html"):  # рекламный чек: свой текст с форматированием
            template = check["caption"]
        else:
            template = esc(check["caption"])
        text = render_template(template, gift=self.emoji(check), count=check["total"])
        if _get(check, "password"):
            text += "\n\n🔐 <b>Чек с паролем</b> — после перехода бот попросит его ввести."
        return text

    def keyboard(self, check: Row | dict) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"🎁 Забрать {self.emoji(check)}", url=self.url(check["code"]))
        ]])

    # ---------- создание ----------
    async def get_or_create_draft(self, admin_id: int, total: int, caption: str | None, gift: Gift,
                                  password: str | None = None) -> Row:
        with_photo = bool(self.settings.get("check_photo") or await self.db.get_gift_banner(gift.id))
        draft = await self.db.find_draft_check(admin_id, total, caption, with_photo, gift.id, password)
        if draft:
            return await self.db.get_check(draft["id"])
        if time.monotonic() - self._last_cleanup > 3600:
            self._last_cleanup = time.monotonic()
            await self.db.cleanup_check_drafts()
        return await self._create(admin_id, total, caption, with_photo, gift, password)

    async def create_ad_check(self, admin_id: int, total: int, gift: Gift, ad_link_id: int,
                              caption_html: str | None = None, password: str | None = None) -> Row:
        """Рекламный чек: сразу «отправлен» (виден в списке), переходы по нему идут в статистику ссылки.
        Свой текст — HTML с форматированием."""
        with_photo = bool(self.settings.get("check_photo") or await self.db.get_gift_banner(gift.id))
        return await self._create(admin_id, total, caption_html, with_photo, gift, password, ad_link_id,
                                  caption_html=caption_html is not None)

    async def _create(self, admin_id: int, total: int, caption: str | None, with_photo: bool, gift: Gift,
                      password: str | None, ad_link_id: int | None = None, caption_html: bool = False) -> Row:
        code = random_code()
        while await self.db.get_check_by_code(code):
            code = random_code()
        check_id = await self.db.create_check(code, total, caption, with_photo, admin_id,
                                              gift.id, gift_emoji(gift), gift.star_count, password, ad_link_id,
                                              caption_html)
        check = await self.db.get_check(check_id)
        assert check is not None
        return check

    # ---------- пароль ----------
    def _locked(self, key: tuple[int, int]) -> bool:
        fails, last = self._password_fails.get(key, (0, 0.0))
        if time.monotonic() - last > PASSWORD_LOCK:
            self._password_fails.pop(key, None)
            return False
        return fails >= PASSWORD_ATTEMPTS

    def _fail(self, key: tuple[int, int]) -> int:
        """Отмечает неверный пароль; возвращает, сколько попыток осталось."""
        fails = self._password_fails.get(key, (0, 0.0))[0] + 1
        self._password_fails[key] = (fails, time.monotonic())
        return max(0, PASSWORD_ATTEMPTS - fails)

    async def _check_password(self, user_id: int, check: Row, password: str | None) -> Activation | None:
        """None — пароль верный (или не нужен), иначе — что показать пользователю."""
        if not check["password"]:
            return None
        key = (user_id, check["id"])
        if self._locked(key):
            return Activation(CheckStatus.LOCKED, check)
        if password is None:
            return Activation(CheckStatus.NEED_PASSWORD, check)
        if password.strip().casefold() != check["password"].casefold():
            left = self._fail(key)
            return Activation(CheckStatus.LOCKED if left == 0 else CheckStatus.WRONG_PASSWORD, check,
                              attempts_left=left)
        self._password_fails.pop(key, None)
        return None

    # ---------- активация ----------
    async def activate(self, user_id: int, code: str, password: str | None = None) -> Activation:
        """password=None — пароль ещё не вводили: для чека с паролем вернётся NEED_PASSWORD."""
        check = await self.db.get_check_by_code(code)

        # Подписка проверяется первой и всегда заново, без кэша — даже если чек закончился или не найден:
        # по ссылке чека человек в любом случае сначала подписывается на каналы.
        missing = await self.subs.missing(user_id, use_cache=False)
        if missing:
            await self.db.set_pending_check(user_id, code)
            available = bool(check and check["is_active"] and check["used"] < check["total"]
                             and not await self.db.has_activated(check["id"], user_id))
            return Activation(CheckStatus.NEED_SUB, check, missing, available)
        await self.rewards.complete_verification(user_id)

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
        if denied := await self._check_password(user_id, check, password):
            await self.db.set_pending_check(user_id, None)  # дальше код чека хранит ввод пароля (FSM)
            return denied

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
