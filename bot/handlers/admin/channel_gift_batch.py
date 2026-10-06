"""Подарки на каналы пачкой: список каналов → подарок → комментарий → отправка в фоне с прогрессом."""
import asyncio
import logging
import re
from dataclasses import dataclass, field

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Gift, InlineKeyboardMarkup, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.callbacks import A
from bot.database import Database
from bot.handlers.admin.channel_gifts import recent
from bot.handlers.admin.channels import extract_target, parse_target
from bot.handlers.admin.common import Input, back, btn, drop_prompt, kb
from bot.handlers.admin.home import star_balance, topup_button
from bot.services.gifts import GiftCatalog, gift_emoji
from bot.settings import Settings
from bot.utils import esc, fmt_num, show

log = logging.getLogger(__name__)

router = Router(name="admin_channel_gift_batch")

BATCH_MAX = 100
COMMENT_MAX = 128
SEND_DELAY = 0.4        # пауза между подарками — чтобы не упереться в лимиты Telegram
PROGRESS_EVERY = 5      # как часто обновлять сообщение с прогрессом
LIST_SHOWN = 30
STOP_ERRORS = ("BALANCE", "STARS")  # звёзд не хватает — остальные подарки тоже не уйдут


@dataclass
class Batch:
    # chat_id → название, в порядке добавления; "@username" — канал, который не удалось проверить (лимит Telegram)
    channels: dict[int | str, str] = field(default_factory=dict)
    gift_id: str = ""
    comment: str | None = None
    running: bool = False
    stop: bool = False


_batches: dict[int, Batch] = {}
_tasks: set[asyncio.Task] = set()


def batch_of(admin_id: int) -> Batch:
    return _batches.setdefault(admin_id, Batch())


