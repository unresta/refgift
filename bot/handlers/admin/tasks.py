import re

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from aiogram.utils.callback_answer import CallbackAnswer
from aiosqlite import Row

from bot.callbacks import A
from bot.database import Database
from bot.handlers.admin.channels import CANCEL, extract_target, pick_chat_keyboard
from bot.handlers.admin.common import Input, back, btn, drop_prompt, kb, prompt
from bot.services.tasks import KINDS, boost_url
from bot.utils import esc, fmt_num, fmt_stars, parse_stars, show

router = Router(name="admin_tasks")

KEEP = {"keep_state": True}
URL_RE = re.compile(r"^(https?://|tg://)\S+$", re.I)
STYLES = [None, "success", "primary", "danger"]
STYLE_NAMES = {None: "обычный", "success": "🟢 зелёный", "primary": "🔵 синий", "danger": "🔴 красный"}
DEFAULT_TITLES = {"sub": "{r}⭐ за подписку", "boost": "{r}⭐ ЗА БУСТ!!!", "link": "{r}⭐ за переход"}
FIELDS = {
    "title": "✏️ Новый текст кнопки (до 64 символов, можно с эмодзи):",
    "reward": "💰 Новая награда в звёздах, например <code>2</code> или <code>0.25</code>:",
    "url": "🔗 Новая ссылка кнопки «Перейти» (https://…):",
    "max_done": "🔢 Сколько раз задание можно выполнить всего? <code>0</code> — без лимита.",
}


async def list_screen(db: Database):
    tasks = await db.tasks()
    done, paid = await db.task_stats()
    lines = [
        "💰 <b>Задания «Заработать звёзды»</b>\n",
        "Пользователь выполняет задание и получает звёзды на баланс в боте — их можно потратить на кейсы.\n",
        "📢 <b>подписка</b> и 🚀 <b>буст</b> проверяются автоматически — бот должен быть админом канала. "
        "🔗 <b>ссылка</b> засчитывается без проверки.\n",
        f"📊 Выполнено: <b>{fmt_num(done)}</b> · начислено <b>{fmt_stars(paid)}</b> ⭐",
    ]
    if not tasks:
        lines.append("\n<i>Заданий пока нет.</i>")
    rows = []
    for t in tasks:
        icon = "🟢" if t["is_active"] and not (t["max_done"] and t["done"] >= t["max_done"]) else "⏸"
        limit = f"/{t['max_done']}" if t["max_done"] else ""
        rows.append([btn(f"{icon} {t['title']} · {fmt_stars(t['reward'])}⭐ · ✅ {t['done']}{limit}", "tk", "card",
                         id=t["id"])])
    rows.append([btn("➕ Новое задание", "tk", "new", style="success")])
    rows.append(back())
    return "\n".join(lines), kb(*rows)


async def bot_status(bot: Bot, task: Row) -> str:
    if task["kind"] == "link":
        return ""
    try:
        me = await bot.get_chat_member(task["chat_id"], bot.id)
        if me.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            return "🤖 Бот: ✅ администратор"
    except TelegramAPIError:
        pass
    return "🤖 Бот: ⚠️ <b>не администратор — проверка не работает</b>"


