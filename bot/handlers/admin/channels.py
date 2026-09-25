import re

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import (CallbackQuery, ChatAdministratorRights, KeyboardButton, KeyboardButtonRequestChat,
                           Message, MessageOriginChannel, MessageOriginChat, ReplyKeyboardMarkup,
                           ReplyKeyboardRemove)
from aiogram.utils.callback_answer import CallbackAnswer
from aiosqlite import Row

from bot.callbacks import A
from bot.config import Config
from bot.database import Database
from bot.handlers.admin.common import Input, back, btn, drop_prompt, kb, prompt
from bot.services.subscription import SubscriptionService
from bot.utils import esc, fmt_dt, fmt_num, show
from bot.views import channel_url

router = Router(name="admin_channels")

CANCEL = "❌ Отмена"
LINK_RE = re.compile(r"^(https?://)?(t\.me|telegram\.me)/(joinchat/|\+)?[\w\-]+/?$", re.I)


def _rights() -> ChatAdministratorRights:
    flags = {name: False for name, f in ChatAdministratorRights.model_fields.items() if f.is_required()}
    flags.update(can_invite_users=True)
    return ChatAdministratorRights(**flags)


def pick_chat_keyboard() -> ReplyKeyboardMarkup:
    rights = _rights()
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="@username, ссылка или ID",
        keyboard=[
            [KeyboardButton(text="📢 Выбрать канал", request_chat=KeyboardButtonRequestChat(
                request_id=1, chat_is_channel=True, request_title=True, request_username=True,
                user_administrator_rights=rights, bot_administrator_rights=rights)),
             KeyboardButton(text="👥 Выбрать группу", request_chat=KeyboardButtonRequestChat(
                 request_id=2, chat_is_channel=False, request_title=True, request_username=True,
                 user_administrator_rights=rights, bot_administrator_rights=rights))],
            [KeyboardButton(text=CANCEL)],
        ],
    )


async def list_screen(db: Database, subs: SubscriptionService):
    channels = await db.channels()
    text = ("📢 <b>Обязательная подписка</b>\n\n"
            "Пользователь должен подписаться на все включённые каналы — только после этого "
            "он видит меню и засчитывается пригласившему.\n\n"
            "🟢 проверяется · 🔴 выключен · ⚠️ проблема с правами бота")
    if not channels:
        text += "\n\n<i>Каналов пока нет — подписка не требуется.</i>"
    rows = []
    for ch in channels:
        icon = "🟢" if ch["is_active"] else "🔴"
        warn = " ⚠️" if ch["chat_id"] in subs.broken else ""
        rows.append([btn(f"{icon} {ch['title']}{warn}", "ch", "card", id=ch["chat_id"])])
    rows.append([btn("➕ Добавить канал", "ch", "add", style="success")])
    rows.append(back())
    return text, kb(*rows)


async def card_screen(bot: Bot, ch: Row, config: Config):
    try:
        members = fmt_num(await bot.get_chat_member_count(ch["chat_id"]))
    except TelegramAPIError:
        members = "—"
    try:
        me = await bot.get_chat_member(ch["chat_id"], bot.id)
        bot_status = ("✅ администратор" if me.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
                      else "⚠️ не администратор — проверка не работает")
    except TelegramAPIError:
        bot_status = "⚠️ нет доступа к чату"

    url = channel_url(ch)
    text = (
        f"📢 <b>{esc(ch['title'])}</b>\n\n"
        f"🆔 <code>{ch['chat_id']}</code>\n"
        f"🔗 {esc(url) if url else '—'}\n"
        f"👥 Участников: <b>{members}</b>\n"
        f"🤖 Бот: {bot_status}\n"
        f"📋 Проверка: {'🟢 включена' if ch['is_active'] else '🔴 выключена'}\n"
        f"📅 Добавлен: {fmt_dt(ch['created_at'], config.tz)}"
    )
    cid = ch["chat_id"]
    return text, kb(
        [btn("🔴 Выключить" if ch["is_active"] else "🟢 Включить", "ch", "toggle", id=cid),
         btn("✏️ Своя ссылка", "ch", "link", id=cid)],
        [btn("⬆️ Выше в списке", "ch", "up", id=cid), btn("🔄 Новая ссылка", "ch", "newlink", id=cid)],
        [btn("🗑 Удалить", "ch", "del", id=cid, style="danger")],
        back("ch", text="« К каналам"),
    )


