import re
import secrets
import string
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, CopyTextButton, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.callbacks import A
from bot.config import Config
from bot.database import Database
from bot.handlers.admin.common import Btn, Input, back, btn, drop_prompt, kb, pager, pages_count, prompt
from bot.handlers.admin.checks import IMAGE_HINT, read_image, send_preview, status_icon
from bot.handlers.admin.home import day_start, export_csv, star_balance
from bot.handlers.user import AD_PREFIX
from bot.services.checks import MAX_ACTIVATIONS, PASSWORD_MAX, CheckService
from bot.services.gifts import GiftImages, gift_emoji
from bot.settings import Settings
from bot.utils import esc, fmt_dt, fmt_num, percent, progress_bar, render_template, show

router = Router(name="admin_ads")

PER_PAGE = 8
PERIODS = (7, 14, 30)
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{2,32}$")
KEEP = {"keep_state": True}
MAX_CHECK_TEXT = 900  # подпись под картинкой — до 1024 символов, с запасом на пометку о пароле


def ad_url(bot_username: str, code: str) -> str:
    return f"https://t.me/{bot_username}?start={AD_PREFIX}{code}"


def random_code() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


async def free_code(db: Database) -> str:
    code = random_code()
    while await db.get_ad_link_by_code(code):
        code = random_code()
    return code


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
                     "новых пользователей, подписки, приведённых друзей и стоимость каждого.\n\n"
                     "🎟 <b>Рекламный чек</b> — пост с чеком на подарки (например, 100 мишек): "
                     "та же статистика плюс активации чека и потраченные звёзды.\n")
    if not links:
        lines.append("<i>Пока пусто.</i>")
    for link in links:
        conv = percent(link["verified"], link["new_users"])
        lines.append(f"• {'🎟 ' if link['has_check'] else ''}<b>{esc(link['name'])}</b> — 👆 {fmt_num(link['clicks'])} · "
                     f"🆕 {fmt_num(link['new_users'])} · ✅ {conv}%")

    v = "arch" if archived else ""
    rows = [[btn(f"{'🎟' if link['has_check'] else '📎'} {link['name']} · 🆕 {link['new_users']}", "lk", "card",
                 id=link["id"])] for link in links]
    rows.append(pager("lk", "open", page, pages, v=v))
    if not archived:
        rows.append([btn("➕ Создать ссылку", "lk", "new", style="success"),
                     btn("🎟 Рекламный чек", "lk", "cnew", style="primary")])
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

