import asyncio
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.callbacks import A
from bot.database import Database
from bot.handlers.admin.common import Input, back, btn, drop_prompt, kb, prompt
from bot.services.broadcast import RATE_DELAY, Broadcaster, BroadcastStats
from bot.settings import Settings
from bot.utils import fmt_num, now, percent, progress_bar, show

router = Router(name="admin_broadcast")

AUDIENCES = {
    "all": "👥 Все",
    "verified": "✅ Прошли подписку",
    "unverified": "⏳ Не прошли подписку",
    "goal": "🎯 Достигли цели",
    "active": "🔥 Активные за 7 дней",
}
URL_RE = re.compile(r"^(https?://|tg://)\S+$")
KEEP = {"keep_state": True}
_background: set[asyncio.Task] = set()


def mmss(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def progress_text(stats: BroadcastStats) -> str:
    if stats.finished:
        head = "⏹ <b>Рассылка остановлена</b>" if stats.cancelled else "✅ <b>Рассылка завершена</b>"
        timing = f"⏱ Длительность: {mmss(stats.elapsed)}"
    else:
        head = "📨 <b>Рассылка идёт…</b>"
        timing = f"⏱ Прошло: {mmss(stats.elapsed)} · осталось ~{mmss(stats.eta)}"
    pct = percent(stats.done, stats.total)
    return (f"{head}\n\n{progress_bar(stats.done, stats.total)} {pct}%\n"
            f"📬 {fmt_num(stats.done)} из {fmt_num(stats.total)}\n\n"
            f"✅ Доставлено: <b>{fmt_num(stats.sent)}</b>\n"
            f"⛔ Заблокировали бота: <b>{fmt_num(stats.blocked)}</b>\n"
            f"⚠️ Ошибки: <b>{fmt_num(stats.failed)}</b>\n\n{timing}")


def running_kb() -> InlineKeyboardMarkup:
    return kb([btn("⏹ Остановить", "bc", "stop", style="danger")], [btn("🔄 Обновить", "bc")], back())


def parse_buttons(text: str) -> list[list[dict[str, str]]]:
    rows = []
    for line in text.strip().splitlines():
        if not line.strip():
            continue
        row = []
        for part in line.split("||"):
            if "|" not in part:
                raise ValueError(f"Нет разделителя «|»: {part.strip()}")
            title, url = (x.strip() for x in part.rsplit("|", 1))
            if not title or not URL_RE.match(url):
                raise ValueError(f"Неверная кнопка: {part.strip()}")
            row.append({"text": title, "url": url})
        rows.append(row)
    return rows


def build_markup(rows: list[list[dict[str, str]]] | None) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(**b) for b in row] for row in rows])


@router.callback_query(A.filter((F.s == "bc") & (F.a == "open")))
async def cb_open(call: CallbackQuery, state: FSMContext, broadcaster: Broadcaster) -> None:
    if broadcaster.running and broadcaster.stats:
        await show(call, progress_text(broadcaster.stats), running_kb())
        return
    await prompt(call, state, Input.bc_message,
                 "📨 <b>Рассылка — шаг 1 из 3</b>\n\n"
                 "Пришлите сообщение для рассылки: текст, фото, видео, GIF, документ, голосовое или кружок.\n"
                 "Форматирование и премиум-эмодзи сохранятся.\n\n"
                 "<i>Альбомы не поддерживаются — отправьте медиа по одному.</i>",
                 back(text="✖️ Отмена"))


@router.message(Input.bc_message)
async def on_message(message: Message, state: FSMContext) -> None:
    if message.media_group_id:
        await message.answer("⚠️ Альбомы не поддерживаются. Пришлите одно сообщение.")
        return
    await drop_prompt(message, state)
    await state.set_state(Input.bc_buttons)
    msg = await message.answer(
        "🔘 <b>Шаг 2 из 3 — кнопки</b>\n\n"
        "Добавить кнопки-ссылки под сообщением? Пришлите их в формате:\n"
        "<code>Текст кнопки | https://example.com</code>\n\n"
        "Каждая кнопка — с новой строки. Несколько кнопок в один ряд — через <code>||</code>:\n"
        "<code>Канал | https://t.me/a || Чат | https://t.me/b</code>",
        reply_markup=kb([btn("⏭ Без кнопок", "bc", "skip", style="primary")], back(text="✖️ Отмена")),
    )
    await state.update_data(src_chat=message.chat.id, src_msg=message.message_id, prompt_id=msg.message_id,
                            buttons=None)


