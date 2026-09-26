"""Рулетка подарков: платная прокрутка за звёзды, приз определяется на сервере в момент оплаты."""
import asyncio
import logging
import secrets
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, WebAppInfo
from aiosqlite import Row

from bot.database import Database
from bot.services.admins import AdminRegistry
from bot.services.gifts import GiftCatalog, gift_emoji
from bot.services.rewards import ClaimResult, RewardService
from bot.settings import Settings
from bot.utils import esc

log = logging.getLogger(__name__)

SPIN_PREFIX = "spin:"
REVEAL_TIMEOUT = 30.0  # секунд: если мини-апп закрыли посреди прокрутки, подарок уйдёт сам
_random = secrets.SystemRandom()

# Кейсы по умолчанию. Подарки ищутся в каталоге по эмодзи и цене.
# Шанс = вес / сумма весов кейса. Подарки за 15 ⭐ — самые частые; отдача игрокам (RTP) ~81% в обоих кейсах.
DEFAULT_CASES = [
    ("Все", 25, [("🏆", 100, 1), ("🌹", 25, 14), ("🎂", 50, 5), ("💝", 15, 33), ("🧸", 15, 33), ("🎁", 25, 14)]),
    ("Романтика", 42, [("💝", 15, 24), ("🧸", 15, 24), ("🌹", 25, 20), ("💐", 50, 20), ("💍", 100, 6),
                       ("💎", 100, 6)]),
]
# Веса первой версии — по ним понимаем, что админ шансы не трогал и их можно обновить.
V1_WEIGHTS = {
    "Все": {("🏆", 100): 0.806, ("🌹", 25): 25, ("🎂", 50): 1.21, ("💝", 15): 21.37, ("🧸", 15): 21.37,
            ("🎁", 25): 25},
    "Романтика": {("💝", 15): 11.05, ("🧸", 15): 11.05, ("🌹", 25): 36.83, ("💐", 50): 27.38, ("💍", 100): 6.84,
                  ("💎", 100): 6.84},
}
DEFAULTS_VERSION = "2"


@dataclass(frozen=True, slots=True)
class CaseEconomics:
    total_weight: float
    expected_payout: float  # средняя стоимость приза, ⭐
    rtp: float              # доля цены, возвращаемая игрокам подарками

    @property
    def margin(self) -> float:
        return 1 - self.rtp


def economics(case: Row, prizes: list[Row]) -> CaseEconomics:
    total = sum(p["weight"] for p in prizes)
    if not total:
        return CaseEconomics(0, 0, 0)
    ev = sum(p["weight"] * p["gift_price"] for p in prizes) / total
    return CaseEconomics(total, ev, ev / case["price"] if case["price"] else 0)


def chance(prize: Row, total_weight: float) -> float:
    return prize["weight"] / total_weight * 100 if total_weight else 0


