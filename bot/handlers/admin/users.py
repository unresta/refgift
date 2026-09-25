from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, MessageOriginUser
from aiogram.utils.callback_answer import CallbackAnswer

from bot.callbacks import A
from bot.config import Config
from bot.database import Database
from bot.handlers.admin.common import Input, back, btn, drop_prompt, kb, pager, pages_count, prompt
from bot.services.admins import AdminRegistry
from bot.services.rewards import RewardService, calc_progress
from bot.services.subscription import SubscriptionService
from bot.settings import Settings
from bot.utils import esc, fmt_dt, show, user_link

router = Router(name="admin_users")

PER_PAGE = 10
LISTS = {"top": "🏆 Топ рефереров", "new": "🆕 Новые пользователи", "banned": "🚫 Забаненные"}


def search_text() -> str:
    return ("👥 <b>Пользователи</b>\n\n"
            "🔎 Отправьте <b>ID</b>, <b>@username</b> или <b>перешлите сообщение</b> пользователя — "
            "откроется его карточка.")


def search_kb():
    return kb(
        [btn("🏆 Топ рефереров", "us", "list", v="top"), btn("🆕 Новые", "us", "list", v="new")],
        [btn("🚫 Забаненные", "us", "list", v="banned")],
        back(),
    )


async def card_screen(db: Database, settings: Settings, config: Config, subs: SubscriptionService,
                      admins: AdminRegistry, user_id: int):
    u = await db.get_user(user_id)
    if not u:
        return "❌ Пользователь не найден", kb(back("us"))
    p = calc_progress(u, settings)
    credited, pending = await db.referral_counts(user_id)
    referrer = await db.get_user(u["referrer_id"]) if u["referrer_id"] else None
    ad_link = await db.get_ad_link(u["ad_link_id"]) if u["ad_link_id"] else None
    pending_claim = await db.user_pending_claim(user_id)
    rank = await db.user_rank(user_id)

    username = f" · @{esc(u['username'])}" if u["username"] else ""
    badges = []
    if admins.is_admin(user_id):
        badges.append("👮 админ")
    if u["is_banned"]:
        badges.append("🚫 в бане")
    if u["is_blocked"]:
        badges.append("⛔ заблокировал бота")

    bonus = f" (из них бонус: {u['bonus_refs']:+d})" if u["bonus_refs"] else ""
    lines = [
        f"👤 <b>{user_link(user_id, u['full_name'])}</b>{username}",
        f"🆔 <code>{user_id}</code>" + (f" · {' · '.join(badges)}" if badges else ""),
        "",
        f"📅 Регистрация: {fmt_dt(u['created_at'], config.tz)}",
        f"👁 Был в боте: {fmt_dt(u['last_seen'], config.tz)}",
        f"✅ Подписка: {'пройдена ' + fmt_dt(u['verified_at'], config.tz) if u['verified_at'] else '❌ не пройдена'}",
        f"🤝 Пригласил: {user_link(referrer['user_id'], referrer['full_name']) if referrer else '—'}"
        + (" (засчитан)" if u["ref_credited"] else " (не засчитан)" if referrer else ""),
        *([f"📎 Пришёл по рекламе: <b>{esc(ad_link['name'])}</b>"] if ad_link else []),
        "",
        f"👥 Друзей: <b>{p.total}</b>{bonus} · место в топе: {rank or '—'}",
        f"⏳ Не завершили подписку: <b>{pending}</b>",
        f"📊 Прогресс: {p.current}/{p.goal} {p.bar}",
        f"🧸 Наград получено: <b>{u['rewards_claimed']}</b> · доступно: <b>{p.available}</b>"
        + (" · ⏳ есть заявка" if pending_claim else ""),
    ]
    uid = user_id
    rows = [
        [btn("➖ 1 друг", "us", "bonus", id=uid, p=0), btn("➕ 1 друг", "us", "bonus", id=uid, p=1)],
        [btn(f"👥 Его рефералы ({credited + pending})", "us", "refs", id=uid),
         btn("✉️ Написать", "us", "msg", id=uid)],
        [btn("🧸 Отправить мишку", "us", "gift", id=uid), btn("♻️ Сбросить награды", "us", "rreset", id=uid)],
    ]
    if pending_claim:
        rows.append([btn(f"🎁 Заявка #{pending_claim['id']}", "cl", "card", id=pending_claim["id"],
                         style="success")])
    if not admins.is_admin(uid):
        rows.append([btn("✅ Разбанить", "us", "unban", id=uid, style="success") if u["is_banned"]
                     else btn("🚫 Забанить", "us", "ban", id=uid, style="danger")])
    rows.append([btn("🔄", "us", "card", id=uid), btn("« К поиску", "us")])
    return "\n".join(lines), kb(*rows)


