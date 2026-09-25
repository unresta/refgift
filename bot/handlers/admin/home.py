import csv
import io
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.callbacks import A, U
from bot.config import Config
from bot.database import Database
from bot.handlers.admin.common import Btn, back, btn, kb
from bot.services.broadcast import Broadcaster
from bot.services.subscription import SubscriptionService
from bot.settings import Settings
from bot.utils import esc, fmt_dt, fmt_num, percent, plural, show


def friends(n: int) -> str:
    return plural(n, "друг", "друга", "друзей")

router = Router(name="admin_home")


def day_start(config: Config, days_ago: int = 0) -> int:
    today = datetime.now(config.tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((today - timedelta(days=days_ago)).timestamp())


async def star_balance(bot: Bot) -> int | None:
    try:
        return (await bot.get_my_star_balance()).amount
    except TelegramAPIError:
        return None


async def home_screen(bot: Bot, db: Database, settings: Settings, config: Config,
                      subs: SubscriptionService, broadcaster: Broadcaster):
    st = await db.stats(day_start(config), settings.goal)
    channels = await db.channels(only_active=True)
    balance = await star_balance(bot)
    price = max(1, settings.get_int("gift_price"))
    mode = "авто ⭐" if settings.get("reward_mode") == "auto" else "вручную 🤝"

    lines = [
        "🛠 <b>Админ-панель</b>\n",
        f"👥 Пользователей: <b>{fmt_num(st['total'])}</b> (+{st['today']} сегодня)",
        f"✅ Прошли подписку: <b>{fmt_num(st['verified'])}</b>",
        f"🔗 Засчитано рефералов: <b>{fmt_num(st['credited'])}</b>",
        f"📎 С рекламы: <b>{fmt_num(st['from_ads'])}</b> (+{st['from_ads_today']} сегодня)",
        f"🧸 Выдано наград: <b>{st['claims_sent']}</b> · ⏳ ждут: <b>{st['claims_pending']}</b>",
    ]
    if balance is not None:
        lines.append(f"⭐ Баланс бота: <b>{fmt_num(balance)}</b> — хватит на ~{balance // price} "
                     f"{settings.get('gift_emoji')}")
    lines.append(f"\n🎯 Цель: <b>{settings.goal}</b> {friends(settings.goal)} · выдача: <b>{mode}</b>")
    lines.append(f"📢 Каналов в проверке: <b>{len(channels)}</b>")

    warnings = []
    if settings.flag("maintenance"):
        warnings.append("🛠 Включён режим техработ — пользователи не видят бота.")
    if not channels:
        warnings.append("📢 Нет каналов — рефералы засчитываются сразу после /start.")
    for ch in channels:
        if ch["chat_id"] in subs.broken:
            warnings.append(f"⚠️ Не проверяется подписка на «{esc(ch['title'])}» — бот не админ?")
    if settings.get("reward_mode") == "auto" and balance is not None and balance < price:
        warnings.append("⭐ Звёзд не хватает на подарок — заявки будут уходить на ручную выдачу.")
    if broadcaster.running:
        warnings.append("📨 Сейчас идёт рассылка.")
    if warnings:
        lines.append("\n" + "\n".join(warnings))

    pending = st["claims_pending"]
    markup = kb(
        [btn("📊 Статистика", "stats"), btn("📎 Рекламные ссылки", "lk")],
        [btn(f"🎁 Заявки · {pending}" if pending else "🎁 Заявки", "cl",
             style="success" if pending else None),
         btn(f"📢 Каналы · {len(channels)}", "ch")],
        [btn("👥 Пользователи", "us"), btn("📨 Рассылка", "bc")],
        [btn("⚙️ Настройки", "st"), btn("📝 Тексты", "tx")],
        [btn("👮 Админы", "ad")],
        [btn("🔄 Обновить", "home", "refresh"), Btn(text="🏠 Меню бота", callback_data=U(a="menu").pack())],
    )
    return "\n".join(lines), markup


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext, bot: Bot, db: Database, settings: Settings,
                    config: Config, subs: SubscriptionService, broadcaster: Broadcaster) -> None:
    await state.clear()
    await show(message, *await home_screen(bot, db, settings, config, subs, broadcaster))


@router.callback_query(A.filter(F.s == "home"))
async def cb_home(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, bot: Bot,
                  db: Database, settings: Settings, config: Config, subs: SubscriptionService,
                  broadcaster: Broadcaster) -> None:
    if callback_data.a == "refresh":
        callback_answer.text = "🔄 Обновлено"
    await show(call, *await home_screen(bot, db, settings, config, subs, broadcaster))


