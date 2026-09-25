from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, CopyTextButton, Message
from aiogram.utils.callback_answer import CallbackAnswer
from aiosqlite import Row

from bot.callbacks import A
from bot.config import Config
from bot.database import Database
from bot.handlers.admin.common import Btn, Input, back, btn, drop_prompt, kb, pager, pages_count, prompt
from bot.services.checks import CheckService
from bot.services.gifts import GiftImages
from bot.settings import Settings
from bot.utils import esc, fmt_dt, fmt_num, percent, progress_bar, show

router = Router(name="admin_checks")

PER_PAGE = 8
MAX_PHOTO_BYTES = 10 * 1024 * 1024
GIFT_ICON = {"sent": "✅", "pending": "⏳", "rejected": "❌"}


def status_icon(check: Row) -> str:
    if not check["is_active"]:
        return "⏸"
    if check["used"] >= check["total"]:
        return "⚪️"
    return "🟢"


async def main_screen(bot: Bot, db: Database, settings: Settings, bot_username: str, page: int):
    me = await bot.me()
    total = await db.count_checks()
    pages = pages_count(total, PER_PAGE)
    page = max(0, min(page, pages - 1))
    items = await db.list_checks(PER_PAGE, page * PER_PAGE)
    has_photo = bool(settings.get("check_photo"))

    lines = [
        "🎟 <b>Чеки на подарки</b>\n",
        "<b>Как создать:</b> в любом чате начните писать",
        f"<code>@{bot_username} 10</code> — чек на 10 активаций",
        f"<code>@{bot_username} 10 С праздником!</code> — со своей подписью",
        "и нажмите на появившуюся карточку — чек уйдёт в чат.\n",
        "Каждый активирует чек один раз и только после подписки на все каналы — "
        "подписка проверяется заново прямо перед выдачей.",
        "После числа бот покажет <b>все доступные подарки</b> — выберите, какой будет в чеке. "
        "Выдача — по режиму из настроек (авто/вручную).\n",
        f"🖼 Картинка: {'✅ загружена' if has_photo else '❌ не загружена — чеки будут текстовыми'}",
    ]
    if not me.supports_inline_queries:
        lines.append("\n⚠️ <b>Inline-режим выключен.</b> Включите: @BotFather → /setinline → выберите бота.")
    if not total:
        lines.append("\n💡 Чтобы отправленные чеки сразу появлялись здесь и помечались «закончился» в чате, "
                     "включите @BotFather → /setinlinefeedback → Enabled. Без этого чек появится в списке "
                     "после первой активации.")

    rows: list[list[Btn]] = [
        [Btn(text="📤 Создать чек", style="success", switch_inline_query="10 ")],
        [btn("🖼 Заменить картинку" if has_photo else "🖼 Загрузить картинку", "ck", "photo",
             style=None if has_photo else "primary"),
         btn("👁 Превью", "ck", "preview")],
    ]
    if has_photo:
        rows.append([btn("🗑 Убрать картинку", "ck", "nophoto")])
    for c in items:
        rows.append([btn(f"{status_icon(c)} {c['gift_emoji'] or ''} {c['code']} · {c['used']}/{c['total']}"
                         + (f" · {c['caption'][:20]}" if c["caption"] else ""), "ck", "card", id=c["id"])])
    rows.append(pager("ck", "open", page, pages))
    rows.append(back())
    return "\n".join(lines), kb(*rows)


