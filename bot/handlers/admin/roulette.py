from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, WebAppInfo
from aiogram.utils.callback_answer import CallbackAnswer

from bot.callbacks import A
from bot.config import Config
from bot.database import Database
from bot.handlers.admin.common import Btn, Input, back, btn, drop_prompt, kb, prompt
from bot.handlers.admin.home import day_start
from bot.services.gifts import GiftCatalog, gift_emoji
from bot.services.roulette import chance, economics
from bot.settings import Settings
from bot.utils import esc, fmt_num, percent, show

router = Router(name="admin_roulette")

FLAGS = {"roulette_enabled", "roulette_demo", "roulette_require_sub"}


def on_off(value: bool) -> str:
    return "вкл" if value else "выкл"


def fmt_chance(value: float) -> str:
    return f"{value:.3g}%" if value < 1 else f"{value:.2f}".rstrip("0").rstrip(".") + "%"


def totals(rows) -> dict[str, int]:
    keys = ("spins", "revenue", "paid_out", "owed", "pending", "refunded")
    return {k: sum(r[k] or 0 for r in rows) for k in keys}


async def main_screen(db: Database, settings: Settings, config: Config):
    all_time = totals(await db.roulette_stats())
    today = totals(await db.roulette_stats(day_start(config)))
    players = await db.val("SELECT COUNT(DISTINCT user_id) FROM spins WHERE paid_at IS NOT NULL")
    profit = all_time["revenue"] - all_time["paid_out"] - all_time["owed"]

    lines = [
        "🎰 <b>Рулетка подарков</b>\n",
        "Мини-апп: пользователь платит звёзды за прокрутку и выигрывает подарок. "
        "Приз разыгрывается на сервере в момент оплаты и сразу отправляется.\n",
        f"Статус: {'🟢 <b>включена</b>' if settings.flag('roulette_enabled') else '🔴 <b>выключена</b>'} · "
        f"демо: {on_off(settings.flag('roulette_demo'))} · "
        f"обязательная подписка: {on_off(settings.flag('roulette_require_sub'))}",
    ]
    if config.webapp_url:
        lines.append(f"🔗 {esc(config.webapp_url)}")
    else:
        lines.append("⚠️ <b>WEBAPP_URL не задан</b> — укажите публичный HTTPS-адрес в .env, иначе кнопки мини-аппа нет.")
    lines += [
        "",
        "📊 <b>За всё время</b>",
        f"Прокруток: <b>{fmt_num(all_time['spins'])}</b> · игроков: <b>{fmt_num(players)}</b>",
        f"Выручка: <b>{fmt_num(all_time['revenue'])}</b> ⭐ · выдано подарков: <b>{fmt_num(all_time['paid_out'])}</b> ⭐",
        f"В очереди на выдачу: <b>{fmt_num(all_time['owed'])}</b> ⭐ ({all_time['pending']} шт.) · "
        f"возвратов: {all_time['refunded']}",
        f"Прибыль: <b>{fmt_num(profit)}</b> ⭐ (отдача игрокам {percent(all_time['paid_out'] + all_time['owed'], all_time['revenue'])}%)",
        f"Сегодня: {fmt_num(today['spins'])} прокруток · {fmt_num(today['revenue'])} ⭐",
        "",
        "<b>Кейсы</b> — RTP = средняя стоимость приза к цене прокрутки:",
    ]
    rows: list[list[Btn]] = []
    for case in await db.roulette_cases():
        prizes = await db.roulette_prizes(case["id"])
        eco = economics(case, prizes)
        icon = "🟢" if case["is_active"] and prizes else "⏸"
        warn = " ⚠️" if eco.rtp > 1 else ""
        rows.append([btn(f"{icon} {case['name']} · {case['price']}⭐ · RTP {eco.rtp:.0%}{warn}", "rl", "case",
                         id=case["id"])])
    rows.append([btn("➕ Новый кейс", "rl", "new", style="success")])
    rows.append([btn(f"{'🔴 Выключить' if settings.flag('roulette_enabled') else '🟢 Включить'} рулетку", "rl", "t",
                     v="roulette_enabled")])
    rows.append([btn(f"🎮 Демо: {on_off(settings.flag('roulette_demo'))}", "rl", "t", v="roulette_demo"),
                 btn(f"📢 Подписка: {on_off(settings.flag('roulette_require_sub'))}", "rl", "t",
                     v="roulette_require_sub")])
    if config.webapp_url:
        rows.append([Btn(text="📱 Открыть мини-апп", web_app=WebAppInfo(url=config.webapp_url))])
    rows.append([btn("🔄 Обновить", "rl"), *back()])
    return "\n".join(lines), kb(*rows)


