"""Магазин подарков: пользователь платит звёзды по цене админа, бот сразу дарит подарок со своего баланса."""
import logging
import time
from dataclasses import dataclass
from enum import StrEnum

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Gift

from bot.database import Database
from bot.services.admins import AdminRegistry
from bot.services.gifts import GiftCatalog, gift_emoji
from bot.services.rewards import RewardService
from bot.settings import Settings
from bot.utils import esc, render_template

log = logging.getLogger(__name__)

PREFIX = "shop:"
MAX_PRICE = 10_000
COMMENT_MAX = 128          # лимит Telegram на подпись к подарку
MAX_COMMENTS = 20
LOW_BALANCE_NOTICE = 3600  # не чаще раза в час напоминать админам, что звёзд не хватает

# Обычные подарки Telegram — в магазине по умолчанию, в этом порядке.
DEFAULT_NAMES = {"🧸": "мишка", "💝": "сердце", "🎁": "подарок", "🌹": "роза", "🎂": "торт", "💐": "букет",
                 "🚀": "ракета", "🍾": "бутылка", "💎": "алмаз", "💍": "кольцо", "🏆": "кубок"}
ORDER = {emoji: i for i, emoji in enumerate(DEFAULT_NAMES)}


@dataclass(frozen=True, slots=True)
class ShopItem:
    gift: Gift
    name: str
    price: int      # платит покупатель
    active: bool

    @property
    def id(self) -> str:
        return self.gift.id

    @property
    def emoji(self) -> str:
        return gift_emoji(self.gift)

    @property
    def cost(self) -> int:
        return self.gift.star_count

    @property
    def profit(self) -> int:
        return self.price - self.cost

    @property
    def style(self) -> str | None:
        """Цвет кнопки по ценовой категории подарка."""
        return "success" if self.cost >= 100 else "primary" if self.cost >= 50 else None


class PayResult(StrEnum):
    SENT = "sent"
    REFUNDED = "refunded"
    FAILED = "failed"        # не отправили и не смогли вернуть звёзды — разбираются админы
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class Purchase:
    result: PayResult
    emoji: str = "🎁"
    name: str = ""
    price: int = 0
    comment: str = ""


