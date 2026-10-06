"""НФТ подарки: каталог для пользователей, ссылка «Написать админу» и юзербот с автоответом."""
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer
from aiosqlite import Row

from bot.callbacks import A
from bot.config import Config
from bot.database import Database
from bot.handlers.admin.common import Input, back, btn, drop_prompt, kb, prompt
from bot.handlers.admin.home import day_start
from bot.services.userbot import UbStatus, Userbot
from bot.settings import Settings
from bot.utils import esc, fmt_num, render_template, send_card, show
from bot.views import nft_card_screen, nft_contact_base

router = Router(name="admin_nft")

LOGIN_CMD = "docker compose run --rm bot python -m bot.userbot_login"
NFT_LINK_RE = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/nft/([A-Za-z0-9]+)-(\d+)/?$", re.I)
URL_RE = re.compile(r"^(?:https?://|tg://)\S+$", re.I)
USERNAME_RE = re.compile(r"^@?([A-Za-z]\w{3,31})$")
UB_FLAGS = {"userbot_enabled", "userbot_paid_only"}

# поле → (подпись, подсказка, лимит символов)
FIELDS = {
    "title": ("✏️ Название", "Пришлите название подарка — до 64 символов.", 64),
    "price": ("💰 Цена", "Пришлите цену так, как её увидит пользователь: например «1 500 ⭐» или «25 TON».\n"
                        "«-» — убрать цену.", 32),
    "description": ("📝 Описание", "Пришлите описание — до 500 символов. Форматирование Telegram сохранится.\n"
                                   "«-» — убрать описание.", 500),
    "link": ("🔗 Ссылка", "Пришлите ссылку на подарок, например <code>https://t.me/nft/PlushPepe-1234</code> — "
                         "в карточке появится превью подарка и кнопка «🔍 Посмотреть подарок».\n«-» — убрать ссылку.", 256),
    "emoji": ("✨ Премиум-эмодзи", "Пришлите один премиум-эмодзи — он будет иконкой на кнопке подарка "
                                  "в списке НФТ подарков.\n«-» — убрать.\n\n"
                                  "<i>Иконки на кнопках Telegram показывает, только если у владельца бота есть "
                                  "Telegram Premium.</i>", 0),
    "photo": ("🖼 Картинка", "Пришлите картинку подарка (как фото). Она покажется в карточке вместо превью ссылки.\n"
                            "«-» — убрать картинку.", 0),
}