async def card_screen(bot: Bot, db: Database, task_id: int):
    t = await db.get_task(task_id)
    if not t:
        return "❌ Задание не найдено", kb(back("tk"))
    limit = str(t["max_done"]) if t["max_done"] else "∞"
    lines = [
        f"💰 <b>Задание #{t['id']}</b> · {'🟢 активно' if t['is_active'] else '⏸ выключено'}\n",
        f"Тип: {KINDS[t['kind']]}",
    ]
    if t["chat_id"]:
        lines.append(f"Канал: «{esc(t['chat_title'])}» (<code>{t['chat_id']}</code>)")
    lines += [
        f"Ссылка: {esc(t['url']) if t['url'] else '—'}",
        f"Кнопка: «{esc(t['title'])}» · цвет {STYLE_NAMES.get(t['style'], 'обычный')}",
        f"Награда: <b>{fmt_stars(t['reward'])}</b> ⭐",
        f"Выполнено: <b>{t['done']}</b> / {limit} · начислено {fmt_stars(t['done'] * t['reward'])} ⭐",
    ]
    if t["max_done"] and t["done"] >= t["max_done"]:
        lines.append("⚠️ Лимит исчерпан — пользователи задание не видят")
    if status := await bot_status(bot, t):
        lines.append(status)
    tid = t["id"]
    return "\n".join(lines), kb(
        [btn("✏️ Текст кнопки", "tk", "field", id=tid, v="title"),
         btn("💰 Награда", "tk", "field", id=tid, v="reward")],
        [btn("🔗 Ссылка", "tk", "field", id=tid, v="url"), btn(f"🔢 Лимит: {limit}", "tk", "field", id=tid, v="max_done")],
        [btn(f"🎨 Цвет: {STYLE_NAMES.get(t['style'], 'обычный')}", "tk", "style", id=tid, style=t["style"])],
        [btn("⬆️ Выше", "tk", "up", id=tid), btn("⏸ Выключить" if t["is_active"] else "▶️ Включить", "tk", "toggle",
                                                  id=tid)],
        [btn("🗑 Удалить", "tk", "del", id=tid, style="danger")],
        back("tk", text="« К заданиям"),
    )


@router.callback_query(A.filter((F.s == "tk") & (F.a == "open")))
async def cb_open(call: CallbackQuery, db: Database) -> None:
    await show(call, *await list_screen(db))


@router.callback_query(A.filter((F.s == "tk") & (F.a == "card")))
async def cb_card(call: CallbackQuery, callback_data: A, bot: Bot, db: Database) -> None:
    await show(call, *await card_screen(bot, db, callback_data.id))


@router.callback_query(A.filter((F.s == "tk") & F.a.in_({"toggle", "up", "style"})))
async def cb_action(call: CallbackQuery, callback_data: A, bot: Bot, db: Database) -> None:
    task = await db.get_task(callback_data.id)
    if not task:
        return
    if callback_data.a == "toggle":
        await db.update_task(task["id"], is_active=int(not task["is_active"]))
    elif callback_data.a == "up":
        await db.move_task_up(task["id"])
    else:
        style = task["style"] if task["style"] in STYLES else None
        await db.update_task(task["id"], style=STYLES[(STYLES.index(style) + 1) % len(STYLES)])
    await show(call, *await card_screen(bot, db, task["id"]))


@router.callback_query(A.filter((F.s == "tk") & (F.a == "del")))
async def cb_delete_confirm(call: CallbackQuery, callback_data: A, db: Database) -> None:
    task = await db.get_task(callback_data.id)
    if not task:
        return
    await show(call, f"🗑 <b>Удалить задание «{esc(task['title'])}»?</b>\n\n"
                     "Начисленные пользователям звёзды останутся на их балансе.", kb(
        [btn("🗑 Да, удалить", "tk", "del_ok", id=task["id"], style="danger")],
        back("tk", "card", "« Отмена", id=task["id"]),
    ))


@router.callback_query(A.filter((F.s == "tk") & (F.a == "del_ok")))
async def cb_delete(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database) -> None:
    await db.delete_task(callback_data.id)
    callback_answer.text = "🗑 Задание удалено"
    await show(call, *await list_screen(db))


# ---------- редактирование полей ----------

@router.callback_query(A.filter((F.s == "tk") & (F.a == "field") & F.v.in_(FIELDS)))
async def cb_field(call: CallbackQuery, callback_data: A, state: FSMContext) -> None:
    await prompt(call, state, Input.tk_field, FIELDS[callback_data.v],
                 back("tk", "card", "✖️ Отмена", id=callback_data.id), task_id=callback_data.id, field=callback_data.v)


