from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.callbacks import A, U
from bot.database import Database
from bot.handlers.admin.common import Btn, Input, back, btn, drop_prompt, kb, prompt
from bot.handlers.admin.tasks import STYLE_NAMES, STYLES
from bot.services.cases import expected_value, prize_label
from bot.services.gifts import GiftCatalog, gift_emoji
from bot.utils import esc, fmt_num, fmt_stars, parse_stars, show
from bot.views import fmt_chance

router = Router(name="admin_cases")


async def list_screen(db: Database):
    stats = await db.case_stats()
    opens = sum(r["opens"] for r in stats.values())
    spent = sum(r["spent"] or 0 for r in stats.values())
    gifts = sum(r["gifts"] for r in stats.values())
    stars = sum(r["stars"] for r in stats.values())
    lines = [
        "📦 <b>Кейсы за звёзды с баланса</b>\n",
        "Пользователь тратит звёзды, заработанные на заданиях, и выигрывает подарок Telegram "
        "(отправляется со звёзд бота) или звёзды обратно на баланс. "
        "Ежедневный кейс открывается раз в сутки.\n",
        f"📊 Открыто: <b>{fmt_num(opens)}</b> · потрачено с балансов <b>{fmt_stars(spent)}</b> ⭐",
        f"Выдано подарками: <b>{fmt_stars(gifts)}</b> ⭐ (реальные звёзды бота) · на баланс: <b>{fmt_stars(stars)}</b> ⭐",
    ]
    rows = []
    for c in await db.cases():
        prizes = await db.case_prizes(c["id"])
        icon = "🟢" if c["is_active"] and prizes else "⏸"
        daily = " · 📅" if c["is_daily"] else ""
        rows.append([btn(f"{icon} {c['emoji'] or ''} {c['name']} · {fmt_stars(c['price'])}⭐{daily}".replace("  ", " "),
                         "cs", "card", id=c["id"])])
    rows.append([btn("➕ Новый кейс", "cs", "new", style="success")])
    rows.append([Btn(text="👀 Как видит пользователь", callback_data=U(a="cases").pack())])
    rows.append(back())
    return "\n".join(lines), kb(*rows)


async def card_screen(db: Database, case_id: int):
    c = await db.get_case(case_id)
    if not c:
        return "❌ Кейс не найден", kb(back("cs"))
    prizes = await db.case_prizes(case_id)
    total = sum(p["weight"] for p in prizes)
    ev = expected_value(prizes)
    gift_ev = sum(p["weight"] * p["value"] for p in prizes if p["kind"] == "gift") / total if total else 0
    stats = (await db.case_stats()).get(case_id)

    icon = f" · премиум-эмодзи <code>{c['emoji_id']}</code>" if c["emoji_id"] else ""
    lines = [
        f"📦 <b>{esc(c['emoji'] or '')} {esc(c['name'])}</b> · {'🟢 активен' if c['is_active'] else '⏸ выключен'}\n",
        f"💰 Цена: <b>{fmt_stars(c['price'])}</b> ⭐" + (" · 📅 <b>раз в сутки</b>" if c["is_daily"] else ""),
        f"🎨 Кнопка: {STYLE_NAMES.get(c['style'], 'обычный')}{icon}",
    ]
    if prizes:
        verdict = ""
        if c["price"]:
            rtp = ev / c["price"]
            verdict = f" → RTP <b>{rtp:.0%}</b>" + (" ⚠️ <b>отдаёт больше, чем стоит</b>" if rtp > 1 else "")
        lines += [
            f"Средний приз: <b>{fmt_stars(round(ev))}</b> ⭐{verdict}",
            f"Из них подарками (звёзды бота): ~<b>{fmt_stars(round(gift_ev))}</b> ⭐ за открытие",
            "",
            "<b>Призы:</b>",
            *(f"{prize_label(p)} — <b>{fmt_chance(p['weight'], total)}</b>" for p in prizes),
        ]
    else:
        lines.append("\n<i>Призов нет — пользователи кейс не видят. Добавьте хотя бы один.</i>")
    if stats:
        lines += ["", f"📊 Открыто: {fmt_num(stats['opens'])} ({fmt_num(stats['players'])} чел.) · "
                      f"потрачено {fmt_stars(stats['spent'] or 0)} ⭐ · подарками {fmt_stars(stats['gifts'])} ⭐ · "
                      f"на баланс {fmt_stars(stats['stars'])} ⭐"]

    cid = c["id"]
    rows = [[btn(f"{prize_label(p)} · {fmt_chance(p['weight'], total)}", "cs", "prize", id=p["id"])] for p in prizes]
    rows.append([btn("🎁 + Подарок", "cs", "add", id=cid, style="success"),
                 btn("⭐ + Звёзды", "cs", "addstars", id=cid, style="success")])
    rows.append([btn("✏️ Название", "cs", "rename", id=cid), btn(f"💰 Цена: {fmt_stars(c['price'])}⭐", "cs", "price",
                                                                 id=cid)])
    rows.append([btn("😀 Эмодзи", "cs", "emoji", id=cid),
                 btn(f"🎨 Цвет: {STYLE_NAMES.get(c['style'], 'обычный')}", "cs", "style", id=cid, style=c["style"])])
    rows.append([btn(f"📅 Раз в сутки: {'вкл' if c['is_daily'] else 'выкл'}", "cs", "daily", id=cid)])
    rows.append([btn("⬆️ Выше", "cs", "up", id=cid),
                 btn("⏸ Выключить" if c["is_active"] else "▶️ Включить", "cs", "toggle", id=cid)])
    rows.append([btn("🗑 Удалить", "cs", "del", id=cid, style="danger")])
    rows.append(back("cs", text="« К кейсам"))
    return "\n".join(lines), kb(*rows)


