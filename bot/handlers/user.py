import logging

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, ChatJoinRequest, ChatMemberUpdated, Message, PreCheckoutQuery
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.utils.callback_answer import CallbackAnswer
from aiosqlite import Row

from bot.callbacks import U
from bot.database import Database
from bot.services.admins import AdminRegistry
from bot.services.checks import CHECK_PREFIX, CheckService, CheckStatus
from bot.services.reminders import ReminderService
from bot.services.rewards import ClaimResult, RewardService
from bot.services.subscription import SubscriptionService
from bot.settings import Settings
from bot.utils import esc, render_template, show
from bot.views import (FRIENDS_PAGE, activation_screen, back_to_menu, friends_screen, invite_screen, kb, menu_screen,
                       subscribe_screen, top_screen)

log = logging.getLogger(__name__)

AD_PREFIX = "ad_"

router = Router(name="user")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

# отдельный роутер для служебных апдейтов (без пользовательских middleware)
service_router = Router(name="service")


async def open_menu(event: Message | CallbackQuery, user_id: int, db: Database, settings: Settings,
                    is_admin: bool) -> None:
    user = await db.get_user(user_id)
    assert user is not None
    pending = await db.user_pending_claim(user_id)
    await show(event, *menu_screen(user, settings, pending is not None, is_admin))


async def pass_gate(event: Message | CallbackQuery, user_id: int, db: Database, settings: Settings,
                    subs: SubscriptionService, rewards: RewardService, checks: CheckService,
                    is_admin: bool) -> list[Row]:
    """Проверяет подписку без кэша. Нет подписки — экран подписки; есть — активируем ждущий чек или открываем меню.

    Возвращает список каналов, на которые пользователь ещё не подписан.
    """
    user = await db.get_user(user_id)
    assert user is not None
    if user["pending_check"]:
        act = await checks.activate(user_id, user["pending_check"])  # внутри — своя проверка подписки
        if act.status is CheckStatus.NEED_SUB:
            await show(event, *subscribe_screen(settings, user["full_name"], act.missing,
                                                for_check=act.available))
            return act.missing
        await show(event, *activation_screen(act, settings))
        return []

    missing = await subs.missing(user_id, use_cache=False)
    if missing:
        await show(event, *subscribe_screen(settings, user["full_name"], missing))
        return missing
    await rewards.complete_verification(user_id)
    await open_menu(event, user_id, db, settings, is_admin)
    return []


@router.message(CommandStart(), flags={"skip_sub": True})
async def cmd_start(message: Message, command: CommandObject, user: Row, is_new: bool, db: Database,
                    settings: Settings, subs: SubscriptionService, rewards: RewardService, checks: CheckService,
                    reminders: ReminderService, is_admin: bool) -> None:
    args = (command.args or "").strip()
    if args.startswith(CHECK_PREFIX):
        code = args.removeprefix(CHECK_PREFIX)
        check = await db.get_check_by_code(code)
        await db.set_pending_check(user["user_id"], code)
        if check and is_new:
            await db.set_source_check(user["user_id"], check["id"])
    elif args.startswith(AD_PREFIX):
        link = await db.get_ad_link_by_code(args.removeprefix(AD_PREFIX))
        if link:
            await db.track_ad_click(link["id"], user["user_id"], is_new)
    elif args.startswith("r") and args[1:].isdigit():
        referrer_id = int(args[1:])
        if referrer_id != user["user_id"] and await db.get_user(referrer_id):
            await db.set_referrer(user["user_id"], referrer_id)
    missing = await pass_gate(message, user["user_id"], db, settings, subs, rewards, checks, is_admin)
    if missing:
        await reminders.on_start(await db.get_user(user["user_id"]))


@router.callback_query(U.filter(F.a == "gift_cta"), flags={"skip_sub": True})
async def gift_cta(call: CallbackQuery, callback_answer: CallbackAnswer, user: Row, db: Database,
                   settings: Settings, subs: SubscriptionService, rewards: RewardService, checks: CheckService,
                   is_admin: bool) -> None:
    """Кнопка из напоминания: ведёт на обязательную подписку (или в меню, если уже подписан)."""
    missing = await pass_gate(call, user["user_id"], db, settings, subs, rewards, checks, is_admin)
    callback_answer.text = ("📢 Подпишись на каналы — и подарок твой!" if missing
                            else "✅ Подписка уже есть — забирай подарок в меню")


@router.callback_query(U.filter(F.a == "check"), flags={"skip_sub": True})
async def check_subscription(call: CallbackQuery, callback_answer: CallbackAnswer, user: Row, db: Database,
                             settings: Settings, subs: SubscriptionService, rewards: RewardService,
                             checks: CheckService, is_admin: bool) -> None:
    missing = await pass_gate(call, user["user_id"], db, settings, subs, rewards, checks, is_admin)
    if missing:
        names = ", ".join(ch["title"] for ch in missing)
        callback_answer.text = f"❌ Ты ещё не подписан: {names}"[:200]
        callback_answer.show_alert = True
    else:
        callback_answer.text = "✅ Подписка подтверждена!"