async def wait_batches() -> None:
    """Для тестов: дождаться фоновых отправок."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


class Unchecked(Exception):
    """Проверить канал не получилось (лимит запросов) — добавим без проверки."""


async def resolve(bot: Bot, target: int | str) -> tuple[int | str, str] | str:
    """Канал → (chat_id, название) или текст ошибки. Unchecked — Telegram не дал проверить."""
    try:
        chat = await bot.get_chat(target)
    except TelegramRetryAfter:
        raise Unchecked
    except TelegramBadRequest as e:
        # Приватный канал без бота: get_chat недоступен, но подарок по ID отправить можно.
        if isinstance(target, int):
            return target, str(target)
        if "not found" in e.message.lower():
            return "не найден"
        raise Unchecked
    except TelegramAPIError as e:
        log.warning("get_chat(%s): %s", target, e)
        raise Unchecked
    if chat.type != "channel":
        return "это не канал"
    return chat.id, chat.title or str(chat.id)


async def add_targets(bot: Bot, batch: Batch, targets: list[tuple[str, int | str]]) -> tuple[int, list[str], int]:
    """Добавляет каналы в пачку. Возвращает (сколько добавлено, ошибки, сколько без проверки).

    Поиск канала по @username у Telegram сильно ограничен по частоте, поэтому проверяем по одному, а упёршись
    в лимит — добавляем остальные как есть: send_gift принимает @username напрямую.
    """
    added, errors, unchecked, limited = 0, [], 0, False
    for label, target in targets:
        shown = str(target)  # для списка — как прислал админ
        if isinstance(target, str):
            target = target.lower()  # юзернеймы не зависят от регистра
        if target in batch.channels:
            continue
        result: tuple[int | str, str] | str
        if limited:
            result = (target, shown)
        else:
            try:
                result = await resolve(bot, target)
            except Unchecked:
                limited = True
                result = (target, shown)
        if isinstance(result, str):
            errors.append(f"{esc(label)} — {result}")
            continue
        if result[0] in batch.channels:
            continue
        if len(batch.channels) >= BATCH_MAX:
            errors.append(f"{esc(label)} — в пачке уже {BATCH_MAX} каналов")
            continue
        batch.channels[result[0]] = result[1]
        added += 1
        unchecked += limited and isinstance(result[0], str)
    return added, errors, unchecked


# ---------- шаг 1: каналы ----------

def builder_screen(batch: Batch) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["📦 <b>Подарки на каналы пачкой</b>\n"]
    if batch.channels:
        items = list(batch.channels.values())
        lines.append(f"Каналов в пачке: <b>{len(items)}</b>")
        lines += [f"{n}. {esc(title)}" for n, title in enumerate(items[:LIST_SHOWN], 1)]
        if len(items) > LIST_SHOWN:
            lines.append(f"… и ещё {len(items) - LIST_SHOWN}")
        lines.append("")
    lines.append("Пришлите каналы — <code>@username</code>, ссылки <code>t.me/…</code> или ID, "
                 "по одному на строку. Можно несколькими сообщениями или переслать пост из канала.\n"
                 f"<i>До {BATCH_MAX} каналов. Быть админом каналов боту не нужно.</i>")
    rows = [[btn("📢 Все из подписки", "cgb", "subs"), btn("🕘 Недавние", "cgb", "recent")]]
    if batch.channels:
        rows.append([btn("🗑 Очистить", "cgb", "clear")])
        rows.append([btn(f"➡️ Выбрать подарок · {len(batch.channels)} кан.", "cgb", "gifts", style="success")])
    rows.append(back("cgift", text="« К подаркам на канал"))
    return "\n".join(lines), kb(*rows)


async def show_builder(event: CallbackQuery | Message, state: FSMContext, batch: Batch) -> None:
    """Экран пачки ждёт ввода каналов, как подсказка prompt()."""
    await state.set_state(Input.cgb_channels)
    msg = await show(event, *builder_screen(batch))
    await state.update_data(prompt_id=msg.message_id if msg else None)


@router.callback_query(A.filter((F.s == "cgb") & F.a.in_({"open", "clear", "new"})))
async def cb_open(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, state: FSMContext) -> None:
    batch = batch_of(call.from_user.id)
    if callback_data.a in ("clear", "new") and not batch.running:
        batch.channels.clear()
        callback_answer.text = "🗑 Пачка очищена" if callback_data.a == "clear" else None
    await show_builder(call, state, batch)


@router.callback_query(A.filter((F.s == "cgb") & F.a.in_({"subs", "recent"})))
async def cb_quick_add(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, state: FSMContext,
                       bot: Bot, db: Database, settings: Settings) -> None:
    batch = batch_of(call.from_user.id)
    if callback_data.a == "subs":
        targets = [(ch["title"], ch["chat_id"]) for ch in await db.channels()]
    else:
        targets = [(title, cid) for cid, title in recent(settings)]
    added, errors, _ = await add_targets(bot, batch, targets)
    callback_answer.text = (f"➕ Добавлено: {added}" + (f", пропущено: {len(errors)}" if errors else "")
                            if targets else "Список пуст")
    await show_builder(call, state, batch)


@router.message(Input.cgb_channels)
async def on_channels(message: Message, state: FSMContext, bot: Bot) -> None:
    batch = batch_of(message.from_user.id)
    if message.text and not message.forward_origin:
        tokens = [t for t in re.split(r"[\s,;]+", message.text) if t]
        targets = [(t, parse_target(t)) for t in tokens]
    else:
        targets = [("пересланный пост", extract_target(message))]
    bad = [f"{esc(label)} — не похоже на канал" for label, t in targets if t is None]
    added, errors, unchecked = await add_targets(bot, batch, [(label, t) for label, t in targets if t is not None])
    errors = bad + errors

    await drop_prompt(message, state)
    report = f"➕ Добавлено каналов: <b>{added}</b>"
    if unchecked:
        report += (f"\nℹ️ Из них {unchecked} — без проверки: Telegram временно ограничил поиск каналов по "
                   "юзернейму. Если какого-то канала нет, это будет видно в отчёте после отправки.")
    if errors:
        report += f"\n⚠️ Не добавлено: {len(errors)}\n" + "\n".join(errors[:15])
        if len(errors) > 15:
            report += f"\n… и ещё {len(errors) - 15}"
    await message.answer(report)
    await show_builder(message, state, batch)


# ---------- шаг 2: подарок ----------

@router.callback_query(A.filter((F.s == "cgb") & (F.a == "gifts")))
async def cb_gifts(call: CallbackQuery, callback_answer: CallbackAnswer, bot: Bot, settings: Settings,
                   catalog: GiftCatalog, state: FSMContext) -> None:
    batch = batch_of(call.from_user.id)
    if not batch.channels:
        callback_answer.text = "Сначала добавьте каналы"
        await show_builder(call, state, batch)
        return
    n = len(batch.channels)
    balance = await star_balance(bot)
    buttons = [btn(f"{gift_emoji(g)} {g.star_count}⭐ · {fmt_num(g.star_count * n)}", "cgb", "gift", v=g.id)
               for g in sorted(await catalog.gifts(), key=lambda g: g.star_count)]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append(topup_button(balance, settings, "cgift"))
    rows.append(back("cgb", text="« К каналам"))
    await show(call, f"🎁 <b>Какой подарок отправить?</b>\n\nКаналов: <b>{n}</b> — по одному подарку на канал.\n"
                     f"⭐ Баланс бота: <b>{fmt_num(balance) if balance is not None else '—'}</b>\n\n"
                     "<i>На кнопке: цена подарка · итого за всю пачку.</i>", kb(*rows))


# ---------- шаг 3: комментарий ----------

def comment_screen(settings: Settings) -> tuple[str, InlineKeyboardMarkup]:
    last = settings.get("cgift_text")
    rows = []
    if last:
        rows.append([btn(f"💬 Как в прошлый раз: «{last[:30]}»", "cgb", "keep")])
    rows.append([btn("🚫 Без комментария", "cgb", "none")])
    rows.append(back("cgb", "gifts", "« К подаркам"))
    return ("💬 <b>Комментарий к подарку</b>\n\n"
            f"Пришлите текст — до {COMMENT_MAX} символов. Он будет у каждого подарка в пачке."), kb(*rows)


@router.callback_query(A.filter((F.s == "cgb") & (F.a == "gift")))
async def cb_gift(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, state: FSMContext,
                  settings: Settings, catalog: GiftCatalog) -> None:
    if await catalog.get(callback_data.v) is None:
        callback_answer.text = "Подарок больше недоступен"
        return
    batch_of(call.from_user.id).gift_id = callback_data.v
    await state.set_state(Input.cgb_comment)
    msg = await show(call, *comment_screen(settings))
    await state.update_data(prompt_id=msg.message_id if msg else None)


@router.message(Input.cgb_comment, F.text)
async def on_comment(message: Message, state: FSMContext, bot: Bot, settings: Settings,
                     catalog: GiftCatalog) -> None:
    raw = message.text.strip()
    if len(raw) > COMMENT_MAX:
        await message.answer(f"⚠️ Слишком длинно: {len(raw)}/{COMMENT_MAX} символов")
        return
    await drop_prompt(message, state)
    await state.clear()
    batch = batch_of(message.from_user.id)
    batch.comment = esc(raw)
    await settings.set("cgift_text", batch.comment)
    await show(message, *await confirm_screen(bot, batch, catalog))


@router.callback_query(A.filter((F.s == "cgb") & F.a.in_({"keep", "none"})))
async def cb_comment_choice(call: CallbackQuery, callback_data: A, bot: Bot, settings: Settings,
                            catalog: GiftCatalog) -> None:
    batch = batch_of(call.from_user.id)
    batch.comment = (settings.get("cgift_text") or None) if callback_data.a == "keep" else None
    await show(call, *await confirm_screen(bot, batch, catalog))


# ---------- шаг 4: подтверждение и отправка ----------

async def confirm_screen(bot: Bot, batch: Batch, catalog: GiftCatalog) -> tuple[str, InlineKeyboardMarkup]:
    gift = await catalog.get(batch.gift_id)
    if gift is None or not batch.channels:
        return "⚠️ Подарок больше недоступен или пачка пуста — начните заново.", kb(back("cgb", text="« К каналам"))
    n = len(batch.channels)
    total = gift.star_count * n
    balance = await star_balance(bot)
    names = list(batch.channels.values())
    lines = [
        "📦 <b>Проверьте пачку</b>\n",
        f"📢 Каналов: <b>{n}</b> — " + ", ".join(esc(t) for t in names[:10]) + (f" и ещё {n - 10}" if n > 10 else ""),
        f"🎁 Подарок: {gift_emoji(gift)} · {gift.star_count} ⭐ на канал",
        f"💬 Комментарий: {f'«{batch.comment}»' if batch.comment else 'без комментария'}",
        f"💰 Итого: <b>{fmt_num(total)}</b> ⭐",
        f"⭐ Баланс бота: <b>{fmt_num(balance) if balance is not None else '—'}</b>",
    ]
    if balance is not None and balance < total:
        lines.append(f"\n⚠️ Звёзд хватит примерно на {balance // gift.star_count} из {n} — остальные не уйдут. "
                     "Пополните баланс или уберите часть каналов.")
    return "\n".join(lines), kb(
        [btn(f"🚀 Отправить на {n} кан. · {fmt_num(total)} ⭐", "cgb", "send", style="success")],
        [btn("💬 Комментарий", "cgb", "gift", v=gift.id), btn("🎁 Подарок", "cgb", "gifts")],
        [btn("📢 Каналы", "cgb")],
    )


async def send_one(bot: Bot, gift_id: str, chat_id: int, text: str | None) -> str | None:
    for attempt in range(2):
        try:
            await bot.send_gift(gift_id=gift_id, chat_id=chat_id, text=text)
            return None
        except TelegramRetryAfter as e:
            if attempt:
                return e.message
            await asyncio.sleep(e.retry_after)
        except TelegramAPIError as e:
            return e.message
    return None


async def edit(bot: Bot, chat_id: int, message_id: int, text: str, markup: InlineKeyboardMarkup | None) -> None:
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
    except TelegramBadRequest:
        pass


async def run_batch(bot: Bot, admin_id: int, message_id: int, batch: Batch, gift: Gift) -> None:
    items = list(batch.channels.items())
    emoji = gift_emoji(gift)
    sent, failed, stopped = 0, [], ""
    stop_kb = kb([btn("⏹ Остановить", "cgb", "stop", style="danger")])
    try:
        for i, (chat_id, title) in enumerate(items, 1):
            if batch.stop:
                stopped = "остановлено"
                break
            error = await send_one(bot, gift.id, chat_id, batch.comment)
            if error:
                failed.append(f"{esc(title)} — <code>{esc(error)}</code>")
                if any(k in error.upper() for k in STOP_ERRORS):
                    stopped = "не хватает звёзд"
                    break
            else:
                sent += 1
            if i % PROGRESS_EVERY == 0 and i < len(items):
                await edit(bot, admin_id, message_id,
                           f"⏳ Отправляю {emoji}… <b>{i}</b> из {len(items)}\n✅ {sent} · ❌ {len(failed)}", stop_kb)
            await asyncio.sleep(SEND_DELAY)
    finally:
        batch.running = batch.stop = False

    log.info("Админ %s: пачка %s × %s — отправлено %s из %s", admin_id, emoji, gift.id, sent, len(items))
    left = len(items) - sent - len(failed)
    lines = [f"📦 <b>Пачка {'остановлена' if stopped else 'отправлена'}</b>\n",
             f"✅ Отправлено: <b>{sent}</b> из {len(items)} {emoji} ({fmt_num(sent * gift.star_count)} ⭐)"]
    if failed:
        lines.append(f"❌ Ошибок: <b>{len(failed)}</b>")
        lines += failed[:20] + ([f"… и ещё {len(failed) - 20}"] if len(failed) > 20 else [])
    if stopped and left:
        lines.append(f"\n⏹ {stopped.capitalize()} — не отправлено: <b>{left}</b>")
    await edit(bot, admin_id, message_id, "\n".join(lines), kb(
        [btn("🔁 Другой подарок на эти каналы", "cgb", "gifts")],
        [btn("📦 Новая пачка", "cgb", "new"), btn("« Подарок на канал", "cgift")],
    ))


@router.callback_query(A.filter((F.s == "cgb") & (F.a == "send")))
async def cb_send(call: CallbackQuery, callback_answer: CallbackAnswer, bot: Bot, catalog: GiftCatalog) -> None:
    batch = batch_of(call.from_user.id)
    if batch.running:
        callback_answer.text = "⏳ Пачка уже отправляется"
        return
    gift = await catalog.get(batch.gift_id)
    if gift is None or not batch.channels:
        callback_answer.text = "Подарок недоступен или пачка пуста"
        return
    batch.running, batch.stop = True, False
    callback_answer.text = f"🚀 Отправляю на {len(batch.channels)} каналов"
    msg = await show(call, f"⏳ Отправляю {gift_emoji(gift)}… <b>0</b> из {len(batch.channels)}",
                     kb([btn("⏹ Остановить", "cgb", "stop", style="danger")]))
    task = asyncio.create_task(run_batch(bot, call.from_user.id, msg.message_id if msg else call.message.message_id,
                                         batch, gift))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@router.callback_query(A.filter((F.s == "cgb") & (F.a == "stop")))
async def cb_stop(call: CallbackQuery, callback_answer: CallbackAnswer) -> None:
    batch = batch_of(call.from_user.id)
    if batch.running:
        batch.stop = True
        callback_answer.text = "⏹ Останавливаю после текущего подарка"
    else:
        callback_answer.text = "Отправка уже завершена"
