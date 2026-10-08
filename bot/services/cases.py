"""Кейсы за звёзды с баланса в боте: приз — подарок Telegram или звёзды обратно на баланс."""
import logging
import secrets
from dataclasses import dataclass
from enum import StrEnum

from aiosqlite import Row

from bot.database import Database
from bot.services.rewards import ClaimResult, RewardService
from bot.utils import esc, fmt_stars, now

log = logging.getLogger(__name__)

DAY = 86400
_random = secrets.SystemRandom()

# Ежедневный кейс по умолчанию: бесплатно раз в сутки, в среднем 0.25 ⭐ на баланс.
DEFAULT_DAILY = ("Ежедневный кейс", "🎁", [(10, 50), (25, 30), (50, 15), (100, 5)])


class OpenStatus(StrEnum):
    OK = "ok"
    NO_FUNDS = "no_funds"
    COOLDOWN = "cooldown"
    UNAVAILABLE = "unavailable"
    BUSY = "busy"


@dataclass(frozen=True, slots=True)
class OpenResult:
    status: OpenStatus
    prize: Row | None = None
    delivered: str = ""   # credited — звёзды на балансе | sent — подарок отправлен | pending — заявка админам
    wait: int = 0         # секунд до следующего открытия ежедневного кейса


def prize_label(prize: Row) -> str:
    if prize["kind"] == "stars":
        return f"⭐ {fmt_stars(prize['value'])} Stars на баланс"
    return f"{prize['emoji'] or '🎁'} подарок за {fmt_stars(prize['value'])} ⭐"


def expected_value(prizes: list[Row]) -> float:
    """Средняя стоимость приза, сотые доли звезды."""
    total = sum(p["weight"] for p in prizes)
    return sum(p["weight"] * p["value"] for p in prizes) / total if total else 0


def case_button_text(case: Row) -> str:
    return f"{case['name']} — {fmt_stars(case['price'])} ⭐"


class CaseService:
    def __init__(self, db: Database, rewards: RewardService) -> None:
        self.db = db
        self.rewards = rewards
        self._busy: set[int] = set()  # пользователи, у которых кейс открывается прямо сейчас

    async def seed_defaults(self) -> None:
        """При первом запуске создаёт ежедневный кейс — на него ведёт кнопка главного меню."""
        if await self.db.cases():
            return
        name, emoji, prizes = DEFAULT_DAILY
        case_id = await self.db.create_case(name, 0, emoji, is_daily=True)
        for value, weight in prizes:
            await self.db.add_case_prize(case_id, "stars", value, weight)
        log.info("Создан ежедневный кейс по умолчанию")

    async def visible(self) -> list[Row]:
        """Включённые кейсы, в которых есть что выиграть."""
        result = []
        for case in await self.db.cases(only_active=True):
            if sum(p["weight"] for p in await self.db.case_prizes(case["id"])) > 0:
                result.append(case)
        return result

    async def wait_left(self, case: Row, user_id: int) -> int:
        """Сколько секунд ждать следующего открытия ежедневного кейса (0 — можно открыть)."""
        if not case["is_daily"]:
            return 0
        last = await self.db.last_case_open(user_id, case["id"])
        return max(0, last + DAY - now()) if last else 0

    async def open(self, user: Row, case_id: int) -> OpenResult:
        user_id = user["user_id"]
        if user_id in self._busy:
            return OpenResult(OpenStatus.BUSY)
        self._busy.add(user_id)
        try:
            return await self._open(user, case_id)
        finally:
            self._busy.discard(user_id)

    async def _open(self, user: Row, case_id: int) -> OpenResult:
        user_id = user["user_id"]
        case = await self.db.get_case(case_id)
        prizes = await self.db.case_prizes(case_id) if case and case["is_active"] else []
        if not prizes or sum(p["weight"] for p in prizes) <= 0:
            return OpenResult(OpenStatus.UNAVAILABLE)
        if wait := await self.wait_left(case, user_id):
            return OpenResult(OpenStatus.COOLDOWN, wait=wait)
        if case["price"] and not await self.db.spend_balance(user_id, case["price"]):
            return OpenResult(OpenStatus.NO_FUNDS)

        prize = _random.choices(prizes, weights=[p["weight"] for p in prizes], k=1)[0]
        open_id = await self.db.add_case_open(user_id, case, prize)
        if prize["kind"] == "stars":
            await self.db.add_balance(user_id, prize["value"])
            delivered = "credited"
        else:
            result = await self.rewards.grant(user, origin=f"📦 кейс «{esc(case['name'])}»", gift_id=prize["gift_id"],
                                              auto=True, with_text=False)
            delivered = "sent" if result is ClaimResult.SENT else "pending"
        await self.db.set_case_open_status(open_id, delivered)
        return OpenResult(OpenStatus.OK, prize, delivered)
