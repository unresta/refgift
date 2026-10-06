import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import (CallbackQuery, ChatJoinRequest, ChatMemberUpdated, LabeledPrice, Message,
                           PreCheckoutQuery)
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.utils.callback_answer import CallbackAnswer
from aiosqlite import Row

from bot.callbacks import A, Shop, ShopBuy, U
from bot.database import Database
from bot.services.admins import AdminRegistry
from bot.services.checks import CHECK_PREFIX, CheckService, CheckStatus
from bot.services.reminders import ReminderService
from bot.services.rewards import ClaimResult, RewardService
from bot.services.roulette import SPIN_PREFIX, RouletteService
from bot.services.shop import PREFIX as SHOP_PREFIX, PayResult, ShopService
from bot.services.subscription import SubscriptionService
from bot.services.userbot import Userbot
from bot.settings import Settings
from bot.utils import esc, render_template, show, show_card
from bot.views import (FRIENDS_PAGE, activation_screen, back_to_menu, friends_screen, invite_screen, kb, menu_screen,
                       nft_card_screen, nft_contact_base, nft_list_screen, plain, shop_comments_screen,
                       shop_done_screen, shop_item_screen, shop_screen,
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
    has_nft = await db.count_nft_gifts(only_active=True) > 0
    await show(event, *menu_screen(user, settings, pending is not None, is_admin, has_nft,
                                   settings.flag("shop_enabled")))


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


async def show_nft_list(call: CallbackQuery, db: Database, settings: Settings, page: int) -> None:
    gifts = await db.nft_gifts(only_active=True)
    try:
        await show(call, *nft_list_screen(gifts, settings, page))
    except TelegramBadRequest as e:
        if not any(g["emoji_id"] for g in gifts):
            raise
        # премиум-эмодзи на кнопках доступны, только если у владельца бота есть Telegram Premium
        log.warning("Кнопки с премиум-эмодзи не приняты (%s) — показываю без них", e)
        await show(call, *nft_list_screen(gifts, settings, page, icons=False))


@router.callback_query(U.filter(F.a == "nft"))
async def cb_nft(call: CallbackQuery, callback_data: U, db: Database, settings: Settings) -> None:
    await show_nft_list(call, db, settings, callback_data.p)


@router.callback_query(U.filter(F.a == "nftg"))
async def cb_nft_gift(call: CallbackQuery, callback_data: U, callback_answer: CallbackAnswer, db: Database,
                      settings: Settings, userbot: Userbot) -> None:
    gift = await db.get_nft_gift(callback_data.p)
    if gift is None or not gift["is_active"]:
        callback_answer.text = "Этот подарок больше недоступен"
        await show_nft_list(call, db, settings, 0)
        return
    await db.nft_gift_viewed(gift["id"])
    text, markup = nft_card_screen(gift, settings, nft_contact_base(settings, userbot.username))
    await show_card(call, text, markup, photo=gift["photo"], preview_url=gift["link"])


@router.callback_query(U.filter(F.a == "shop"))
async def cb_shop(call: CallbackQuery, callback_answer: CallbackAnswer, user: Row, db: Database, settings: Settings,
                  shop: ShopService, is_admin: bool) -> None:
    if not settings.flag("shop_enabled"):
        callback_answer.text = "Магазин сейчас закрыт"
        await open_menu(call, user["user_id"], db, settings, is_admin)
        return
    await show(call, *shop_screen(await shop.items(only_active=True), settings))


async def shop_item_or_list(call: CallbackQuery, callback_answer: CallbackAnswer, gift_id: str,
                            settings: Settings, shop: ShopService):
    """Подарок из магазина; если он больше не продаётся — показывает список и возвращает None."""
    item = await shop.item(gift_id)
    if settings.flag("shop_enabled") and item is not None and item.active:
        return item
    callback_answer.text = "Этот подарок больше не продаётся"
    await show(call, *shop_screen(await shop.items(only_active=True), settings))
    return None


@router.callback_query(Shop.filter())
async def cb_shop_item(call: CallbackQuery, callback_data: Shop, callback_answer: CallbackAnswer, db: Database,
                       settings: Settings, shop: ShopService) -> None:
    if item := await shop_item_or_list(call, callback_answer, callback_data.g, settings, shop):
        await show(call, *shop_item_screen(item, shop.default_comment(), bool(await db.shop_comments())))


@router.callback_query(ShopBuy.filter())
async def cb_buy(call: CallbackQuery, callback_data: ShopBuy, callback_answer: CallbackAnswer, db: Database,
                 settings: Settings, shop: ShopService) -> None:
    item = await shop_item_or_list(call, callback_answer, callback_data.g, settings, shop)
    if item is None:
        return
    comments = await db.shop_comments()
    if callback_data.c < 0:
        if comments:
            await show(call, *shop_comments_screen(item, comments))
        else:
            await show(call, *shop_item_screen(item, shop.default_comment(), False))
        return
    comment = await shop.comment_text(callback_data.c)
    if comment is None:
        callback_answer.text = "Этот вариант больше недоступен — выбери другой"
        await show(call, *shop_item_screen(item, shop.default_comment(), bool(comments)))
        return
    price = shop.price_for(item, callback_data.c)
    try:
        await call.message.answer_invoice(
            title=f"{item.emoji} {item.name.capitalize()}"[:32],
            description=f"Подарок Telegram с подписью «{plain(comment, 200)}» — придёт сразу после оплаты.",
            payload=shop.payload(item, callback_data.c),
            currency="XTR",
            prices=[LabeledPrice(label=f"{item.emoji} {item.name}", amount=price)],
        )
    except TelegramAPIError as e:
        log.warning("Счёт магазина не создан: %s", e)
        callback_answer.text = "Не удалось создать счёт — попробуй чуть позже"
        callback_answer.show_alert = True
        return
    callback_answer.text = f"🧾 Оплати {price} ⭐ — и {item.emoji} сразу придёт тебе"


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
async def on_pre_checkout(query: PreCheckoutQuery, roulette: RouletteService, shop: ShopService) -> None:
    payload = query.invoice_payload
    if payload.startswith(SHOP_PREFIX):
        error = await shop.validate(payload, query.total_amount)
        await query.answer(ok=error is None, error_message=error)
        return
    if payload.startswith(SPIN_PREFIX):
        ok = await roulette.validate_payment(query.from_user.id, payload, query.total_amount)
    else:
        ok = payload.startswith("topup:")
    await query.answer(ok=ok, error_message="Счёт устарел, откройте рулетку и попробуйте ещё раз.")


@service_router.message(F.successful_payment, F.successful_payment.invoice_payload.startswith(SHOP_PREFIX))
async def on_shop_payment(message: Message, shop: ShopService) -> None:
    payment = message.successful_payment
    purchase = await shop.on_paid(message.from_user.id, payment.invoice_payload, payment.total_amount,
                                  payment.telegram_payment_charge_id)
    if purchase.result is PayResult.SENT:
        text, markup = shop_done_screen(purchase.emoji, purchase.name, purchase.comment)
        await message.answer(text, reply_markup=markup)
    elif purchase.result is PayResult.REFUNDED:
        await message.answer(f"😔 <b>Не получилось отправить подарок</b>\n\n{purchase.price} ⭐ уже вернулись "
                             "на твой счёт. Попробуй чуть позже.", reply_markup=kb(back_to_menu()))
    elif purchase.result is PayResult.FAILED:
        await message.answer("😔 <b>Не получилось отправить подарок</b>\n\nМы уже знаем о проблеме — "
                             "админ свяжется с тобой и всё решит.", reply_markup=kb(back_to_menu()))


@service_router.message(F.successful_payment, F.successful_payment.invoice_payload.startswith(SPIN_PREFIX))
async def on_spin_payment(message: Message, roulette: RouletteService) -> None:
    """Оплачена прокрутка: приз разыгран, но подарок и сообщение о нём придут после остановки рулетки."""
    payment = message.successful_payment
    spin = await roulette.on_paid(message.from_user.id, payment.invoice_payload, payment.telegram_payment_charge_id)
    if spin and spin["status"] == "refunded":
        await message.answer("↩️ Не удалось провести прокрутку — звёзды вернулись на ваш счёт.")


@service_router.message(F.successful_payment)
async def on_payment(message: Message, bot: Bot, admins: AdminRegistry) -> None:
    payment = message.successful_payment
    try:
        balance = f"\nТеперь на балансе: <b>{(await bot.get_my_star_balance()).amount}</b> ⭐"
    except TelegramAPIError:
        balance = ""
    markup = None
    if admins.is_admin(message.from_user.id):
        markup = kb([Btn(text="⭐ К балансу", callback_data=A(s="bal").pack())],
                    [Btn(text="🛠 В админку", callback_data=A(s="home").pack())])
    await message.answer(f"✅ Спасибо! Баланс бота пополнен на <b>{payment.total_amount}</b> ⭐{balance}",
                         reply_markup=markup)
    if not admins.is_admin(message.from_user.id):
        await admins.notify(f"⭐ {esc(message.from_user.full_name)} пополнил баланс бота на "
                            f"{payment.total_amount} ⭐")