@router.message(Input.bc_buttons, F.text)
async def on_buttons(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    try:
        rows = parse_buttons(message.text)
    except ValueError as e:
        await message.answer(f"⚠️ {e}\n\nФормат: <code>Текст | https://ссылка</code>")
        return
    await drop_prompt(message, state)
    await state.update_data(buttons=rows)
    await show_audience(message, db, settings)


@router.callback_query(A.filter((F.s == "bc") & (F.a == "skip")), Input.bc_buttons, flags=KEEP)
async def cb_skip(call: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    await state.update_data(buttons=None)
    await show_audience(call, db, settings)


async def show_audience(event: Message | CallbackQuery, db: Database, settings: Settings) -> None:
    since = now() - 7 * 86400
    rows = []
    for key, title in AUDIENCES.items():
        count = await db.audience_count(key, settings.goal, since)
        rows.append([btn(f"{title} · {fmt_num(count)}", "bc", "aud", v=key)])
    rows.append(back(text="✖️ Отмена"))
    await show(event, "🎯 <b>Шаг 3 из 3 — кому отправить?</b>\n\n"
                      "Заблокировавшие бота и забаненные исключаются автоматически.", kb(*rows))


@router.callback_query(A.filter((F.s == "bc") & (F.a == "aud")), Input.bc_buttons, flags=KEEP)
async def cb_audience(call: CallbackQuery, callback_data: A, state: FSMContext, bot: Bot, db: Database,
                      settings: Settings) -> None:
    audience = callback_data.v if callback_data.v in AUDIENCES else "all"
    data = await state.get_data()
    count = await db.audience_count(audience, settings.goal, now() - 7 * 86400)
    await state.update_data(audience=audience)

    try:
        await call.message.delete()
    except TelegramBadRequest:
        pass
    await call.message.answer("👀 <b>Предпросмотр:</b>")
    try:
        await bot.copy_message(call.from_user.id, data["src_chat"], data["src_msg"],
                               reply_markup=build_markup(data.get("buttons")))
    except TelegramAPIError as e:
        await call.message.answer(f"❌ Не удалось показать сообщение: {e.message}")
        await state.clear()
        return

    eta = mmss(count * RATE_DELAY * 1.1)
    await call.message.answer(
        f"📨 <b>Всё готово к запуску</b>\n\n"
        f"🎯 Аудитория: <b>{AUDIENCES[audience]}</b>\n"
        f"👥 Получателей: <b>{fmt_num(count)}</b>\n"
        f"⏱ Займёт примерно: <b>{eta}</b>",
        reply_markup=kb(
            [btn("🚀 Запустить рассылку", "bc", "go", style="success")] if count else [],
            [btn("🎯 Другая аудитория", "bc", "reaud")],
            back(text="✖️ Отмена"),
        ),
    )


@router.callback_query(A.filter((F.s == "bc") & (F.a == "reaud")), Input.bc_buttons, flags=KEEP)
async def cb_reaudience(call: CallbackQuery, db: Database, settings: Settings) -> None:
    await show_audience(call, db, settings)


@router.callback_query(A.filter((F.s == "bc") & (F.a == "go")), Input.bc_buttons, flags=KEEP)
async def cb_go(call: CallbackQuery, state: FSMContext, bot: Bot, db: Database, settings: Settings,
                broadcaster: Broadcaster) -> None:
    data = await state.get_data()
    await state.clear()
    if broadcaster.running:
        await show(call, "⚠️ Уже идёт другая рассылка.", kb(back()))
        return

    ids = await db.audience_ids(data.get("audience", "all"), settings.goal, now() - 7 * 86400)
    stats = broadcaster.start(ids, data["src_chat"], data["src_msg"], build_markup(data.get("buttons")))
    status = await show(call, progress_text(stats), running_kb())
    if status:
        task = asyncio.create_task(_progress_loop(bot, status.chat.id, status.message_id, broadcaster, stats))
        _background.add(task)
        task.add_done_callback(_background.discard)


async def _progress_loop(bot: Bot, chat_id: int, message_id: int, broadcaster: Broadcaster,
                         stats: BroadcastStats) -> None:
    """Единственный «писатель» статус-сообщения: обновляет прогресс и выводит итог."""
    last = ""
    while not broadcaster.is_done():
        await broadcaster.wait(timeout=3)
        text = progress_text(stats)
        if text == last:
            continue
        last = text
        markup = kb(back(text="« В админку")) if stats.finished else running_kb()
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
        except TelegramAPIError:
            pass


@router.callback_query(A.filter((F.s == "bc") & (F.a == "stop")))
async def cb_stop(call: CallbackQuery, broadcaster: Broadcaster) -> None:
    broadcaster.stop()
    if broadcaster.stats:
        await show(call, "⏹ Останавливаю рассылку…\n\n" + progress_text(broadcaster.stats), kb(back()))