async def case_screen(db: Database, case_id: int):
    case = await db.get_roulette_case(case_id)
    if not case:
        return "❌ Кейс не найден", kb(back("rl"))
    prizes = await db.roulette_prizes(case_id)
    eco = economics(case, prizes)
    stats = next((r for r in await db.roulette_stats() if r["case_id"] == case_id), None)

    lines = [
        f"🎰 <b>Кейс «{esc(case['name'])}»</b> · {case['price']} ⭐ · "
        f"{'🟢 активен' if case['is_active'] else '⏸ выключен'}\n",
    ]
    if prizes:
        verdict = "⚠️ <b>убыточный</b> — в среднем отдаёт больше, чем стоит" if eco.rtp > 1 else \
            f"маржа ~<b>{case['price'] - eco.expected_payout:.1f}</b> ⭐ ({eco.margin:.0%}) с прокрутки"
        lines += [
            f"Средний приз: <b>{eco.expected_payout:.1f}</b> ⭐ → RTP <b>{eco.rtp:.1%}</b>, {verdict}",
            f"Сумма весов: {eco.total_weight:g} — шансы пересчитываются в проценты автоматически.",
            "",
            "<b>Призы</b> (шанс в мини-аппе):",
        ]
        for p in prizes:
            lines.append(f"{p['gift_emoji']} {p['gift_price']} ⭐ — <b>{fmt_chance(chance(p, eco.total_weight))}</b>")
    else:
        lines.append("<i>Призов нет — кейс не показывается в мини-аппе. Добавьте хотя бы один.</i>")
    if stats:
        lines += ["", f"📊 Прокруток: {fmt_num(stats['spins'])} · выручка {fmt_num(stats['revenue'])} ⭐ · "
                      f"выдано {fmt_num(stats['paid_out'])} ⭐"]

    cid = case["id"]
    rows = [[btn(f"{p['gift_emoji']} {p['gift_price']}⭐ · {fmt_chance(chance(p, eco.total_weight))}", "rl", "prize",
                 id=p["id"])] for p in prizes]
    rows.append([btn("➕ Добавить приз", "rl", "add", id=cid, style="success")])
    rows.append([btn("✏️ Название", "rl", "rename", id=cid), btn(f"💰 Цена: {case['price']}⭐", "rl", "price", id=cid)])
    rows.append([btn("⏸ Выключить" if case["is_active"] else "▶️ Включить", "rl", "toggle", id=cid),
                 btn("🗑 Удалить", "rl", "del", id=cid, style="danger")])
    rows.append(back("rl", text="« К рулетке"))
    return "\n".join(lines), kb(*rows)


@router.callback_query(A.filter((F.s == "rl") & (F.a == "open")))
async def cb_open(call: CallbackQuery, db: Database, settings: Settings, config: Config) -> None:
    await show(call, *await main_screen(db, settings, config))


@router.callback_query(A.filter((F.s == "rl") & (F.a == "t") & F.v.in_(FLAGS)))
async def cb_flag(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                  settings: Settings, config: Config) -> None:
    callback_answer.text = "Включено" if await settings.toggle(callback_data.v) else "Выключено"
    await show(call, *await main_screen(db, settings, config))


@router.callback_query(A.filter((F.s == "rl") & (F.a == "case")))
async def cb_case(call: CallbackQuery, callback_data: A, db: Database) -> None:
    await show(call, *await case_screen(db, callback_data.id))


