"""Экраны пользовательской части: текст + клавиатура."""
import html
import re
from urllib.parse import quote

from aiogram.types import InlineKeyboardButton as Btn, InlineKeyboardMarkup, WebAppInfo
from aiosqlite import Row

from bot.callbacks import A, Shop, U
from bot.services.checks import Activation, CheckStatus
from bot.services.rewards import calc_progress
from bot.services.shop import ShopItem
from bot.settings import Settings
from bot.utils import esc, plural, render_template

Screen = tuple[str, InlineKeyboardMarkup]


def kb(*rows: list[Btn]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def back_to_menu() -> list[Btn]:
    return [Btn(text="« В меню", callback_data=U(a="menu").pack())]


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


def menu_screen(user: Row, settings: Settings, has_pending_claim: bool, is_admin: bool,
                has_nft: bool = False, has_shop: bool = False) -> Screen:
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
    if has_shop:
        rows.append([Btn(text="🛍 Купить подарок", callback_data=U(a="shop").pack())])
    if settings.webapp_url and settings.flag("roulette_enabled"):
        rows.append([Btn(text="🎰 Рулетка подарков", web_app=WebAppInfo(url=settings.webapp_url))])
    if has_nft:
        rows.append([Btn(text="💎 НФТ подарки", callback_data=U(a="nft").pack())])
    rows.append([Btn(text="❓ Как это работает", callback_data=U(a="rules").pack())])
    if is_admin:
        rows.append([Btn(text="🛠 Админ-панель", callback_data=A(s="home").pack())])
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
        back_to_menu(),
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
        back_to_menu(),
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
        back_to_menu(),
    )


def activation_screen(act: Activation, settings: Settings) -> Screen:
    gift = (act.check["gift_emoji"] if act.check and act.check["gift_emoji"] else None) or settings.get("gift_emoji")
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
    return f"💎 {gift['title']}" + (f" · {gift['price']}" if gift["price"] else "")


def nft_list_screen(gifts: list[Row], settings: Settings, page: int) -> Screen:
    pages = max(1, -(-len(gifts) // NFT_PAGE))
    page = max(0, min(page, pages - 1))
    text = settings.get("text_nft_list")
    if not gifts:
        text += "\n\n<i>Подарков пока нет — загляни чуть позже.</i>"
    rows = [[Btn(text=nft_button_text(g), callback_data=U(a="nftg", p=g["id"]).pack())]
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


def shop_done_screen(emoji: str, name: str) -> Screen:
    return (f"🎉 <b>Подарок отправлен!</b>\n\n{emoji} {esc(name).capitalize()} уже у тебя — "
            "загляни в свой профиль → «Подарки»."), kb(
        [Btn(text="🛍 Купить ещё", style="primary", callback_data=U(a="shop").pack())],
        back_to_menu(),
    )