@router.callback_query(U.filter(F.a == "menu"))
async def cb_menu(call: CallbackQuery, user: Row, db: Database, settings: Settings, is_admin: bool) -> None:
    await open_menu(call, user["user_id"], db, settings, is_admin)


@router.callback_query(U.filter(F.a == "invite"))
async def cb_invite(call: CallbackQuery, user: Row, settings: Settings, bot_username: str) -> None:
    await show(call, *invite_screen(user, settings, bot_username))


@router.callback_query(U.filter(F.a == "friends"))
async def cb_friends(call: CallbackQuery, callback_data: U, user: Row, db: Database) -> None:
    credited, pending = await db.referral_counts(user["user_id"])
    pages = max(1, -(-(credited + pending) // FRIENDS_PAGE))
    page = max(0, min(callback_data.p, pages - 1))
    referrals = await db.list_referrals(user["user_id"], FRIENDS_PAGE, page * FRIENDS_PAGE)
    await show(call, *friends_screen(referrals, credited, pending, page))


@router.callback_query(U.filter(F.a == "top"))
async def cb_top(call: CallbackQuery, user: Row, db: Database) -> None:
    top = await db.top_referrers(10)
    rank = await db.user_rank(user["user_id"])
    await show(call, *top_screen(top, rank, user["ref_count"] + user["bonus_refs"], user["user_id"]))


@router.callback_query(U.filter(F.a == "rules"))
async def cb_rules(call: CallbackQuery, settings: Settings) -> None:
    text = render_template(settings.get("text_rules"), goal=settings.goal)
    await show(call, text, kb(
        [Btn(text="🔗 Пригласить друзей", style="primary", callback_data=U(a="invite").pack())],
        back_to_menu(),
    ))


@router.callback_query(U.filter(F.a == "claim"))
async def cb_claim(call: CallbackQuery, callback_answer: CallbackAnswer, user: Row, db: Database,
                   settings: Settings, rewards: RewardService, is_admin: bool) -> None:
    result = await rewards.claim(user)
    if result is ClaimResult.UNAVAILABLE:
        callback_answer.text = "Награда пока недоступна — пригласи ещё друзей 🙌"
        callback_answer.show_alert = True
        await open_menu(call, user["user_id"], db, settings, is_admin)
        return

    key = "text_reward_sent" if result is ClaimResult.SENT else "text_reward_pending"
    text = render_template(settings.get(key), name=esc(user["full_name"]))
    callback_answer.text = "🎉 Готово!"
    await show(call, text, kb(back_to_menu()))


@router.callback_query(U.filter(F.a == "noop"))
async def cb_noop(call: CallbackQuery) -> None:
    pass


@router.message()
async def fallback(message: Message, user: Row, db: Database, settings: Settings, is_admin: bool) -> None:
    """Любое непонятное сообщение — просто показываем меню."""
    await open_menu(message, user["user_id"], db, settings, is_admin)


# ---------- служебные апдейты ----------

@service_router.chat_join_request()
async def on_join_request(request: ChatJoinRequest, db: Database, subs: SubscriptionService) -> None:
    if await db.get_channel(request.chat.id):
        await db.add_join_request(request.chat.id, request.from_user.id)
        subs.reset_cache(request.from_user.id)


@service_router.my_chat_member()
async def on_bot_status(update: ChatMemberUpdated, db: Database, admins: AdminRegistry) -> None:
    status = update.new_chat_member.status
    if update.chat.type == "private":
        await db.set_blocked(update.chat.id, status == ChatMemberStatus.KICKED)
        return
    channel = await db.get_channel(update.chat.id)
    if channel and status != ChatMemberStatus.ADMINISTRATOR:
        await admins.notify(
            f"⚠️ <b>Бот потерял права админа в «{esc(channel['title'])}»</b>\n\n"
            "Проверка подписки на этот канал сейчас пропускается. "
            "Верните боту права администратора или отключите канал в админке."
        )


@service_router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=query.invoice_payload.startswith("topup:"),
                       error_message="Счёт устарел, запросите новый.")


@service_router.message(F.successful_payment)
async def on_payment(message: Message, admins: AdminRegistry) -> None:
    payment = message.successful_payment
    await message.answer(f"✅ Спасибо! Баланс бота пополнен на <b>{payment.total_amount}</b> ⭐")
    if not admins.is_admin(message.from_user.id):
        await admins.notify(f"⭐ {esc(message.from_user.full_name)} пополнил баланс бота на "
                            f"{payment.total_amount} ⭐")