class ShopService:
    def __init__(self, bot: Bot, db: Database, settings: Settings, rewards: RewardService, catalog: GiftCatalog,
                 admins: AdminRegistry, bot_username: str) -> None:
        self.bot = bot
        self.bot_username = bot_username
        self.db = db
        self.settings = settings
        self.rewards = rewards
        self.catalog = catalog
        self.admins = admins
        self._low_notice_at = 0.0

    async def items(self, only_active: bool = False) -> list[ShopItem]:
        overrides = await self.db.shop_overrides()
        result = []
        for g in await self.catalog.gifts():
            o = overrides.get(g.id)
            emoji = gift_emoji(g)
            default_active = emoji in DEFAULT_NAMES and g.remaining_count is None  # лимитированные — вручную
            item = ShopItem(
                gift=g,
                name=(o["name"] if o and o["name"] else DEFAULT_NAMES.get(emoji, "подарок")),
                price=(o["price"] if o and o["price"] else g.star_count),
                active=bool(o["is_active"]) if o and o["is_active"] is not None else default_active,
            )
            if item.active or not only_active:
                result.append(item)
        return sorted(result, key=lambda i: (i.price, i.cost, ORDER.get(i.emoji, len(ORDER))))

    async def item(self, gift_id: str) -> ShopItem | None:
        return next((i for i in await self.items() if i.id == gift_id), None)

    # ---------- комментарии ----------

    def default_comment(self) -> str:
        """Подпись к подарку по цене магазина (HTML, как в настройках)."""
        return render_template(self.settings.get("shop_comment"), bot=f"@{self.bot_username}")

    async def comment_text(self, comment_id: int) -> str | None:
        """0 — наш комментарий, иначе — выбранный вариант своего (None, если его удалили)."""
        if comment_id == 0:
            return self.default_comment()
        row = await self.db.get_shop_comment(comment_id)
        return row["text"] if row else None

    @staticmethod
    def price_for(item: ShopItem, comment_id: int) -> int:
        """С нашим комментарием — цена магазина, со своим — себестоимость."""
        return item.price if comment_id == 0 else item.cost

    def payload(self, item: ShopItem, comment_id: int = 0) -> str:
        return f"{PREFIX}{item.id}:{self.price_for(item, comment_id)}:{comment_id}"

    async def _parse(self, payload: str) -> tuple[str, ShopItem | None, int, int]:
        """shop:<gift_id>:<цена>[:<комментарий>] → (gift_id, подарок, цена, комментарий)."""
        gift_id, price, comment = (payload.removeprefix(PREFIX).split(":") + ["0"])[:3]
        to_int = lambda v: int(v) if v.isdigit() else -1  # noqa: E731
        return gift_id, await self.item(gift_id), to_int(price), to_int(comment)

    async def validate(self, payload: str, amount: int) -> str | None:
        """Проверка перед оплатой. Возвращает текст отказа или None."""
        _, item, price, comment_id = await self._parse(payload)
        if not self.settings.flag("shop_enabled") or item is None or not item.active:
            return "Этот подарок больше не продаётся — откройте магазин заново."
        if comment_id < 0 or await self.comment_text(comment_id) is None:
            return "Этот вариант комментария больше недоступен — выберите другой."
        if not price == amount == self.price_for(item, comment_id):
            return "Цена изменилась — откройте магазин и выберите подарок заново."
        try:
            balance = (await self.bot.get_my_star_balance()).amount
        except TelegramAPIError:
            balance = None
        if balance is not None and balance < item.cost:
            await self._notify_low_balance(balance, item)
            return "Подарок временно недоступен — попробуйте чуть позже."
        return None

    async def _notify_low_balance(self, balance: int, item: ShopItem) -> None:
        if time.monotonic() - self._low_notice_at < LOW_BALANCE_NOTICE and self._low_notice_at:
            return
        self._low_notice_at = time.monotonic()
        await self.admins.notify(
            f"⚠️ <b>Магазин: не хватает звёзд</b>\n\nПокупатель хотел {item.emoji} {esc(item.name)}, "
            f"а на балансе бота {balance} ⭐ (нужно {item.cost} ⭐). Пополните баланс в /admin.")

    async def on_paid(self, user_id: int, payload: str, amount: int, charge_id: str) -> Purchase:
        gift_id, item, _, comment_id = await self._parse(payload)
        emoji, name = (item.emoji, item.name) if item else ("🎁", "подарок")
        # вариант удалили между проверкой и оплатой — отправим с нашим комментарием
        comment = await self.comment_text(max(0, comment_id)) or self.default_comment()
        order_id = await self.db.add_shop_order(user_id, gift_id, emoji, name, amount,
                                                item.cost if item else 0, charge_id, comment)
        if order_id is None:
            return Purchase(PayResult.DUPLICATE, emoji, name, amount, comment)

        error = await self.rewards.send_gift(user_id, gift_id, text=comment)
        if error is None:
            return Purchase(PayResult.SENT, emoji, name, amount, comment)

        log.warning("Магазин: подарок %s для %s не отправлен: %s", gift_id, user_id, error)
        try:
            await self.bot.refund_star_payment(user_id, charge_id)
            result = PayResult.REFUNDED
        except TelegramAPIError as e:
            error = f"{error}; возврат не удался: {e.message}"
            result = PayResult.FAILED
        await self.db.set_shop_order_status(order_id, result.value, error)
        await self.admins.notify(
            f"⚠️ <b>Магазин: подарок не отправлен</b>\n\n{emoji} {esc(name)} за {amount} ⭐, покупатель "
            f"<code>{user_id}</code>\nОшибка: <code>{esc(error)}</code>\n"
            + ("↩️ Звёзды возвращены покупателю." if result is PayResult.REFUNDED
               else "❗️ Звёзды вернуть не удалось — свяжитесь с покупателем."))
        return Purchase(result, emoji, name, amount, comment)