async def card_screen(db: Database, checks: CheckService, config: Config, check_id: int):
    c = await db.get_check(check_id)
    if not c:
        return "❌ Чек не найден", kb(back("ck"))
    st = await db.check_stats(check_id)
    creator = await db.get_user(c["created_by"])
    recent = await db.check_activations(check_id, 5, 0)
    status = {"⏸": "выключен", "⚪️": "закончился", "🟢": "активен"}[status_icon(c)]
    left = c["total"] - c["used"]

    lines = [
        f"🎟 <b>Чек</b> <code>{c['code']}</code> · {status_icon(c)} {status}",
        f"📅 Создан: {fmt_dt(c['created_at'], config.tz)} · 👤 {esc(creator['full_name']) if creator else c['created_by']}",
        f"🎁 Подарок: {checks.emoji(c)} · {checks.price(c)} ⭐ за активацию",
        f"💬 Подпись: «{esc(c['caption'])}»" if c["caption"] else "💬 Подпись: из шаблона «Тексты → Подпись чека»",
        "",
        f"📊 Активации: <b>{fmt_num(c['used'])} / {fmt_num(c['total'])}</b> · осталось {fmt_num(left)}",
        f"{progress_bar(c['used'], c['total'])} {percent(c['used'], c['total'])}%",
        f"🆕 Новых пользователей: <b>{fmt_num(st['new_users'])}</b>",
        f"🎁 Подарки: ✅ {st['sent']} · ⏳ в очереди {st['pending']} · ❌ отклонено {st['rejected']}",
    ]
    if st["first_at"]:
        lines.append(f"🕒 Первая активация: {fmt_dt(st['first_at'], config.tz)} · "
                     f"последняя: {fmt_dt(st['last_at'], config.tz)}")
    lines.append("📨 Сообщение в чате: " + ("отслеживается ✅" if c["inline_message_id"]
                                            else "не отслеживается (нужен /setinlinefeedback)"))
    lines.append(f"\n🔗 <code>{checks.url(c['code'])}</code>")
    if recent:
        lines.append("\n<b>Последние активации:</b>")
        for a in recent:
            icon = GIFT_ICON.get(a["gift_status"], "•")
            lines.append(f"{icon} {esc(a['full_name']) or a['user_id']} — {fmt_dt(a['created_at'], config.tz)}")

    cid = c["id"]
    rows = [
        [Btn(text="📤 Отправить ещё раз", switch_inline_query=f"#{c['code']}"),
         Btn(text="📋 Ссылка", copy_text=CopyTextButton(text=checks.url(c["code"])))],
        [btn(f"👥 Все активации ({c['used']})", "ck", "acts", id=cid), btn("🔄", "ck", "card", id=cid)],
        [btn("▶️ Включить" if not c["is_active"] else "⏸ Выключить", "ck", "toggle", id=cid),
         btn("🗑 Удалить", "ck", "del", id=cid, style="danger")],
        back("ck", text="« К чекам"),
    ]
    return "\n".join(lines), kb(*rows)


@router.callback_query(A.filter((F.s == "ck") & (F.a == "open")))
async def cb_open(call: CallbackQuery, callback_data: A, bot: Bot, db: Database, settings: Settings,
                  bot_username: str) -> None:
    await show(call, *await main_screen(bot, db, settings, bot_username, callback_data.p))


@router.callback_query(A.filter((F.s == "ck") & (F.a == "card")))
async def cb_card(call: CallbackQuery, callback_data: A, db: Database, checks: CheckService,
                  config: Config) -> None:
    await show(call, *await card_screen(db, checks, config, callback_data.id))


@router.callback_query(A.filter((F.s == "ck") & (F.a == "toggle")))
async def cb_toggle(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                    checks: CheckService, config: Config) -> None:
    c = await db.get_check(callback_data.id)
    if not c:
        return
    await db.set_check_active(c["id"], not c["is_active"])
    fresh = await db.get_check(c["id"])
    if c["is_active"]:
        await checks.close_message(fresh, "⛔ <b>Чек отключён.</b>")
        callback_answer.text = "⏸ Чек выключен — активировать его больше нельзя"
    else:
        if fresh["used"] < fresh["total"]:
            await checks.reopen_message(fresh)
        callback_answer.text = "▶️ Чек снова активен"
    await show(call, *await card_screen(db, checks, config, c["id"]))


@router.callback_query(A.filter((F.s == "ck") & (F.a == "acts")))
async def cb_activations(call: CallbackQuery, callback_data: A, db: Database, config: Config) -> None:
    c = await db.get_check(callback_data.id)
    if not c:
        return
    pages = pages_count(c["used"], 10)
    page = max(0, min(callback_data.p, pages - 1))
    acts = await db.check_activations(c["id"], 10, page * 10)
    rows = [[btn(f"{GIFT_ICON.get(a['gift_status'], '•')} {a['full_name'] or a['user_id']} · "
                 f"{fmt_dt(a['created_at'], config.tz)[:-6]}", "us", "card", id=a["user_id"])] for a in acts]
    rows.append(pager("ck", "acts", page, pages, id=c["id"]))
    rows.append(back("ck", "card", "« К чеку", id=c["id"]))
    await show(call, f"👥 <b>Активации чека</b> <code>{c['code']}</code> — {c['used']}/{c['total']}\n\n"
                     "✅ подарок отправлен · ⏳ в очереди · ❌ отклонён", kb(*rows))


@router.callback_query(A.filter((F.s == "ck") & (F.a == "del")))
async def cb_delete_confirm(call: CallbackQuery, callback_data: A, db: Database) -> None:
    c = await db.get_check(callback_data.id)
    if not c:
        return
    await show(call, f"🗑 <b>Удалить чек</b> <code>{c['code']}</code>?\n\n"
                     "Ссылка перестанет работать, статистика по чеку пропадёт. "
                     "Выданные подарки и заявки останутся.\n\n"
                     "<i>Чтобы просто остановить выдачу — лучше выключите чек.</i>", kb(
        [btn("🗑 Да, удалить", "ck", "del_ok", id=c["id"], style="danger")],
        [btn("⏸ Лучше выключить", "ck", "toggle", id=c["id"])] if c["is_active"] else [],
        back("ck", "card", "« Отмена", id=c["id"]),
    ))


