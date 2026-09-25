from dataclasses import dataclass

from bot.database import Database

# Плюшевый мишка 🧸 (15 ⭐). В админке можно выбрать любой подарок из доступных.
DEFAULT_GIFT_ID = "5170233102089322756"

DEFAULTS: dict[str, str] = {
    "ref_goal": "5",
    "reward_mode": "auto",          # auto — подарок Telegram за звёзды бота, manual — заявка админу
    "gift_id": DEFAULT_GIFT_ID,
    "gift_emoji": "🧸",
    "gift_price": "15",
    "gift_text": "Спасибо, что пригласил друзей! 🧸",
    "repeatable": "0",              # можно ли получать награду повторно за каждые N друзей
    "notify_referrer": "1",
    "maintenance": "0",
    "sub_cache_ttl": "60",

    "check_photo": "",             # file_id картинки чека (загружается в админке)
    "check_caption": (
        "🎁 <b>Чек на подарок {gift}</b>\n\n"
        "Первые <b>{count}</b> получат {gift} прямо в Telegram — бесплатно!\n"
        "Жми кнопку ниже 👇"
    ),

    "text_subscribe": (
        "👋 Привет, <b>{name}</b>!\n\n"
        "Чтобы участвовать в акции и получить мишку 🧸, подпишись на каналы ниже.\n\n"
        "Когда подпишешься — нажми «✅ Я подписался»."
    ),
    "text_menu": (
        "🧸 <b>Мишка за друзей</b>\n\n"
        "Пригласи <b>{goal}</b> друзей — и получи плюшевого мишку в подарок прямо в Telegram!\n\n"
        "{progress}"
    ),
    "text_rules": (
        "❓ <b>Как это работает</b>\n\n"
        "1️⃣ Открой «Пригласить друзей» и скопируй свою ссылку.\n"
        "2️⃣ Отправь её друзьям.\n"
        "3️⃣ Друг засчитывается, когда запустит бота и подпишется на все каналы.\n"
        "4️⃣ Как наберёшь <b>{goal}</b> друзей — жми «Забрать мишку» 🧸\n\n"
        "⚠️ Накрутка ботами и фейковыми аккаунтами — бан без награды."
    ),
    "text_share": "🧸 Забирай плюшевого мишку в Telegram бесплатно — жми на ссылку!",
    "text_reward_sent": (
        "🎉 <b>Поздравляем!</b>\n\n"
        "Мишка 🧸 уже у тебя — загляни в свой профиль → «Подарки».\n"
        "Спасибо, что пригласил друзей!"
    ),
    "text_reward_pending": (
        "✅ <b>Заявка принята!</b>\n\n"
        "Мишка 🧸 будет отправлен в ближайшее время — мы пришлём уведомление."
    ),
}


@dataclass(frozen=True, slots=True)
class TextMeta:
    title: str
    hint: str
    placeholders: tuple[str, ...]


TEXTS: dict[str, TextMeta] = {
    "text_subscribe": TextMeta("Экран подписки", "Первый экран, пока пользователь не подписался", ("name",)),
    "text_menu": TextMeta("Главное меню", "{progress} — готовый блок с прогресс-баром",
                          ("name", "goal", "count", "left", "progress")),
    "text_rules": TextMeta("Как это работает", "Раздел с правилами", ("goal",)),
    "text_share": TextMeta("Текст «Поделиться»", "Подставляется при пересылке ссылки другу", ("goal",)),
    "text_reward_sent": TextMeta("Награда отправлена", "Когда мишка ушёл пользователю", ("name",)),
    "check_caption": TextMeta("Подпись чека", "Текст под картинкой чека, если при создании не указана своя",
                              ("gift", "count")),
    "text_reward_pending": TextMeta("Заявка на награду", "Когда выдача ручная или звёзд не хватило", ("name",)),
}


class Settings:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._values: dict[str, str] = dict(DEFAULTS)

    async def load(self) -> None:
        self._values.update(await self._db.load_settings())

    def get(self, key: str) -> str:
        return self._values.get(key, DEFAULTS.get(key, ""))

    def get_int(self, key: str) -> int:
        try:
            return int(self.get(key))
        except ValueError:
            return int(DEFAULTS[key])

    def flag(self, key: str) -> bool:
        return self.get(key) == "1"

    def is_default(self, key: str) -> bool:
        return self.get(key) == DEFAULTS.get(key)

    async def set(self, key: str, value: object) -> None:
        if isinstance(value, bool):
            value = int(value)
        self._values[key] = str(value)
        await self._db.set_setting(key, str(value))

    async def toggle(self, key: str) -> bool:
        await self.set(key, not self.flag(key))
        return self.flag(key)

    async def reset(self, key: str) -> None:
        self._values[key] = DEFAULTS[key]
        await self._db.delete_setting(key)

    # удобные шорткаты
    @property
    def goal(self) -> int:
        return max(1, self.get_int("ref_goal"))

    @property
    def repeatable(self) -> bool:
        return self.flag("repeatable")
