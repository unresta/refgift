from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, CopyTextButton, Message
from aiogram.utils.callback_answer import CallbackAnswer
from aiosqlite import Row

from bot.callbacks import A
from bot.config import Config
from bot.database import Database
from bot.handlers.admin.home import star_balance, topup_button
from bot.handlers.admin.common import Btn, Input, back, btn, drop_prompt, kb, pager, pages_count, prompt
from bot.services.checks import CheckService
from bot.services.gifts import GiftImages, gift_emoji
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


async def main_screen(bot: Bot, db: Database, settings: Settings, bot_username: str, page: int,
                      gift_images: GiftImages):
    me = await bot.me()
    banners = {b["gift_id"] for b in await db.gift_banners()}
    catalog = await gift_images.catalog.gifts()
    own = sum(1 for g in catalog if g.id in banners)
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
        f"🎨 Свои баннеры: <b>{own}</b> из {len(catalog)} подарков",
        f"🖼 Общий баннер: {'✅ загружен' if has_photo else '❌ не загружен'} — для подарков без своего, "
        "бот добавляет на него значок подарка и цену",
    ]
    if not has_photo and own < len(catalog):
        lines.append("<i>Подарки без баннера будут в чеке текстом.</i>")
    if not me.supports_inline_queries:
        lines.append("\n⚠️ <b>Inline-режим выключен.</b> Включите: @BotFather → /setinline → выберите бота.")
    if not total:
        lines.append("\n💡 Чтобы отправленные чеки сразу появлялись здесь, "
                     "включите @BotFather → /setinlinefeedback → Enabled. Без этого чек появится в списке "
                     "после первой активации.")

    rows: list[list[Btn]] = [
        [Btn(text="📤 Создать чек", style="success", switch_inline_query="10 ")],
        topup_button(await star_balance(bot), settings, "ck"),
        [btn(f"🎨 Баннеры подарков · {own}/{len(catalog)}", "ck", "banners", style="primary")],
        [btn("🖼 Заменить общий баннер" if has_photo else "🖼 Загрузить общий баннер", "ck", "photo"),
         btn("👁 Превью", "ck", "preview")],
    ]
    if has_photo:
        rows.append([btn("🗑 Убрать общий баннер", "ck", "nophoto")])
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
                  bot_username: str, gift_images: GiftImages) -> None:
    await show(call, *await main_screen(bot, db, settings, bot_username, callback_data.p, gift_images))


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
    callback_answer.text = ("⏸ Чек выключен — активировать его больше нельзя" if c["is_active"]
                            else "▶️ Чек снова активен")
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
                    db: Database, settings: Settings, checks: CheckService, bot_username: str,
                    gift_images: GiftImages) -> None:
    c = await db.get_check(callback_data.id)
    if c:
        await db.delete_check(c["id"])
    callback_answer.text = "🗑 Чек удалён"
    await show(call, *await main_screen(bot, db, settings, bot_username, 0, gift_images))


# ---------- загрузка картинок ----------

IMAGE_HINT = ("Пришлите картинку <b>JPG или PNG</b> — обычным фото или файлом (без сжатия), до 10 МБ.\n"
              "Лучше всего смотрится горизонтальная, например 1280×720.\n\n"
              "<i>Применится к новым отправкам чеков.</i>")


async def read_image(message: Message, bot: Bot) -> str | None:
    """file_id фото из сообщения. Картинку-файл перезаливает фото (иначе её не показать в inline)."""
    if message.photo:
        return message.photo[-1].file_id
    doc = message.document
    if not doc or doc.mime_type not in ("image/jpeg", "image/png"):
        await message.answer("⚠️ Пришлите картинку JPG или PNG — фото или файлом")
        return None
    if (doc.file_size or 0) > MAX_PHOTO_BYTES:
        await message.answer("⚠️ Файл больше 10 МБ — сожмите картинку")
        return None
    try:
        data = await bot.download(doc)
        sent = await bot.send_photo(message.chat.id, BufferedInputFile(data.read(), doc.file_name or "check.jpg"))
    except TelegramAPIError as e:
        await message.answer(f"❌ Не удалось обработать картинку: {esc(e.message)}")
        return None
    try:
        await bot.delete_message(message.chat.id, sent.message_id)
    except TelegramAPIError:
        pass
    return sent.photo[-1].file_id


def sample_check(emoji: str | None = None, price: int | None = None) -> dict:
    return {"code": "preview", "total": 10, "caption": None, "gift_emoji": emoji, "gift_price": price}


