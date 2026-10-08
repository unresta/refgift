from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.callbacks import A, U
from bot.handlers.admin.common import Btn, Input, back, btn, drop_prompt, kb, prompt
from bot.handlers.admin.tasks import URL_RE
from bot.services.banner import Banner
from bot.settings import Settings
from bot.utils import esc, show

router = Router(name="admin_menu")


def menu_screen(settings: Settings):
    url = settings.get("giveaway_url")
    enabled = settings.flag("banner_enabled")
    custom = settings.flag("banner_custom")
    text = "\n".join([
        "🏠 <b>Главное меню бота</b>\n",
        f"🖼 Баннер: <b>{'вкл' if enabled else 'выкл'}</b> · {'свой' if custom else 'по умолчанию'}",
        "   <i>картинка над каждым экраном меню: при старте, в заданиях, кейсах, профиле…</i>",
        f"📣 «Мой канал с раздачами»: {esc(url) if url else '— (кнопка скрыта)'}",
        "",
        "Кнопки меню: Получить подарки · Заработать звёзды · Ежедневный кейс · Купить подарки · "
        "Профиль / Баланс · Мой канал с раздачами.",
        "<i>Премиум-эмодзи на кнопках видны, если у владельца бота есть Telegram Premium.</i>",
    ])
    rows = [
        [btn(f"🖼 Баннер: {'вкл' if enabled else 'выкл'}", "mn", "toggle"), btn("🔄 Заменить баннер", "mn", "banner")],
    ]
    if custom:
        rows.append([btn("↩️ Баннер по умолчанию", "mn", "reset")])
    rows.append([btn("📝 Текст главного меню", "tx", "edit", v="text_main"),
                 btn("📝 Текст подписки", "tx", "edit", v="text_subscribe")])
    rows.append([btn("📣 Ссылка на канал с раздачами", "mn", "url", style="primary")])
    rows.append([Btn(text="👀 Как видит пользователь", callback_data=U(a="menu").pack())])
    rows.append(back())
    return text, kb(*rows)


@router.callback_query(A.filter((F.s == "mn") & (F.a == "open")))
async def cb_open(call: CallbackQuery, settings: Settings) -> None:
    await show(call, *menu_screen(settings))


@router.callback_query(A.filter((F.s == "mn") & F.a.in_({"toggle", "reset"})))
async def cb_action(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, settings: Settings,
                    banner: Banner) -> None:
    if callback_data.a == "toggle":
        callback_answer.text = "Баннер включён" if await settings.toggle("banner_enabled") else "Баннер выключен"
    else:
        await banner.reset()
        callback_answer.text = "↩️ Баннер по умолчанию"
    await show(call, *menu_screen(settings))


@router.callback_query(A.filter((F.s == "mn") & (F.a == "banner")))
async def cb_banner(call: CallbackQuery, state: FSMContext) -> None:
    await prompt(call, state, Input.mn_banner, "🖼 <b>Новый баннер</b>\n\nПришлите картинку <b>как фото</b> "
                                               "(лучше горизонтальную, например 1280×720).",
                 back("mn", text="✖️ Отмена"))


@router.message(Input.mn_banner, F.photo)
async def on_banner(message: Message, state: FSMContext, settings: Settings, banner: Banner) -> None:
    await drop_prompt(message, state)
    await state.clear()
    photo = message.photo[-1]
    await banner.set_photo(photo.file_id, photo.file_unique_id, custom=True)
    await message.answer("✅ Баннер обновлён")
    await show(message, *menu_screen(settings))


@router.message(Input.mn_banner)
async def on_banner_wrong(message: Message) -> None:
    await message.answer("⚠️ Пришлите картинку как фото (не файлом)")


@router.callback_query(A.filter((F.s == "mn") & (F.a == "url")))
async def cb_url(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    current = settings.get("giveaway_url")
    await prompt(call, state, Input.mn_url,
                 "📣 <b>Мой канал с раздачами</b>\n\n"
                 f"Сейчас: {esc(current) if current else '—'}\n\n"
                 "Пришлите ссылку на канал (<code>https://t.me/…</code>) или <code>-</code>, чтобы скрыть кнопку.",
                 back("mn", text="✖️ Отмена"))


@router.message(Input.mn_url, F.text)
async def on_url(message: Message, state: FSMContext, settings: Settings) -> None:
    raw = message.text.strip()
    if raw.startswith("@") and len(raw) > 1:
        raw = f"https://t.me/{raw[1:]}"
    elif raw.startswith("t.me/"):
        raw = "https://" + raw
    if raw != "-" and not URL_RE.match(raw):
        await message.answer("⚠️ Нужна ссылка вида <code>https://t.me/channel</code>, @username или <code>-</code>")
        return
    await drop_prompt(message, state)
    await state.clear()
    await settings.set("giveaway_url", "" if raw == "-" else raw)
    await show(message, *menu_screen(settings))