@router.callback_query(A.filter((F.s == "cs") & (F.a == "open")))
async def cb_open(call: CallbackQuery, db: Database) -> None:
    await show(call, *await list_screen(db))


@router.callback_query(A.filter((F.s == "cs") & (F.a == "card")))
async def cb_card(call: CallbackQuery, callback_data: A, db: Database) -> None:
    await show(call, *await card_screen(db, callback_data.id))


@router.callback_query(A.filter((F.s == "cs") & F.a.in_({"toggle", "up", "style", "daily"})))
async def cb_action(call: CallbackQuery, callback_data: A, db: Database) -> None:
    case = await db.get_case(callback_data.id)
    if not case:
        return
    if callback_data.a == "toggle":
        await db.update_case(case["id"], is_active=int(not case["is_active"]))
    elif callback_data.a == "daily":
        await db.update_case(case["id"], is_daily=int(not case["is_daily"]))
    elif callback_data.a == "up":
        await db.move_case_up(case["id"])
    else:
        style = case["style"] if case["style"] in STYLES else None
        await db.update_case(case["id"], style=STYLES[(STYLES.index(style) + 1) % len(STYLES)])
    await show(call, *await card_screen(db, case["id"]))


@router.callback_query(A.filter((F.s == "cs") & (F.a == "del")))
async def cb_delete_confirm(call: CallbackQuery, callback_data: A, db: Database) -> None:
    case = await db.get_case(callback_data.id)
    if not case:
        return
    await show(call, f"🗑 <b>Удалить кейс «{esc(case['name'])}»?</b>\n\nИстория открытий сохранится.", kb(
        [btn("🗑 Да, удалить", "cs", "del_ok", id=case["id"], style="danger")],
        back("cs", "card", "« Отмена", id=case["id"]),
    ))


@router.callback_query(A.filter((F.s == "cs") & (F.a == "del_ok")))
async def cb_delete(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database) -> None:
    await db.delete_case(callback_data.id)
    callback_answer.text = "🗑 Кейс удалён"
    await show(call, *await list_screen(db))


# ---------- создание и редактирование ----------

@router.callback_query(A.filter((F.s == "cs") & (F.a == "new")))
async def cb_new(call: CallbackQuery, state: FSMContext) -> None:
    await prompt(call, state, Input.cs_name, "➕ <b>Новый кейс</b>\n\nКак он будет называться? Например: "
                                             "<i>мини бокс</i>", back("cs", text="✖️ Отмена"), case_id=0)


@router.callback_query(A.filter((F.s == "cs") & (F.a == "rename")))
async def cb_rename(call: CallbackQuery, callback_data: A, state: FSMContext) -> None:
    await prompt(call, state, Input.cs_name, "✏️ Новое название кейса (до 32 символов):",
                 back("cs", "card", "✖️ Отмена", id=callback_data.id), case_id=callback_data.id)


