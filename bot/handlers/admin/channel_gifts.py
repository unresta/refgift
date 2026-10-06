"""Отправка подарков Telegram на каналы за звёзды бота."""
import json
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.types import (CallbackQuery, KeyboardButton, KeyboardButtonRequestChat, Message, ReplyKeyboardMarkup,
                           ReplyKeyboardRemove)
from aiogram.utils.callback_answer import CallbackAnswer

from bot.callbacks import A
from bot.database import Database
from bot.handlers.admin.channels import extract_target
from bot.handlers.admin.common import Btn, Input, back, btn, drop_prompt, kb, prompt
from bot.handlers.admin.home import star_balance, topup_button
from bot.services.gifts import GiftCatalog, gift_emoji
from bot.settings import Settings
from bot.utils import esc, fmt_num, show

log = logging.getLogger(__name__)

router = Router(name="admin_channel_gifts")

CANCEL = "❌ Отмена"
MAX_QTY = 20
RECENT = 5
_sending: set[int] = set()  # админы, у которых сейчас идёт отправка — защита от двойного нажатия


def pick_channel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="@username, ссылка или ID канала",
        keyboard=[
            [KeyboardButton(text="📢 Выбрать канал", request_chat=KeyboardButtonRequestChat(
                request_id=10, chat_is_channel=True, request_title=True, request_username=True))],
            [KeyboardButton(text=CANCEL)],
        ],
    )


def recent(settings: Settings) -> list[tuple[int, str]]:
    try:
        return [(int(cid), str(title)) for cid, title in json.loads(settings.get("cgift_recent"))]
    except (ValueError, TypeError):
        return []


async def remember(settings: Settings, chat_id: int, title: str) -> None:
    items = [(chat_id, title)] + [r for r in recent(settings) if r[0] != chat_id]
    await settings.set("cgift_recent", json.dumps(items[:RECENT], ensure_ascii=False))


async def chat_title(bot: Bot, settings: Settings, chat_id: int) -> str:
    try:
        chat = await bot.get_chat(chat_id)
        return chat.title or str(chat_id)
    except TelegramAPIError:
        return next((t for cid, t in recent(settings) if cid == chat_id), str(chat_id))


async def targets_screen(db: Database, settings: Settings):
    seen: set[int] = set()
    rows = []
    for cid, title in recent(settings) + [(ch["chat_id"], ch["title"]) for ch in await db.channels()]:
        if cid in seen:
            continue
        seen.add(cid)
        rows.append([btn(f"📢 {title}", "cgift", "gifts", id=cid)])
    text = ("🎁 <b>Подарок на канал</b>\n\n"
            "Бот отправит подарок Telegram на канал за звёзды своего баланса — "
            "он появится в профиле канала.\n\n"
            "Выберите канал из списка или укажите другой.")
    rows.append([btn("➕ Другой канал", "cgift", "add", style="success")])
    rows.append([btn("📦 Пачкой на несколько каналов", "cgb", style="primary")])
    rows.append(back())
    return text, kb(*rows)


async def gifts_screen(bot: Bot, settings: Settings, catalog: GiftCatalog, chat_id: int):
    title = await chat_title(bot, settings, chat_id)
    balance = await star_balance(bot)
    buttons = [btn(f"{gift_emoji(g)} {g.star_count}⭐" + (f" ({g.remaining_count})" if g.remaining_count else ""),
                   "cgift", "pick", id=chat_id, p=1, v=g.id)
               for g in sorted(await catalog.gifts(), key=lambda g: g.star_count)]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append(topup_button(balance, settings, "cgift"))
    rows.append(back("cgift", text="« К каналам"))
    text = (f"🎁 <b>Подарок на канал</b>\n\n📢 Получатель: <b>{esc(title)}</b>\n"
            f"⭐ Баланс бота: <b>{fmt_num(balance) if balance is not None else '—'}</b>\n\n"
            "Выберите подарок. <i>(N) — осталось лимитированных подарков.</i>")
    if not buttons:
        text += "\n\n<i>Не удалось получить список подарков — попробуйте позже.</i>"
    return text, kb(*rows)


