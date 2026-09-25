import asyncio

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.callback_answer import CallbackAnswer

from bot.callbacks import A
from bot.config import Config
from bot.database import Database
from bot.handlers.admin.common import back, btn, kb, pager, pages_count
from bot.handlers.admin.home import star_balance, topup_button
from bot.services.rewards import RewardService
from bot.services.roulette import RouletteService
from bot.settings import Settings
from bot.utils import esc, fmt_dt, show, user_link

router = Router(name="admin_claims")

PER_PAGE = 8
STATUS = {
    "pending": ("⏳", "Ожидают"),
    "sent": ("✅", "Выданы"),
    "rejected": ("❌", "Отклонены"),
}
STATUS_ONE = {"pending": "ожидает", "sent": "выдана", "rejected": "отклонена"}
METHOD = {"auto": "подарок ⭐", "manual": "вручную", "admin": "админом из профиля"}


async def list_screen(db: Database, config: Config, status: str, page: int):
    counts = {s: await db.count_claims(s) for s in STATUS}
    pages = pages_count(counts[status], PER_PAGE)
    page = max(0, min(page, pages - 1))
    claims = await db.list_claims(status, PER_PAGE, page * PER_PAGE)

    icon, title = STATUS[status]
    text = f"🎁 <b>Заявки на награду</b> — {icon} {title.lower()}\n\n"
    if status == "pending":
        text += ("Здесь заявки, которые нужно выдать вручную: включена ручная выдача "
                 "или бот не смог отправить подарок (например, не хватило звёзд).")
    if not claims:
        text += "\n\n<i>Пусто 🙌</i>"

    tabs = [btn(f"{'• ' if s == status else ''}{ic} {counts[s]}", "cl", v=s) for s, (ic, _) in STATUS.items()]
    rows = [tabs]
    for c in claims:
        name = c["full_name"] or str(c["user_id"])
        date = fmt_dt(c["created_at"], config.tz)[:-6]
        warn = " ⚠️" if c["error"] and status == "pending" else ""
        rows.append([btn(f"#{c['id']} · {name} · {date}{warn}", "cl", "card", id=c["id"], v=status)])
    rows.append(pager("cl", "open", page, pages, v=status))
    if status == "pending" and counts["pending"]:
        rows.append([btn(f"🚀 Отправить все подарками ({counts['pending']})", "cl", "all", style="success")])
    rows.append(back())
    return text, kb(*rows)


async def card_screen(db: Database, config: Config, claim_id: int, back_status: str = "pending"):
    c = await db.get_claim(claim_id)
    if not c:
        return "Заявка не найдена", kb(back("cl"))
    icon = STATUS[c["status"]][0]
    user = await db.get_user(c["user_id"])
    total = (user["ref_count"] + user["bonus_refs"]) if user else 0
    username = f" (@{esc(c['username'])})" if c["username"] else ""
    lines = [
        f"🎁 <b>Заявка #{c['id']}</b> — {icon} {STATUS_ONE[c['status']]}\n",
        f"👤 {user_link(c['user_id'], c['full_name'])}{username}",
        f"🆔 <code>{c['user_id']}</code>",
        f"👥 Друзей засчитано: <b>{total}</b>",
        f"📅 Создана: {fmt_dt(c['created_at'], config.tz)}",
    ]
    if c["check_id"]:
        check = await db.get_check(c["check_id"])
        lines.append(f"🎟 По чеку: <code>{check['code'] if check else 'удалён'}</code>")
    spin = await db.get_spin(c["spin_id"]) if c["spin_id"] else None
    if spin:
        lines.append(f"🎰 Рулетка «{esc(spin['case_name'])}»: оплачено <b>{spin['price']}</b> ⭐, "
                     f"выпал {spin['gift_emoji']} {spin['gift_price']} ⭐")
    if c["processed_at"]:
        how = f" · {METHOD.get(c['method'], c['method'])}" if c["method"] else ""
        lines.append(f"🏁 Обработана: {fmt_dt(c['processed_at'], config.tz)}{how}")
    if c["error"]:
        lines.append(f"\n⚠️ Последняя ошибка:\n<code>{esc(c['error'])}</code>")

    rows = []
    if c["status"] == "pending":
        rows += [
            [btn("🎁 Отправить подарок", "cl", "send", id=c["id"], style="success")],
            [btn("✅ Выдал вручную", "cl", "done", id=c["id"]),
             btn("❌ Отклонить", "cl", "reject", id=c["id"], style="danger")],
        ]
        if spin and spin["charge_id"]:
            rows.append([btn(f"↩️ Вернуть {spin['price']} ⭐ за прокрутку", "cl", "refund", id=c["id"])])
    rows.append([btn("👤 Профиль пользователя", "us", "card", id=c["user_id"])])
    rows.append(back("cl", v=back_status, text="« К заявкам"))
    return "\n".join(lines), kb(*rows)


