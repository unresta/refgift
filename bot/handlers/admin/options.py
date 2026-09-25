from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LabeledPrice, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.callbacks import A
from bot.handlers.admin.common import Input, back, btn, drop_prompt, kb, prompt
from bot.handlers.admin.home import friends, star_balance
from bot.services.rewards import RewardService
from bot.services.subscription import SubscriptionService
from bot.settings import Settings
from bot.utils import esc, show

router = Router(name="admin_options")

FLAGS = {"repeatable", "notify_referrer", "maintenance"}
TTL_STEPS = [0, 30, 60, 300]
GOAL_PRESETS = [1, 3, 5, 10, 15, 20]


def on_off(value: bool) -> str:
    return "вкл ✅" if value else "выкл"


def settings_screen(settings: Settings):
    auto = settings.get("reward_mode") == "auto"
    goal = settings.goal
    ttl = settings.get_int("sub_cache_ttl")
    maintenance = settings.flag("maintenance")
    text = "\n".join([
        "⚙️ <b>Настройки</b>\n",
        f"🎯 Цель: <b>{goal}</b> {friends(goal)}",
        f"🎁 Выдача: <b>{'автоматически' if auto else 'вручную'}</b>",
        "   " + ("<i>бот сам дарит подарок за звёзды своего баланса</i>" if auto
                 else "<i>заявка приходит админам, выдаёте сами</i>"),
        f"🧸 Подарок: {settings.get('gift_emoji')} · {settings.get('gift_price')} ⭐",
        f"💬 Подпись: «{settings.get('gift_text') or '—'}»",
        f"🔁 Повторные награды: <b>{on_off(settings.repeatable)}</b>",
        "   " + (f"<i>мишка за каждые {goal} {friends(goal)}</i>" if settings.repeatable
                 else "<i>один мишка на пользователя</i>"),
        f"🔔 Уведомлять пригласившего: <b>{on_off(settings.flag('notify_referrer'))}</b>",
        f"⏱ Кэш проверки подписки: <b>{ttl} сек</b>",
        f"🛠 Техработы: <b>{on_off(maintenance)}</b>",
    ])
    return text, kb(
        [btn(f"🎯 Цель: {goal} {friends(goal)}", "st", "goal", style="primary")],
        [btn(f"🎁 Выдача: {'авто' if auto else 'вручную'}", "st", "mode"), btn("🧸 Выбрать подарок", "st", "gifts")],
        [btn("💬 Подпись к подарку", "st", "gifttext")],
        [btn(f"🔁 Повтор: {'вкл' if settings.repeatable else 'выкл'}", "st", "t", v="repeatable"),
         btn(f"🔔 Уведомления: {'вкл' if settings.flag('notify_referrer') else 'выкл'}", "st", "t",
             v="notify_referrer")],
        [btn(f"⏱ Кэш подписки: {ttl} с", "st", "ttl")],
        [btn("🛠 Выключить техработы" if maintenance else "🛠 Включить техработы", "st", "t", v="maintenance",
             style="success" if maintenance else "danger")],
        back(),
    )


@router.callback_query(A.filter((F.s == "st") & (F.a == "open")))
async def cb_open(call: CallbackQuery, settings: Settings) -> None:
    await show(call, *settings_screen(settings))


@router.callback_query(A.filter((F.s == "st") & F.a.in_({"t", "mode", "ttl"})))
async def cb_toggle(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, settings: Settings,
                    subs: SubscriptionService) -> None:
    if callback_data.a == "t" and callback_data.v in FLAGS:
        value = await settings.toggle(callback_data.v)
        callback_answer.text = "Включено" if value else "Выключено"
    elif callback_data.a == "mode":
        new = "manual" if settings.get("reward_mode") == "auto" else "auto"
        await settings.set("reward_mode", new)
        callback_answer.text = "Автовыдача подарком" if new == "auto" else "Ручная выдача через заявки"
    elif callback_data.a == "ttl":
        cur = settings.get_int("sub_cache_ttl")
        nxt = TTL_STEPS[(TTL_STEPS.index(cur) + 1) % len(TTL_STEPS)] if cur in TTL_STEPS else 60
        await settings.set("sub_cache_ttl", nxt)
        subs.reset_cache()
    await show(call, *settings_screen(settings))


# ---------- цель ----------

def goal_screen(settings: Settings):
    goal = settings.goal
    presets = [btn(f"{'• ' if n == goal else ''}{n}", "st", "setgoal", id=n) for n in GOAL_PRESETS]
    return (f"🎯 <b>Сколько друзей нужно пригласить?</b>\n\nСейчас: <b>{goal}</b>\n\n"
            "<i>Изменение сразу применится ко всем — у кого уже хватает друзей, "
            "появится кнопка «Забрать мишку».</i>"), kb(
        [btn("➖", "st", "setgoal", id=max(1, goal - 1)), btn(str(goal), "noop"),
         btn("➕", "st", "setgoal", id=goal + 1)],
        presets[:3], presets[3:],
        [btn("✏️ Ввести число", "st", "goalin")],
        back("st"),
    )


@router.callback_query(A.filter((F.s == "st") & (F.a == "goal")))
async def cb_goal(call: CallbackQuery, settings: Settings) -> None:
    await show(call, *goal_screen(settings))


@router.callback_query(A.filter((F.s == "st") & (F.a == "setgoal")))
async def cb_set_goal(call: CallbackQuery, callback_data: A, settings: Settings) -> None:
    await settings.set("ref_goal", max(1, min(1000, callback_data.id)))
    await show(call, *goal_screen(settings))