def parse_field(field: str, raw: str) -> tuple[object, str | None]:
    """Значение поля задания или текст ошибки."""
    if field == "title":
        return raw, None if 1 <= len(raw) <= 64 else "⚠️ Текст кнопки — от 1 до 64 символов"
    if field == "reward":
        value = parse_stars(raw, 10_000)
        return value, None if value else "⚠️ Нужно число звёзд больше 0, например <code>2</code> или <code>0.25</code>"
    if field == "url":
        return raw, None if URL_RE.match(raw) else "⚠️ Нужна ссылка вида <code>https://t.me/…</code>"
    if not raw.isdigit() or int(raw) > 10_000_000:
        return None, "⚠️ Нужно целое число, <code>0</code> — без лимита"
    return int(raw), None


@router.message(Input.tk_field, F.text)
async def on_field(message: Message, state: FSMContext, bot: Bot, db: Database) -> None:
    data = await state.get_data()
    value, error = parse_field(data["field"], message.text.strip())
    if error:
        await message.answer(error)
        return
    await drop_prompt(message, state)
    await state.clear()
    await db.update_task(data["task_id"], **{data["field"]: value})
    await show(message, *await card_screen(bot, db, data["task_id"]))


# ---------- новое задание ----------

@router.callback_query(A.filter((F.s == "tk") & (F.a == "new")))
async def cb_new(call: CallbackQuery) -> None:
    await show(call, "➕ <b>Новое задание</b>\n\nЧто нужно сделать пользователю?\n\n"
                     "📢 <b>Подписка</b> — подписаться на канал (проверка автоматически)\n"
                     "🚀 <b>Буст</b> — отдать буст каналу, нужен Telegram Premium (проверка автоматически)\n"
                     "🔗 <b>Ссылка</b> — перейти по ссылке: бот, сайт, пост (без проверки)\n\n"
                     "<i>Для подписки и буста бот должен быть админом канала.</i>", kb(
        [btn(KINDS["sub"], "tk", "kind", v="sub"), btn(KINDS["boost"], "tk", "kind", v="boost")],
        [btn(KINDS["link"], "tk", "kind", v="link")],
        back("tk", text="✖️ Отмена"),
    ))


@router.callback_query(A.filter((F.s == "tk") & (F.a == "kind") & F.v.in_(KINDS)))
async def cb_kind(call: CallbackQuery, callback_data: A, state: FSMContext) -> None:
    if callback_data.v == "link":
        await prompt(call, state, Input.tk_url, "🔗 Пришлите ссылку, по которой нужно перейти (https://…):",
                     back("tk", text="✖️ Отмена"), kind="link")
        return
    await state.set_state(Input.tk_channel)
    await state.update_data(kind=callback_data.v)
    try:
        await call.message.delete()
    except TelegramAPIError:
        pass
    action = "подписаться" if callback_data.v == "sub" else "отдать буст"
    await call.message.answer(
        f"📢 <b>На какой канал нужно {action}?</b>\n\n"
        "1️⃣ Добавьте бота администратором в канал — иначе он не сможет проверить задание.\n"
        "2️⃣ Нажмите «📢 Выбрать канал» внизу 👇\n\n"
        "Или отправьте <code>@username</code>, ID канала — либо перешлите любой пост из канала.",
        reply_markup=pick_chat_keyboard(),
    )


async def ask_reward(message: Message, state: FSMContext) -> None:
    await state.set_state(Input.tk_reward)
    msg = await message.answer("💰 <b>Сколько звёзд начислить за выполнение?</b>\n\n"
                               "Например <code>2</code> или <code>0.25</code>.",
                               reply_markup=kb(back("tk", text="✖️ Отмена")))
    await state.update_data(prompt_id=msg.message_id)