@router.message(Input.cs_name, F.text)
async def on_name(message: Message, state: FSMContext, db: Database) -> None:
    name = message.text.strip()
    if not 1 <= len(name) <= 32:
        await message.answer("⚠️ Название — от 1 до 32 символов")
        return
    data = await drop_prompt(message, state)
    if data.get("case_id"):
        await state.clear()
        await db.update_case(data["case_id"], name=name)
        await show(message, *await card_screen(db, data["case_id"]))
        return
    await state.set_state(Input.cs_price)
    msg = await message.answer(f"💰 Сколько звёзд с баланса стоит кейс «{esc(name)}»? Например <code>5</code> "
                               "или <code>0.5</code>; <code>0</code> — бесплатный.",
                               reply_markup=kb(back("cs", text="✖️ Отмена")))
    await state.update_data(name=name, prompt_id=msg.message_id)


@router.callback_query(A.filter((F.s == "cs") & (F.a == "price")))
async def cb_price(call: CallbackQuery, callback_data: A, state: FSMContext) -> None:
    await prompt(call, state, Input.cs_price, "💰 Новая цена в звёздах с баланса (<code>0</code> — бесплатно):",
                 back("cs", "card", "✖️ Отмена", id=callback_data.id), case_id=callback_data.id)


@router.message(Input.cs_price, F.text)
async def on_price(message: Message, state: FSMContext, db: Database) -> None:
    price = parse_stars(message.text, 100_000)
    if price is None:
        await message.answer("⚠️ Нужно число звёзд от 0 до 100 000, например <code>5</code> или <code>0.5</code>")
        return
    data = await drop_prompt(message, state)
    await state.clear()
    if data.get("case_id"):
        case_id = data["case_id"]
        await db.update_case(case_id, price=price)
    else:
        case_id = await db.create_case(data["name"], price)
        await message.answer("✅ Кейс создан — добавьте в него призы")
    await show(message, *await card_screen(db, case_id))


@router.callback_query(A.filter((F.s == "cs") & (F.a == "emoji")))
async def cb_emoji(call: CallbackQuery, callback_data: A, state: FSMContext) -> None:
    await prompt(call, state, Input.cs_emoji,
                 "😀 <b>Эмодзи кейса</b>\n\nПришлите эмодзи — обычный или <b>премиум</b>: премиум станет иконкой "
                 "на кнопке (нужен Telegram Premium у владельца бота, иначе покажется обычный).\n\n"
                 "<code>-</code> — убрать эмодзи.",
                 back("cs", "card", "✖️ Отмена", id=callback_data.id), case_id=callback_data.id)


@router.message(Input.cs_emoji, F.text)
async def on_emoji(message: Message, state: FSMContext, db: Database) -> None:
    text = message.text.strip()
    custom = next((e for e in message.entities or [] if e.type == "custom_emoji"), None)
    if custom:
        emoji, emoji_id = custom.extract_from(message.text), custom.custom_emoji_id
    elif text == "-":
        emoji, emoji_id = None, None
    elif 1 <= len(text) <= 8 and " " not in text:
        emoji, emoji_id = text, None
    else:
        await message.answer("⚠️ Пришлите один эмодзи или <code>-</code>")
        return
    data = await drop_prompt(message, state)
    await state.clear()
    await db.update_case(data["case_id"], emoji=emoji, emoji_id=emoji_id)
    await show(message, *await card_screen(db, data["case_id"]))


# ---------- призы ----------

@router.callback_query(A.filter((F.s == "cs") & (F.a == "add")))
async def cb_add_gift(call: CallbackQuery, callback_data: A, catalog: GiftCatalog) -> None:
    gifts = sorted(await catalog.gifts(), key=lambda g: g.star_count)
    buttons = [btn(f"{gift_emoji(g)} {g.star_count}⭐", "cs", "pick", id=callback_data.id, v=g.id) for g in gifts]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append(back("cs", "card", "✖️ Отмена", id=callback_data.id))
    await show(call, "🎁 <b>Какой подарок добавить в кейс?</b>\n\n<i>Выигрыш отправляется со звёзд бота.</i>"
               if gifts else "⚠️ Не удалось загрузить подарки", kb(*rows))


WEIGHT_HINT = ("Пришлите шанс в процентах, например <code>25</code> или <code>0.5</code>.\n"
               "<i>Если сумма шансов призов не равна 100, бот пересчитает их пропорционально.</i>")