@router.callback_query(A.filter((F.s == "rl") & (F.a == "toggle")))
async def cb_toggle(call: CallbackQuery, callback_data: A, db: Database) -> None:
    case = await db.get_roulette_case(callback_data.id)
    if case:
        await db.update_roulette_case(case["id"], is_active=int(not case["is_active"]))
    await show(call, *await case_screen(db, callback_data.id))


@router.callback_query(A.filter((F.s == "rl") & (F.a == "del")))
async def cb_delete_confirm(call: CallbackQuery, callback_data: A, db: Database) -> None:
    case = await db.get_roulette_case(callback_data.id)
    if not case:
        return
    await show(call, f"🗑 <b>Удалить кейс «{esc(case['name'])}»?</b>\n\nИстория прокруток сохранится.", kb(
        [btn("🗑 Да, удалить", "rl", "del_ok", id=case["id"], style="danger")],
        back("rl", "case", "« Отмена", id=case["id"]),
    ))


@router.callback_query(A.filter((F.s == "rl") & (F.a == "del_ok")))
async def cb_delete(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                    settings: Settings, config: Config) -> None:
    await db.delete_roulette_case(callback_data.id)
    callback_answer.text = "🗑 Кейс удалён"
    await show(call, *await main_screen(db, settings, config))


# ---------- создание и редактирование кейса ----------

@router.callback_query(A.filter((F.s == "rl") & (F.a == "new")))
async def cb_new(call: CallbackQuery, state: FSMContext) -> None:
    await prompt(call, state, Input.rl_name, "➕ <b>Новый кейс</b>\n\nКак он будет называться в мини-аппе? "
                                             "Например: <i>Романтика</i>", back("rl", text="✖️ Отмена"), case_id=0)


@router.callback_query(A.filter((F.s == "rl") & (F.a == "rename")))
async def cb_rename(call: CallbackQuery, callback_data: A, state: FSMContext) -> None:
    await prompt(call, state, Input.rl_name, "✏️ Новое название кейса (до 24 символов):",
                 back("rl", "case", "✖️ Отмена", id=callback_data.id), case_id=callback_data.id)


@router.message(Input.rl_name, F.text)
async def on_name(message: Message, state: FSMContext, db: Database) -> None:
    name = message.text.strip()
    if not 1 <= len(name) <= 24:
        await message.answer("⚠️ Название — от 1 до 24 символов")
        return
    data = await drop_prompt(message, state)
    if data.get("case_id"):
        await state.clear()
        await db.update_roulette_case(data["case_id"], name=name)
        await show(message, *await case_screen(db, data["case_id"]))
        return
    await state.set_state(Input.rl_price)
    msg = await message.answer(f"💰 Сколько звёзд стоит прокрутка кейса «{esc(name)}»? Например <code>25</code>",
                               reply_markup=kb(back("rl", text="✖️ Отмена")))
    await state.update_data(name=name, prompt_id=msg.message_id)


@router.callback_query(A.filter((F.s == "rl") & (F.a == "price")))
async def cb_price(call: CallbackQuery, callback_data: A, state: FSMContext) -> None:
    await prompt(call, state, Input.rl_price, "💰 Новая цена прокрутки в звёздах (от 1 до 10 000):",
                 back("rl", "case", "✖️ Отмена", id=callback_data.id), case_id=callback_data.id)


@router.message(Input.rl_price, F.text)
async def on_price(message: Message, state: FSMContext, db: Database) -> None:
    raw = message.text.strip().rstrip("⭐ ")
    if not raw.isdigit() or not 1 <= int(raw) <= 10_000:
        await message.answer("⚠️ Нужно целое число звёзд от 1 до 10 000")
        return
    data = await drop_prompt(message, state)
    await state.clear()
    if data.get("case_id"):
        await db.update_roulette_case(data["case_id"], price=int(raw))
        case_id = data["case_id"]
    else:
        case_id = await db.create_roulette_case(data["name"], int(raw))
        await message.answer("✅ Кейс создан — добавьте в него призы")
    await show(message, *await case_screen(db, case_id))


# ---------- призы ----------