def title_from_link(slug: str, number: str) -> str:
    """PlushPepe + 1234 → «Plush Pepe #1234»."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", slug) + f" #{number}"


def normalize_url(raw: str) -> str | None:
    raw = raw.strip()
    if m := USERNAME_RE.fullmatch(raw):
        return f"https://t.me/{m.group(1)}"
    if re.match(r"^(t\.me|telegram\.me)/", raw, re.I):
        raw = "https://" + raw
    return raw if URL_RE.match(raw) else None


def ub_short(userbot: Userbot, settings: Settings) -> str:
    if userbot.online:
        name = f"@{userbot.username}" if userbot.username else esc(userbot.me.first_name)
        return f"🟢 {name}" + ("" if settings.flag("userbot_enabled") else " · <i>автоответ выключен</i>")
    return {UbStatus.CONNECTING: "⏳ подключается…", UbStatus.ERROR: "🔴 ошибка подключения"}.get(
        userbot.status, "⚪️ не подключён")


# ---------- главный экран ----------

async def main_screen(db: Database, settings: Settings, userbot: Userbot):
    gifts = await db.nft_gifts()
    active = sum(g["is_active"] for g in gifts)
    linked = sum(1 for g in gifts if g["chat_link"])
    contact = nft_contact_base(settings, userbot.username)
    auto = " <i>(юзербот)</i>" if contact and not settings.get("nft_contact_url") else ""
    lines = [
        "💎 <b>НФТ подарки</b>\n",
        "В меню бота есть раздел «💎 НФТ подарки». Пользователь выбирает подарок и видит инструкцию: "
        "написать админу и ждать подарок.\n",
        f"🤖 Юзербот: {ub_short(userbot, settings)}",
        f"🎁 Подарков: <b>{len(gifts)}</b> · показываются: <b>{active}</b>",
        f"🔗 Ссылки на чат: <b>{linked}</b> из {len(gifts)} — у каждого подарка своя, с готовым сообщением",
        f"💬 Сообщение: «{render_template(settings.get('nft_link_message'), gift='<i>название</i>')}»",
        f"✍️ Запасная ссылка: {esc(contact) + auto if contact else '—'}",
    ]
    if gifts and linked < len(gifts):
        lines.append("\n⚠️ <i>Не у всех подарков есть ссылка на чат — для них кнопка «Написать админу» "
                     "ведёт на запасную ссылку. Подключите юзербота и нажмите «🔄 Обновить ссылки».</i>")
    if not active:
        lines.append("\n<i>Кнопка «💎 НФТ подарки» появится в меню, когда будет хотя бы один видимый подарок.</i>")
    rows = [[btn(f"{'🟢' if g['is_active'] else '🔴'} {g['title']}" + (f" · {g['price']}" if g["price"] else ""),
                 "nft", "card", id=g["id"])] for g in gifts]
    rows.append([btn("➕ Добавить подарок", "nft", "add", style="success")])
    rows.append([btn("💬 Текст сообщения", "nft", "msg"), btn("🔄 Обновить ссылки", "nft", "sync")])
    rows.append([btn("🤖 Юзербот", "ub"), btn("✍️ Запасная ссылка", "nft", "contact")])
    rows.append(back())
    return "\n".join(lines), kb(*rows)


@router.callback_query(A.filter((F.s == "nft") & (F.a == "open")))
async def cb_open(call: CallbackQuery, db: Database, settings: Settings, userbot: Userbot) -> None:
    await show(call, *await main_screen(db, settings, userbot))


# ---------- карточка подарка ----------

def card_screen(g: Row, link_views: dict[str, int] | None = None, link_error: str | None = None):
    gid = g["id"]
    if g["chat_link"]:
        clicks = (link_views or {}).get(g["chat_link_slug"])
        chat_link = esc(g["chat_link"]) + (f" · переходов: <b>{fmt_num(clicks)}</b>" if clicks is not None else "")
    else:
        chat_link = f"⚠️ нет ({esc(link_error)})" if link_error else "⚠️ нет — подключите юзербота"
    picture = "загружена" if g["photo"] else ("превью ссылки" if g["link"] else "—")
    lines = [
        f"💎 <b>{esc(g['title'])}</b>\n",
        f"💰 Цена: {esc(g['price']) if g['price'] else '—'}",
        f"📝 Описание: {'есть' if g['description'] else '—'}",
        f"🔗 Ссылка на подарок: {esc(g['link']) if g['link'] else '—'}",
        f"💬 Ссылка на чат: {chat_link}",
        f"🖼 Картинка: {picture}",
        f"✨ Премиум-эмодзи на кнопке: {'задан' if g['emoji_id'] else '—'}",
        f"👁 Открыли: <b>{fmt_num(g['views'])}</b> раз",
        f"Статус: {'🟢 показывается пользователям' if g['is_active'] else '🔴 скрыт'}",
    ]
    if not g["price"]:
        lines.append("\n💡 <i>Укажите цену — так пользователю понятнее, сколько стоит подарок.</i>")
    elif not (g["link"] or g["photo"]):
        lines.append("\n💡 <i>Добавьте ссылку или картинку — карточка с превью подарка смотрится лучше.</i>")

    def edit(field: str):
        return btn(FIELDS[field][0], "nft", "edit", id=gid, v=field)

    return "\n".join(lines), kb(
        [edit("title"), edit("price")],
        [edit("description"), edit("link")],
        [edit("photo"), edit("emoji")],
        [btn("👁 Как видит пользователь", "nft", "preview", id=gid, style="primary")],
        [btn("🔴 Скрыть" if g["is_active"] else "🟢 Показать", "nft", "toggle", id=gid),
         btn("⬆️ Выше", "nft", "up", id=gid)],
        [btn("🔄 Обновить ссылку на чат", "nft", "sync1", id=gid),
         btn("🗑 Удалить", "nft", "del", id=gid, style="danger")],
        back("nft", text="« К НФТ подаркам"),
    )


async def open_card(event: CallbackQuery | Message, db: Database, settings: Settings, userbot: Userbot,
                    gift_id: int, callback_answer: CallbackAnswer | None = None) -> None:
    g = await db.get_nft_gift(gift_id)
    if g is None:
        if callback_answer:
            callback_answer.text = "Подарок не найден"
        await show(event, *await main_screen(db, settings, userbot))
        return
    views = await userbot.chat_link_views() if g["chat_link"] else {}
    await show(event, *card_screen(g, views, userbot.link_errors.get(g["id"])))


@router.callback_query(A.filter((F.s == "nft") & (F.a == "card")))
async def cb_card(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                  settings: Settings, userbot: Userbot) -> None:
    await open_card(call, db, settings, userbot, callback_data.id, callback_answer)


@router.callback_query(A.filter((F.s == "nft") & F.a.in_({"toggle", "up"})))
async def cb_action(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                    settings: Settings, userbot: Userbot) -> None:
    g = await db.get_nft_gift(callback_data.id)
    if g and callback_data.a == "toggle":
        await db.update_nft_gift(g["id"], is_active=0 if g["is_active"] else 1)
        callback_answer.text = "🔴 Скрыт" if g["is_active"] else "🟢 Показывается"
    elif g:
        await db.move_nft_gift_up(g["id"])
        callback_answer.text = "⬆️ Перемещён"
    await open_card(call, db, settings, userbot, callback_data.id, callback_answer)


@router.callback_query(A.filter((F.s == "nft") & (F.a == "preview")))
async def cb_preview(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, bot: Bot,
                     db: Database, settings: Settings, userbot: Userbot) -> None:
    g = await db.get_nft_gift(callback_data.id)
    if g is None:
        return
    text, markup = nft_card_screen(g, settings, nft_contact_base(settings, userbot.username))
    await send_card(bot, call.from_user.id, text, markup, g["photo"], g["link"])
    callback_answer.text = "👆 Так карточку видит пользователь"
    # админскую карточку — заново под превью, чтобы продолжить редактирование
    text, markup = card_screen(g, link_error=userbot.link_errors.get(g["id"]))
    await call.message.answer(text, reply_markup=markup)
    try:
        await call.message.delete()
    except TelegramBadRequest:
        pass


@router.callback_query(A.filter((F.s == "nft") & (F.a == "del")))
async def cb_delete_confirm(call: CallbackQuery, callback_data: A, db: Database) -> None:
    g = await db.get_nft_gift(callback_data.id)
    if g is None:
        return
    await show(call, f"🗑 Удалить подарок <b>{esc(g['title'])}</b>?\n\n"
                     "<i>Если хотите убрать его временно — лучше нажмите «🔴 Скрыть».</i>", kb(
        [btn("🗑 Да, удалить", "nft", "del_ok", id=g["id"], style="danger")],
        back("nft", "card", "« Отмена", id=g["id"]),
    ))


@router.callback_query(A.filter((F.s == "nft") & (F.a == "del_ok")))
async def cb_delete(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                    settings: Settings, userbot: Userbot) -> None:
    g = await db.get_nft_gift(callback_data.id)
    if g:
        await userbot.delete_nft_link(g["chat_link_slug"])
        await db.delete_nft_gift(g["id"])
    callback_answer.text = "🗑 Подарок удалён"
    await show(call, *await main_screen(db, settings, userbot))


# ---------- добавление и редактирование ----------

@router.callback_query(A.filter((F.s == "nft") & (F.a == "add")))
async def cb_add(call: CallbackQuery, state: FSMContext) -> None:
    await prompt(call, state, Input.nft_add,
                 "➕ <b>Новый НФТ подарок</b>\n\n"
                 "Пришлите название подарка — или ссылку на него, например "
                 "<code>https://t.me/nft/PlushPepe-1234</code>: название подставится само, "
                 "а в карточке будет превью подарка.\n\n"
                 "Цену, описание и картинку добавите следующим шагом.",
                 back("nft", text="✖️ Отмена"))


@router.message(Input.nft_add, F.text)
async def on_add(message: Message, state: FSMContext, db: Database, settings: Settings, userbot: Userbot) -> None:
    raw = message.text.strip()
    link = None
    if m := NFT_LINK_RE.match(raw):
        link = f"https://t.me/nft/{m.group(1)}-{m.group(2)}"
        title = title_from_link(m.group(1), m.group(2))
    else:
        title = raw
    if len(title) > 64:
        await message.answer(f"⚠️ Слишком длинное название: {len(title)}/64 символов")
        return
    await drop_prompt(message, state)
    await state.clear()
    gift_id = await db.create_nft_gift(title, link)
    error = await userbot.sync_nft_link(gift_id)
    await message.answer(f"✅ Подарок «{esc(title)}» добавлен и уже виден пользователям.\n"
                         + ("🔗 Ссылка на чат создана.\n" if not error else f"⚠️ Ссылка на чат не создана: {esc(error)}\n")
                         + "Добавьте цену и описание 👇")
    await open_card(message, db, settings, userbot, gift_id)


@router.callback_query(A.filter((F.s == "nft") & (F.a == "edit") & F.v.in_(FIELDS)))
async def cb_edit(call: CallbackQuery, callback_data: A, state: FSMContext) -> None:
    label, hint, _ = FIELDS[callback_data.v]
    await prompt(call, state, Input.nft_field, f"<b>{label}</b>\n\n{hint}",
                 back("nft", "card", "✖️ Отмена", id=callback_data.id),
                 gift_id=callback_data.id, field=callback_data.v)


@router.message(Input.nft_field, F.photo)
async def on_edit_photo(message: Message, state: FSMContext, db: Database, settings: Settings,
                        userbot: Userbot) -> None:
    data = await state.get_data()
    if data.get("field") != "photo":
        await message.answer("⚠️ Сейчас нужен текст, а не картинка")
        return
    await drop_prompt(message, state)
    await state.clear()
    await db.update_nft_gift(data["gift_id"], photo=message.photo[-1].file_id)
    await message.answer("✅ Картинка сохранена")
    await open_card(message, db, settings, userbot, data["gift_id"])


@router.message(Input.nft_field, F.text)
async def on_edit_text(message: Message, state: FSMContext, db: Database, settings: Settings,
                       userbot: Userbot) -> None:
    data = await state.get_data()
    field = data["field"]
    raw = message.text.strip()
    _, _, limit = FIELDS[field]
    value: str | None
    if raw == "-" and field != "title":
        value = None
    elif field == "photo":
        await message.answer("⚠️ Пришлите картинку как фото или «-», чтобы убрать её")
        return
    elif field == "emoji":
        value = next((e.custom_emoji_id for e in message.entities or [] if e.type == "custom_emoji"), None)
        if value is None:
            await message.answer("⚠️ Это не премиум-эмодзи. Выберите эмодзи из премиум-набора "
                                 "(с анимацией или из стикерпака эмодзи) и пришлите его.")
            return
    elif len(raw) > limit:
        await message.answer(f"⚠️ Слишком длинно: {len(raw)}/{limit} символов")
        return
    elif field == "link":
        value = normalize_url(raw)
        if value is None:
            await message.answer("⚠️ Это не похоже на ссылку. Пример: <code>https://t.me/nft/PlushPepe-1234</code>")
            return
    elif field == "description":
        value = message.html_text.strip()
    else:
        value = raw
    await drop_prompt(message, state)
    await state.clear()
    await db.update_nft_gift(data["gift_id"], **{"emoji_id" if field == "emoji" else field: value})
    if field == "title":  # название есть в сообщении ссылки на чат — обновим её
        await userbot.sync_nft_link(data["gift_id"])
    await message.answer("✅ Сохранено")
    await open_card(message, db, settings, userbot, data["gift_id"])


# ---------- ссылки на чат ----------

@router.callback_query(A.filter((F.s == "nft") & (F.a == "sync1")))
async def cb_sync_one(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                      settings: Settings, userbot: Userbot) -> None:
    error = await userbot.sync_nft_link(callback_data.id)
    callback_answer.text = f"❌ {error}"[:200] if error else "🔗 Ссылка на чат обновлена"
    callback_answer.show_alert = bool(error)
    await open_card(call, db, settings, userbot, callback_data.id, callback_answer)


@router.callback_query(A.filter((F.s == "nft") & (F.a == "sync")))
async def cb_sync(call: CallbackQuery, callback_answer: CallbackAnswer, db: Database, settings: Settings,
                  userbot: Userbot) -> None:
    if not userbot.online:
        callback_answer.text = "Юзербот не подключён — откройте «🤖 Юзербот»"
        callback_answer.show_alert = True
        return
    ok, failed = await userbot.sync_nft_links()
    callback_answer.text = f"🔗 Обновлено ссылок: {ok}" + (f", с ошибкой: {failed}" if failed else "")
    callback_answer.show_alert = bool(failed)
    await show(call, *await main_screen(db, settings, userbot))


@router.callback_query(A.filter((F.s == "nft") & (F.a == "msg")))
async def cb_link_message(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await prompt(call, state, Input.nft_link_text,
                 "💬 <b>Сообщение в ссылке на чат</b>\n\n"
                 f"Сейчас: «{settings.get('nft_link_message')}»\n\n"
                 "Этот текст подставится в поле ввода, когда пользователь откроет чат по ссылке подарка — "
                 "ему останется нажать «Отправить».\n\n"
                 "Переменная: <code>{gift}</code> — название подарка. Пример: <code>Привет, оплата за {gift}</code>\n"
                 "<i>После сохранения ссылки всех подарков обновятся.</i>",
                 back("nft", text="✖️ Отмена"))


@router.message(Input.nft_link_text, F.text)
async def on_link_message(message: Message, state: FSMContext, db: Database, settings: Settings,
                          userbot: Userbot) -> None:
    if len(message.text) > 500:
        await message.answer(f"⚠️ Слишком длинно: {len(message.text)}/500 символов")
        return
    await drop_prompt(message, state)
    await state.clear()
    await settings.set("nft_link_message", message.html_text)
    ok, failed = await userbot.sync_nft_links() if userbot.online else (0, 0)
    note = (f"🔗 Обновлено ссылок: {ok}" + (f", с ошибкой: {failed}" if failed else "") if userbot.online
            else "⚠️ Юзербот не подключён — ссылки обновятся, когда он подключится и вы нажмёте «🔄 Обновить ссылки»")
    await message.answer(f"✅ Сообщение сохранено\n{note}")
    await show(message, *await main_screen(db, settings, userbot))


# ---------- запасная ссылка «Написать админу» ----------

@router.callback_query(A.filter((F.s == "nft") & (F.a == "contact")))
async def cb_contact(call: CallbackQuery, state: FSMContext, settings: Settings, userbot: Userbot) -> None:
    current = settings.get("nft_contact_url")
    auto = f"https://t.me/{userbot.username}" if userbot.username else None
    await prompt(call, state, Input.nft_contact,
                 "✍️ <b>Запасная ссылка «Написать админу»</b>\n\n"
                 "Используется для подарков, у которых ещё нет своей ссылки на чат.\n\n"
                 f"Сейчас: {esc(current) if current else (esc(auto) + ' <i>(юзербот)</i>' if auto else '—')}\n\n"
                 "Пришлите <code>@username</code> или ссылку — по ней пользователь напишет админу.\n"
                 "Для <code>@username</code> бот сам подставит в сообщение название выбранного подарка.\n\n"
                 "«-» — ссылка на подключённого юзербота.",
                 back("nft", text="✖️ Отмена"))


@router.message(Input.nft_contact, F.text)
async def on_contact(message: Message, state: FSMContext, db: Database, settings: Settings,
                     userbot: Userbot) -> None:
    raw = message.text.strip()
    url = "" if raw == "-" else normalize_url(raw)
    if url is None:
        await message.answer("⚠️ Пришлите @username или ссылку, например <code>https://t.me/username</code>")
        return
    await drop_prompt(message, state)
    await state.clear()
    await settings.set("nft_contact_url", url)
    await message.answer("✅ Ссылка сохранена" if url else "✅ Теперь кнопка ведёт на юзербота")
    await show(message, *await main_screen(db, settings, userbot))


# ---------- юзербот ----------

async def ub_screen(db: Database, settings: Settings, config: Config, userbot: Userbot):
    lines = ["🤖 <b>Юзербот</b>\n",
             "Аккаунт Telegram, которому пишут покупатели. На каждое сообщение он сразу отвечает "
             "текстом ниже — и так же на каждое повторное.\n"]
    st = userbot.status
    if st is UbStatus.NO_API:
        lines += ["Статус: ⚪️ <b>не настроен</b>\n",
                  "<b>Как подключить:</b>",
                  "1️⃣ На https://my.telegram.org → «API development tools» получите <code>api_id</code> "
                  "и <code>api_hash</code> (с аккаунта, который будет юзерботом).",
                  "2️⃣ Впишите их в <code>.env</code>: <code>USERBOT_API_ID=…</code> и "
                  "<code>USERBOT_API_HASH=…</code> — и перезапустите бота.",
                  f"3️⃣ Войдите в аккаунт на сервере:\n<code>{LOGIN_CMD}</code>",
                  "4️⃣ Нажмите «🔄 Переподключить»."]
    elif st is UbStatus.NO_SESSION:
        lines += ["Статус: ⚪️ <b>вход не выполнен</b>\n",
                  f"Выполните на сервере и введите телефон и код:\n<code>{LOGIN_CMD}</code>\n",
                  "Затем нажмите «🔄 Переподключить»."]
    elif st is UbStatus.CONNECTING:
        lines.append("Статус: ⏳ <b>подключается…</b>")
    elif st is UbStatus.ERROR or not userbot.online:
        lines += [f"Статус: 🔴 <b>ошибка</b>: <code>{esc(userbot.error or 'нет соединения')}</code>\n",
                  f"Нажмите «🔄 Переподключить». Если сессия отозвана — войдите заново:\n<code>{LOGIN_CMD}</code>"]
    else:
        await userbot.refresh_paid()
        me = userbot.me
        lines.append(f"Статус: 🟢 <b>{esc(me.first_name)}</b>"
                     + (f" (@{me.username})" if me.username else "") + f" · ID <code>{me.id}</code>")
        if userbot.paid_stars:
            lines.append(f"💰 Сообщение от не-контактов стоит: <b>{fmt_num(userbot.paid_stars)}</b> ⭐")
        elif userbot.paid_stars == 0:
            lines.append("💰 Платные сообщения <b>выключены</b> — включите на этом аккаунте: "
                         "Настройки → Конфиденциальность → Сообщения.")

    paid_only = settings.flag("userbot_paid_only")
    today, total = await db.userbot_stats(day_start(config)), await db.userbot_stats()
    lines += [
        "",
        f"💬 Автоответ: <b>{'вкл ✅' if settings.flag('userbot_enabled') else 'выкл'}</b>",
        f"🎯 Кому отвечать: <b>{'только на платные сообщения' if paid_only else 'всем, кроме контактов'}</b>",
        f"📊 Сообщений сегодня: <b>{fmt_num(today['messages'])}</b> от {fmt_num(today['users'])} чел. · "
        f"⭐ {fmt_num(today['stars'])}",
        f"📊 Всего: <b>{fmt_num(total['messages'])}</b> от {fmt_num(total['users'])} чел. · ⭐ {fmt_num(total['stars'])}",
        "\n━━━━━━━━ автоответ ━━━━━━━━\n",
        render_template(settings.get("userbot_reply"), name="Иван"),
    ]
    reply_row = [btn("✏️ Текст автоответа", "ub", "reply", style="primary")]
    if not settings.is_default("userbot_reply"):
        reply_row.append(btn("↩️ По умолчанию", "ub", "reset"))
    return "\n".join(lines), kb(
        reply_row,
        [btn(f"💬 Автоответ: {'вкл' if settings.flag('userbot_enabled') else 'выкл'}", "ub", "t",
             v="userbot_enabled"),
         btn(f"🎯 Только платные: {'да' if paid_only else 'нет'}", "ub", "t", v="userbot_paid_only")],
        [btn("🔄 Переподключить", "ub", "reconnect")],
        back("nft", text="« К НФТ подаркам"),
    )


@router.callback_query(A.filter((F.s == "ub") & (F.a == "open")))
async def cb_ub(call: CallbackQuery, db: Database, settings: Settings, config: Config, userbot: Userbot) -> None:
    await show(call, *await ub_screen(db, settings, config, userbot))


@router.callback_query(A.filter((F.s == "ub") & F.a.in_({"t", "reset", "reconnect"})))
async def cb_ub_action(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                       settings: Settings, config: Config, userbot: Userbot) -> None:
    if callback_data.a == "t" and callback_data.v in UB_FLAGS:
        value = await settings.toggle(callback_data.v)
        callback_answer.text = "Включено" if value else "Выключено"
    elif callback_data.a == "reset":
        await settings.reset("userbot_reply")
        callback_answer.text = "↩️ Восстановлен текст по умолчанию"
    elif callback_data.a == "reconnect":
        await userbot.start()
        callback_answer.text = "🟢 Юзербот подключён" if userbot.online else "❌ Не удалось подключиться"
    await show(call, *await ub_screen(db, settings, config, userbot))


@router.callback_query(A.filter((F.s == "ub") & (F.a == "reply")))
async def cb_ub_reply(call: CallbackQuery, state: FSMContext) -> None:
    await prompt(call, state, Input.ub_reply,
                 "✏️ <b>Текст автоответа юзербота</b>\n\n"
                 "Пришлите текст, который юзербот отправит в ответ на каждое сообщение. "
                 "Форматирование и премиум-эмодзи сохранятся (премиум-эмодзи — если у аккаунта юзербота есть Premium).\n\n"
                 "Переменная: <code>{name}</code> — имя пользователя.",
                 back("ub", text="✖️ Отмена"))


@router.message(Input.ub_reply, F.text)
async def on_ub_reply(message: Message, state: FSMContext, db: Database, settings: Settings, config: Config,
                      userbot: Userbot) -> None:
    if len(message.text) > 2000:
        await message.answer(f"⚠️ Слишком длинно: {len(message.text)}/2000 символов")
        return
    await drop_prompt(message, state)
    await state.clear()
    await settings.set("userbot_reply", message.html_text)
    await message.answer("✅ Автоответ сохранён")
    await show(message, *await ub_screen(db, settings, config, userbot))