@router.callback_query(A.filter((F.s == "ch") & (F.a == "open")))
async def cb_list(call: CallbackQuery, db: Database, subs: SubscriptionService) -> None:
    await show(call, *await list_screen(db, subs))


@router.callback_query(A.filter((F.s == "ch") & (F.a == "card")))
async def cb_card(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, bot: Bot,
                  db: Database, config: Config, subs: SubscriptionService) -> None:
    ch = await db.get_channel(callback_data.id)
    if not ch:
        callback_answer.text = "Канал не найден"
        await show(call, *await list_screen(db, subs))
        return
    await show(call, *await card_screen(bot, ch, config))


@router.callback_query(A.filter((F.s == "ch") & F.a.in_({"toggle", "up", "newlink"})))
async def cb_channel_action(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, bot: Bot,
                            db: Database, config: Config, subs: SubscriptionService) -> None:
    cid = callback_data.id
    if callback_data.a == "toggle":
        await db.toggle_channel(cid)
        subs.reset_cache()
        callback_answer.text = "Готово"
    elif callback_data.a == "up":
        await db.move_channel_up(cid)
        callback_answer.text = "⬆️ Перемещён"
    elif callback_data.a == "newlink":
        try:
            link = await bot.create_chat_invite_link(cid, name="Реферальный бот")
            await db.set_channel_link(cid, link.invite_link)
            callback_answer.text = "🔄 Ссылка обновлена"
        except TelegramAPIError as e:
            callback_answer.text = f"Не удалось: {e.message}"[:200]
            callback_answer.show_alert = True
    ch = await db.get_channel(cid)
    if ch:
        await show(call, *await card_screen(bot, ch, config))


@router.callback_query(A.filter((F.s == "ch") & (F.a == "del")))
async def cb_delete_confirm(call: CallbackQuery, callback_data: A, db: Database) -> None:
    ch = await db.get_channel(callback_data.id)
    if not ch:
        return
    await show(call, f"🗑 Удалить канал <b>{esc(ch['title'])}</b> из обязательной подписки?\n\n"
                     "Уже засчитанные рефералы останутся.", kb(
        [btn("🗑 Да, удалить", "ch", "del_ok", id=ch["chat_id"], style="danger")],
        back("ch", "card", "« Отмена", id=ch["chat_id"]),
    ))


@router.callback_query(A.filter((F.s == "ch") & (F.a == "del_ok")))
async def cb_delete(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                    subs: SubscriptionService) -> None:
    await db.delete_channel(callback_data.id)
    subs.reset_cache()
    subs.broken.pop(callback_data.id, None)
    callback_answer.text = "🗑 Канал удалён"
    await show(call, *await list_screen(db, subs))


# ---------- своя ссылка ----------

@router.callback_query(A.filter((F.s == "ch") & (F.a == "link")))
async def cb_link(call: CallbackQuery, callback_data: A, state: FSMContext) -> None:
    await prompt(call, state, Input.channel_link,
                 "✏️ <b>Своя ссылка на канал</b>\n\nОтправьте ссылку вида <code>https://t.me/+AbCdEf</code> "
                 "или <code>https://t.me/channel</code>.\n\n"
                 "Удобно для ссылок с заявками на вступление или для отслеживания переходов.",
                 back("ch", "card", "✖️ Отмена", id=callback_data.id), chat_id=callback_data.id)


@router.message(Input.channel_link, F.text)
async def on_link(message: Message, state: FSMContext, bot: Bot, db: Database, config: Config) -> None:
    link = message.text.strip()
    if not LINK_RE.match(link):
        await message.answer("⚠️ Это не похоже на ссылку Telegram. Пример: <code>https://t.me/+AbCdEf</code>")
        return
    if not link.startswith("http"):
        link = "https://" + link
    data = await drop_prompt(message, state)
    await state.clear()
    await db.set_channel_link(data["chat_id"], link)
    ch = await db.get_channel(data["chat_id"])
    if ch:
        await message.answer("✅ Ссылка сохранена")
        await show(message, *await card_screen(bot, ch, config))