class RouletteService:
    def __init__(self, bot: Bot, db: Database, settings: Settings, rewards: RewardService, catalog: GiftCatalog,
                 admins: AdminRegistry) -> None:
        self.bot = bot
        self.db = db
        self.settings = settings
        self.rewards = rewards
        self.catalog = catalog
        self.admins = admins
        self._tasks: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return self.settings.flag("roulette_enabled")

    async def seed_defaults(self) -> None:
        """Создаёт кейсы с макета при первом запуске (если кейсов ещё нет)."""
        if await self.db.roulette_cases():
            return
        gifts = await self.catalog.gifts()
        if not gifts:
            return
        await self.settings.set("roulette_defaults_version", DEFAULTS_VERSION)
        for name, price, spec in DEFAULT_CASES:
            case_id = await self.db.create_roulette_case(name, price)
            for emoji, gift_price, weight in spec:
                gift = next((g for g in gifts if gift_emoji(g) == emoji and g.star_count == gift_price), None)
                if gift:
                    await self.db.add_roulette_prize(case_id, gift.id, emoji, gift_price, weight)
        log.info("Созданы кейсы рулетки по умолчанию")

    async def upgrade_default_weights(self) -> None:
        """Переносит новые шансы по умолчанию в уже созданные кейсы — только если их не меняли вручную."""
        if self.settings.get("roulette_defaults_version") == DEFAULTS_VERSION:
            return
        new = {name: {(e, p): w for e, p, w in spec} for name, _, spec in DEFAULT_CASES}
        for case in await self.db.roulette_cases():
            old = V1_WEIGHTS.get(case["name"])
            prizes = await self.db.roulette_prizes(case["id"])
            current = {(p["gift_emoji"], p["gift_price"]): p["weight"] for p in prizes}
            if not old or not current or any(old.get(k) != w for k, w in current.items()):
                continue  # не кейс по умолчанию или шансы уже настроены админом
            for p in prizes:
                await self.db.set_prize_weight(p["id"], new[case["name"]][(p["gift_emoji"], p["gift_price"])])
            log.info("Обновлены шансы кейса «%s»", case["name"])
        await self.settings.set("roulette_defaults_version", DEFAULTS_VERSION)

    async def active_cases(self) -> list[tuple[Row, list[Row]]]:
        result = []
        for case in await self.db.roulette_cases(only_active=True):
            prizes = await self.db.roulette_prizes(case["id"])
            if prizes and sum(p["weight"] for p in prizes) > 0:
                result.append((case, prizes))
        return result

    @staticmethod
    def roll(prizes: list[Row]) -> Row:
        return _random.choices(prizes, weights=[p["weight"] for p in prizes], k=1)[0]

    # ---------- оплата ----------
    async def create_invoice(self, user_id: int, case: Row) -> tuple[int, str]:
        spin_id = await self.db.create_spin(user_id, case)
        link = await self.bot.create_invoice_link(
            title=f"Рулетка «{case['name']}»",
            description=f"Прокрутка рулетки подарков за {case['price']} ⭐. Выигрыш сразу придёт в профиль.",
            payload=f"{SPIN_PREFIX}{spin_id}",
            currency="XTR",
            prices=[LabeledPrice(label="Прокрутка", amount=case["price"])],
        )
        return spin_id, link

    async def validate_payment(self, user_id: int, payload: str, amount: int) -> bool:
        spin = await self._spin_from_payload(payload)
        return bool(spin and spin["user_id"] == user_id and spin["status"] == "created"
                    and spin["price"] == amount and self.enabled
                    and await self.db.roulette_prizes(spin["case_id"]))

    async def _spin_from_payload(self, payload: str) -> Row | None:
        raw = payload.removeprefix(SPIN_PREFIX)
        return await self.db.get_spin(int(raw)) if raw.isdigit() else None

    async def on_paid(self, user_id: int, payload: str, charge_id: str) -> Row | None:
        """Оплата прошла: разыгрываем приз и отправляем подарок. Идемпотентно."""
        spin = await self._spin_from_payload(payload)
        if not spin or spin["user_id"] != user_id:
            return None
        prizes = await self.db.roulette_prizes(spin["case_id"])
        if not prizes:  # кейс удалили между счётом и оплатой — возвращаем звёзды
            try:
                await self.bot.refund_star_payment(user_id, charge_id)
                await self.db.set_spin_status(spin["id"], "refunded")
            except TelegramAPIError as e:
                log.error("Не удалось вернуть звёзды за спин %s: %s", spin["id"], e)
            return await self.db.get_spin(spin["id"])

        prize = self.roll(prizes)
        if not await self.db.mark_spin_paid(spin["id"], charge_id, prize):
            return await self.db.get_spin(spin["id"])  # уже обработан

        # Приз уже определён, но отправляем его после остановки рулетки — чтобы не убить интригу.
        # Мини-апп вызовет deliver() сам; если его закрыли, сработает таймаут.
        self._schedule_delivery(spin["id"], REVEAL_TIMEOUT)
        return await self.db.get_spin(spin["id"])

    # ---------- выдача выигрыша ----------
    def _schedule_delivery(self, spin_id: int, delay: float) -> None:
        task = asyncio.create_task(self._deliver_later(spin_id, delay))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver_later(self, spin_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            await self.deliver(spin_id)
        except Exception:
            log.exception("Не удалось выдать выигрыш за спин %s", spin_id)

    async def recover(self) -> None:
        """После перезапуска: выдать выигрыши, оплаченные, но ещё не отправленные."""
        for spin_id in await self.db.undelivered_spin_ids():
            self._schedule_delivery(spin_id, 0)

    async def deliver(self, spin_id: int) -> Row | None:
        """Отправляет выигрыш и пишет о нём в чат. Идемпотентно: второй вызов ничего не делает."""
        if not await self.db.claim_spin_delivery(spin_id):
            return await self.db.get_spin(spin_id)
        spin = await self.db.get_spin(spin_id)
        user = await self.db.get_user(spin["user_id"]) if spin else None
        if spin is None or user is None:
            return spin
        result = await self.rewards.grant(
            user, origin=f"🎰 рулетка «{esc(spin['case_name'])}»", gift_id=spin["gift_id"], spin_id=spin_id, auto=True,
        )
        status = "sent" if result is ClaimResult.SENT else "pending"
        await self.db.set_spin_status(spin_id, status)
        await self._notify_chat(spin, status)
        return await self.db.get_spin(spin_id)

    async def _notify_chat(self, spin: Row, status: str) -> None:
        where = ("уже в вашем профиле Telegram 🎉" if status == "sent"
                 else "будет отправлен в ближайшее время — пришлём уведомление.")
        text = (f"🎰 <b>Рулетка «{esc(spin['case_name'])}»</b>\n\n"
                f"Выпал {spin['gift_emoji']} за <b>{spin['gift_price']}</b> ⭐ — подарок {where}")
        markup = None
        if self.settings.webapp_url:
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="🎰 Крутить ещё", web_app=WebAppInfo(url=self.settings.webapp_url))]])
        try:
            await self.bot.send_message(spin["user_id"], text, reply_markup=markup)
        except TelegramAPIError:
            pass

    async def refund_claim(self, claim_id: int, admin_id: int) -> tuple[bool, str]:
        """Отмена выигрыша из очереди заявок: возвращаем пользователю звёзды за прокрутку."""
        claim = await self.db.get_claim(claim_id)
        if not claim or not claim["spin_id"] or claim["status"] != "pending":
            return False, "Возврат недоступен"
        spin = await self.db.get_spin(claim["spin_id"])
        if not spin or not spin["charge_id"]:
            return False, "Платёж не найден"
        try:
            await self.bot.refund_star_payment(spin["user_id"], spin["charge_id"])
        except TelegramAPIError as e:
            return False, f"Telegram: {e.message}"
        await self.db.finish_claim(claim_id, "rejected", None, admin_id, "refunded")
        await self.db.set_spin_status(spin["id"], "refunded")
        try:
            await self.bot.send_message(spin["user_id"], f"↩️ Вам вернули <b>{spin['price']}</b> ⭐ за прокрутку "
                                                         "рулетки — подарок не удалось отправить. Извините!")
        except TelegramAPIError:
            pass
        return True, f"↩️ Возвращено {spin['price']} ⭐"