async def send_preview(message: Message, checks: CheckService, photo: str | None, sample: dict) -> None:
    caption, markup = checks.caption(sample), checks.keyboard(sample)
    if photo:
        await message.answer_photo(photo, caption=caption, reply_markup=markup)
    else:
        await message.answer(caption, reply_markup=markup)


@router.message(StateFilter(Input.check_photo, Input.gift_banner))
async def on_image(message: Message, state: FSMContext, bot: Bot, db: Database, settings: Settings,
                   checks: CheckService, gift_images: GiftImages) -> None:
    file_id = await read_image(message, bot)
    if not file_id:
        return
    data = await drop_prompt(message, state)
    current = await state.get_state()
    await state.clear()

    if current == Input.gift_banner.state:
        gift_id = data["gift_id"]
        gift = await gift_images.catalog.get(gift_id)
        emoji = gift_emoji(gift) if gift else data.get("emoji", "🎁")
        price = gift.star_count if gift else data.get("price", 0)
        await db.set_gift_banner(gift_id, file_id, emoji, price)
        await message.answer(f"✅ Баннер для {emoji} сохранён. Так будет выглядеть чек:")
        await send_preview(message, checks, file_id, sample_check(emoji, price))
        await message.answer("🎨 Дальше", reply_markup=kb([btn("🎨 К баннерам подарков", "ck", "banners")],
                                                          [btn("🎟 К чекам", "ck")]))
        return

    await settings.set("check_photo", file_id)
    gift_images.schedule()  # варианты общего баннера со значком каждого подарка — для выбора в inline
    await message.answer("✅ Общий баннер сохранён. В фоне готовлю его варианты со значком каждого подарка "
                         "без своего баннера (секунда-две на подарок).\n\nТак выглядит общий баннер:")
    await send_preview(message, checks, file_id, sample_check())
    await message.answer("🎟 Вернуться к чекам", reply_markup=kb([btn("🎟 К чекам", "ck")]))


@router.callback_query(A.filter((F.s == "ck") & (F.a == "photo")))
async def cb_photo(call: CallbackQuery, state: FSMContext) -> None:
    await prompt(call, state, Input.check_photo,
                 "🖼 <b>Общий баннер чеков</b>\n\nИспользуется для подарков без своего баннера — "
                 "бот сам добавит на него значок подарка и цену.\n\n" + IMAGE_HINT,
                 back("ck", text="✖️ Отмена"))


@router.callback_query(A.filter((F.s == "ck") & (F.a == "preview")))
async def cb_preview(call: CallbackQuery, settings: Settings, checks: CheckService) -> None:
    await send_preview(call.message, checks, settings.get("check_photo") or None, sample_check())
    await call.message.answer("🎟 Вернуться к чекам", reply_markup=kb([btn("🎟 К чекам", "ck")]))


@router.callback_query(A.filter((F.s == "ck") & (F.a == "nophoto")))
async def cb_no_photo(call: CallbackQuery, callback_answer: CallbackAnswer, bot: Bot, db: Database,
                      settings: Settings, bot_username: str, gift_images: GiftImages) -> None:
    await settings.set("check_photo", "")
    callback_answer.text = "🗑 Общий баннер убран — подарки без своего баннера будут текстом"
    await show(call, *await main_screen(bot, db, settings, bot_username, 0, gift_images))


# ---------- баннеры подарков ----------

async def banners_screen(db: Database, settings: Settings, gift_images: GiftImages):
    catalog = sorted(await gift_images.catalog.gifts(), key=lambda g: g.star_count)
    banners = {b["gift_id"]: b for b in await db.gift_banners()}
    has_common = bool(settings.get("check_photo"))
    own = sum(1 for g in catalog if g.id in banners)

    text = ("🎨 <b>Баннеры подарков</b>\n\n"
            "У каждого подарка может быть свой баннер — он показывается в чеке как есть.\n"
            f"Без своего баннера — {'общий баннер со значком подарка' if has_common else 'текстовый чек'}.\n\n"
            f"✅ свой баннер · ➕ {'общий' if has_common else 'нет баннера'}\n"
            f"Своих: <b>{own}</b> из {len(catalog)}")
    buttons = [btn(f"{'✅' if g.id in banners else '➕'} {gift_emoji(g)} {g.star_count}⭐", "ck", "banner", v=g.id)
               for g in catalog]
    # баннеры подарков, которых больше нет в каталоге — чтобы их можно было удалить
    catalog_ids = {g.id for g in catalog}
    buttons += [btn(f"✅ {b['gift_emoji']} {b['gift_price']}⭐ · недоступен", "ck", "banner", v=gid)
                for gid, b in banners.items() if gid not in catalog_ids]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append(back("ck", text="« К чекам"))
    return text, kb(*rows)