@router.callback_query(A.filter((F.s == "st") & (F.a == "goalin")))
async def cb_goal_input(call: CallbackQuery, state: FSMContext) -> None:
    await prompt(call, state, Input.goal, "✏️ Пришлите число от 1 до 1000:", back("st", "goal", "✖️ Отмена"))


@router.message(Input.goal, F.text)
async def on_goal(message: Message, state: FSMContext, settings: Settings) -> None:
    raw = message.text.strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 1000:
        await message.answer("⚠️ Нужно целое число от 1 до 1000")
        return
    await drop_prompt(message, state)
    await state.clear()
    await settings.set("ref_goal", int(raw))
    await show(message, *goal_screen(settings))


# ---------- подарок ----------

@router.callback_query(A.filter((F.s == "st") & (F.a == "gifts")))
async def cb_gifts(call: CallbackQuery, callback_answer: CallbackAnswer, bot: Bot, settings: Settings) -> None:
    try:
        gifts = (await bot.get_available_gifts()).gifts
    except TelegramAPIError as e:
        callback_answer.text = f"Не удалось получить подарки: {e.message}"[:200]
        callback_answer.show_alert = True
        return
    current = settings.get("gift_id")
    balance = await star_balance(bot)
    buttons = []
    for g in sorted(gifts, key=lambda x: x.star_count):
        emoji = g.sticker.emoji or "🎁"
        left = f" ({g.remaining_count})" if g.remaining_count is not None else ""
        mark = "✅ " if g.id == current else ""
        buttons.append(btn(f"{mark}{emoji} {g.star_count}⭐{left}", "st", "gift", v=g.id,
                           style="success" if g.id == current else None))
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([btn("⭐ Пополнить баланс", "st", "topup", style="primary"),
                 btn("🧪 Тест себе", "st", "test")])
    rows.append(back("st"))
    await show(call, "🧸 <b>Выберите подарок</b>\n\n"
                     f"Сейчас: {settings.get('gift_emoji')} · {settings.get('gift_price')} ⭐\n"
                     f"Баланс бота: <b>{balance if balance is not None else '—'}</b> ⭐\n\n"
                     "Подарок оплачивается звёздами с баланса бота — пополнить можно кнопкой "
                     "«⭐ Пополнить баланс».\n"
                     "<i>(N) — осталось лимитированных подарков.</i>", kb(*rows))


@router.callback_query(A.filter((F.s == "st") & (F.a == "gift")))
async def cb_pick_gift(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, bot: Bot,
                       settings: Settings) -> None:
    try:
        gifts = (await bot.get_available_gifts()).gifts
    except TelegramAPIError:
        gifts = []
    gift = next((g for g in gifts if g.id == callback_data.v), None)
    if not gift:
        callback_answer.text = "Подарок больше недоступен"
        return
    await settings.set("gift_id", gift.id)
    await settings.set("gift_emoji", gift.sticker.emoji or "🎁")
    await settings.set("gift_price", gift.star_count)
    callback_answer.text = f"Выбран {gift.sticker.emoji} за {gift.star_count} ⭐"
    await cb_gifts(call, callback_answer, bot, settings)


TOPUP_AMOUNTS = [15, 50, 100, 250, 500, 1000]


@router.callback_query(A.filter((F.s == "st") & (F.a == "topup")))
async def cb_topup(call: CallbackQuery, settings: Settings) -> None:
    price = max(1, settings.get_int("gift_price"))
    buttons = [btn(f"{n} ⭐ · ~{n // price} {settings.get('gift_emoji')}", "st", "pay", id=n)
               for n in TOPUP_AMOUNTS]
    await show(call, "⭐ <b>Пополнение баланса бота</b>\n\n"
                     "Бот пришлёт счёт в Telegram Stars — после оплаты звёзды поступят на баланс бота "
                     "и будут тратиться на подарки.", kb(*(buttons[i:i + 2] for i in range(0, 6, 2)),
                                                         back("st", "gifts")))


@router.callback_query(A.filter((F.s == "st") & (F.a == "pay")))
async def cb_pay(call: CallbackQuery, callback_data: A) -> None:
    amount = max(1, min(10000, callback_data.id))
    await call.message.answer_invoice(
        title=f"Пополнение на {amount} ⭐",
        description="Звёзды поступят на баланс бота и пойдут на подарки пользователям.",
        payload=f"topup:{amount}",
        currency="XTR",
        prices=[LabeledPrice(label="Пополнение", amount=amount)],
    )


@router.callback_query(A.filter((F.s == "st") & (F.a == "test")))
async def cb_test(call: CallbackQuery, callback_answer: CallbackAnswer, rewards: RewardService) -> None:
    error = await rewards.send_gift(call.from_user.id)
    callback_answer.text = f"❌ {error}"[:200] if error else "🎁 Тестовый подарок отправлен — проверьте профиль"
    callback_answer.show_alert = True


@router.callback_query(A.filter((F.s == "st") & (F.a == "gifttext")))
async def cb_gift_text(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await prompt(call, state, Input.gift_text,
                 "💬 <b>Подпись к подарку</b>\n\n"
                 f"Сейчас: «{settings.get('gift_text') or '—'}»\n\n"
                 "Пришлите новый текст (до 128 символов) или «-», чтобы убрать подпись.",
                 back("st", text="✖️ Отмена"))


@router.message(Input.gift_text, F.text)
async def on_gift_text(message: Message, state: FSMContext, settings: Settings) -> None:
    raw = message.text.strip()
    if len(raw) > 128:
        await message.answer(f"⚠️ Слишком длинно: {len(raw)}/128 символов")
        return
    await drop_prompt(message, state)
    await state.clear()
    await settings.set("gift_text", "" if raw == "-" else esc(raw))
    await show(message, *settings_screen(settings))
