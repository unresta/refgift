import json
import re

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.callbacks import A
from bot.database import Database
from bot.handlers.admin.checks import IMAGE_HINT, read_image
from bot.handlers.admin.common import Input, back, btn, drop_prompt, kb, prompt
from bot.services.reminders import MAX_COUNT, MAX_VARIANTS, ReminderService
from bot.settings import DEFAULTS, Settings
from bot.utils import esc, fmt_num, percent, plural, show

router = Router(name="admin_reminders")

INTERVALS = [1, 2, 5, 10, 15, 30, 60, 180]
MAX_TEXT = 1000  # с картинкой подпись ограничена 1024 символами


def minutes(n: int) -> str:
    if n % 60 == 0 and n >= 60:
        h = n // 60
        return f"{h} {plural(h, 'час', 'часа', 'часов')}"
    return f"{n} мин"


def first_line(text: str, limit: int = 28) -> str:
    plain = re.sub(r"<[^>]+>", "", text).split("\n")[0].strip()
    return plain if len(plain) <= limit else plain[:limit - 1] + "…"


async def main_screen(db: Database, settings: Settings, reminders: ReminderService):
    st = await db.reminder_stats()
    count, step_min = reminders.count, reminders.interval // 60
    schedule = ", ".join(minutes(step_min * i) for i in range(1, count + 1))
    texts = reminders.texts()
    has_photo = bool(settings.get("remind_photo"))

    sent, conv = st["sent"], st["converted"]
    total_conv = sum(conv.values())
    lines = [
        "🔔 <b>Напоминания «Забери подарок»</b>\n",
        "Кто нажал /start, но не прошёл подписку, получает напоминания с кнопкой — "
        "она ведёт на обязательную подписку. Прошёл подписку — напоминания прекращаются.\n",
        f"Статус: {'🟢 <b>включены</b>' if reminders.enabled else '🔴 <b>выключены</b>'}",
        f"Напоминаний: <b>{count}</b> — через {schedule} после старта",
        f"Кнопка: «{esc(settings.get('remind_button'))}»",
        f"Тексты: <b>{len(texts)}</b> {plural(len(texts), 'вариант', 'варианта', 'вариантов')} — "
        "по очереди: 1-е напоминание — вариант 1, 2-е — вариант 2…",
        f"Картинка: {'✅ есть' if has_photo else 'нет'}",
        "",
        "📊 <b>Статистика</b>",
        f"⏳ Ждут напоминания: <b>{fmt_num(st['queued'])}</b>",
        f"📨 Получили хотя бы одно: <b>{fmt_num(st['reached'])}</b>",
        f"✅ Прошли подписку после напоминания: <b>{fmt_num(total_conv)}</b> "
        f"({percent(total_conv, st['reached'])}%)",
    ]
    for step in sorted(sent):
        lines.append(f"   #{step}: отправлено {fmt_num(sent[step])} → подписались {fmt_num(conv.get(step, 0))} "
                     f"({percent(conv.get(step, 0), sent[step])}%)")
    if not reminders.enabled:
        lines.append("\n<i>Новым пользователям напоминания не ставятся, уже запланированные не отправляются.</i>")

    return "\n".join(lines), kb(
        [btn("🔴 Выключить" if reminders.enabled else "🟢 Включить", "rm", "toggle",
             style="danger" if reminders.enabled else "success")],
        [btn(f"🔢 Количество: {count}", "rm", "count"), btn(f"⏱ Интервал: {minutes(step_min)}", "rm", "interval")],
        [btn(f"📝 Тексты · {len(texts)}", "rm", "texts"), btn("🔘 Текст кнопки", "rm", "button")],
        [btn("🖼 Заменить картинку" if has_photo else "🖼 Добавить картинку", "rm", "photo"),
         btn("👁 Превью всех", "rm", "preview")],
        [btn("🗑 Убрать картинку", "rm", "nophoto")] if has_photo else [],
        [btn("🔄 Обновить", "rm")],
        back(),
    )


@router.callback_query(A.filter((F.s == "rm") & (F.a == "open")))
async def cb_open(call: CallbackQuery, db: Database, settings: Settings, reminders: ReminderService) -> None:
    await show(call, *await main_screen(db, settings, reminders))


@router.callback_query(A.filter((F.s == "rm") & F.a.in_({"toggle", "nophoto"})))
async def cb_simple(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                    settings: Settings, reminders: ReminderService) -> None:
    if callback_data.a == "toggle":
        on = await settings.toggle("remind_enabled")
        callback_answer.text = "🟢 Напоминания включены" if on else "🔴 Напоминания выключены"
    else:
        await settings.set("remind_photo", "")
        callback_answer.text = "🗑 Картинка убрана"
    await show(call, *await main_screen(db, settings, reminders))


# ---------- количество и интервал ----------