# ---------- статистика ----------

@router.callback_query(A.filter(F.s == "stats"))
async def cb_stats(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, bot: Bot,
                   db: Database, settings: Settings, config: Config) -> None:
    if callback_data.a == "export":
        await export_csv(call, db, config)
        callback_answer.text = "📥 Файл отправлен"
        return

    st = await db.stats(day_start(config), settings.goal)
    balance = await star_balance(bot)

    since = day_start(config, 6)
    buckets = [0] * 7
    for ts in await db.registrations_since(since):
        idx = (ts - since) // 86400
        if 0 <= idx < 7:
            buckets[idx] += 1
    peak = max(buckets) or 1
    chart = []
    for i, count in enumerate(buckets):
        day = datetime.fromtimestamp(since + i * 86400, config.tz).strftime("%d.%m")
        bar = "▇" * round(count / peak * 12) or "▏"
        chart.append(f"<code>{day} {bar}</code> {count}")

    text = "\n".join([
        "📊 <b>Статистика</b>\n",
        "👥 <b>Пользователи</b>",
        f"├ Всего: <b>{fmt_num(st['total'])}</b>",
        f"├ Новые: сегодня <b>+{st['today']}</b> · 7 дн. <b>+{st['week']}</b> · 30 дн. <b>+{st['month']}</b>",
        f"├ Активны за 24 ч: <b>{fmt_num(st['active24'])}</b>",
        f"├ Прошли подписку: <b>{fmt_num(st['verified'])}</b> ({percent(st['verified'], st['total'])}%)",
        f"├ Пришли с рекламы: <b>{fmt_num(st['from_ads'])}</b> (сегодня +{st['from_ads_today']})",
        f"├ Заблокировали бота: <b>{fmt_num(st['blocked'])}</b>",
        f"└ В бане: <b>{st['banned']}</b>",
        "",
        "🔗 <b>Рефералы</b>",
        f"├ Пришли по ссылкам: <b>{fmt_num(st['invited'])}</b> (сегодня +{st['invited_today']})",
        f"├ Засчитано: <b>{fmt_num(st['credited'])}</b> "
        f"(конверсия {percent(st['credited'], st['invited'])}%)",
        f"├ Ждут подписку: <b>{fmt_num(st['ref_pending'])}</b>",
        f"└ Достигли цели ({settings.goal}): <b>{fmt_num(st['reached_goal'])}</b>",
        "",
        "🧸 <b>Награды</b>",
        f"├ Выдано: <b>{st['claims_sent']}</b> (авто {st['sent_auto']} · вручную {st['sent_manual']})",
        f"├ Ожидают: <b>{st['claims_pending']}</b>",
        f"├ Отклонено: <b>{st['claims_rejected']}</b>",
        f"└ Баланс звёзд: <b>{fmt_num(balance) if balance is not None else '—'}</b> ⭐",
        "",
        "📈 <b>Регистрации за 7 дней</b>",
        *chart,
        "",
        f"<i>Обновлено: {fmt_dt(int(datetime.now().timestamp()), config.tz)}</i>",
    ])
    await show(call, text, kb(
        [btn("📥 Выгрузить CSV", "stats", "export"), btn("🔄 Обновить", "stats")],
        back(),
    ))


async def export_csv(call: CallbackQuery, db: Database, config: Config, ad_link_id: int | None = None,
                     label: str = "") -> None:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["user_id", "username", "full_name", "referrer_id", "credited", "ref_count", "bonus_refs",
                     "total", "rewards_claimed", "verified", "banned", "blocked", "created_at", "last_seen",
                     "ad_link_id"])
    for r in await db.export_users(ad_link_id):
        writer.writerow([
            r["user_id"], r["username"] or "", r["full_name"], r["referrer_id"] or "", r["ref_credited"],
            r["ref_count"], r["bonus_refs"], r["total"], r["rewards_claimed"],
            fmt_dt(r["verified_at"], config.tz), r["is_banned"], r["is_blocked"],
            fmt_dt(r["created_at"], config.tz), fmt_dt(r["last_seen"], config.tz), r["ad_link_id"] or "",
        ])
    data = buf.getvalue().encode("utf-8-sig")  # BOM — чтобы Excel корректно открыл кириллицу
    stamp = datetime.now(config.tz).strftime("%Y-%m-%d_%H-%M")
    await call.message.answer_document(
        BufferedInputFile(data, filename=f"users_{label + '_' if label else ''}{stamp}.csv"),
        caption=f"📥 Выгрузка пользователей{' по ссылке ' + esc(label) if label else ''} (разделитель «;»)",
    )
