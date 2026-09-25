"""Inline-режим: админы создают чеки (@bot 10 подпись), пользователи делятся реф-ссылкой."""
from aiogram import Bot, Router
from aiogram.types import (ChosenInlineResult, InlineKeyboardButton, InlineKeyboardMarkup, InlineQuery,
                           InlineQueryResultArticle, InlineQueryResultCachedPhoto, InlineQueryResultsButton,
                           InputTextMessageContent)
from aiogram.types import Gift
from aiosqlite import Row

from bot.database import Database
from bot.handlers.admin.home import star_balance
from bot.services.admins import AdminRegistry
from bot.services.checks import MAX_ACTIVATIONS, CheckService
from bot.services.gifts import GiftImages
from bot.settings import Settings
from bot.utils import fmt_num, render_template
from bot.views import ref_link

router = Router(name="inline")

RESULT_PREFIX = "chk:"
MAX_CAPTION = 900


async def check_result(check: Row, checks: CheckService, settings: Settings, images: GiftImages,
                       balance: int | None, gift: Gift | None = None, is_default: bool = False):
    total, left = check["total"], check["total"] - check["used"]
    emoji, price = checks.emoji(check), checks.price(check)
    need = left * price
    title = f"{emoji} {price}⭐ × {fmt_num(total)}" + (" · по умолчанию" if is_default else "")
    if check["used"]:
        title += f" · осталось {left}"
    description = f"Всего ~{fmt_num(need)} ⭐"
    if balance is not None:
        description += f" · баланс {fmt_num(balance)} ⭐"
        if settings.get("reward_mode") == "auto" and balance < need:
            description += " ⚠️ не хватит — часть уйдёт в заявки"
    description += "\nНажмите, чтобы отправить чек в этот чат"

    common = dict(id=f"{RESULT_PREFIX}{check['id']}", title=title, description=description,
                  reply_markup=checks.keyboard(check))
    gift = gift or await images.catalog.get(check["gift_id"] or "")
    if gift:
        photo = await images.file_id(gift)
    else:  # подарок пропал из каталога — берём сохранённый баннер или общий
        photo = await images.db.get_gift_banner(check["gift_id"] or "") or settings.get("check_photo") or None
    if photo:
        return InlineQueryResultCachedPhoto(photo_file_id=photo, caption=checks.caption(check), **common)
    return InlineQueryResultArticle(input_message_content=InputTextMessageContent(message_text=checks.caption(check)),
                                    **common)


def hint(text: str) -> InlineQueryResultsButton:
    return InlineQueryResultsButton(text=text, start_parameter="checks_help")


@router.inline_query()
async def on_inline(query: InlineQuery, bot: Bot, db: Database, settings: Settings, admins: AdminRegistry,
                    checks: CheckService, gift_images: GiftImages, bot_username: str) -> None:
    if not admins.is_admin(query.from_user.id):
        await user_inline(query, db, settings, bot_username)
        return

    text = query.query.strip()
    balance = await star_balance(bot)

    # @bot #code — повторно отправить существующий чек
    if text.startswith("#"):
        check = await db.get_check_by_code(text[1:].strip())
        if not check:
            await query.answer([], cache_time=0, is_personal=True, button=hint("❓ Чек не найден"))
            return
        result = await check_result(check, checks, settings, gift_images, balance)
        await query.answer([result], cache_time=0, is_personal=True)
        return

    parts = text.split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        await query.answer([], cache_time=0, is_personal=True,
                           button=hint("🎟 Введите число активаций: 10 [подпись]"))
        return

    total = int(parts[0])
    if not 1 <= total <= MAX_ACTIVATIONS:
        await query.answer([], cache_time=0, is_personal=True,
                           button=hint(f"⚠️ Активаций: от 1 до {fmt_num(MAX_ACTIVATIONS)}"))
        return
    caption = parts[1].strip()[:MAX_CAPTION] if len(parts) > 1 else None

    # Все доступные подарки: подарок по умолчанию первым, остальные — по цене.
    default_id = settings.get("gift_id")
    gifts = sorted(await gift_images.catalog.gifts(), key=lambda g: (g.id != default_id, g.star_count))
    if not gifts:
        await query.answer([], cache_time=0, is_personal=True, button=hint("⚠️ Не удалось загрузить подарки"))
        return
    results = []
    for gift in gifts[:50]:
        check = await checks.get_or_create_draft(query.from_user.id, total, caption, gift)
        results.append(await check_result(check, checks, settings, gift_images, balance, gift,
                                          is_default=gift.id == default_id))
    await query.answer(results, cache_time=0, is_personal=True,
                       button=hint(f"🎟 Выберите подарок для чека на {fmt_num(total)} активаций"))


async def user_inline(query: InlineQuery, db: Database, settings: Settings, bot_username: str) -> None:
    """Обычный пользователь: карточка с его реферальной ссылкой."""
    user = await db.get_user(query.from_user.id)
    if not user or user["verified_at"] is None:
        await query.answer([], cache_time=60, is_personal=True,
                           button=InlineQueryResultsButton(text="🧸 Открыть бота", start_parameter="inline"))
        return
    link = ref_link(bot_username, user["user_id"])
    share = render_template(settings.get("text_share"), goal=settings.goal)
    result = InlineQueryResultArticle(
        id="invite",
        title=f"{settings.get('gift_emoji')} Пригласить друга",
        description="Отправить свою ссылку — друг засчитается после подписки",
        input_message_content=InputTextMessageContent(message_text=f"{share}\n\n{link}"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"{settings.get('gift_emoji')} Забрать подарок", url=link)
        ]]),
    )
    await query.answer([result], cache_time=60, is_personal=True)


@router.chosen_inline_result()
async def on_chosen(result: ChosenInlineResult, db: Database, admins: AdminRegistry) -> None:
    """Работает, если в @BotFather включён /setinlinefeedback: отмечаем чек отправленным."""
    if not result.result_id.startswith(RESULT_PREFIX) or not admins.is_admin(result.from_user.id):
        return
    check_id = int(result.result_id.removeprefix(RESULT_PREFIX))
    await db.mark_check_sent(check_id, result.inline_message_id)