@router.message(Input.tk_channel, F.text == CANCEL)
async def on_channel_cancel(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    await message.answer("Отменено", reply_markup=ReplyKeyboardRemove())
    await show(message, *await list_screen(db))


@router.message(Input.tk_channel)
async def on_channel(message: Message, state: FSMContext, bot: Bot) -> None:
    target = extract_target(message)
    if target is None:
        await message.answer("🤔 Не понял. Нажмите «📢 Выбрать канал», пришлите @username, ID "
                             "или перешлите пост из канала.")
        return
    try:
        chat = await bot.get_chat(target)
        me = await bot.get_chat_member(chat.id, bot.id)
    except TelegramAPIError:
        await message.answer("❌ Не нашёл канал. Убедитесь, что бот добавлен туда администратором, и повторите.")
        return
    if chat.type == "private":
        await message.answer("❌ Это личный чат, а нужен канал или группа.")
        return
    if me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        await message.answer(f"⚠️ Бот не администратор в «{esc(chat.title)}» — он не сможет проверить задание. "
                             "Добавьте его админом и повторите.")
        return

    kind = (await state.get_data())["kind"]
    if kind == "boost":
        url = boost_url(chat.username, chat.id)
    else:
        url = f"https://t.me/{chat.username}" if chat.username else chat.invite_link
        if not url:
            try:
                url = (await bot.create_chat_invite_link(chat.id, name="Задание")).invite_link
            except TelegramAPIError:
                await message.answer("⚠️ Не удалось получить ссылку-приглашение. "
                                     "Дайте боту право «Пригласительные ссылки» и повторите.")
                return
    await state.update_data(chat_id=chat.id, chat_title=chat.title or str(chat.id), url=url)
    await message.answer(f"✅ Канал «{esc(chat.title)}»", reply_markup=ReplyKeyboardRemove())
    await ask_reward(message, state)


@router.message(Input.tk_url, F.text)
async def on_url(message: Message, state: FSMContext) -> None:
    url = message.text.strip()
    if not URL_RE.match(url):
        await message.answer("⚠️ Нужна ссылка вида <code>https://t.me/…</code>")
        return
    await drop_prompt(message, state)
    await state.update_data(url=url)
    await ask_reward(message, state)


@router.message(Input.tk_reward, F.text)
async def on_reward(message: Message, state: FSMContext) -> None:
    reward = parse_stars(message.text, 10_000)
    if not reward:
        await message.answer("⚠️ Нужно число звёзд больше 0, например <code>2</code> или <code>0.25</code>")
        return
    data = await drop_prompt(message, state)
    title = DEFAULT_TITLES[data["kind"]].format(r=fmt_stars(reward))
    await state.set_state(Input.tk_title)
    msg = await message.answer(
        "✏️ <b>Текст кнопки задания</b> (до 64 символов, можно с эмодзи)\n\n"
        f"Пришлите свой или оставьте «{esc(title)}».",
        reply_markup=kb([btn(f"✅ «{title}»", "tk", "title_def", style="success")], back("tk", text="✖️ Отмена")),
    )
    await state.update_data(reward=reward, default_title=title, prompt_id=msg.message_id)


async def create(event: Message | CallbackQuery, state: FSMContext, bot: Bot, db: Database, title: str) -> None:
    data = await state.get_data()
    await state.clear()
    task_id = await db.create_task(data["kind"], title, data["reward"], data.get("url"),
                                   data.get("chat_id"), data.get("chat_title"))
    await show(event, *await card_screen(bot, db, task_id))


@router.callback_query(A.filter((F.s == "tk") & (F.a == "title_def")), Input.tk_title, flags=KEEP)
async def cb_title_default(call: CallbackQuery, callback_answer: CallbackAnswer, state: FSMContext, bot: Bot,
                           db: Database) -> None:
    callback_answer.text = "✅ Задание создано"
    await create(call, state, bot, db, (await state.get_data())["default_title"])


@router.message(Input.tk_title, F.text)
async def on_title(message: Message, state: FSMContext, bot: Bot, db: Database) -> None:
    title = message.text.strip()
    if not 1 <= len(title) <= 64:
        await message.answer("⚠️ Текст кнопки — от 1 до 64 символов")
        return
    await drop_prompt(message, state)
    await message.answer("✅ Задание создано")
    await create(message, state, bot, db, title)


@router.callback_query(A.filter((F.s == "tk") & (F.a == "title_def")))
async def cb_title_expired(call: CallbackQuery, db: Database) -> None:
    await show(call, *await list_screen(db))

