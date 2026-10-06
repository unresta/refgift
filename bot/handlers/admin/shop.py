"""Магазин подарков: цены, названия и видимость подарков, статистика продаж."""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.callbacks import A
from bot.config import Config
from bot.database import Database
from bot.handlers.admin.common import Input, back, btn, drop_prompt, kb, prompt
from bot.handlers.admin.home import day_start, star_balance, topup_button
from bot.services.shop import COMMENT_MAX, DEFAULT_NAMES, MAX_COMMENTS, MAX_PRICE, ShopItem, ShopService
from bot.settings import Settings
from bot.utils import esc, fmt_num, render_template, show
from bot.views import plain

router = Router(name="admin_shop")

STEPS = (-10, -1, 1, 10)


def signed(n: int) -> str:
    return f"+{fmt_num(n)}" if n > 0 else f"−{fmt_num(-n)}" if n < 0 else "0"


def stats_line(label: str, st) -> str:
    profit = st["revenue"] - st["cost"]
    return (f"{label}: продаж <b>{fmt_num(st['sold'])}</b> · выручка <b>{fmt_num(st['revenue'])}</b> ⭐ · "
            f"прибыль <b>{signed(profit)}</b> ⭐")


async def main_screen(bot: Bot, db: Database, settings: Settings, config: Config, shop: ShopService):
    items = await shop.items()
    enabled = settings.flag("shop_enabled")
    balance = await star_balance(bot)
    today, total = await db.shop_stats(day_start(config)), await db.shop_stats()
    losing = [i for i in items if i.active and i.profit < 0]
    comments = await db.shop_comments()

    lines = [
        "🛍 <b>Магазин подарков</b>\n",
        "В меню бота — «🛍 Купить подарок». Пользователь выбирает подарок, платит звёздами по вашей цене, "
        "и бот сразу дарит его со своего баланса. Не получилось отправить — звёзды возвращаются автоматически.\n",
        f"Статус: {'🟢 <b>открыт</b>' if enabled else '🔴 <b>закрыт</b>'}",
        f"⭐ Баланс бота: <b>{fmt_num(balance) if balance is not None else '—'}</b>",
        "",
        "💬 <b>По цене магазина</b> — подарок с нашим комментарием:",
        f"<blockquote>{shop.default_comment()}</blockquote>",
        f"✍️ <b>По себестоимости</b> — со своим комментарием из готовых вариантов: <b>{len(comments)}</b> шт."
        + ("" if comments else " <i>(вариантов нет — кнопки не будет)</i>"),
        "",
        stats_line("📊 Сегодня", today),
        stats_line("📊 Всего", total) + (f" · возвратов {total['refunded']}" if total["refunded"] else ""),
        "",
        "<i>Кнопка: 🟢 в магазине / ⚪️ скрыт · цена для покупателя · прибыль с продажи "
        "(цена − стоимость подарка для бота).</i>",
    ]
    if losing:
        lines.append(f"\n⚠️ {len(losing)} шт. продаются дешевле, чем стоят боту — каждая такая продажа в минус.")
    if not items:
        lines.append("\n<i>Не удалось получить подарки Telegram — попробуйте позже.</i>")

    rows = [[btn(f"{'🟢' if i.active else '⚪️'} {i.emoji} {i.name} — {i.price} ⭐ · {signed(i.profit)}",
                 "shop", "item", v=i.id)] for i in items]
    rows.append([btn("💬 Наш комментарий", "shop", "comment"), btn(f"✍️ Свои варианты · {len(comments)}", "shop", "cm")])
    rows.append([btn("🔴 Закрыть магазин" if enabled else "🟢 Открыть магазин", "shop", "t",
                     style="danger" if enabled else "success")])
    rows.append(topup_button(balance, settings, "shop"))
    rows.append(back())
    return "\n".join(lines), kb(*rows)