@router.callback_query(A.filter((F.s == "cs") & (F.a == "pick")))
async def cb_pick(call: CallbackQuery, callback_data: A, state: FSMContext, catalog: GiftCatalog) -> None:
    gift = await catalog.get(callback_data.v)
    if not gift:
        return
    await prompt(call, state, Input.cs_weight, f"🎲 <b>Шанс выпадения {gift_emoji(gift)} {gift.star_count} ⭐</b>\n\n"
                                               + WEIGHT_HINT,
                 back("cs", "card", "✖️ Отмена", id=callback_data.id),
                 case_id=callback_data.id, kind="gift", gift_id=gift.id, emoji=gift_emoji(gift),
                 value=gift.star_count * 100)


@router.callback_query(A.filter((F.s == "cs") & (F.a == "addstars")))
async def cb_add_stars(call: CallbackQuery, callback_data: A, state: FSMContext) -> None:
    await prompt(call, state, Input.cs_stars, "⭐ <b>Сколько звёзд начислить на баланс?</b>\n\n"
                                              "Например <code>0.5</code> или <code>10</code>.",
                 back("cs", "card", "✖️ Отмена", id=callback_data.id), case_id=callback_data.id)


@router.message(Input.cs_stars, F.text)
async def on_stars(message: Message, state: FSMContext) -> None:
    value = parse_stars(message.text, 100_000)
    if not value:
        await message.answer("⚠️ Нужно число звёзд больше 0, например <code>0.5</code>")
        return
    await drop_prompt(message, state)
    await state.set_state(Input.cs_weight)
    data = await state.get_data()
    msg = await message.answer(f"🎲 <b>Шанс выпадения ⭐ {fmt_stars(value)} на баланс</b>\n\n" + WEIGHT_HINT,
                               reply_markup=kb(back("cs", "card", "✖️ Отмена", id=data["case_id"])))
    await state.update_data(kind="stars", value=value, gift_id=None, emoji=None, prompt_id=msg.message_id)


@router.callback_query(A.filter((F.s == "cs") & (F.a == "prize")))
async def cb_prize(call: CallbackQuery, callback_data: A, db: Database) -> None:
    prize = await db.get_case_prize(callback_data.id)
    if not prize:
        return
    total = sum(p["weight"] for p in await db.case_prizes(prize["case_id"]))
    await show(call, f"<b>{prize_label(prize)}</b>\n\n"
                     f"Вес: <b>{prize['weight']:g}</b> → шанс <b>{fmt_chance(prize['weight'], total)}</b>",
               kb([btn("✏️ Изменить шанс", "cs", "weight", id=prize["id"], style="primary"),
                   btn("🗑 Убрать", "cs", "prize_del", id=prize["id"], style="danger")],
                  back("cs", "card", "« К кейсу", id=prize["case_id"])))


@router.callback_query(A.filter((F.s == "cs") & (F.a == "weight")))
async def cb_weight(call: CallbackQuery, callback_data: A, state: FSMContext, db: Database) -> None:
    prize = await db.get_case_prize(callback_data.id)
    if not prize:
        return
    await prompt(call, state, Input.cs_weight, f"🎲 Новый шанс для «{prize_label(prize)}» в процентах "
                                               f"(сейчас вес {prize['weight']:g}):",
                 back("cs", "prize", "✖️ Отмена", id=prize["id"]), prize_id=prize["id"], case_id=prize["case_id"])


@router.message(Input.cs_weight, F.text)
async def on_weight(message: Message, state: FSMContext, db: Database) -> None:
    try:
        weight = float(message.text.strip().rstrip("%").replace(",", ".").strip())
    except ValueError:
        weight = -1
    if not 0 < weight <= 100:
        await message.answer("⚠️ Пришлите число больше 0 и не больше 100, например <code>25</code>")
        return
    data = await drop_prompt(message, state)
    await state.clear()
    if data.get("prize_id"):
        await db.set_case_prize_weight(data["prize_id"], weight)
    else:
        await db.add_case_prize(data["case_id"], data["kind"], data["value"], weight, data["gift_id"], data["emoji"])
    await show(message, *await card_screen(db, data["case_id"]))


@router.callback_query(A.filter((F.s == "cs") & (F.a == "prize_del")))
async def cb_prize_delete(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer,
                          db: Database) -> None:
    prize = await db.get_case_prize(callback_data.id)
    if not prize:
        return
    await db.delete_case_prize(prize["id"])
    callback_answer.text = "🗑 Приз убран"
    await show(call, *await card_screen(db, prize["case_id"]))