@router.callback_query(A.filter((F.s == "rl") & (F.a == "add")))
async def cb_add_prize(call: CallbackQuery, callback_data: A, catalog: GiftCatalog) -> None:
    gifts = sorted(await catalog.gifts(), key=lambda g: g.star_count)
    buttons = [btn(f"{gift_emoji(g)} {g.star_count}⭐", "rl", "pick", id=callback_data.id, v=g.id) for g in gifts]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append(back("rl", "case", "✖️ Отмена", id=callback_data.id))
    await show(call, "🎁 <b>Какой подарок добавить в кейс?</b>" if gifts else "⚠️ Не удалось загрузить подарки",
               kb(*rows))


@router.callback_query(A.filter((F.s == "rl") & (F.a == "pick")))
async def cb_pick(call: CallbackQuery, callback_data: A, state: FSMContext, catalog: GiftCatalog) -> None:
    gift = await catalog.get(callback_data.v)
    if not gift:
        return
    await prompt(call, state, Input.rl_weight,
                 f"🎲 <b>Шанс выпадения {gift_emoji(gift)} {gift.star_count} ⭐</b>\n\n"
                 "Пришлите шанс в процентах, например <code>25</code> или <code>0.806</code>.\n"
                 "<i>Если сумма шансов призов не равна 100, бот пересчитает их пропорционально.</i>",
                 back("rl", "case", "✖️ Отмена", id=callback_data.id),
                 case_id=callback_data.id, gift_id=gift.id, emoji=gift_emoji(gift), price=gift.star_count)


@router.callback_query(A.filter((F.s == "rl") & (F.a == "prize")))
async def cb_prize(call: CallbackQuery, callback_data: A, db: Database) -> None:
    prize = await db.get_roulette_prize(callback_data.id)
    if not prize:
        return
    prizes = await db.roulette_prizes(prize["case_id"])
    total = sum(p["weight"] for p in prizes)
    await show(call, f"{prize['gift_emoji']} <b>{prize['gift_price']} ⭐</b>\n\n"
                     f"Вес: <b>{prize['weight']:g}</b> → шанс в мини-аппе <b>{fmt_chance(chance(prize, total))}</b>",
               kb([btn("✏️ Изменить шанс", "rl", "weight", id=prize["id"], style="primary"),
                   btn("🗑 Убрать", "rl", "prize_del", id=prize["id"], style="danger")],
                  back("rl", "case", "« К кейсу", id=prize["case_id"])))


@router.callback_query(A.filter((F.s == "rl") & (F.a == "weight")))
async def cb_weight(call: CallbackQuery, callback_data: A, state: FSMContext, db: Database) -> None:
    prize = await db.get_roulette_prize(callback_data.id)
    if not prize:
        return
    await prompt(call, state, Input.rl_weight,
                 f"🎲 Новый шанс для {prize['gift_emoji']} {prize['gift_price']} ⭐ в процентах "
                 f"(сейчас вес {prize['weight']:g}):",
                 back("rl", "prize", "✖️ Отмена", id=prize["id"]), prize_id=prize["id"], case_id=prize["case_id"])


@router.message(Input.rl_weight, F.text)
async def on_weight(message: Message, state: FSMContext, db: Database) -> None:
    raw = message.text.strip().rstrip("%").replace(",", ".").strip()
    try:
        weight = float(raw)
    except ValueError:
        weight = -1
    if not 0 < weight <= 100:
        await message.answer("⚠️ Пришлите число больше 0 и не больше 100, например <code>25</code> или <code>0.806</code>")
        return
    data = await drop_prompt(message, state)
    await state.clear()
    if data.get("prize_id"):
        await db.set_prize_weight(data["prize_id"], weight)
    else:
        await db.add_roulette_prize(data["case_id"], data["gift_id"], data["emoji"], data["price"], weight)
    await show(message, *await case_screen(db, data["case_id"]))


@router.callback_query(A.filter((F.s == "rl") & (F.a == "prize_del")))
async def cb_prize_delete(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer,
                          db: Database) -> None:
    prize = await db.get_roulette_prize(callback_data.id)
    if not prize:
        return
    await db.delete_roulette_prize(prize["id"])
    callback_answer.text = "🗑 Приз убран"
    await show(call, *await case_screen(db, prize["case_id"]))
