import re
import secrets
import string
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, CopyTextButton, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.callbacks import A
from bot.config import Config
from bot.database import Database
from bot.handlers.admin.common import Btn, Input, back, btn, drop_prompt, kb, pager, pages_count, prompt
from bot.handlers.admin.home import day_start, export_csv
from bot.handlers.user import AD_PREFIX
from bot.settings import Settings
from bot.utils import esc, fmt_dt, fmt_num, percent, show

router = Router(name="admin_ads")

PER_PAGE = 8
PERIODS = (7, 14, 30)
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{2,32}$")
KEEP = {"keep_state": True}


def ad_url(bot_username: str, code: str) -> str:
    return f"https://t.me/{bot_username}?start={AD_PREFIX}{code}"


def random_code() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


def money(value: float) -> str:
    if value >= 100:
        return f"{fmt_num(round(value))} ₽"
    return f"{value:.2f}".rstrip("0").rstrip(".") + " ₽"


def per(cost: float, count: int) -> str:
    return money(cost / count) if count else "—"


# ---------- список ----------

async def list_screen(db: Database, archived: bool, page: int):
    total = await db.count_ad_links(archived)
    other = await db.count_ad_links(not archived)
    pages = pages_count(total, PER_PAGE)
    page = max(0, min(page, pages - 1))
    links = await db.list_ad_links(archived, PER_PAGE, page * PER_PAGE)

    lines = ["🗂 <b>Архив рекламных ссылок</b>\n" if archived else "📎 <b>Рекламные ссылки</b>\n"]
    if not archived:
        lines.append("Создайте отдельную ссылку для каждой площадки или поста — бот посчитает переходы, "
                     "новых пользователей, подписки, приведённых друзей и стоимость каждого.\n")
    if not links:
        lines.append("<i>Пока пусто.</i>")
    for link in links:
        conv = percent(link["verified"], link["new_users"])
        lines.append(f"• <b>{esc(link['name'])}</b> — 👆 {fmt_num(link['clicks'])} · "
                     f"🆕 {fmt_num(link['new_users'])} · ✅ {conv}%")

    v = "arch" if archived else ""
    rows = [[btn(f"📎 {link['name']} · 🆕 {link['new_users']}", "lk", "card", id=link["id"])] for link in links]
    rows.append(pager("lk", "open", page, pages, v=v))
    if not archived:
        rows.append([btn("➕ Создать ссылку", "lk", "new", style="success")])
        if other:
            rows.append([btn(f"🗂 Архив · {other}", "lk", v="arch")])
        rows.append(back())
    else:
        rows.append(back("lk", text="« К активным"))
    return "\n".join(lines), kb(*rows)


@router.callback_query(A.filter((F.s == "lk") & (F.a == "open")))
async def cb_list(call: CallbackQuery, callback_data: A, db: Database) -> None:
    await show(call, *await list_screen(db, callback_data.v == "arch", callback_data.p))


# ---------- карточка со статистикой ----------