@router.callback_query(A.filter((F.s == "ck") & (F.a == "del_ok")))
async def cb_delete(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, bot: Bot,
                    db: Database, settings: Settings, checks: CheckService, bot_username: str) -> None:
    c = await db.get_check(callback_data.id)
    if c:
        await checks.close_message(c, "⛔ <b>Чек удалён.</b>")
        await db.delete_check(c["id"])
    callback_answer.text = "🗑 Чек удалён"
    await show(call, *await main_screen(bot, db, settings, bot_username, 0))


# ---------- картинка ----------

@router.callback_query(A.filter((F.s == "ck") & (F.a == "photo")))
async def cb_photo(call: CallbackQuery, state: FSMContext) -> None:
    await prompt(call, state, Input.check_photo,
                 "🖼 <b>Картинка для чеков</b>\n\n"
                 "Пришлите картинку <b>JPG или PNG</b> — обычным фото или файлом (без сжатия), до 10 МБ.\n"
                 "Лучше всего смотрится горизонтальная, например 1280×720.\n\n"
                 "<i>Новая картинка применится к новым отправкам чеков.</i>",
                 back("ck", text="✖️ Отмена"))


@router.message(Input.check_photo, F.photo)
async def on_photo(message: Message, state: FSMContext, settings: Settings, checks: CheckService,
                   gift_images: GiftImages) -> None:
    await _save_photo(message, state, settings, checks, gift_images, message.photo[-1].file_id)


@router.message(Input.check_photo, F.document)
async def on_photo_document(message: Message, state: FSMContext, bot: Bot, settings: Settings,
                            checks: CheckService, gift_images: GiftImages) -> None:
    doc = message.document
    if doc.mime_type not in ("image/jpeg", "image/png"):
        await message.answer("⚠️ Нужен файл JPG или PNG")
        return
    if (doc.file_size or 0) > MAX_PHOTO_BYTES:
        await message.answer("⚠️ Файл больше 10 МБ — сожмите картинку")
        return
    # Файл-документ нельзя показать как фото в inline — перезаливаем его фотографией.
    try:
        data = await bot.download(doc)
        sent = await bot.send_photo(message.chat.id, BufferedInputFile(data.read(), doc.file_name or "check.jpg"))
    except TelegramAPIError as e:
        await message.answer(f"❌ Не удалось обработать картинку: {esc(e.message)}")
        return
    file_id = sent.photo[-1].file_id
    try:
        await bot.delete_message(message.chat.id, sent.message_id)
    except TelegramAPIError:
        pass
    await _save_photo(message, state, settings, checks, gift_images, file_id)


@router.message(Input.check_photo)
async def on_photo_wrong(message: Message) -> None:
    await message.answer("⚠️ Пришлите картинку JPG или PNG — фото или файлом")


async def _save_photo(message: Message, state: FSMContext, settings: Settings, checks: CheckService,
                      gift_images: GiftImages, file_id: str) -> None:
    await drop_prompt(message, state)
    await state.clear()
    await settings.set("check_photo", file_id)
    gift_images.schedule()  # варианты картинки со значком каждого подарка — для выбора в inline
    await message.answer("✅ Картинка сохранена. В фоне готовлю её варианты со значком каждого подарка "
                         "(пару секунд на подарок).\n\nТак будет выглядеть чек на 10 активаций:")
    await send_preview(message, settings, checks)
    await message.answer("🎟 Вернуться к чекам", reply_markup=kb([btn("🎟 К чекам", "ck")]))


async def send_preview(message: Message, settings: Settings, checks: CheckService) -> None:
    sample = {"code": "preview", "total": 10, "caption": None}
    caption, markup = checks.caption(sample), checks.keyboard(sample)
    photo = settings.get("check_photo")
    if photo:
        await message.answer_photo(photo, caption=caption, reply_markup=markup)
    else:
        await message.answer(caption, reply_markup=markup)


@router.callback_query(A.filter((F.s == "ck") & (F.a == "preview")))
async def cb_preview(call: CallbackQuery, settings: Settings, checks: CheckService) -> None:
    await send_preview(call.message, settings, checks)
    await call.message.answer("🎟 Вернуться к чекам", reply_markup=kb([btn("🎟 К чекам", "ck")]))


@router.callback_query(A.filter((F.s == "ck") & (F.a == "nophoto")))
async def cb_no_photo(call: CallbackQuery, callback_answer: CallbackAnswer, bot: Bot, db: Database,
                      settings: Settings, bot_username: str) -> None:
    await settings.set("check_photo", "")
    callback_answer.text = "🗑 Картинка убрана — чеки будут текстовыми"
    await show(call, *await main_screen(bot, db, settings, bot_username, 0))
