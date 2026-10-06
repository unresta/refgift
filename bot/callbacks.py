from aiogram.filters.callback_data import CallbackData


class U(CallbackData, prefix="u"):
    """Пользовательское меню."""
    a: str
    p: int = 0


class A(CallbackData, prefix="a"):
    """Админ-панель: s — раздел, a — действие, id/p/v — параметры."""
    s: str
    a: str = "open"
    id: int = 0
    p: int = 0
    v: str = ""


class Shop(CallbackData, prefix="sh"):
    """Магазин: карточка подарка; g — id подарка Telegram."""
    g: str


class ShopBuy(CallbackData, prefix="sb"):
    """Магазин: c = 0 — счёт с нашим комментарием, c > 0 — с выбранным своим, c = -1 — список своих."""
    g: str
    c: int = 0