async def card_screen(db: Database, settings: Settings, config: Config, bot_username: str,
                      link_id: int, period: int = 7):
    link = await db.get_ad_link(link_id)
    if not link:
        return "❌ Ссылка не найдена", kb(back("lk"))
    period = period if period in PERIODS else 7
    st = await db.ad_link_stats(link_id, settings.goal, day_start(config))
    url = ad_url(bot_username, link["code"])
    cost = link["cost"] or 0.0
    new, verified = st["new_users"], st["verified"]

    lines = [
        f"📎 <b>{esc(link['name'])}</b>" + (" · 🗂 в архиве" if link["is_archived"] else ""),
        f"<code>{url}</code>",
        "",
        f"📅 Создана: {fmt_dt(link['created_at'], config.tz)}",
        f"💰 Стоимость: <b>{money(cost)}</b>" if cost else "💰 Стоимость: <i>не указана</i>",
        "",
        "📊 <b>Воронка</b>",
        f"👆 Переходов: <b>{fmt_num(st['clicks'])}</b> "
        f"(уникальных {fmt_num(st['unique_clicks'])}, сегодня +{st['clicks_today']})",
        f"🆕 Новых пользователей: <b>{fmt_num(new)}</b> — {percent(new, st['unique_clicks'])}% "
        f"(сегодня +{st['new_today']})",
        f"↩️ Уже были в боте: <b>{fmt_num(st['returning_users'])}</b>",
        f"✅ Прошли подписку: <b>{fmt_num(verified)}</b> — {percent(verified, new)}% от новых",
        f"👥 Пригласили друзей: <b>{fmt_num(st['inviters'])}</b> чел. → "
        f"<b>{fmt_num(st['referrals'])}</b> засчитанных рефералов",
        f"🎯 Достигли цели: <b>{fmt_num(st['reached_goal'])}</b> · 🧸 наград: <b>{fmt_num(st['rewards'])}</b>",
        "",
        "📈 <b>Удержание</b>",
        f"🔥 Активны: 24 ч — <b>{fmt_num(st['active24'])}</b> · 7 дн. — <b>{fmt_num(st['active7'])}</b>",
        f"⛔ Заблокировали бота: <b>{fmt_num(st['blocked'])}</b> ({percent(st['blocked'], new)}%)",
    ]
    if cost:
        lines += [
            "",
            "💸 <b>Стоимость</b>",
            f"Переход: <b>{per(cost, st['unique_clicks'])}</b> · новый: <b>{per(cost, new)}</b>",
            f"Подписчик: <b>{per(cost, verified)}</b>",
            f"С учётом приведённых друзей: <b>{per(cost, verified + st['referrals'])}</b>",
        ]
    if st["first_click"]:
        lines += ["", f"🕒 Переходы: с {fmt_dt(st['first_click'], config.tz)} "
                      f"по {fmt_dt(st['last_click'], config.tz)}"]

    lines += ["", f"📆 <b>По дням</b> — новые · переходы, {period} дн."]
    since = day_start(config, period - 1)
    new_ts, click_ts = await db.ad_link_daily(link_id, since)
    new_b, click_b = [0] * period, [0] * period
    for ts_list, buckets in ((new_ts, new_b), (click_ts, click_b)):
        for ts in ts_list:
            idx = (ts - since) // 86400
            if 0 <= idx < period:
                buckets[idx] += 1
    peak = max(new_b) or 1
    for i in range(period):
        day = datetime.fromtimestamp(since + i * 86400, config.tz).strftime("%d.%m")
        bar = "▇" * round(new_b[i] / peak * 10) or "▏"
        lines.append(f"<code>{day} {bar:<10}</code> {new_b[i]} · {click_b[i]}")

    lid = link_id
    periods = [btn(f"{'• ' if p == period else ''}{p} дн.", "lk", "card", id=lid, p=p) for p in PERIODS]
    rows = [
        [Btn(text="📋 Скопировать ссылку", copy_text=CopyTextButton(text=url)),
         btn("🔄 Обновить", "lk", "card", id=lid, p=period)],
        periods,
        [btn("✏️ Название", "lk", "rename", id=lid), btn("💰 Стоимость", "lk", "cost", id=lid)],
        [btn("📥 Пользователи CSV", "lk", "csv", id=lid)],
        [btn("🗂 Вернуть из архива" if link["is_archived"] else "🗂 В архив", "lk", "arch", id=lid),
         btn("🗑 Удалить", "lk", "del", id=lid, style="danger")],
        back("lk", v="arch" if link["is_archived"] else "", text="« К ссылкам"),
    ]
    return "\n".join(lines), kb(*rows)


@router.callback_query(A.filter((F.s == "lk") & (F.a == "card")))
async def cb_card(call: CallbackQuery, callback_data: A, db: Database, settings: Settings, config: Config,
                  bot_username: str) -> None:
    await show(call, *await card_screen(db, settings, config, bot_username, callback_data.id,
                                        callback_data.p or 7))


@router.callback_query(A.filter((F.s == "lk") & (F.a == "csv")))
async def cb_csv(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                 config: Config) -> None:
    link = await db.get_ad_link(callback_data.id)
    if not link:
        return
    await export_csv(call, db, config, ad_link_id=link["id"], label=link["code"])
    callback_answer.text = "📥 Файл отправлен"


@router.callback_query(A.filter((F.s == "lk") & (F.a == "arch")))
async def cb_archive(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                     settings: Settings, config: Config, bot_username: str) -> None:
    link = await db.get_ad_link(callback_data.id)
    if not link:
        return
    await db.update_ad_link(link["id"], is_archived=int(not link["is_archived"]))
    callback_answer.text = "Возвращена из архива" if link["is_archived"] else "🗂 Перенесена в архив — переходы " \
                                                                              "продолжают считаться"
    await show(call, *await card_screen(db, settings, config, bot_username, link["id"]))


@router.callback_query(A.filter((F.s == "lk") & (F.a == "del")))
async def cb_delete_confirm(call: CallbackQuery, callback_data: A, db: Database) -> None:
    link = await db.get_ad_link(callback_data.id)
    if not link:
        return
    await show(call, f"🗑 <b>Удалить ссылку «{esc(link['name'])}»?</b>\n\n"
                     "Статистика по ней пропадёт, а переходы по ссылке перестанут считаться. "
                     "Пользователи останутся в боте.\n\n"
                     "<i>Если нужно просто убрать её из списка — используйте архив.</i>", kb(
        [btn("🗑 Да, удалить", "lk", "del_ok", id=link["id"], style="danger")],
        [btn("🗂 Лучше в архив", "lk", "arch", id=link["id"])],
        back("lk", "card", "« Отмена", id=link["id"]),
    ))


@router.callback_query(A.filter((F.s == "lk") & (F.a == "del_ok")))
async def cb_delete(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database) -> None:
    await db.delete_ad_link(callback_data.id)
    callback_answer.text = "🗑 Ссылка удалена"
    await show(call, *await list_screen(db, False, 0))


# ---------- создание ----------