async def card_screen(db: Database, settings: Settings, config: Config, checks: CheckService, bot_username: str,
                      link_id: int, period: int = 7):
    link = await db.get_ad_link(link_id)
    if not link:
        return "❌ Ссылка не найдена", kb(back("lk"))
    period = period if period in PERIODS else 7
    st = await db.ad_link_stats(link_id, settings.goal, day_start(config))
    check = await db.ad_link_check(link_id)
    url = checks.url(check["code"]) if check else ad_url(bot_username, link["code"])
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
    ]
    if check:
        cst = await db.check_stats(check["id"])
        price = checks.price(check)
        status = {"⏸": "выключен", "⚪️": "закончился", "🟢": "активен"}[status_icon(check)]
        lines += [
            "",
            f"🎟 <b>Чек</b> <code>{check['code']}</code> · {status_icon(check)} {status} · "
            f"{checks.emoji(check)} {price} ⭐",
            f"Активации: <b>{fmt_num(check['used'])} / {fmt_num(check['total'])}</b> · "
            f"осталось {fmt_num(check['total'] - check['used'])}",
            f"{progress_bar(check['used'], check['total'])} {percent(check['used'], check['total'])}%",
            f"💬 Текст: {'свой' if check['caption'] else 'стандартный'} · "
            f"🖼 картинка: {'своя' if check['photo'] else 'подарка'} · "
            + (f"🔐 пароль <code>{esc(check['password'])}</code>" if check["password"] else "🔓 без пароля"),
            f"🆕 Новых через чек: <b>{fmt_num(cst['new_users'])}</b>",
            f"🎁 Подарки: ✅ {cst['sent']} · ⏳ {cst['pending']} · ❌ {cst['rejected']} · "
            f"потрачено <b>{fmt_num(cst['sent'] * price)}</b> ⭐",
        ]
        if cst["first_at"]:
            lines.append(f"🕒 Активации: с {fmt_dt(cst['first_at'], config.tz)} по {fmt_dt(cst['last_at'], config.tz)}")
    lines += [
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
            *([f"Активация чека: <b>{per(cost, check['used'])}</b>"] if check else []),
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
        [Btn(text="📤 Отправить пост", switch_inline_query=f"#{check['code']}"),
         btn("👁 Пост с чеком", "lk", "cpost", id=lid)] if check else [],
        [btn("💬 Текст", "lk", "ctext", id=lid), btn("🖼 Картинка", "lk", "cphoto", id=lid),
         btn("🔐 Пароль", "lk", "cpw", id=lid)] if check else [],
        [btn("🎟 Открыть чек", "ck", "card", id=check["id"])] if check
        else [btn("🎟 Добавить чек на подарки", "lk", "cadd", id=lid, style="primary")],
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
                  checks: CheckService, bot_username: str) -> None:
    await show(call, *await card_screen(db, settings, config, checks, bot_username, callback_data.id,
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
                     settings: Settings, config: Config, checks: CheckService, bot_username: str) -> None:
    link = await db.get_ad_link(callback_data.id)
    if not link:
        return
    await db.update_ad_link(link["id"], is_archived=int(not link["is_archived"]))
    callback_answer.text = "Возвращена из архива" if link["is_archived"] else "🗂 Перенесена в архив — переходы " \
                                                                              "продолжают считаться"
    await show(call, *await card_screen(db, settings, config, checks, bot_username, link["id"]))


@router.callback_query(A.filter((F.s == "lk") & (F.a == "del")))
async def cb_delete_confirm(call: CallbackQuery, callback_data: A, db: Database) -> None:
    link = await db.get_ad_link(callback_data.id)
    if not link:
        return
    await show(call, f"🗑 <b>Удалить ссылку «{esc(link['name'])}»?</b>\n\n"
                     "Статистика по ней пропадёт, а переходы по ссылке перестанут считаться. "
                     "Пользователи останутся в боте, рекламный чек продолжит работать.\n\n"
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
                  config: Config, checks: CheckService, bot_username: str, code: str) -> None:
    data = await state.get_data()
    await state.clear()
    link_id = await db.create_ad_link(code, data["name"], event.from_user.id)
    await show(event, *await card_screen(db, settings, config, checks, bot_username, link_id))


@router.message(Input.ad_code, F.text)
async def on_code(message: Message, state: FSMContext, db: Database, settings: Settings, config: Config,
                  checks: CheckService, bot_username: str) -> None:
    code = message.text.strip()
    if not CODE_RE.match(code):
        await message.answer("⚠️ Только латиница, цифры, <code>_</code> и <code>-</code>, от 2 до 32 символов")
        return
    if await db.get_ad_link_by_code(code):
        await message.answer("⚠️ Такой код уже занят — придумайте другой")
        return
    await drop_prompt(message, state)
    await message.answer("✅ Ссылка создана — копируйте и запускайте рекламу")
    await _create(message, state, db, settings, config, checks, bot_username, code)


@router.callback_query(A.filter((F.s == "lk") & (F.a == "rnd")), Input.ad_code, flags=KEEP)
async def cb_random(call: CallbackQuery, callback_answer: CallbackAnswer, state: FSMContext, db: Database,
                    settings: Settings, config: Config, checks: CheckService, bot_username: str) -> None:
    callback_answer.text = "✅ Ссылка создана"
    await _create(call, state, db, settings, config, checks, bot_username, await free_code(db))


# ---------- редактирование ----------

@router.callback_query(A.filter((F.s == "lk") & (F.a == "rename")))
async def cb_rename(call: CallbackQuery, callback_data: A, state: FSMContext) -> None:
    await prompt(call, state, Input.ad_rename, "✏️ Пришлите новое название ссылки (до 64 символов):",
                 back("lk", "card", "✖️ Отмена", id=callback_data.id), link_id=callback_data.id)


@router.message(Input.ad_rename, F.text)
async def on_rename(message: Message, state: FSMContext, db: Database, settings: Settings, config: Config,
                    checks: CheckService, bot_username: str) -> None:
    name = message.text.strip()
    if not 1 <= len(name) <= 64:
        await message.answer("⚠️ Название должно быть от 1 до 64 символов")
        return
    data = await drop_prompt(message, state)
    await state.clear()
    await db.update_ad_link(data["link_id"], name=name)
    await show(message, *await card_screen(db, settings, config, checks, bot_username, data["link_id"]))


@router.callback_query(A.filter((F.s == "lk") & (F.a == "cost")))
async def cb_cost(call: CallbackQuery, callback_data: A, state: FSMContext) -> None:
    await prompt(call, state, Input.ad_cost,
                 "💰 <b>Сколько стоила реклама?</b>\n\n"
                 "Пришлите сумму в рублях, например <code>5000</code> или <code>1499.90</code>.\n"
                 "Бот посчитает цену перехода, нового пользователя и подписчика. <code>0</code> — убрать.",
                 back("lk", "card", "✖️ Отмена", id=callback_data.id), link_id=callback_data.id)


@router.message(Input.ad_cost, F.text)
async def on_cost(message: Message, state: FSMContext, db: Database, settings: Settings, config: Config,
                  checks: CheckService, bot_username: str) -> None:
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
    await show(message, *await card_screen(db, settings, config, checks, bot_username, data["link_id"]))


# ---------- рекламный чек ----------

POST_HINT = ("👆 <b>Готовый пост с чеком</b> — перешлите его рекламщику или в канал (кнопка сохранится), "
             "либо отправьте в любой чат кнопкой «📤 Отправить пост».\n\n"
             "Переходы по чеку, новые пользователи, подписки и активации — в статистике рекламы.")
TEXT_HINT = ("Форматирование (жирный, курсив, ссылки, спойлеры, премиум-эмодзи) сохранится.\n"
             "Переменные: <code>{gift}</code> — эмодзи подарка, <code>{count}</code> — число активаций.")
PASSWORD_HINT = ("В тексте поста пароля не будет — укажите его сами, например в рекламном посте или в комментариях. "
                 "Пользователь введёт его в боте; регистр не важен, 5 ошибок — пауза 10 минут.")


async def post_photo(check, db: Database, settings: Settings, gift_images: GiftImages) -> str | None:
    """Картинка поста — как у inline-чека: своя, баннер подарка, общий баннер со значком или без картинки."""
    if check["photo"]:
        return check["photo"]
    gift = await gift_images.catalog.get(check["gift_id"] or "")
    if gift:
        return await gift_images.file_id(gift)
    return await db.get_gift_banner(check["gift_id"] or "") or settings.get("check_photo") or None


def cancel_check(data: dict) -> list[Btn]:
    if data.get("link_id"):
        return back("lk", "card", "✖️ Отмена", id=data["link_id"])
    return back("lk", text="✖️ Отмена")


def step(data: dict, n: int) -> str:
    """«шаг n из N»: у новой рекламы первый шаг — название, у существующей его нет."""
    total = 5
    if data.get("link_id"):
        n, total = n - 1, total - 1
    return f"шаг {n} из {total}"


def check_text_error(plain: str) -> str | None:
    if len(plain) > MAX_CHECK_TEXT:
        return f"⚠️ Слишком длинно: {len(plain)}/{MAX_CHECK_TEXT} символов — это подпись под картинкой"
    return None


@router.callback_query(A.filter((F.s == "lk") & (F.a == "cnew")))
async def cb_check_new(call: CallbackQuery, state: FSMContext) -> None:
    await prompt(call, state, Input.adc_name,
                 f"🎟 <b>Рекламный чек — {step({}, 1)}</b>\n\n"
                 "Пост с чеком на подарки для рекламы: бот посчитает переходы, новых пользователей, подписки, "
                 "активации и потраченные звёзды.\n\n"
                 "Как назвать рекламу? Название видите только вы.\n"
                 "Например: <i>Канал @crypto_news, пост 25.09</i>",
                 back("lk", text="✖️ Отмена"))


@router.callback_query(A.filter((F.s == "lk") & (F.a == "cadd")))
async def cb_check_add(call: CallbackQuery, callback_data: A, state: FSMContext, db: Database) -> None:
    link = await db.get_ad_link(callback_data.id)
    if not link:
        return
    await prompt(call, state, Input.adc_total,
                 f"🎟 <b>Чек для «{esc(link['name'])}» — {step({'link_id': link['id']}, 2)}</b>\n\n"
                 "Сколько подарков в чеке? Пришлите число активаций, например <code>100</code>.",
                 back("lk", "card", "✖️ Отмена", id=link["id"]), link_id=link["id"])


@router.message(Input.adc_name, F.text)
async def on_check_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not 1 <= len(name) <= 64:
        await message.answer("⚠️ Название должно быть от 1 до 64 символов")
        return
    await drop_prompt(message, state)
    await state.set_state(Input.adc_total)
    msg = await message.answer(f"🎟 <b>{step({}, 2).capitalize()} — сколько подарков?</b>\n\n"
                               "Пришлите число активаций чека, например <code>100</code>. "
                               "Каждый человек активирует чек один раз.",
                               reply_markup=kb(back("lk", text="✖️ Отмена")))
    await state.update_data(name=name, prompt_id=msg.message_id)


@router.message(Input.adc_total, F.text)
async def on_check_total(message: Message, state: FSMContext, settings: Settings) -> None:
    raw = message.text.strip().replace(" ", "")
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_ACTIVATIONS:
        await message.answer(f"⚠️ Пришлите число от 1 до {fmt_num(MAX_ACTIVATIONS)}")
        return
    data = await drop_prompt(message, state)
    await state.set_state(Input.adc_text)
    msg = await message.answer(
        f"💬 <b>{step(data, 3).capitalize()} — текст чека</b>\n\n"
        "Пришлите текст, который будет в посте над кнопкой «Забрать».\n" + TEXT_HINT + "\n\n"
        "Сейчас стандартный текст (меняется в «Тексты → Подпись чека»):\n"
        f"<blockquote>{render_template(settings.get('check_caption'), gift=settings.get('gift_emoji'), count=raw)}"
        "</blockquote>",
        reply_markup=kb([btn("⏭ Оставить стандартный", "lk", "ctext_skip", style="primary")], cancel_check(data)),
    )
    await state.update_data(total=int(raw), prompt_id=msg.message_id)


async def ask_password(event: Message | CallbackQuery, state: FSMContext, data: dict) -> None:
    await state.set_state(Input.adc_password)
    msg = await show(event, f"🔐 <b>{step(data, 4).capitalize()} — пароль</b>\n\n"
                            "Пришлите пароль, если чек должен работать только с ним (до "
                            f"{PASSWORD_MAX} символов), или создайте чек без пароля.\n\n" + PASSWORD_HINT,
                     kb([btn("🔓 Без пароля", "lk", "cpw_skip", style="primary")], cancel_check(data)))
    await state.update_data(prompt_id=msg.message_id if msg else None)


@router.message(Input.adc_text, F.text)
async def on_check_text(message: Message, state: FSMContext) -> None:
    if error := check_text_error(message.text):
        await message.answer(error)
        return
    data = await drop_prompt(message, state)
    await state.update_data(caption=message.html_text)
    await ask_password(message, state, data)


@router.callback_query(A.filter((F.s == "lk") & (F.a == "ctext_skip")), Input.adc_text, flags=KEEP)
async def cb_check_text_skip(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(caption=None)
    await ask_password(call, state, await state.get_data())


async def ask_gift(event: Message | CallbackQuery, state: FSMContext, bot: Bot, settings: Settings,
                   gift_images: GiftImages) -> None:
    data = await state.get_data()
    total = data["total"]
    default_id = settings.get("gift_id")
    gifts = sorted(await gift_images.catalog.gifts(), key=lambda g: (g.id != default_id, g.star_count))
    if not gifts:
        await show(event, "⚠️ Не удалось загрузить подарки — попробуйте ещё раз", kb(cancel_check(data)))
        return
    balance = await star_balance(bot)
    await state.set_state(Input.adc_gift)
    rows = [[btn(f"{gift_emoji(g)} {g.star_count} ⭐ × {fmt_num(total)} = {fmt_num(g.star_count * total)} ⭐",
                 "lk", "cgift", v=g.id, style="primary" if g.id == default_id else None)] for g in gifts[:30]]
    text = (f"🎁 <b>{step(data, 5).capitalize()} — какой подарок?</b>\n\n"
            f"Чек на <b>{fmt_num(total)}</b> активаций"
            + (f" · 🔐 пароль <code>{esc(data['password'])}</code>" if data.get("password") else "")
            + ". Звёзды списываются при каждой активации, а не сразу.")
    if balance is not None:
        text += f"\n⭐ Баланс бота: <b>{fmt_num(balance)}</b>"
        if settings.get("reward_mode") == "auto" and balance < gifts[0].star_count * total:
            text += " — на весь чек может не хватить, остальное уйдёт в заявки"
    msg = await show(event, text, kb(*rows, cancel_check(data)))
    await state.update_data(prompt_id=msg.message_id if msg else None)


@router.message(Input.adc_password, F.text)
async def on_check_password(message: Message, state: FSMContext, bot: Bot, settings: Settings,
                            gift_images: GiftImages) -> None:
    password = message.text.strip()
    if not 1 <= len(password) <= PASSWORD_MAX:
        await message.answer(f"⚠️ Пароль — от 1 до {PASSWORD_MAX} символов")
        return
    await drop_prompt(message, state)
    await state.update_data(password=password)
    await ask_gift(message, state, bot, settings, gift_images)


@router.callback_query(A.filter((F.s == "lk") & (F.a == "cpw_skip")), Input.adc_password, flags=KEEP)
async def cb_check_password_skip(call: CallbackQuery, state: FSMContext, bot: Bot, settings: Settings,
                                 gift_images: GiftImages) -> None:
    await state.update_data(password=None)
    await ask_gift(call, state, bot, settings, gift_images)


@router.callback_query(A.filter((F.s == "lk") & (F.a == "cgift")), Input.adc_gift, flags=KEEP)
async def cb_check_gift(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, state: FSMContext,
                        db: Database, settings: Settings, config: Config, checks: CheckService,
                        gift_images: GiftImages, bot_username: str) -> None:
    gift = await gift_images.catalog.get(callback_data.v)
    if not gift:
        callback_answer.text = "⚠️ Подарок больше недоступен — выберите другой"
        return
    data = await state.get_data()
    await state.clear()
    link_id = data.get("link_id") or await db.create_ad_link(await free_code(db), data["name"], call.from_user.id)
    check = await checks.create_ad_check(call.from_user.id, data["total"], gift, link_id,
                                         data.get("caption"), data.get("password"))
    callback_answer.text = "✅ Рекламный чек создан"
    try:
        await call.message.delete()
    except TelegramBadRequest:
        pass
    await send_post(call.message, check, db, settings, checks, gift_images)
    await show(call.message, *await card_screen(db, settings, config, checks, bot_username, link_id))


async def send_post(message: Message, check, db: Database, settings: Settings, checks: CheckService,
                    gift_images: GiftImages, markup=None) -> None:
    await send_preview(message, checks, await post_photo(check, db, settings, gift_images), check)
    hint = POST_HINT
    if check["password"]:
        hint += f"\n\n🔐 Пароль: <code>{esc(check['password'])}</code> — не забудьте указать его в рекламе."
    await message.answer(hint, reply_markup=markup)


@router.callback_query(A.filter((F.s == "lk") & (F.a == "cpost")))
async def cb_check_post(call: CallbackQuery, callback_data: A, db: Database, settings: Settings,
                        checks: CheckService, gift_images: GiftImages) -> None:
    check = await db.ad_link_check(callback_data.id)
    if not check:
        return
    await send_post(call.message, check, db, settings, checks, gift_images,
                    kb(back("lk", "card", "« К статистике", id=callback_data.id)))


# ---------- рекламный чек: изменить текст и пароль ----------

@router.callback_query(A.filter((F.s == "lk") & (F.a == "ctext")))
async def cb_check_text_edit(call: CallbackQuery, callback_data: A, state: FSMContext, db: Database,
                             checks: CheckService) -> None:
    check = await db.ad_link_check(callback_data.id)
    if not check:
        return
    rows = [back("lk", "card", "✖️ Отмена", id=callback_data.id)]
    if check["caption"]:
        rows.insert(0, [btn("↩️ Вернуть стандартный", "lk", "ctext_reset", id=callback_data.id)])
    await state.set_state(Input.adc_edit_text)
    msg = await show(call, "💬 <b>Новый текст чека</b>\n\n" + TEXT_HINT + "\n\n"
                           f"Сейчас:\n<blockquote>{checks.caption(check)}</blockquote>\n\n"
                           "<i>Уже опубликованные посты не изменятся — после правки возьмите новый пост "
                           "«👁 Пост с чеком».</i>", kb(*rows))
    await state.update_data(prompt_id=msg.message_id if msg else None, link_id=callback_data.id)


@router.message(Input.adc_edit_text, F.text)
async def on_check_text_edit(message: Message, state: FSMContext, db: Database, settings: Settings, config: Config,
                             checks: CheckService, bot_username: str) -> None:
    if error := check_text_error(message.text):
        await message.answer(error)
        return
    data = await drop_prompt(message, state)
    await state.clear()
    if check := await db.ad_link_check(data["link_id"]):
        await db.update_check(check["id"], caption=message.html_text, caption_html=1)
    await message.answer("✅ Текст чека сохранён")
    await show(message, *await card_screen(db, settings, config, checks, bot_username, data["link_id"]))


@router.callback_query(A.filter((F.s == "lk") & (F.a == "ctext_reset")))
async def cb_check_text_reset(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                              settings: Settings, config: Config, checks: CheckService, bot_username: str) -> None:
    if check := await db.ad_link_check(callback_data.id):
        await db.update_check(check["id"], caption=None, caption_html=0)
    callback_answer.text = "↩️ Стандартный текст"
    await show(call, *await card_screen(db, settings, config, checks, bot_username, callback_data.id))


@router.callback_query(A.filter((F.s == "lk") & (F.a == "cpw")))
async def cb_check_password_edit(call: CallbackQuery, callback_data: A, state: FSMContext, db: Database) -> None:
    check = await db.ad_link_check(callback_data.id)
    if not check:
        return
    rows = [back("lk", "card", "✖️ Отмена", id=callback_data.id)]
    if check["password"]:
        rows.insert(0, [btn("🔓 Убрать пароль", "lk", "cpw_off", id=callback_data.id, style="danger")])
    await state.set_state(Input.adc_edit_password)
    current = f"<code>{esc(check['password'])}</code>" if check["password"] else "нет"
    msg = await show(call, f"🔐 <b>Пароль чека</b> — сейчас {current}\n\n"
                           f"Пришлите новый пароль (до {PASSWORD_MAX} символов). Применится сразу.\n\n"
                           + PASSWORD_HINT, kb(*rows))
    await state.update_data(prompt_id=msg.message_id if msg else None, link_id=callback_data.id)


@router.message(Input.adc_edit_password, F.text)
async def on_check_password_edit(message: Message, state: FSMContext, db: Database, settings: Settings,
                                 config: Config, checks: CheckService, bot_username: str) -> None:
    password = message.text.strip()
    if not 1 <= len(password) <= PASSWORD_MAX:
        await message.answer(f"⚠️ Пароль — от 1 до {PASSWORD_MAX} символов")
        return
    data = await drop_prompt(message, state)
    await state.clear()
    if check := await db.ad_link_check(data["link_id"]):
        await db.update_check(check["id"], password=password)
    await message.answer("✅ Пароль сохранён")
    await show(message, *await card_screen(db, settings, config, checks, bot_username, data["link_id"]))


@router.callback_query(A.filter((F.s == "lk") & (F.a == "cpw_off")))
async def cb_check_password_off(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                                settings: Settings, config: Config, checks: CheckService, bot_username: str) -> None:
    if check := await db.ad_link_check(callback_data.id):
        await db.update_check(check["id"], password=None)
    callback_answer.text = "🔓 Пароль убран — чек работает без него"
    await show(call, *await card_screen(db, settings, config, checks, bot_username, callback_data.id))


@router.callback_query(A.filter((F.s == "lk") & (F.a == "cphoto")))
async def cb_check_photo(call: CallbackQuery, callback_data: A, state: FSMContext, db: Database) -> None:
    check = await db.ad_link_check(callback_data.id)
    if not check:
        return
    rows = [back("lk", "card", "✖️ Отмена", id=callback_data.id)]
    if check["photo"]:
        rows.insert(0, [btn("↩️ Вернуть картинку подарка", "lk", "cphoto_off", id=callback_data.id)])
    await state.set_state(Input.adc_photo)
    msg = await show(call, "🖼 <b>Картинка чека</b> — сейчас "
                           f"{'своя' if check['photo'] else 'баннер подарка'}\n\n" + IMAGE_HINT.replace(
                               "Применится к новым отправкам чеков.",
                               "Уже опубликованные посты не изменятся — возьмите новый «👁 Пост с чеком»."),
                     kb(*rows))
    await state.update_data(prompt_id=msg.message_id if msg else None, link_id=callback_data.id)


@router.message(Input.adc_photo)
async def on_check_photo(message: Message, state: FSMContext, bot: Bot, db: Database, settings: Settings,
                         config: Config, checks: CheckService, gift_images: GiftImages, bot_username: str) -> None:
    file_id = await read_image(message, bot)
    if not file_id:
        return
    data = await drop_prompt(message, state)
    await state.clear()
    check = await db.ad_link_check(data["link_id"])
    if check:
        await db.update_check(check["id"], photo=file_id)
        await message.answer("✅ Картинка сохранена. Так выглядит пост:")
        await send_post(message, await db.get_check(check["id"]), db, settings, checks, gift_images)
    await show(message, *await card_screen(db, settings, config, checks, bot_username, data["link_id"]))


@router.callback_query(A.filter((F.s == "lk") & (F.a == "cphoto_off")))
async def cb_check_photo_off(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                             settings: Settings, config: Config, checks: CheckService, bot_username: str) -> None:
    if check := await db.ad_link_check(callback_data.id):
        await db.update_check(check["id"], photo=None)
    callback_answer.text = "↩️ Картинка подарка"
    await show(call, *await card_screen(db, settings, config, checks, bot_username, callback_data.id))