async def banner_screen(db: Database, settings: Settings, gift_images: GiftImages, gift_id: str):
    gift = await gift_images.catalog.get(gift_id)
    banner_row = next((b for b in await db.gift_banners() if b["gift_id"] == gift_id), None)
    emoji = gift_emoji(gift) if gift else (banner_row["gift_emoji"] if banner_row else "🎁")
    price = gift.star_count if gift else (banner_row["gift_price"] if banner_row else 0)
    if banner_row:
        status = "✅ свой баннер"
    elif settings.get("check_photo"):
        status = "общий баннер со значком подарка"
    else:
        status = "нет — чек будет текстовым"
    text = (f"🎨 <b>Баннер для {emoji} · {price} ⭐</b>\n\n"
            f"Сейчас: {status}" + ("" if gift else "\n\n⚠️ Подарок сейчас недоступен для отправки."))
    rows = [[btn("🖼 Заменить баннер" if banner_row else "🖼 Загрузить баннер", "ck", "bn_up", v=gift_id,
                 style=None if banner_row else "primary"),
             btn("👁 Как выглядит чек", "ck", "bn_view", v=gift_id)]]
    if banner_row:
        rows.append([btn("🗑 Убрать свой баннер", "ck", "bn_del", v=gift_id, style="danger")])
    rows.append(back("ck", "banners", "« К баннерам"))
    return text, kb(*rows), emoji, price


@router.callback_query(A.filter((F.s == "ck") & (F.a == "banners")))
async def cb_banners(call: CallbackQuery, db: Database, settings: Settings, gift_images: GiftImages) -> None:
    await show(call, *await banners_screen(db, settings, gift_images))


@router.callback_query(A.filter((F.s == "ck") & (F.a == "banner")))
async def cb_banner(call: CallbackQuery, callback_data: A, db: Database, settings: Settings,
                    gift_images: GiftImages) -> None:
    text, markup, _, _ = await banner_screen(db, settings, gift_images, callback_data.v)
    await show(call, text, markup)


@router.callback_query(A.filter((F.s == "ck") & (F.a == "bn_up")))
async def cb_banner_upload(call: CallbackQuery, callback_data: A, state: FSMContext, db: Database,
                           settings: Settings, gift_images: GiftImages) -> None:
    _, _, emoji, price = await banner_screen(db, settings, gift_images, callback_data.v)
    await prompt(call, state, Input.gift_banner,
                 f"🎨 <b>Баннер для {emoji} · {price} ⭐</b>\n\n"
                 "Баннер покажется в чеке как есть — бот ничего на него не добавляет.\n\n" + IMAGE_HINT,
                 back("ck", "banner", "✖️ Отмена", v=callback_data.v),
                 gift_id=callback_data.v, emoji=emoji, price=price)


@router.callback_query(A.filter((F.s == "ck") & (F.a == "bn_view")))
async def cb_banner_view(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                         settings: Settings, checks: CheckService, gift_images: GiftImages) -> None:
    gift = await gift_images.catalog.get(callback_data.v)
    _, _, emoji, price = await banner_screen(db, settings, gift_images, callback_data.v)
    if gift:
        photo = await gift_images.file_id(gift)
    else:
        photo = await db.get_gift_banner(callback_data.v) or settings.get("check_photo") or None
    if photo and photo == settings.get("check_photo") and not await db.get_gift_banner(callback_data.v):
        callback_answer.text = "Вариант со значком ещё готовится — показываю общий баннер"
    await send_preview(call.message, checks, photo, sample_check(emoji, price))
    await call.message.answer("🎨 Дальше", reply_markup=kb([btn(f"« К баннеру {emoji}", "ck", "banner",
                                                                v=callback_data.v)]))


@router.callback_query(A.filter((F.s == "ck") & (F.a == "bn_del")))
async def cb_banner_delete(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                           settings: Settings, gift_images: GiftImages) -> None:
    await db.delete_gift_banner(callback_data.v)
    gift = await gift_images.catalog.get(callback_data.v)
    if gift:
        gift_images.schedule([gift])  # нужен вариант общего баннера для этого подарка
    callback_answer.text = "🗑 Свой баннер убран"
    text, markup, _, _ = await banner_screen(db, settings, gift_images, callback_data.v)
    await show(call, text, markup)