@router.callback_query(A.filter((F.s == "lk") & (F.a == "new")))
async def cb_new(call: CallbackQuery, state: FSMContext) -> None:
    await prompt(call, state, Input.ad_name,
                 "➕ <b>Новая рекламная ссылка — шаг 1 из 2</b>\n\n"
                 "Как её назвать? Название видите только вы.\n"
                 "Например: <i>Канал @crypto_news, пост 25.09</i>",
                 back("lk", text="✖️ Отмена"))


@router.message(Input.ad_name, F.text)
async def on_name(message: Message, state: FSMContext, bot_username: str) -> None:
    name = message.text.strip()
    if not 1 <= len(name) <= 64:
        await message.answer("⚠️ Название должно быть от 1 до 64 символов")
        return
    await drop_prompt(message, state)
    await state.set_state(Input.ad_code)
    msg = await message.answer(
        "🔤 <b>Шаг 2 из 2 — код ссылки</b>\n\n"
        f"Код будет в самой ссылке: <code>t.me/{bot_username}?start={AD_PREFIX}<b>код</b></code>\n\n"
        "Пришлите свой — латиница, цифры, <code>_</code> и <code>-</code>, от 2 до 32 символов "
        "(например <code>tiktok_sept</code>) — или сгенерируйте случайный.",
        reply_markup=kb([btn("🎲 Случайный код", "lk", "rnd", style="primary")], back("lk", text="✖️ Отмена")),
    )
    await state.update_data(name=name, prompt_id=msg.message_id)


async def _create(event: Message | CallbackQuery, state: FSMContext, db: Database, settings: Settings,
                  config: Config, bot_username: str, code: str) -> None:
    data = await state.get_data()
    await state.clear()
    link_id = await db.create_ad_link(code, data["name"], event.from_user.id)
    await show(event, *await card_screen(db, settings, config, bot_username, link_id))


@router.message(Input.ad_code, F.text)
async def on_code(message: Message, state: FSMContext, db: Database, settings: Settings, config: Config,
                  bot_username: str) -> None:
    code = message.text.strip()
    if not CODE_RE.match(code):
        await message.answer("⚠️ Только латиница, цифры, <code>_</code> и <code>-</code>, от 2 до 32 символов")
        return
    if await db.get_ad_link_by_code(code):
        await message.answer("⚠️ Такой код уже занят — придумайте другой")
        return
    await drop_prompt(message, state)
    await message.answer("✅ Ссылка создана — копируйте и запускайте рекламу")
    await _create(message, state, db, settings, config, bot_username, code)


@router.callback_query(A.filter((F.s == "lk") & (F.a == "rnd")), Input.ad_code, flags=KEEP)
async def cb_random(call: CallbackQuery, callback_answer: CallbackAnswer, state: FSMContext, db: Database,
                    settings: Settings, config: Config, bot_username: str) -> None:
    code = random_code()
    while await db.get_ad_link_by_code(code):
        code = random_code()
    callback_answer.text = "✅ Ссылка создана"
    await _create(call, state, db, settings, config, bot_username, code)


# ---------- редактирование ----------

@router.callback_query(A.filter((F.s == "lk") & (F.a == "rename")))
async def cb_rename(call: CallbackQuery, callback_data: A, state: FSMContext) -> None:
    await prompt(call, state, Input.ad_rename, "✏️ Пришлите новое название ссылки (до 64 символов):",
                 back("lk", "card", "✖️ Отмена", id=callback_data.id), link_id=callback_data.id)


@router.message(Input.ad_rename, F.text)
async def on_rename(message: Message, state: FSMContext, db: Database, settings: Settings, config: Config,
                    bot_username: str) -> None:
    name = message.text.strip()
    if not 1 <= len(name) <= 64:
        await message.answer("⚠️ Название должно быть от 1 до 64 символов")
        return
    data = await drop_prompt(message, state)
    await state.clear()
    await db.update_ad_link(data["link_id"], name=name)
    await show(message, *await card_screen(db, settings, config, bot_username, data["link_id"]))


@router.callback_query(A.filter((F.s == "lk") & (F.a == "cost")))
async def cb_cost(call: CallbackQuery, callback_data: A, state: FSMContext) -> None:
    await prompt(call, state, Input.ad_cost,
                 "💰 <b>Сколько стоила реклама?</b>\n\n"
                 "Пришлите сумму в рублях, например <code>5000</code> или <code>1499.90</code>.\n"
                 "Бот посчитает цену перехода, нового пользователя и подписчика. <code>0</code> — убрать.",
                 back("lk", "card", "✖️ Отмена", id=callback_data.id), link_id=callback_data.id)


@router.message(Input.ad_cost, F.text)
async def on_cost(message: Message, state: FSMContext, db: Database, settings: Settings, config: Config,
                  bot_username: str) -> None:
    raw = message.text.strip().replace(" ", "").replace(" ", "").replace(",", ".").rstrip("₽р.")
    try:
        cost = float(raw)
    except ValueError:
        cost = -1
    if not 0 <= cost <= 1_000_000_000:
        await message.answer("⚠️ Пришлите сумму числом, например <code>5000</code>")
        return
    data = await drop_prompt(message, state)
    await state.clear()
    await db.update_ad_link(data["link_id"], cost=cost)
    await show(message, *await card_screen(db, settings, config, bot_username, data["link_id"]))