@router.callback_query(A.filter((F.s == "rm") & (F.a == "count")))
async def cb_count(call: CallbackQuery, callback_data: A, settings: Settings, reminders: ReminderService) -> None:
    if callback_data.id:
        await settings.set("remind_count", max(1, min(MAX_COUNT, callback_data.id)))
    count = reminders.count
    buttons = [btn(f"{'• ' if n == count else ''}{n}", "rm", "count", id=n) for n in range(1, MAX_COUNT + 1)]
    await show(call, f"🔢 <b>Сколько напоминаний отправлять?</b>\n\nСейчас: <b>{count}</b>\n\n"
                     "<i>Применяется сразу, в том числе к уже запущенным цепочкам.</i>",
               kb(buttons[:5], buttons[5:], back("rm")))


@router.callback_query(A.filter((F.s == "rm") & (F.a == "interval")))
async def cb_interval(call: CallbackQuery, callback_data: A, settings: Settings,
                      reminders: ReminderService) -> None:
    if callback_data.id:
        await settings.set("remind_interval", max(1, min(1440, callback_data.id)))
    cur = reminders.interval // 60
    buttons = [btn(f"{'• ' if n == cur else ''}{minutes(n)}", "rm", "interval", id=n) for n in INTERVALS]
    await show(call, f"⏱ <b>Интервал между напоминаниями</b>\n\nСейчас: <b>{minutes(cur)}</b> — "
                     "столько же проходит от /start до первого напоминания.\n\n"
                     "<i>Новый интервал действует для следующих отправок.</i>",
               kb(buttons[:4], buttons[4:], [btn("✏️ Своё значение", "rm", "interval_in")], back("rm")))


@router.callback_query(A.filter((F.s == "rm") & (F.a == "interval_in")))
async def cb_interval_input(call: CallbackQuery, state: FSMContext) -> None:
    await prompt(call, state, Input.remind_interval, "✏️ Пришлите интервал в минутах — от 1 до 1440:",
                 back("rm", "interval", "✖️ Отмена"))


@router.message(Input.remind_interval, F.text)
async def on_interval(message: Message, state: FSMContext, db: Database, settings: Settings,
                      reminders: ReminderService) -> None:
    raw = message.text.strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 1440:
        await message.answer("⚠️ Нужно целое число минут от 1 до 1440")
        return
    await drop_prompt(message, state)
    await state.clear()
    await settings.set("remind_interval", int(raw))
    await show(message, *await main_screen(db, settings, reminders))


# ---------- кнопка ----------