# ---------- добавление ----------

@router.callback_query(A.filter((F.s == "ch") & (F.a == "add")))
async def cb_add(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Input.channel)
    try:
        await call.message.delete()
    except TelegramAPIError:
        pass
    await call.message.answer(
        "➕ <b>Добавление канала</b>\n\n"
        "1️⃣ Добавьте бота администратором в канал или группу.\n"
        "   Для приватного канала нужно право «Пригласительные ссылки».\n"
        "2️⃣ Нажмите «📢 Выбрать канал» внизу 👇\n\n"
        "Или отправьте <code>@username</code>, ссылку, ID канала — либо перешлите любой пост из канала.",
        reply_markup=pick_chat_keyboard(),
    )


def _extract_target(message: Message) -> int | str | None:
    if message.chat_shared:
        return message.chat_shared.chat_id
    origin = message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        return origin.chat.id
    if isinstance(origin, MessageOriginChat):
        return origin.sender_chat.id
    if message.text:
        raw = message.text.strip()
        if raw.lstrip("-").isdigit():
            return int(raw)
        m = re.match(r"^(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z]\w{3,})/?$", raw)
        if m:
            return "@" + m.group(1)
        if re.match(r"^@[A-Za-z]\w{3,}$", raw):
            return raw
    return None


@router.message(Input.channel, F.text == CANCEL)
async def on_add_cancel(message: Message, state: FSMContext, db: Database, subs: SubscriptionService) -> None:
    await state.clear()
    await message.answer("Отменено", reply_markup=ReplyKeyboardRemove())
    await show(message, *await list_screen(db, subs))


@router.message(Input.channel)
async def on_add(message: Message, state: FSMContext, bot: Bot, db: Database, config: Config,
                 subs: SubscriptionService) -> None:
    target = _extract_target(message)
    if target is None:
        if message.text and ("/+" in message.text or "joinchat" in message.text):
            await message.answer("🔒 По ссылке-приглашению бот не может найти приватный канал.\n"
                                 "Нажмите «📢 Выбрать канал», пришлите ID или перешлите пост из канала.")
        else:
            await message.answer("🤔 Не понял. Нажмите «📢 Выбрать канал», пришлите @username, ID "
                                 "или перешлите пост из канала.")
        return

    try:
        chat = await bot.get_chat(target)
    except TelegramAPIError:
        await message.answer("❌ Не нашёл чат. Убедитесь, что бот добавлен туда администратором, и попробуйте ещё раз.")
        return
    if chat.type == "private":
        await message.answer("❌ Это личный чат, а нужен канал или группа.")
        return

    try:
        me = await bot.get_chat_member(chat.id, bot.id)
    except TelegramAPIError:
        me = None
    if me is None or me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        await message.answer(f"⚠️ Бот не администратор в «{esc(chat.title)}».\n"
                             "Добавьте его админом и повторите — иначе проверить подписку невозможно.")
        return

    link = f"https://t.me/{chat.username}" if chat.username else chat.invite_link
    if not link:
        try:
            link = (await bot.create_chat_invite_link(chat.id, name="Реферальный бот")).invite_link
        except TelegramAPIError:
            await message.answer("⚠️ Не удалось получить ссылку-приглашение. "
                                 "Дайте боту право «Пригласительные ссылки» и повторите.")
            return

    await db.add_channel(chat.id, chat.title or str(chat.id), chat.username, link)
    subs.reset_cache()
    subs.broken.pop(chat.id, None)
    await state.clear()
    await message.answer(f"✅ «{esc(chat.title)}» добавлен в обязательную подписку",
                         reply_markup=ReplyKeyboardRemove())
    ch = await db.get_channel(chat.id)
    assert ch is not None
    await show(message, *await card_screen(bot, ch, config))
