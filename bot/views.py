"""Экраны пользовательской части: текст + клавиатура."""
import html
import re
from urllib.parse import quote

from aiogram.types import InlineKeyboardButton as Btn, InlineKeyboardMarkup, WebAppInfo
from aiosqlite import Row

from bot.callbacks import A, Shop, ShopBuy, U
from bot.services.cases import OpenResult, case_button_text, prize_label
from bot.services.checks import PASSWORD_LOCK, Activation, CheckStatus
from bot.services.rewards import calc_progress
from bot.services.shop import ShopItem
from bot.services.tasks import LINK_DELAY
from bot.settings import Settings
from bot.utils import esc, fmt_duration, fmt_stars, icon_text, plural, render_template

Screen = tuple[str, InlineKeyboardMarkup]

# Премиум-эмодзи на кнопках главного меню (нужен Telegram Premium у владельца бота, иначе — обычные эмодзи)
ICON_GIFTS = "5280519723287610631"
ICON_EARN = "5967512159033234930"
ICON_DAILY = "5280789747881512758"
ICON_SHOP = "5203996991054432397"
ICON_CHANNEL = "5850654130497916523"


def kb(*rows: list[Btn]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def icon_btn(text: str, icon_id: str | None, fallback: str, **kw) -> Btn:
    text, icon = icon_text(text, icon_id, fallback)
    return Btn(text=text, icon_custom_emoji_id=icon, **kw)


def back_to_menu() -> list[Btn]:
    return [Btn(text="◀️ В меню", callback_data=U(a="menu").pack())]


def back_to_refs() -> list[Btn]:
    return [Btn(text="◀️ Назад", callback_data=U(a="refs").pack())]


def channel_url(ch: Row) -> str | None:
    if ch["invite_link"]:
        return ch["invite_link"]
    if ch["username"]:
        return f"https://t.me/{ch['username']}"
    return None


def ref_link(bot_username: str, user_id: int) -> str:
    return f"https://t.me/{bot_username}?start=r{user_id}"


def subscribe_screen(settings: Settings, name: str, missing: list[Row], for_check: bool = False) -> Screen:
    text = render_template(settings.get("text_subscribe"), name=esc(name))
    if for_check:
        text += f"\n\n🎁 <b>Сразу после подписки получишь подарок {settings.get('gift_emoji')} по чеку!</b>"
    rows = []
    for i, ch in enumerate(missing, 1):
        url = channel_url(ch)
        if url:
            rows.append([Btn(text=f"{i}. {ch['title']}", url=url)])
    rows.append([Btn(text="✅ Я подписался", style="success", callback_data=U(a="check").pack())])
    return text, kb(*rows)


def main_menu_screen(user: Row, settings: Settings, is_admin: bool, has_nft: bool = False,
                     has_shop: bool = False) -> Screen:
    text = render_template(settings.get("text_main"), name=esc(user["full_name"]),
                           balance=fmt_stars(user["balance"]))
    rows: list[list[Btn]] = [
        [icon_btn("Получить подарки!", ICON_GIFTS, "🎁", style="success", callback_data=U(a="refs").pack())],
    ]
    if has_nft:
        rows.append([Btn(text="💎 НФТ подарки", style="primary", callback_data=U(a="nft").pack())])
    rows += [
        [icon_btn("Заработать звёзды", ICON_EARN, "⭐", callback_data=U(a="tasks").pack())],
        [icon_btn("Ежедневный кейс", ICON_DAILY, "📦", callback_data=U(a="cases").pack())],
    ]
    if has_shop:
        rows.append([icon_btn("Купить подарки", ICON_SHOP, "🛍", callback_data=U(a="shop").pack())])
    rows.append([Btn(text="👤 Профиль / Баланс", callback_data=U(a="profile").pack())])
    if url := settings.get("giveaway_url"):
        rows.append([icon_btn("Мой канал с раздачами", ICON_CHANNEL, "📣", style="primary", url=url)])
    if settings.webapp_url and settings.flag("roulette_enabled"):
        rows.append([Btn(text="🎰 Рулетка подарков", web_app=WebAppInfo(url=settings.webapp_url))])
    if is_admin:
        rows.append([Btn(text="🛠 Админ-панель", callback_data=A(s="home").pack())])
    return text, kb(*rows)


def refs_screen(user: Row, settings: Settings, has_pending_claim: bool) -> Screen:
    """«Получить подарки»: подарок за приглашённых друзей."""
    p = calc_progress(user, settings)
    friends = plural(p.left, "друга", "друзей", "друзей")

    if p.available:
        progress = (f"🎉 <b>Цель выполнена!</b> Ты пригласил {p.total} "
                    f"{plural(p.total, 'друга', 'друзей', 'друзей')}.\nЗабирай мишку 👇")
    elif p.finished:
        progress = ("⏳ <b>Мишка уже в пути</b> — пришлём уведомление, как только он будет у тебя."
                    if has_pending_claim else "✅ <b>Мишка получен!</b> Спасибо, что пригласил друзей 💛")
    else:
        progress = (f"📊 Твой прогресс: <b>{p.current}/{p.goal}</b>\n"
                    f"{p.bar} {p.pct}%\n"
                    f"Осталось пригласить: <b>{p.left}</b> {friends}")
        if has_pending_claim:
            progress += "\n\n⏳ Предыдущий мишка уже в пути."

    text = render_template(
        settings.get("text_menu"),
        name=esc(user["full_name"]), goal=p.goal, count=p.total, left=p.left, progress=progress,
    )

    rows: list[list[Btn]] = []
    if p.available:
        rows.append([Btn(text="🧸 Забрать мишку", style="success", callback_data=U(a="claim").pack())])
    rows.append([Btn(text="🔗 Пригласить друзей", style=None if p.finished else "primary",
                     callback_data=U(a="invite").pack())])
    rows.append([Btn(text="👥 Мои друзья", callback_data=U(a="friends").pack()),
                 Btn(text="🏆 Топ", callback_data=U(a="top").pack())])
    rows.append([Btn(text="❓ Как это работает", callback_data=U(a="rules").pack())])
    rows.append(back_to_menu())
    return text, kb(*rows)


def invite_screen(user: Row, settings: Settings, bot_username: str) -> Screen:
    p = calc_progress(user, settings)
    link = ref_link(bot_username, user["user_id"])
    share_text = render_template(settings.get("text_share"), goal=p.goal)
    share_url = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(share_text, safe='')}"

    if p.available:
        status = "🎉 Цель выполнена — можно забрать мишку!"
    elif p.finished:
        status = f"✅ Мишка уже получен. Друзей всего: <b>{p.total}</b> — спасибо!"
    else:
        status = f"📊 Прогресс: <b>{p.current}/{p.goal}</b>  {p.bar}"
    text = (
        "🔗 <b>Твоя ссылка для приглашения</b>\n\n"
        f"<code>{link}</code>\n\n"
        "Отправь её друзьям — нажми «📤 Поделиться» или скопируй.\n\n"
        "ℹ️ Друг засчитывается, когда запустит бота и подпишется на все каналы.\n\n"
        f"{status}"
    )
    return text, kb(
        [Btn(text="📤 Поделиться", style="primary", url=share_url)],
        [Btn(text="📋 Скопировать ссылку", copy_text={"text": link})],
        [Btn(text="👥 Мои друзья", callback_data=U(a="friends").pack())],
        back_to_refs(),
    )