async def item_screen(db: Database, item: ShopItem):
    sold = await db.shop_stats(gift_id=item.id)
    gid = item.id
    profit = ("📈 С одной продажи: <b>" + signed(item.profit) + "</b> ⭐" if item.profit >= 0
              else f"📉 С одной продажи: <b>{signed(item.profit)}</b> ⭐ — <b>продаётся в минус</b>")
    profit += f"\n✍️ Со своим комментарием: {fmt_num(item.cost)} ⭐ (по себестоимости, прибыль 0)"
    limited = f"\n⏳ Лимитированный: осталось {fmt_num(item.gift.remaining_count)}" if item.gift.remaining_count else ""
    text = (
        f"{item.emoji} <b>{esc(item.name)}</b>\n\n"
        f"💰 Цена с нашим комментарием: <b>{fmt_num(item.price)}</b> ⭐\n"
        f"🏷 Стоит боту: {fmt_num(item.cost)} ⭐ (списывается с баланса при отправке)\n"
        f"{profit}\n"
        f"🛒 Продано: <b>{fmt_num(sold['sold'])}</b> на {fmt_num(sold['revenue'])} ⭐{limited}\n\n"
        f"Статус: {'🟢 в магазине' if item.active else '⚪️ скрыт'}"
    )

    def step(n: int):
        return btn(f"{'+' if n > 0 else '−'}{abs(n)}", "shop", "price", id=max(1, min(MAX_PRICE, item.price + n)),
                   v=gid)

    return text, kb(
        [step(-10), step(-1), btn(f"{item.price} ⭐", "noop"), step(1), step(10)],
        [btn(f"= себестоимости ({item.cost})", "shop", "price", id=item.cost, v=gid),
         btn("✏️ Своя цена", "shop", "pricein", v=gid, style="primary")],
        [btn("⚪️ Скрыть" if item.active else "🟢 Показать в магазине", "shop", "toggle", v=gid),
         btn("✏️ Название", "shop", "name", v=gid)],
        back("shop", text="« К магазину"),
    )


async def open_item(event: CallbackQuery | Message, bot: Bot, db: Database, settings: Settings, config: Config,
                    shop: ShopService, gift_id: str, callback_answer: CallbackAnswer | None = None) -> None:
    item = await shop.item(gift_id)
    if item is None:
        if callback_answer:
            callback_answer.text = "Подарок больше недоступен в Telegram"
        await show(event, *await main_screen(bot, db, settings, config, shop))
        return
    await show(event, *await item_screen(db, item))


@router.callback_query(A.filter((F.s == "shop") & F.a.in_({"open", "t"})))
async def cb_open(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, bot: Bot, db: Database,
                  settings: Settings, config: Config, shop: ShopService) -> None:
    if callback_data.a == "t":
        value = await settings.toggle("shop_enabled")
        callback_answer.text = "🟢 Магазин открыт" if value else "🔴 Магазин закрыт"
    await show(call, *await main_screen(bot, db, settings, config, shop))


@router.callback_query(A.filter((F.s == "shop") & F.a.in_({"item", "price", "toggle"})))
async def cb_item(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, bot: Bot, db: Database,
                  settings: Settings, config: Config, shop: ShopService) -> None:
    item = await shop.item(callback_data.v)
    if item and callback_data.a == "price":
        await db.set_shop_item(item.id, price=max(1, min(MAX_PRICE, callback_data.id)))
    elif item and callback_data.a == "toggle":
        await db.set_shop_item(item.id, is_active=0 if item.active else 1)
        callback_answer.text = "⚪️ Скрыт из магазина" if item.active else "🟢 Теперь в магазине"
    await open_item(call, bot, db, settings, config, shop, callback_data.v, callback_answer)


@router.callback_query(A.filter((F.s == "shop") & F.a.in_({"pricein", "name"})))
async def cb_input(call: CallbackQuery, callback_data: A, state: FSMContext, shop: ShopService) -> None:
    item = await shop.item(callback_data.v)
    if item is None:
        return
    cancel = back("shop", "item", "✖️ Отмена", v=item.id)
    if callback_data.a == "pricein":
        await prompt(call, state, Input.shop_price,
                     f"💰 <b>Цена {item.emoji} {esc(item.name)}</b>\n\n"
                     f"Сейчас: {item.price} ⭐ · стоит боту: {item.cost} ⭐\n\n"
                     f"Пришлите цену в звёздах — от 1 до {fmt_num(MAX_PRICE)}.", cancel, gift_id=item.id)
    else:
        default = DEFAULT_NAMES.get(item.emoji, "подарок")
        await prompt(call, state, Input.shop_name,
                     f"✏️ <b>Название {item.emoji}</b>\n\nСейчас: «{esc(item.name)}»\n\n"
                     f"Пришлите новое название — до 24 символов. Оно будет на кнопке: "
                     f"«{item.emoji} название - {item.price} ⭐».\n«-» — вернуть «{default}».", cancel, gift_id=item.id)


@router.message(Input.shop_price, F.text)
async def on_price(message: Message, state: FSMContext, bot: Bot, db: Database, settings: Settings,
                   config: Config, shop: ShopService) -> None:
    raw = message.text.strip().replace(" ", "").replace(" ", "").rstrip("⭐")
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_PRICE:
        await message.answer(f"⚠️ Нужно целое число звёзд от 1 до {fmt_num(MAX_PRICE)}")
        return
    data = await drop_prompt(message, state)
    await state.clear()
    await db.set_shop_item(data["gift_id"], price=int(raw))
    await open_item(message, bot, db, settings, config, shop, data["gift_id"])