@router.callback_query(A.filter((F.s == "us") & (F.a == "open")))
async def cb_open(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Input.user_search)
    msg = await show(call, search_text(), search_kb())
    await state.update_data(prompt_id=msg.message_id if msg else None)


def _extract_user_query(message: Message) -> str | None:
    if isinstance(message.forward_origin, MessageOriginUser):
        return str(message.forward_origin.sender_user.id)
    if message.forward_origin:
        return ""  # переслано от скрытого пользователя
    return message.text


@router.message(Input.user_search)
async def on_search(message: Message, state: FSMContext, db: Database, settings: Settings, config: Config,
                    subs: SubscriptionService, admins: AdminRegistry) -> None:
    query = _extract_user_query(message)
    if query == "":
        await message.answer("🙈 Пользователь скрыл аккаунт при пересылке. Пришлите его ID или @username.")
        return
    user = await db.find_user(query or "")
    if not user:
        await message.answer("❌ Не найден. Пользователь должен хотя бы раз запустить бота.\n"
                             "Попробуйте ещё раз или нажмите «« Назад».")
        return
    await drop_prompt(message, state)
    await state.clear()
    await show(message, *await card_screen(db, settings, config, subs, admins, user["user_id"]))


@router.callback_query(A.filter((F.s == "us") & (F.a == "list")))
async def cb_list(call: CallbackQuery, callback_data: A, db: Database) -> None:
    kind = callback_data.v if callback_data.v in LISTS else "new"
    if kind == "top":
        total = min(100, await db.val("SELECT COUNT(*) FROM users WHERE ref_count + bonus_refs > 0"))
    else:
        total = await db.count_users(kind)
    pages = pages_count(total, PER_PAGE)
    page = max(0, min(callback_data.p, pages - 1))
    if kind == "top":
        users = await db.top_referrers(PER_PAGE, page * PER_PAGE)
    else:
        users = await db.list_users(kind, PER_PAGE, page * PER_PAGE)

    rows = []
    for i, u in enumerate(users, page * PER_PAGE + 1):
        name = u["full_name"] or str(u["user_id"])
        extra = f" · {u['ref_count'] + u['bonus_refs']} 👥"
        prefix = f"{i}. " if kind == "top" else ""
        rows.append([btn(f"{prefix}{name}{extra}", "us", "card", id=u["user_id"])])
    rows.append(pager("us", "list", page, pages, v=kind))
    rows.append(back("us"))
    text = f"<b>{LISTS[kind]}</b> · всего {total}" + ("" if users else "\n\n<i>Пусто</i>")
    await show(call, text, kb(*rows))


@router.callback_query(A.filter((F.s == "us") & (F.a == "card")))
async def cb_card(call: CallbackQuery, callback_data: A, db: Database, settings: Settings, config: Config,
                  subs: SubscriptionService, admins: AdminRegistry) -> None:
    await show(call, *await card_screen(db, settings, config, subs, admins, callback_data.id))


@router.callback_query(A.filter((F.s == "us") & F.a.in_({"ban", "unban", "bonus", "rreset_ok"})))
async def cb_action(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                    settings: Settings, config: Config, subs: SubscriptionService, admins: AdminRegistry) -> None:
    uid, action = callback_data.id, callback_data.a
    if action in ("ban", "unban"):
        if admins.is_admin(uid):
            callback_answer.text = "Нельзя забанить администратора"
            return
        await db.set_banned(uid, action == "ban")
        callback_answer.text = "🚫 Забанен" if action == "ban" else "✅ Разбанен"
    elif action == "bonus":
        delta = 1 if callback_data.p else -1
        await db.add_bonus(uid, delta)
        callback_answer.text = f"{delta:+d} друг"
    elif action == "rreset_ok":
        await db.set_rewards_claimed(uid, 0)
        callback_answer.text = "♻️ Награды сброшены"
    await show(call, *await card_screen(db, settings, config, subs, admins, uid))