@router.callback_query(A.filter((F.s == "rm") & (F.a == "button")))
async def cb_button(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await prompt(call, state, Input.remind_button,
                 f"🔘 <b>Текст кнопки</b>\n\nСейчас: «{esc(settings.get('remind_button'))}»\n\n"
                 "Пришлите новый текст (до 40 символов) или «-», чтобы вернуть стандартный.",
                 back("rm", text="✖️ Отмена"))


@router.message(Input.remind_button, F.text)
async def on_button(message: Message, state: FSMContext, db: Database, settings: Settings,
                    reminders: ReminderService) -> None:
    raw = message.text.strip()
    if len(raw) > 40:
        await message.answer(f"⚠️ Слишком длинно: {len(raw)}/40")
        return
    await drop_prompt(message, state)
    await state.clear()
    await settings.set("remind_button", DEFAULTS["remind_button"] if raw == "-" else raw)
    await show(message, *await main_screen(db, settings, reminders))


# ---------- картинка ----------

@router.callback_query(A.filter((F.s == "rm") & (F.a == "photo")))
async def cb_photo(call: CallbackQuery, state: FSMContext) -> None:
    await prompt(call, state, Input.remind_photo,
                 "🖼 <b>Картинка напоминаний</b>\n\nБудет прикреплена ко всем напоминаниям.\n\n" + IMAGE_HINT,
                 back("rm", text="✖️ Отмена"))


@router.message(Input.remind_photo)
async def on_photo(message: Message, state: FSMContext, bot: Bot, db: Database, settings: Settings,
                   reminders: ReminderService) -> None:
    file_id = await read_image(message, bot)
    if not file_id:
        return
    await drop_prompt(message, state)
    await state.clear()
    await settings.set("remind_photo", file_id)
    await message.answer("✅ Картинка сохранена")
    await show(message, *await main_screen(db, settings, reminders))


# ---------- превью ----------

@router.callback_query(A.filter((F.s == "rm") & (F.a == "preview")))
async def cb_preview(call: CallbackQuery, reminders: ReminderService) -> None:
    for step in range(1, reminders.count + 1):
        await call.message.answer(f"👁 <b>Напоминание #{step}</b> — через "
                                  f"{minutes(reminders.interval // 60 * step)} после старта:")
        await reminders.send(call.from_user.id, step, call.from_user.full_name)
    await call.message.answer("🔔 Вернуться", reply_markup=kb([btn("🔔 К напоминаниям", "rm")]))


# ---------- тексты ----------

def texts_screen(reminders: ReminderService):
    texts = reminders.texts()
    count = reminders.count
    rows = [[btn(f"{i}. {first_line(t)}", "rm", "text", id=i)] for i, t in enumerate(texts, 1)]
    if len(texts) < MAX_VARIANTS:
        rows.append([btn("➕ Добавить вариант", "rm", "text_add", style="success")])
    rows.append([btn("↩️ Вернуть стандартные", "rm", "text_reset")])
    rows.append(back("rm"))
    note = ""
    if len(texts) < count:
        note = f"\n\n<i>Вариантов меньше, чем напоминаний ({count}) — тексты пойдут по кругу.</i>"
    elif len(texts) > count:
        note = f"\n\n<i>Используются первые {count} — по числу напоминаний.</i>"
    return ("📝 <b>Тексты напоминаний</b>\n\n"
            "Напоминание №1 берёт вариант 1, №2 — вариант 2 и так далее.\n"
            "Переменные: <code>{name}</code> — имя, <code>{gift}</code> — эмодзи подарка." + note), kb(*rows)


def text_card(reminders: ReminderService, index: int):
    texts = reminders.texts()
    text, _ = reminders.render(index, "Иван")
    rows = [[btn("✏️ Изменить", "rm", "text_edit", id=index, style="primary")]]
    if len(texts) > 1:
        rows[0].append(btn("🗑 Удалить", "rm", "text_del", id=index, style="danger"))
    rows.append(back("rm", "texts", "« К текстам"))
    return f"📝 <b>Вариант {index}</b>\n━━━━━━━━━━ предпросмотр ━━━━━━━━━━\n\n{text}", kb(*rows)


@router.callback_query(A.filter((F.s == "rm") & (F.a == "texts")))
async def cb_texts(call: CallbackQuery, reminders: ReminderService) -> None:
    await show(call, *texts_screen(reminders))


@router.callback_query(A.filter((F.s == "rm") & (F.a == "text")))
async def cb_text(call: CallbackQuery, callback_data: A, reminders: ReminderService) -> None:
    if not 1 <= callback_data.id <= len(reminders.texts()):
        await show(call, *texts_screen(reminders))
        return
    await show(call, *text_card(reminders, callback_data.id))


@router.callback_query(A.filter((F.s == "rm") & (F.a == "text_del")))
async def cb_text_delete(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer,
                         reminders: ReminderService) -> None:
    texts = reminders.texts()
    if len(texts) > 1 and 1 <= callback_data.id <= len(texts):
        texts.pop(callback_data.id - 1)
        await reminders.set_texts(texts)
        callback_answer.text = "🗑 Вариант удалён"
    await show(call, *texts_screen(reminders))


@router.callback_query(A.filter((F.s == "rm") & (F.a == "text_reset")))
async def cb_text_reset(call: CallbackQuery, callback_answer: CallbackAnswer, reminders: ReminderService) -> None:
    await reminders.set_texts(json.loads(DEFAULTS["remind_texts"]))
    callback_answer.text = "↩️ Стандартные тексты восстановлены"
    await show(call, *texts_screen(reminders))


@router.callback_query(A.filter((F.s == "rm") & F.a.in_({"text_edit", "text_add"})))
async def cb_text_edit(call: CallbackQuery, callback_data: A, state: FSMContext) -> None:
    index = callback_data.id if callback_data.a == "text_edit" else 0
    title = f"✏️ <b>Вариант {index}</b>" if index else "➕ <b>Новый вариант напоминания</b>"
    cancel = back("rm", "text", "✖️ Отмена", id=index) if index else back("rm", "texts", "✖️ Отмена")
    await prompt(call, state, Input.remind_text,
                 f"{title}\n\nПришлите текст с обычным форматированием Telegram — жирный, курсив, "
                 "эмодзи сохранятся.\nПеременные: <code>{name}</code>, <code>{gift}</code>. "
                 f"До {MAX_TEXT} символов.", cancel, index=index)


@router.message(Input.remind_text, F.text)
async def on_text(message: Message, state: FSMContext, reminders: ReminderService) -> None:
    if len(message.text) > MAX_TEXT:
        await message.answer(f"⚠️ Слишком длинно: {len(message.text)}/{MAX_TEXT}")
        return
    data = await drop_prompt(message, state)
    await state.clear()
    texts = reminders.texts()
    index = data.get("index", 0)
    if 1 <= index <= len(texts):
        texts[index - 1] = message.html_text
    else:
        texts.append(message.html_text)
        index = len(texts)
    await reminders.set_texts(texts)
    await message.answer("✅ Сохранено")
    await show(message, *text_card(reminders, min(index, len(reminders.texts()))))