async def confirm_screen(bot: Bot, settings: Settings, catalog: GiftCatalog, chat_id: int, gift_id: str, qty: int):
    gift = await catalog.get(gift_id)
    if gift is None:
        return None
    qty = max(1, min(MAX_QTY, qty))
    title = await chat_title(bot, settings, chat_id)
    balance = await star_balance(bot)
    total = gift.star_count * qty
    caption = settings.get("cgift_text")
    lines = [
        "🎁 <b>Подарок на канал</b>\n",
        f"📢 Получатель: <b>{esc(title)}</b>",
        f"🧸 Подарок: {gift_emoji(gift)} · {gift.star_count} ⭐",
        f"🔢 Количество: <b>{qty}</b>",
        f"💰 Итого: <b>{fmt_num(total)}</b> ⭐",
        f"⭐ Баланс бота: <b>{fmt_num(balance) if balance is not None else '—'}</b>",
        f"💬 Подпись: «{caption or '—'}»",
    ]
    if balance is not None and balance < total:
        lines.append("\n⚠️ Звёзд не хватает — пополните баланс или уменьшите количество.")

    def qty_btn(text: str, n: int) -> Btn:
        return btn(text, "cgift", "pick", id=chat_id, p=max(1, min(MAX_QTY, n)), v=gift_id)

    return "\n".join(lines), kb(
        [qty_btn("➖", qty - 1), btn(str(qty), "noop"), qty_btn("➕", qty + 1)],
        [qty_btn("1", 1), qty_btn("5", 5), qty_btn("10", 10)],
        [btn("💬 Изменить подпись", "cgift", "text", id=chat_id, p=qty, v=gift_id)],
        topup_button(balance, settings, "cgift") if balance is not None and balance < total else [],
        [btn(f"🚀 Отправить {qty} × {gift_emoji(gift)} за {fmt_num(total)} ⭐", "cgift", "send",
             id=chat_id, p=qty, v=gift_id, style="success")],
        back("cgift", "gifts", "« К подаркам", id=chat_id),
    )


@router.callback_query(A.filter((F.s == "cgift") & (F.a == "open")))
async def cb_open(call: CallbackQuery, db: Database, settings: Settings) -> None:
    await show(call, *await targets_screen(db, settings))


@router.callback_query(A.filter((F.s == "cgift") & (F.a == "gifts")))
async def cb_gifts(call: CallbackQuery, callback_data: A, bot: Bot, settings: Settings,
                   catalog: GiftCatalog) -> None:
    await show(call, *await gifts_screen(bot, settings, catalog, callback_data.id))


@router.callback_query(A.filter((F.s == "cgift") & (F.a == "pick")))
async def cb_pick(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, bot: Bot,
                  settings: Settings, catalog: GiftCatalog) -> None:
    screen = await confirm_screen(bot, settings, catalog, callback_data.id, callback_data.v, callback_data.p)
    if screen is None:
        callback_answer.text = "Подарок больше недоступен"
        await show(call, *await gifts_screen(bot, settings, catalog, callback_data.id))
        return
    await show(call, *screen)


# ---------- выбор канала ----------

@router.callback_query(A.filter((F.s == "cgift") & (F.a == "add")))
async def cb_add(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Input.cgift_target)
    try:
        await call.message.delete()
    except TelegramAPIError:
        pass
    await call.message.answer(
        "➕ <b>Канал-получатель</b>\n\n"
        "Нажмите «📢 Выбрать канал» внизу 👇\n\n"
        "Или отправьте <code>@username</code>, ссылку, ID канала — либо перешлите любой пост из канала.\n"
        "<i>Быть админом канала боту не нужно.</i>",
        reply_markup=pick_channel_keyboard(),
    )