@router.callback_query(A.filter((F.s == "cl") & (F.a == "open")))
async def cb_list(call: CallbackQuery, callback_data: A, db: Database, config: Config) -> None:
    status = callback_data.v if callback_data.v in STATUS else "pending"
    await show(call, *await list_screen(db, config, status, callback_data.p))


@router.callback_query(A.filter((F.s == "cl") & (F.a == "card")))
async def cb_card(call: CallbackQuery, callback_data: A, db: Database, config: Config) -> None:
    await show(call, *await card_screen(db, config, callback_data.id, callback_data.v or "pending"))


@router.callback_query(A.filter((F.s == "cl") & F.a.in_({"send", "done", "reject_ok"})))
async def cb_process(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                     config: Config, rewards: RewardService) -> None:
    action = "reject" if callback_data.a == "reject_ok" else callback_data.a
    ok, msg = await rewards.process_claim(callback_data.id, action, call.from_user.id)
    callback_answer.text = msg[:200]
    callback_answer.show_alert = not ok
    await show(call, *await card_screen(db, config, callback_data.id))


@router.callback_query(A.filter((F.s == "cl") & (F.a == "reject")))
async def cb_reject_confirm(call: CallbackQuery, callback_data: A) -> None:
    await show(call, f"❌ Отклонить заявку #{callback_data.id}?\n\n"
                     "Пользователь получит уведомление, повторно забрать эту награду он не сможет "
                     "(можно сбросить в его профиле).", kb(
        [btn("❌ Да, отклонить", "cl", "reject_ok", id=callback_data.id, style="danger")],
        back("cl", "card", "« Отмена", id=callback_data.id),
    ))


@router.callback_query(A.filter((F.s == "cl") & (F.a == "refund")))
async def cb_refund_confirm(call: CallbackQuery, callback_data: A) -> None:
    await show(call, "↩️ <b>Вернуть звёзды за прокрутку?</b>\n\nПользователь получит свои звёзды обратно, "
                     "заявка на подарок будет закрыта.", kb(
        [btn("↩️ Да, вернуть", "cl", "refund_ok", id=callback_data.id, style="danger")],
        back("cl", "card", "« Отмена", id=callback_data.id),
    ))


@router.callback_query(A.filter((F.s == "cl") & (F.a == "refund_ok")))
async def cb_refund(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                    config: Config, roulette: RouletteService) -> None:
    ok, msg = await roulette.refund_claim(callback_data.id, call.from_user.id)
    callback_answer.text = msg[:200]
    callback_answer.show_alert = not ok
    await show(call, *await card_screen(db, config, callback_data.id))


@router.callback_query(A.filter((F.s == "cl") & (F.a == "all")))
async def cb_all_confirm(call: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    count = await db.count_claims("pending")
    price = settings.get_int("gift_price")
    balance = await star_balance(bot)
    need = count * price
    bal = f"{balance} ⭐" if balance is not None else "неизвестен"
    warn = "\n\n⚠️ Звёзд может не хватить — часть заявок останется в очереди." if (
        balance is not None and balance < need) else ""
    await show(call, f"🚀 <b>Отправить все ожидающие заявки подарками?</b>\n\n"
                     f"Заявок: <b>{count}</b>\nНужно: ~<b>{need}</b> ⭐\nБаланс: <b>{bal}</b>{warn}", kb(
        [btn("🚀 Отправить", "cl", "all_ok", style="success")],
        topup_button(balance, settings, "cl"),
        back("cl", text="« Отмена"),
    ))


@router.callback_query(A.filter((F.s == "cl") & (F.a == "all_ok")))
async def cb_all(call: CallbackQuery, db: Database, config: Config, rewards: RewardService) -> None:
    ids = await db.pending_claim_ids()
    await show(call, f"⏳ Отправляю {len(ids)} подарков…")
    sent, errors = 0, []
    for cid in ids:
        ok, msg = await rewards.process_claim(cid, "send", call.from_user.id)
        if ok:
            sent += 1
        else:
            errors.append(msg)
            if "BALANCE_TOO_LOW" in msg.upper() or "not enough" in msg.lower():
                break  # нет звёзд — дальше пробовать бессмысленно
        await asyncio.sleep(0.1)
    text = f"🚀 <b>Готово</b>\n\n✅ Отправлено: <b>{sent}</b>\n⏳ Осталось в очереди: <b>{len(ids) - sent}</b>"
    if errors:
        text += f"\n\n⚠️ Последняя ошибка:\n<code>{esc(errors[-1])}</code>"
    await show(call, text, kb([btn("🎁 К заявкам", "cl")], back()))
