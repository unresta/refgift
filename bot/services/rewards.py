import logging
from dataclasses import dataclass
from enum import StrEnum

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiosqlite import Row

from bot.callbacks import A, U
from bot.database import Database
from bot.services.admins import AdminRegistry
from bot.settings import Settings
from bot.utils import esc, progress_bar, render_template, user_link

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Progress:
    total: int       # всего засчитано друзей (с бонусами)
    goal: int
    claimed: int     # сколько наград уже забрал
    available: int   # сколько наград можно забрать прямо сейчас
    current: int     # прогресс к следующей награде
    finished: bool   # награда получена и повторной нет

    @property
    def left(self) -> int:
        return max(0, self.goal - self.current)

    @property
    def bar(self) -> str:
        return progress_bar(self.current, self.goal)

    @property
    def pct(self) -> int:
        return min(100, round(self.current * 100 / self.goal))


def calc_progress(user: Row, settings: Settings) -> Progress:
    goal = settings.goal
    total = max(0, user["ref_count"] + user["bonus_refs"])
    claimed = user["rewards_claimed"]
    if settings.repeatable:
        earned = total // goal
        available = max(0, earned - claimed)
        current = goal if available else max(0, total - claimed * goal)
        finished = False
    else:
        earned = min(1, total // goal)
        available = max(0, earned - claimed)
        current = min(total, goal)
        finished = claimed >= 1
    return Progress(total, goal, claimed, available, min(current, goal), finished)


def allowed_rewards(user: Row, settings: Settings) -> int:
    total = max(0, user["ref_count"] + user["bonus_refs"])
    earned = total // settings.goal
    return earned if settings.repeatable else min(1, earned)


class ClaimResult(StrEnum):
    SENT = "sent"
    PENDING = "pending"
    UNAVAILABLE = "unavailable"


def claim_admin_kb(claim_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Отправить подарок", style="success",
                              callback_data=A(s="cl", a="send", id=claim_id).pack())],
        [InlineKeyboardButton(text="✅ Выдал вручную", callback_data=A(s="cl", a="done", id=claim_id).pack()),
         InlineKeyboardButton(text="❌ Отклонить", style="danger",
                              callback_data=A(s="cl", a="reject", id=claim_id).pack())],
        [InlineKeyboardButton(text="👤 Профиль", callback_data=A(s="us", a="card", id=user_id).pack())],
    ])


