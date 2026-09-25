import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.callbacks import A
from bot.handlers.admin.common import Input, back, btn, drop_prompt, kb, prompt
from bot.settings import TEXTS, Settings
from bot.utils import progress_bar, render_template, show

router = Router(name="admin_texts")

PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def sample_values(settings: Settings) -> dict[str, object]:
    goal = settings.goal
    cur = min(3, goal)
    return {
        "name": "Иван", "goal": goal, "count": cur, "left": goal - cur,
        "progress": f"📊 Твой прогресс: <b>{cur}/{goal}</b>\n{progress_bar(cur, goal)} "
                    f"{round(cur * 100 / goal)}%\nОсталось пригласить: <b>{goal - cur}</b>",
    }


def list_screen(settings: Settings):
    rows = [[btn(f"{'✏️ ' if not settings.is_default(k) else ''}{meta.title}", "tx", "card", v=k)]
            for k, meta in TEXTS.items()]
    rows.append(back())
    return ("📝 <b>Тексты бота</b>\n\n"
            "Выберите текст, чтобы посмотреть, как он выглядит, и изменить.\n"
            "✏️ — текст изменён (не по умолчанию)."), kb(*rows)


def card_screen(settings: Settings, key: str):
    meta = TEXTS[key]
    placeholders = ", ".join(f"<code>{{{p}}}</code>" for p in meta.placeholders)
    preview = render_template(settings.get(key), **sample_values(settings))
    text = (f"📝 <b>{meta.title}</b>\n<i>{meta.hint}</i>\n\n"
            f"Переменные: {placeholders}\n"
            f"━━━━━━━━━━ предпросмотр ━━━━━━━━━━\n\n{preview}")
    rows = [[btn("✏️ Изменить", "tx", "edit", v=key, style="primary")]]
    if not settings.is_default(key):
        rows[0].append(btn("↩️ По умолчанию", "tx", "reset", v=key))
    rows.append(back("tx", text="« К текстам"))
    return text, kb(*rows)


@router.callback_query(A.filter((F.s == "tx") & (F.a == "open")))
async def cb_list(call: CallbackQuery, settings: Settings) -> None:
    await show(call, *list_screen(settings))


@router.callback_query(A.filter((F.s == "tx") & (F.a == "card") & F.v.in_(TEXTS)))
async def cb_card(call: CallbackQuery, callback_data: A, settings: Settings) -> None:
    await show(call, *card_screen(settings, callback_data.v))


@router.callback_query(A.filter((F.s == "tx") & (F.a == "reset") & F.v.in_(TEXTS)))
async def cb_reset(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer,
                   settings: Settings) -> None:
    await settings.reset(callback_data.v)
    callback_answer.text = "↩️ Восстановлен текст по умолчанию"
    await show(call, *card_screen(settings, callback_data.v))


@router.callback_query(A.filter((F.s == "tx") & (F.a == "edit") & F.v.in_(TEXTS)))
async def cb_edit(call: CallbackQuery, callback_data: A, state: FSMContext) -> None:
    meta = TEXTS[callback_data.v]
    placeholders = ", ".join(f"<code>{{{p}}}</code>" for p in meta.placeholders)
    await prompt(call, state, Input.text,
                 f"✏️ <b>{meta.title}</b>\n\n"
                 "Пришлите новый текст. Используйте обычное форматирование Telegram — жирный, курсив, "
                 "ссылки, спойлеры, премиум-эмодзи — всё сохранится.\n\n"
                 f"Переменные: {placeholders}\n"
                 "<i>Совет: текущий текст можно скопировать из предпросмотра.</i>",
                 back("tx", "card", "✖️ Отмена", v=callback_data.v), key=callback_data.v)


@router.message(Input.text, F.text)
async def on_text(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    key = data["key"]
    html = message.html_text
    if len(message.text) > 3500:
        await message.answer(f"⚠️ Слишком длинно: {len(message.text)}/3500 символов")
        return
    unknown = sorted(set(PLACEHOLDER_RE.findall(html)) - set(TEXTS[key].placeholders))
    await drop_prompt(message, state)
    await state.clear()
    await settings.set(key, html)
    if unknown:
        await message.answer("⚠️ Неизвестные переменные останутся как есть: "
                             + ", ".join(f"<code>{{{u}}}</code>" for u in unknown))
    await message.answer("✅ Текст сохранён")
    await show(message, *card_screen(settings, key))