@router.callback_query(A.filter((F.s == "us") & (F.a == "rreset")))
async def cb_rreset_confirm(call: CallbackQuery, callback_data: A) -> None:
    await show(call, "♻️ <b>Сбросить счётчик полученных наград?</b>\n\n"
                     "Пользователь снова сможет забрать мишку, если у него хватает друзей.", kb(
        [btn("♻️ Да, сбросить", "us", "rreset_ok", id=callback_data.id, style="danger")],
        back("us", "card", "« Отмена", id=callback_data.id),
    ))


@router.callback_query(A.filter((F.s == "us") & (F.a == "gift")))
async def cb_gift_confirm(call: CallbackQuery, callback_data: A, settings: Settings) -> None:
    await show(call, f"🧸 <b>Отправить подарок {settings.get('gift_emoji')} этому пользователю?</b>\n\n"
                     f"Спишется ~{settings.get('gift_price')} ⭐ с баланса бота. "
                     "Прогресс пользователя не меняется.", kb(
        [btn("🎁 Отправить", "us", "gift_ok", id=callback_data.id, style="success")],
        back("us", "card", "« Отмена", id=callback_data.id),
    ))


@router.callback_query(A.filter((F.s == "us") & (F.a == "gift_ok")))
async def cb_gift(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                  settings: Settings, config: Config, subs: SubscriptionService, admins: AdminRegistry,
                  rewards: RewardService) -> None:
    error = await rewards.admin_gift(callback_data.id, call.from_user.id)
    callback_answer.text = f"❌ {error}"[:200] if error else "🎁 Подарок отправлен"
    callback_answer.show_alert = bool(error)
    await show(call, *await card_screen(db, settings, config, subs, admins, callback_data.id))


@router.callback_query(A.filter((F.s == "us") & (F.a == "refs")))
async def cb_refs(call: CallbackQuery, callback_data: A, db: Database) -> None:
    uid = callback_data.id
    credited, pending = await db.referral_counts(uid)
    pages = pages_count(credited + pending, PER_PAGE)
    page = max(0, min(callback_data.p, pages - 1))
    refs = await db.list_referrals(uid, PER_PAGE, page * PER_PAGE)
    rows = [[btn(f"{'✅' if r['ref_credited'] else '⏳'} {r['full_name'] or r['user_id']}", "us", "card",
                 id=r["user_id"])] for r in refs]
    rows.append(pager("us", "refs", page, pages, id=uid))
    rows.append(back("us", "card", "« К профилю", id=uid))
    await show(call, f"👥 <b>Рефералы пользователя</b> <code>{uid}</code>\n\n"
                     f"✅ Засчитано: {credited} · ⏳ Ждут подписку: {pending}", kb(*rows))


# ---------- личное сообщение ----------

@router.callback_query(A.filter((F.s == "us") & (F.a == "msg")))
async def cb_msg(call: CallbackQuery, callback_data: A, state: FSMContext) -> None:
    await prompt(call, state, Input.user_message,
                 "✉️ <b>Сообщение пользователю</b>\n\nПришлите текст, фото, видео или любое другое сообщение — "
                 "бот перешлёт его от своего имени.",
                 back("us", "card", "✖️ Отмена", id=callback_data.id), target=callback_data.id)


@router.message(Input.user_message)
async def on_msg(message: Message, state: FSMContext, bot: Bot, db: Database, settings: Settings,
                 config: Config, subs: SubscriptionService, admins: AdminRegistry) -> None:
    data = await drop_prompt(message, state)
    await state.clear()
    target = data["target"]
    try:
        await message.copy_to(target)
        await message.reply("✅ Доставлено")
    except TelegramForbiddenError:
        await db.set_blocked(target)
        await message.reply("⛔ Пользователь заблокировал бота")
    except TelegramAPIError as e:
        await message.reply(f"❌ Ошибка: {esc(e.message)}")
    await show(message, *await card_screen(db, settings, config, subs, admins, target))