FRIENDS_PAGE = 10


def friends_screen(referrals: list[Row], credited: int, pending: int, page: int) -> Screen:
    total = credited + pending
    lines = ["👥 <b>Твои друзья</b>\n",
             f"✅ Засчитано: <b>{credited}</b>",
             f"⏳ Ещё не подписались: <b>{pending}</b>"]
    if not total:
        lines.append("\nПока никого нет. Отправь ссылку друзьям — они появятся здесь 👇")
    else:
        lines.append("")
        for i, r in enumerate(referrals, page * FRIENDS_PAGE + 1):
            mark = "✅" if r["ref_credited"] else "⏳"
            name = esc(r["full_name"]) or "Без имени"
            suffix = "" if r["ref_credited"] else " — <i>ждём подписку</i>"
            lines.append(f"{i}. {mark} {name}{suffix}")
        if pending:
            lines.append("\n💡 Напомни друзьям с ⏳ подписаться на каналы — тогда они засчитаются.")

    pages = max(1, -(-total // FRIENDS_PAGE))
    nav: list[Btn] = []
    if pages > 1:
        nav = [
            Btn(text="◀️", callback_data=U(a="friends", p=(page - 1) % pages).pack()),
            Btn(text=f"{page + 1}/{pages}", callback_data=U(a="noop").pack()),
            Btn(text="▶️", callback_data=U(a="friends", p=(page + 1) % pages).pack()),
        ]
    return "\n".join(lines), kb(
        nav,
        [Btn(text="🔗 Пригласить друзей", style="primary", callback_data=U(a="invite").pack())],
        back_to_refs(),
    )


MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def top_screen(top: list[Row], my_rank: int | None, my_total: int, user_id: int) -> Screen:
    lines = ["🏆 <b>Топ приглашающих</b>\n"]
    if not top:
        lines.append("Пока пусто — стань первым! 🚀")
    for i, r in enumerate(top, 1):
        place = MEDALS.get(i, f"{i}.")
        name = esc(r["full_name"]) or "Без имени"
        me = " ← ты" if r["user_id"] == user_id else ""
        lines.append(f"{place} {name} — <b>{r['total']}</b>{me}")
    lines.append("")
    lines.append(f"📍 Твоё место: <b>{my_rank}</b> · друзей: <b>{my_total}</b>" if my_rank
                 else "📍 Ты пока не в рейтинге — пригласи первого друга!")
    return "\n".join(lines), kb(
        [Btn(text="🔗 Пригласить друзей", style="primary", callback_data=U(a="invite").pack())],
        back_to_refs(),
    )


def activation_screen(act: Activation, settings: Settings) -> Screen:
    gift = (act.check["gift_emoji"] if act.check and act.check["gift_emoji"] else None) or settings.get("gift_emoji")
    password_texts = {
        CheckStatus.NEED_PASSWORD: f"🔐 <b>Чек на подарок {gift} защищён паролем</b>\n\nОтправь пароль сообщением 👇",
        CheckStatus.WRONG_PASSWORD: f"❌ <b>Неверный пароль</b>\n\nПопробуй ещё раз — осталось попыток: "
                                    f"<b>{act.attempts_left}</b>",
        CheckStatus.LOCKED: f"⏳ <b>Слишком много неверных попыток</b>\n\nПопробуй через {PASSWORD_LOCK // 60} минут — "
                            "открой чек по ссылке ещё раз.",
    }
    if act.status in password_texts:
        return password_texts[act.status], kb(back_to_menu())
    goal = settings.goal
    more = f"\n\n💡 Хочешь ещё? Пригласи <b>{goal}</b> {plural(goal, 'друга', 'друзей', 'друзей')} — и получи {settings.get('gift_emoji')}!"
    texts = {
        CheckStatus.SENT: f"🎉 <b>Чек активирован!</b>\n\nПодарок {gift} уже у тебя — загляни в свой профиль → «Подарки».",
        CheckStatus.PENDING: f"✅ <b>Чек активирован!</b>\n\nПодарок {gift} отправим в ближайшее время — пришлём уведомление.",
        CheckStatus.ALREADY: "🙌 <b>Ты уже активировал этот чек.</b>\n\nОдин чек — один подарок на человека.",
        CheckStatus.EXHAUSTED: "😔 <b>Этот чек уже разобрали</b> — но подарок можно получить и без чека.",
        CheckStatus.INACTIVE: "⛔ <b>Этот чек больше не действует.</b>",
        CheckStatus.NOT_FOUND: "❓ <b>Чек не найден.</b> Проверь ссылку.",
    }
    return texts[act.status] + more, kb(
        [Btn(text="🔗 Пригласить друзей", style="primary", callback_data=U(a="invite").pack())],
        back_to_menu(),
    )


# ---------- НФТ подарки ----------

NFT_PAGE = 8
USERNAME_URL_RE = re.compile(r"https://t\.me/([A-Za-z]\w{3,})/?")


def nft_contact_base(settings: Settings, userbot_username: str | None) -> str:
    """Ссылка кнопки «Написать админу»: из настроек, иначе — на юзербота."""
    return settings.get("nft_contact_url") or (f"https://t.me/{userbot_username}" if userbot_username else "")


def nft_contact_url(gift: Row, settings: Settings, base: str) -> str:
    """Своя ссылка на чат подарка; иначе запасная ссылка (для t.me/username — с тем же готовым текстом)."""
    if gift["chat_link"]:
        return gift["chat_link"]
    m = USERNAME_URL_RE.fullmatch(base)
    if m:
        text = html.unescape(re.sub(r"<[^>]+>", "", render_template(settings.get("nft_link_message"),
                                                                     gift=gift["title"])))
        return f"https://t.me/{m.group(1)}?text={quote(text, safe='')}"
    return base


def nft_button_text(gift: Row) -> str:
    return gift["title"] + (f" · {gift['price']}" if gift["price"] else "")


def nft_list_screen(gifts: list[Row], settings: Settings, page: int) -> Screen:
    pages = max(1, -(-len(gifts) // NFT_PAGE))
    page = max(0, min(page, pages - 1))
    text = settings.get("text_nft_list")
    if not gifts:
        text += "\n\n<i>Подарков пока нет — загляни чуть позже.</i>"
    rows = [[Btn(text=nft_button_text(g), icon_custom_emoji_id=g["emoji_id"],
                 callback_data=U(a="nftg", p=g["id"]).pack())]
            for g in gifts[page * NFT_PAGE:(page + 1) * NFT_PAGE]]
    if pages > 1:
        rows.append([
            Btn(text="◀️", callback_data=U(a="nft", p=(page - 1) % pages).pack()),
            Btn(text=f"{page + 1}/{pages}", callback_data=U(a="noop").pack()),
            Btn(text="▶️", callback_data=U(a="nft", p=(page + 1) % pages).pack()),
        ])
    rows.append(back_to_menu())
    return text, kb(*rows)


def nft_card_screen(gift: Row, settings: Settings, contact_base: str) -> Screen:
    title = esc(gift["title"])
    lines = [f"💎 <b>{title}</b>"]
    if gift["price"]:
        lines.append(f"💰 Цена: <b>{esc(gift['price'])}</b>")
    if gift["description"]:
        lines.append(f"\n{gift['description']}")
    lines.append("\n" + render_template(settings.get("text_nft_howto"), gift=f"«{title}»"))

    rows: list[list[Btn]] = []
    if contact := nft_contact_url(gift, settings, contact_base):
        rows.append([Btn(text="✍️ Написать админу", style="success", url=contact)])
    if gift["link"]:
        rows.append([Btn(text="🔍 Посмотреть подарок", url=gift["link"])])
    rows.append([Btn(text="« К подаркам", callback_data=U(a="nft").pack())])
    return "\n".join(lines), kb(*rows)


# ---------- магазин подарков ----------

def shop_screen(items: list[ShopItem], settings: Settings) -> Screen:
    buttons = [Btn(text=f"{i.emoji} {i.name} - {i.price} ⭐", style=i.style, callback_data=Shop(g=i.id).pack())
               for i in items]
    text = settings.get("text_shop")
    if not buttons:
        text += "\n\n<i>Подарков пока нет — загляни чуть позже.</i>"
    return text, kb(*[buttons[n:n + 2] for n in range(0, len(buttons), 2)], back_to_menu())


def plain(html_text: str, limit: int | None = None) -> str:
    """HTML-текст для кнопки: без тегов, с обрезкой."""
    text = html.unescape(re.sub(r"<[^>]+>", "", html_text))
    return text if limit is None or len(text) <= limit else text[:limit - 1] + "…"


def shop_item_screen(item: ShopItem, default_comment: str, has_comments: bool) -> Screen:
    lines = [f"{item.emoji} <b>{esc(item.name).capitalize()}</b>\n", "Как подписать подарок?\n",
             f"💬 <b>С нашим комментарием — {item.price} ⭐</b>", f"<blockquote>{default_comment}</blockquote>"]
    rows = [[Btn(text=f"💬 С нашим комментарием · {item.price} ⭐", style="success",
                 callback_data=ShopBuy(g=item.id, c=0).pack())]]
    if has_comments:
        lines += [f"\n✍️ <b>Со своим комментарием — {item.cost} ⭐</b>", "Выбери текст из готовых вариантов."]
        rows.append([Btn(text=f"✍️ Свой комментарий · {item.cost} ⭐", style="primary",
                         callback_data=ShopBuy(g=item.id, c=-1).pack())])
    rows.append([Btn(text="« К подаркам", callback_data=U(a="shop").pack())])
    return "\n".join(lines), kb(*rows)


def shop_comments_screen(item: ShopItem, comments: list[Row]) -> Screen:
    lines = [f"✍️ <b>Свой комментарий</b> к {item.emoji} {esc(item.name)} — <b>{item.cost} ⭐</b>\n",
             "Выбери текст — подарок придёт с ним:\n"]
    rows = []
    for n, c in enumerate(comments, 1):
        lines.append(f"<b>{n}.</b> {c['text']}")
        rows.append([Btn(text=f"{n}. {plain(c['text'], 40)}", callback_data=ShopBuy(g=item.id, c=c["id"]).pack())])
    rows.append([Btn(text="« Назад", callback_data=Shop(g=item.id).pack())])
    return "\n".join(lines), kb(*rows)


def shop_done_screen(emoji: str, name: str, comment: str) -> Screen:
    return (f"🎉 <b>Подарок отправлен!</b>\n\n{emoji} {esc(name).capitalize()} уже у тебя — "
            "загляни в свой профиль → «Подарки»."
            + (f"\n\n💬 Подпись: <blockquote>{comment}</blockquote>" if comment else "")), kb(
        [Btn(text="🛍 Купить ещё", style="primary", callback_data=U(a="shop").pack())],
        back_to_menu(),
    )


# ---------- профиль ----------

def profile_screen(user: Row, tasks_done: int, earned: int, case_opens: int) -> Screen:
    total = user["ref_count"] + user["bonus_refs"]
    text = "\n".join([
        "👤 <b>Профиль</b>\n",
        f"🆔 ID: <code>{user['user_id']}</code>",
        f"💳 Баланс: <b>{fmt_stars(user['balance'])} Stars</b>\n",
        f"✅ Выполнено заданий: <b>{tasks_done}</b> (заработано {fmt_stars(earned)} ⭐)",
        f"📦 Открыто кейсов: <b>{case_opens}</b>",
        f"👥 Приглашено друзей: <b>{total}</b>",
    ])
    return text, kb(
        [icon_btn("Заработать звёзды", ICON_EARN, "⭐", style="success", callback_data=U(a="tasks").pack())],
        [icon_btn("Кейсы", ICON_DAILY, "📦", callback_data=U(a="cases").pack())],
        back_to_menu(),
    )


# ---------- задания ----------

TASK_HEADERS = {
    "sub": "📢 <b>Подпишись на канал {chat}</b>",
    "boost": "🚀 <b>Забусти канал {chat}</b>",
    "link": "🔗 <b>Перейди по ссылке</b>",
}
TASK_STEPS = {
    "sub": "1️⃣ Нажми «Перейти» и подпишись\n2️⃣ Вернись и нажми «Проверить»",
    "boost": "1️⃣ Нажми «Перейти» и отдай буст каналу (нужен Telegram Premium)\n"
             "2️⃣ Вернись и нажми «Проверить»",
    "link": f"1️⃣ Нажми «Перейти»\n2️⃣ Через {LINK_DELAY} секунд вернись и нажми «Проверить»",
}


def tasks_screen(tasks: list[Row], settings: Settings) -> Screen:
    text = render_template(settings.get("text_tasks"), count=len(tasks))
    if not tasks:
        text += "\n\n<i>Ты выполнил все задания — новые появятся совсем скоро!</i>"
    rows = [[Btn(text=t["title"], style=t["style"], callback_data=U(a="task", p=t["id"]).pack())] for t in tasks]
    rows.append(back_to_menu())
    return text, kb(*rows)


def task_screen(task: Row) -> Screen:
    chat = f"«{esc(task['chat_title'])}»" if task["chat_title"] else ""
    text = "\n".join([
        TASK_HEADERS[task["kind"]].format(chat=chat).replace("  ", " "),
        f"<i>{esc(task['title'])}</i>\n",
        f"💰 Награда: <b>{fmt_stars(task['reward'])} ⭐</b> на баланс\n",
        TASK_STEPS[task["kind"]],
    ])
    rows = []
    if task["url"]:
        rows.append([Btn(text="➡️ Перейти", style="primary", url=task["url"])])
    rows.append([Btn(text="✅ Проверить", style="success", callback_data=U(a="taskchk", p=task["id"]).pack())])
    rows.append([Btn(text="◀️ К заданиям", callback_data=U(a="tasks").pack())])
    return text, kb(*rows)


# ---------- кейсы ----------

def cases_screen(cases: list[Row], balance: int, settings: Settings) -> Screen:
    text = render_template(settings.get("text_cases"), balance=fmt_stars(balance))
    if not cases:
        text += "\n\n<i>Кейсов пока нет — загляни чуть позже.</i>"
    rows = [[icon_btn(case_button_text(c), c["emoji_id"], c["emoji"] or "", style=c["style"],
                      callback_data=U(a="case", p=c["id"]).pack())] for c in cases]
    rows.append(back_to_menu())
    return text, kb(*rows)


def fmt_chance(weight: float, total: float) -> str:
    value = weight / total * 100 if total else 0
    return f"{value:.2g}%" if value < 1 else f"{value:.1f}".rstrip("0").rstrip(".") + "%"


def case_screen(case: Row, prizes: list[Row], balance: int, wait: int) -> Screen:
    total = sum(p["weight"] for p in prizes)
    price = fmt_stars(case["price"])
    lines = [
        f"{case['emoji'] or '📦'} <b>{esc(case['name'])}</b>\n",
        f"💰 Цена: <b>{'бесплатно' if not case['price'] else price + ' ⭐'}</b>"
        + (" · раз в сутки" if case["is_daily"] else ""),
        f"💳 Твой баланс: <b>{fmt_stars(balance)} ⭐</b>\n",
        "<b>Что может выпасть:</b>",
        *(f"{prize_label(p)} — {fmt_chance(p['weight'], total)}" for p in prizes),
    ]
    rows = []
    if wait:
        lines.append(f"\n⏳ Следующее открытие через <b>{fmt_duration(wait)}</b>")
        rows.append([Btn(text=f"⏳ Через {fmt_duration(wait)}", callback_data=U(a="case", p=case["id"]).pack())])
    else:
        label = f"🎁 Открыть за {price} ⭐" if case["price"] else "🎁 Открыть бесплатно"
        rows.append([Btn(text=label, style="success", callback_data=U(a="caseopen", p=case["id"]).pack())])
        if case["price"] > balance:
            lines.append(f"\n⚠️ Не хватает <b>{fmt_stars(case['price'] - balance)} ⭐</b> — заработай их на заданиях")
            rows.append([icon_btn("Заработать звёзды", ICON_EARN, "⭐", callback_data=U(a="tasks").pack())])
    rows.append([Btn(text="◀️ К кейсам", callback_data=U(a="cases").pack())])
    return "\n".join(lines), kb(*rows)


def case_result_screen(case: Row, result: OpenResult, balance: int) -> Screen:
    prize = result.prize
    assert prize is not None
    if prize["kind"] == "stars":
        lines = [f"🎉 <b>Тебе выпало {fmt_stars(prize['value'])} ⭐!</b>\n", "Звёзды уже на твоём балансе."]
    else:
        where = ("Подарок уже в твоём профиле → «Подарки»." if result.delivered == "sent"
                 else "Подарок отправим в ближайшее время — пришлём уведомление.")
        lines = [f"🎉 <b>Тебе выпал {prize['emoji'] or '🎁'} подарок за {fmt_stars(prize['value'])} ⭐!</b>\n", where]
    lines.append(f"\n💳 Баланс: <b>{fmt_stars(balance)} Stars</b>")
    rows = []
    if not case["is_daily"] and balance >= case["price"]:
        rows.append([Btn(text=f"🔁 Открыть ещё за {fmt_stars(case['price'])} ⭐", style="success",
                         callback_data=U(a="caseopen", p=case["id"]).pack())])
    rows.append([Btn(text="📦 К кейсам", callback_data=U(a="cases").pack())])
    rows.append(back_to_menu())
    return "\n".join(lines), kb(*rows)