class RewardService:
    def __init__(self, bot: Bot, db: Database, settings: Settings, admins: AdminRegistry) -> None:
        self.bot = bot
        self.db = db
        self.settings = settings
        self.admins = admins
        self._busy: set[int] = set()  # заявки в обработке — защита от одновременных кликов админов

    # ---------- рефералы ----------
    async def complete_verification(self, user_id: int) -> None:
        """Пользователь прошёл обязательную подписку: отмечаем и засчитываем пригласившему."""
        await self.db.mark_verified(user_id)
        referrer_id = await self.db.credit_referral(user_id)
        if referrer_id and self.settings.flag("notify_referrer"):
            await self._notify_referrer(referrer_id, user_id)

    async def _notify_referrer(self, referrer_id: int, friend_id: int) -> None:
        referrer = await self.db.get_user(referrer_id)
        friend = await self.db.get_user(friend_id)
        if not referrer or not friend or referrer["is_banned"]:
            return
        p = calc_progress(referrer, self.settings)
        friend_name = esc(friend["full_name"]) or "Новый друг"
        if p.available:
            text = (f"🎉 <b>{friend_name}</b> присоединился по твоей ссылке!\n\n"
                    f"Цель выполнена: <b>{p.goal}/{p.goal}</b>\n{p.bar} 100%\n\n"
                    "Забирай своего мишку 🧸 👇")
            kb = [[InlineKeyboardButton(text="🧸 Забрать мишку", style="success",
                                        callback_data=U(a="claim").pack())]]
        else:
            text = (f"🎉 <b>{friend_name}</b> присоединился по твоей ссылке!\n\n"
                    f"Прогресс: <b>{p.current}/{p.goal}</b>\n{p.bar} {p.pct}%\n"
                    f"Осталось пригласить: <b>{p.left}</b>")
            kb = [[InlineKeyboardButton(text="🔗 Пригласить ещё", callback_data=U(a="invite").pack())]]
        try:
            await self.bot.send_message(referrer_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        except TelegramForbiddenError:
            await self.db.set_blocked(referrer_id)
        except TelegramAPIError as e:
            log.debug("Не удалось уведомить реферера %s: %s", referrer_id, e)

    # ---------- награды ----------
    async def send_gift(self, user_id: int, gift_id: str | None = None) -> str | None:
        """Отправляет подарок Telegram за звёзды бота. Возвращает текст ошибки или None."""
        try:
            await self.bot.send_gift(
                gift_id=gift_id or self.settings.get("gift_id"),
                user_id=user_id,
                text=self.settings.get("gift_text") or None,
            )
        except TelegramAPIError as e:
            log.warning("send_gift(%s) failed: %s", user_id, e)
            return e.message
        return None

    async def claim(self, user: Row) -> ClaimResult:
        user_id = user["user_id"]
        if not await self.db.try_consume_reward(user_id, allowed_rewards(user, self.settings)):
            return ClaimResult.UNAVAILABLE

        return await self.grant(user)

    async def grant(self, user: Row, check_id: int | None = None, origin: str | None = None,
                    gift_id: str | None = None, spin_id: int | None = None, auto: bool | None = None) -> ClaimResult:
        """Выдаёт подарок: автоматически за звёзды или заявкой админам (ручной режим / ошибка отправки).

        auto=True — всегда пробовать автоотправку (оплаченные прокрутки рулетки), независимо от режима выдачи.
        """
        user_id = user["user_id"]
        gift_id = gift_id or self.settings.get("gift_id")
        error: str | None = None
        if auto or (auto is None and self.settings.get("reward_mode") == "auto"):
            error = await self.send_gift(user_id, gift_id)
            if error is None:
                await self.db.create_claim(user_id, "sent", "auto", gift_id, check_id=check_id, spin_id=spin_id)
                return ClaimResult.SENT

        claim_id = await self.db.create_claim(user_id, "pending", None, gift_id, error, check_id=check_id,
                                              spin_id=spin_id)
        await self._notify_admins_about_claim(claim_id, user, error, origin)
        return ClaimResult.PENDING

    async def _notify_admins_about_claim(self, claim_id: int, user: Row, error: str | None,
                                         origin: str | None = None) -> None:
        p = calc_progress(user, self.settings)
        username = f" (@{esc(user['username'])})" if user["username"] else ""
        text = (f"🧸 <b>Заявка #{claim_id} на награду</b>" + (f" · {origin}" if origin else "") + "\n\n"
                f"👤 {user_link(user['user_id'], user['full_name'])}{username}\n"
                f"🆔 <code>{user['user_id']}</code>\n"
                f"👥 Друзей: <b>{p.total}</b> · наград получено: {p.claimed}")
        if error:
            text += (f"\n\n⚠️ <b>Автоотправка не удалась:</b>\n<code>{esc(error)}</code>\n"
                     "Проверьте баланс звёзд бота и нажмите «Отправить подарок».")
        await self.admins.notify(text, claim_admin_kb(claim_id, user["user_id"]))

    async def notify_user_reward_sent(self, user_id: int) -> None:
        user = await self.db.get_user(user_id)
        text = render_template(self.settings.get("text_reward_sent"), name=esc(user["full_name"] if user else ""))
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🏠 В меню", callback_data=U(a="menu").pack())
        ]])
        try:
            await self.bot.send_message(user_id, text, reply_markup=kb)
        except TelegramForbiddenError:
            await self.db.set_blocked(user_id)
        except TelegramAPIError:
            pass

    async def process_claim(self, claim_id: int, action: str, admin_id: int) -> tuple[bool, str]:
        """action: send | done | reject. Возвращает (успех, сообщение для админа)."""
        if claim_id in self._busy:
            return False, "Заявка уже обрабатывается"
        self._busy.add(claim_id)
        try:
            return await self._process_claim(claim_id, action, admin_id)
        finally:
            self._busy.discard(claim_id)

    async def _process_claim(self, claim_id: int, action: str, admin_id: int) -> tuple[bool, str]:
        claim = await self.db.get_claim(claim_id)
        if not claim:
            return False, "Заявка не найдена"
        if claim["status"] != "pending":
            return False, "Заявка уже обработана"

        user_id = claim["user_id"]
        if action == "send":
            error = await self.send_gift(user_id, claim["gift_id"])
            if error:
                await self.db.set_claim_error(claim_id, error)
                return False, f"Не удалось отправить: {error}"
            await self.db.finish_claim(claim_id, "sent", "auto", admin_id)
            if claim["spin_id"]:
                await self.db.set_spin_status(claim["spin_id"], "sent")
            await self.notify_user_reward_sent(user_id)
            return True, "🎁 Подарок отправлен"

        if action == "done":
            await self.db.finish_claim(claim_id, "sent", "manual", admin_id)
            if claim["spin_id"]:
                await self.db.set_spin_status(claim["spin_id"], "sent")
            await self.notify_user_reward_sent(user_id)
            return True, "✅ Отмечено как выданное"

        if action == "reject":
            await self.db.finish_claim(claim_id, "rejected", None, admin_id)
            try:
                await self.bot.send_message(
                    user_id, "❌ Заявка на награду отклонена администратором.\n"
                             "Если это ошибка — напишите в поддержку."
                )
            except TelegramAPIError:
                pass
            return True, "❌ Заявка отклонена"

        return False, "Неизвестное действие"

    async def admin_gift(self, user_id: int, admin_id: int) -> str | None:
        """Принудительная отправка подарка админом (вне прогресса)."""
        error = await self.send_gift(user_id)
        if error is None:
            await self.db.create_claim(user_id, "sent", "admin", self.settings.get("gift_id"), processed_by=admin_id)
            await self.notify_user_reward_sent(user_id)
        return error