@router.message(Input.shop_name, F.text)
async def on_name(message: Message, state: FSMContext, bot: Bot, db: Database, settings: Settings,
                  config: Config, shop: ShopService) -> None:
    raw = message.text.strip()
    if len(raw) > 24:
        await message.answer(f"⚠️ Слишком длинно: {len(raw)}/24 символов")
        return
    data = await drop_prompt(message, state)
    await state.clear()
    await db.set_shop_item(data["gift_id"], name=None if raw == "-" else raw)
    await open_item(message, bot, db, settings, config, shop, data["gift_id"])


# ---------- комментарии ----------

@router.callback_query(A.filter((F.s == "shop") & (F.a == "comment")))
async def cb_comment(call: CallbackQuery, state: FSMContext, settings: Settings, shop: ShopService) -> None:
    await prompt(call, state, Input.shop_comment,
                 "💬 <b>Наш комментарий</b>\n\n"
                 "С этой подписью уходят подарки по цене магазина. Сейчас:\n"
                 f"<blockquote>{shop.default_comment()}</blockquote>\n"
                 f"Пришлите новый текст — до {COMMENT_MAX} символов. "
                 "<code>{bot}</code> — юзернейм бота, например чтобы получатель нашёл магазин.\n"
                 "«-» — вернуть текст по умолчанию.",
                 back("shop", text="✖️ Отмена"))


@router.message(Input.shop_comment, F.text)
async def on_comment(message: Message, state: FSMContext, bot: Bot, db: Database, settings: Settings,
                     config: Config, shop: ShopService) -> None:
    raw = message.text.strip()
    if raw != "-":
        value = esc(raw)
        length = len(render_template(raw, bot=f"@{shop.bot_username}"))
        if length > COMMENT_MAX:
            await message.answer(f"⚠️ Слишком длинно: {length}/{COMMENT_MAX} символов")
            return
    await drop_prompt(message, state)
    await state.clear()
    if raw == "-":
        await settings.reset("shop_comment")
    else:
        await settings.set("shop_comment", value)
    await message.answer("✅ Комментарий сохранён")
    await show(message, *await main_screen(bot, db, settings, config, shop))


async def comments_screen(db: Database):
    comments = await db.shop_comments()
    lines = ["✍️ <b>Свои комментарии</b>\n",
             "Покупатель может выбрать один из этих текстов вместо нашего — тогда подарок стоит "
             "<b>по себестоимости</b> (столько, сколько он стоит боту).\n"]
    lines += [f"<b>{n}.</b> {c['text']}" for n, c in enumerate(comments, 1)]
    if not comments:
        lines.append("<i>Вариантов пока нет — покупатели видят только подарок с нашим комментарием.</i>")
    rows = [[btn(f"🗑 {n}. {plain(c['text'], 40)}", "shop", "cmdel", id=c["id"])] for n, c in enumerate(comments, 1)]
    if len(comments) < MAX_COMMENTS:
        rows.append([btn("➕ Добавить вариант", "shop", "cmadd", style="success")])
    rows.append(back("shop", text="« К магазину"))
    return "\n".join(lines), kb(*rows)


@router.callback_query(A.filter((F.s == "shop") & F.a.in_({"cm", "cmdel"})))
async def cb_comments(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database) -> None:
    if callback_data.a == "cmdel":
        await db.delete_shop_comment(callback_data.id)
        callback_answer.text = "🗑 Вариант удалён"
    await show(call, *await comments_screen(db))


@router.callback_query(A.filter((F.s == "shop") & (F.a == "cmadd")))
async def cb_comment_add(call: CallbackQuery, state: FSMContext) -> None:
    await prompt(call, state, Input.shop_comment_add,
                 "➕ <b>Новый вариант комментария</b>\n\n"
                 f"Пришлите текст — до {COMMENT_MAX} символов. Например: «С днём рождения! 🎉»",
                 back("shop", "cm", "✖️ Отмена"))


@router.message(Input.shop_comment_add, F.text)
async def on_comment_add(message: Message, state: FSMContext, db: Database) -> None:
    raw = message.text.strip()
    if len(raw) > COMMENT_MAX:
        await message.answer(f"⚠️ Слишком длинно: {len(raw)}/{COMMENT_MAX} символов")
        return
    await drop_prompt(message, state)
    await state.clear()
    await db.add_shop_comment(esc(raw))
    await show(message, *await comments_screen(db))
