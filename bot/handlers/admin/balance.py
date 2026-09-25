from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LabeledPrice, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.callbacks import A
from bot.database import Database
from bot.handlers.admin.common import Input, back, btn, drop_prompt, kb, prompt
from bot.handlers.admin.home import star_balance
from bot.settings import Settings
from bot.utils import esc, fmt_num, show

router = Router(name="admin_balance")

PRESETS = [50, 100, 250, 500, 1000, 2500]
MAX_TOPUP = 10_000
# откуда пришли — туда и кнопка «Назад»
ORIGINS = {"": ("home", "open", "« В админку"), "gifts": ("st", "gifts", "« К подаркам"),
           "cl": ("cl", "open", "« К заявкам"), "ck": ("ck", "open", "« К чекам")}


async def balance_screen(bot: Bot, db: Database, settings: Settings, origin: str):
    balance = await star_balance(bot)
    price = max(1, settings.get_int("gift_price"))
    emoji = settings.get("gift_emoji")
    pending = await db.count_claims("pending")

    lines = ["⭐ <b>Баланс бота</b>\n"]
    if balance is None:
        lines.append("Сейчас: <i>не удалось получить</i>")
    else:
        lines.append(f"Сейчас: <b>{fmt_num(balance)}</b> ⭐")
        lines.append(f"Хватит на: ~<b>{fmt_num(balance // price)}</b> {emoji} (по {price} ⭐)")
    if pending:
        need = pending * price
        short = f" — не хватает {fmt_num(need - balance)} ⭐" if balance is not None and balance < need else ""
        lines.append(f"⏳ Заявок в очереди: <b>{pending}</b>, нужно ~{fmt_num(need)} ⭐{short}")
    lines.append("\nВыберите сумму — бот пришлёт счёт в Telegram Stars. После оплаты звёзды сразу "
                 "поступят на баланс бота и пойдут на подарки.")

    presets = [btn(f"{fmt_num(n)} ⭐ · ~{fmt_num(n // price)} {emoji}", "bal", "pay", id=n, v=origin)
               for n in PRESETS]
    target, action, label = ORIGINS.get(origin, ORIGINS[""])
    return "\n".join(lines), kb(
        presets[0:2], presets[2:4], presets[4:6],
        [btn("✏️ Своя сумма", "bal", "custom", v=origin, style="primary")],
        [btn("🔄 Обновить", "bal", v=origin)],
        back(target, action, label),
    )


async def send_invoice(message: Message, amount: int) -> str | None:
    try:
        await message.answer_invoice(
            title=f"Пополнение на {fmt_num(amount)} ⭐",
            description="Звёзды поступят на баланс бота и пойдут на подарки пользователям.",
            payload=f"topup:{amount}",
            currency="XTR",
            prices=[LabeledPrice(label="Пополнение", amount=amount)],
        )
    except TelegramAPIError as e:
        return e.message
    return None


@router.callback_query(A.filter((F.s == "bal") & (F.a == "open")))
async def cb_open(call: CallbackQuery, callback_data: A, bot: Bot, db: Database, settings: Settings) -> None:
    await show(call, *await balance_screen(bot, db, settings, callback_data.v))


@router.callback_query(A.filter((F.s == "bal") & (F.a == "pay")))
async def cb_pay(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer) -> None:
    error = await send_invoice(call.message, max(1, min(MAX_TOPUP, callback_data.id)))
    if error:
        callback_answer.text = f"❌ Не удалось создать счёт: {error}"[:200]
        callback_answer.show_alert = True
    else:
        callback_answer.text = "🧾 Счёт отправлен — оплатите его ниже"


@router.callback_query(A.filter((F.s == "bal") & (F.a == "custom")))
async def cb_custom(call: CallbackQuery, callback_data: A, state: FSMContext) -> None:
    await prompt(call, state, Input.topup_amount,
                 f"✏️ <b>Своя сумма пополнения</b>\n\nПришлите количество звёзд — от 1 до {fmt_num(MAX_TOPUP)}.",
                 back("bal", text="✖️ Отмена", v=callback_data.v), origin=callback_data.v)


@router.message(Input.topup_amount, F.text)
async def on_custom(message: Message, state: FSMContext) -> None:
    raw = message.text.strip().replace(" ", "").replace(" ", "").rstrip("⭐")
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_TOPUP:
        await message.answer(f"⚠️ Нужно целое число звёзд от 1 до {fmt_num(MAX_TOPUP)}")
        return
    data = await drop_prompt(message, state)
    await state.clear()
    error = await send_invoice(message, int(raw))
    if error:
        await message.answer(f"❌ Не удалось создать счёт: {esc(error)}")
    await message.answer("⭐ Вернуться к балансу", reply_markup=kb([btn("⭐ К балансу", "bal",
                                                                       v=data.get("origin", ""))]))
