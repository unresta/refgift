import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import LinkPreviewOptions
from aiogram.utils.callback_answer import CallbackAnswerMiddleware

from bot.config import Config, load_config
from bot.database import Database
from bot.handlers import inline, user
from bot.handlers.admin import build_admin_router
from bot.middlewares import SubscriptionGate, ThrottlingMiddleware, UserMiddleware
from bot.services.admins import AdminRegistry
from bot.services.broadcast import Broadcaster
from bot.services.checks import CheckService
from bot.services.gifts import GiftCatalog, GiftImages
from bot.services.rewards import RewardService
from bot.services.subscription import SubscriptionService
from bot.settings import Settings

log = logging.getLogger("bot")


async def build(config: Config, bot: Bot) -> tuple[Dispatcher, Database, AdminRegistry]:
    db = Database(config.db_path)
    await db.connect()

    settings = Settings(db)
    await settings.load()
    admins = AdminRegistry(bot, db, config.super_admins)
    await admins.load()
    subs = SubscriptionService(bot, db, settings)
    rewards = RewardService(bot, db, settings, admins)
    broadcaster = Broadcaster(bot, db)
    me = await bot.get_me()
    checks = CheckService(bot, db, settings, subs, rewards, me.username)
    gift_images = GiftImages(bot, db, settings, GiftCatalog(bot), admins)

    dp = Dispatcher(
        storage=MemoryStorage(),
        config=config, db=db, settings=settings, admins=admins, subs=subs,
        rewards=rewards, broadcaster=broadcaster, checks=checks, gift_images=gift_images,
        bot_username=me.username,
    )

    throttling = ThrottlingMiddleware()
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(UserMiddleware())
        observer.outer_middleware(throttling)
    dp.callback_query.middleware(CallbackAnswerMiddleware())

    user.router.message.middleware(SubscriptionGate())
    user.router.callback_query.middleware(SubscriptionGate())

    dp.include_routers(build_admin_router(), inline.router, user.service_router, user.router)
    return dp, db, admins


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(name)s: %(message)s")
    config = load_config()
    bot = Bot(config.bot_token, default=DefaultBotProperties(
        parse_mode=ParseMode.HTML,
        link_preview=LinkPreviewOptions(is_disabled=True),
    ))
    dp, db, admins = await build(config, bot)
    dp["gift_images"].schedule()  # догенерировать картинки чеков для новых подарков

    await admins.setup_commands()
    await bot.delete_webhook(drop_pending_updates=False)
    log.info("Бот запущен. Админы: %s", ", ".join(map(str, admins.all_ids)))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
