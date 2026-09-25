"""Экраны пользовательской части: текст + клавиатура."""
from urllib.parse import quote

from aiogram.types import InlineKeyboardButton as Btn, InlineKeyboardMarkup
from aiosqlite import Row

from bot.callbacks import A, U
from bot.services.checks import Activation, CheckStatus
from bot.services.rewards import calc_progress
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


def menu_screen(user: Row, settings: Settings, has_pending_claim: bool, is_admin: bool) -> Screen:
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
