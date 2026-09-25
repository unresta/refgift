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