@router.message(Input.cgift_target, F.text == CANCEL)
async def on_add_cancel(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    await state.clear()
    await message.answer("Отменено", reply_markup=ReplyKeyboardRemove())
    await show(message, *await targets_screen(db, settings))


@router.message(Input.cgift_target)
async def on_add(message: Message, state: FSMContext, bot: Bot, settings: Settings, catalog: GiftCatalog) -> None:
    target = extract_target(message)
    if target is None:
        await message.answer("🤔 Не понял. Нажмите «📢 Выбрать канал», пришлите @username, ID "
                             "или перешлите пост из канала.")
        return

    shared_title = message.chat_shared.title if message.chat_shared else None
    try:
        chat = await bot.get_chat(target)
        chat_id, title, kind = chat.id, chat.title or str(chat.id), chat.type
    except TelegramRetryAfter as e:
        await message.answer(f"⏳ Telegram временно ограничил поиск каналов по юзернейму — попробуйте через "
                             f"{max(1, e.retry_after // 60)} мин., пришлите ID канала или перешлите из него пост.")
        return
    except TelegramAPIError:
        # Приватный канал без бота: get_chat недоступен, но подарок по ID отправить можно.
        if not isinstance(target, int):
            await message.answer("❌ Не нашёл канал. Проверьте @username или пришлите ID канала.")
            return
        chat_id, title, kind = target, shared_title or str(target), "channel"
    if kind != "channel":
        await message.answer("❌ Подарки можно отправлять только на каналы.")
        return

    await state.clear()
    await remember(settings, chat_id, title)
    await message.answer(f"✅ Получатель: «{esc(title)}»", reply_markup=ReplyKeyboardRemove())
    await show(message, *await gifts_screen(bot, settings, catalog, chat_id))


# ---------- подпись ----------

@router.callback_query(A.filter((F.s == "cgift") & (F.a == "text")))
async def cb_text(call: CallbackQuery, callback_data: A, state: FSMContext, settings: Settings) -> None:
    await prompt(call, state, Input.cgift_text,
                 "💬 <b>Подпись к подарку на канал</b>\n\n"
                 f"Сейчас: «{settings.get('cgift_text') or '—'}»\n\n"
                 "Пришлите новый текст (до 128 символов) или «-», чтобы убрать подпись.",
                 back("cgift", "pick", "✖️ Отмена", id=callback_data.id, p=callback_data.p, v=callback_data.v),
                 chat_id=callback_data.id, qty=callback_data.p, gift_id=callback_data.v)


@router.message(Input.cgift_text, F.text)
async def on_text(message: Message, state: FSMContext, bot: Bot, settings: Settings,
                  catalog: GiftCatalog) -> None:
    raw = message.text.strip()
    if len(raw) > 128:
        await message.answer(f"⚠️ Слишком длинно: {len(raw)}/128 символов")
        return
    data = await drop_prompt(message, state)
    await state.clear()
    await settings.set("cgift_text", "" if raw == "-" else esc(raw))
    screen = await confirm_screen(bot, settings, catalog, data["chat_id"], data["gift_id"], data["qty"])
    if screen is None:
        screen = await gifts_screen(bot, settings, catalog, data["chat_id"])
    await show(message, *screen)


# ---------- отправка ----------

@router.callback_query(A.filter((F.s == "cgift") & (F.a == "send")))
async def cb_send(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, bot: Bot,
                  settings: Settings, catalog: GiftCatalog) -> None:
    admin_id = call.from_user.id
    if admin_id in _sending:
        callback_answer.text = "⏳ Уже отправляю…"
        return
    chat_id, gift_id, qty = callback_data.id, callback_data.v, max(1, min(MAX_QTY, callback_data.p))
    gift = await catalog.get(gift_id)
    if gift is None:
        callback_answer.text = "Подарок больше недоступен"
        await show(call, *await gifts_screen(bot, settings, catalog, chat_id))
        return

    _sending.add(admin_id)
    await call.answer("⏳ Отправляю…")  # сразу: отправка нескольких подарков может занять время
    callback_answer.disable()
    try:
        title = await chat_title(bot, settings, chat_id)
        await show(call, f"⏳ Отправляю {qty} × {gift_emoji(gift)} на «{esc(title)}»…")
        sent, error = 0, None
        for _ in range(qty):
            try:
                await bot.send_gift(gift_id=gift.id, chat_id=chat_id, text=settings.get("cgift_text") or None)
            except TelegramAPIError as e:
                log.warning("send_gift(chat %s) failed: %s", chat_id, e)
                error = e.message
                break
            sent += 1
    finally:
        _sending.discard(admin_id)

    log.info("Админ %s отправил %s × %s на канал %s", admin_id, sent, gift.id, chat_id)
    lines = [f"🎁 <b>Подарок на канал</b>\n\n📢 «{esc(title)}»",
             f"✅ Отправлено: <b>{sent}</b> из {qty} {gift_emoji(gift)} ({fmt_num(sent * gift.star_count)} ⭐)"]
    if error:
        lines.append(f"❌ Ошибка: <code>{esc(error)}</code>")
    await show(call, "\n".join(lines), kb(
        [btn("🔁 Ещё раз", "cgift", "pick", id=chat_id, p=qty, v=gift.id)],
        [btn("🎁 Другой подарок", "cgift", "gifts", id=chat_id), btn("📢 Другой канал", "cgift")],
        back(text="« В админку"),
    ))
