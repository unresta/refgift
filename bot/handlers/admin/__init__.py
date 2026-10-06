from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.callbacks import A
from bot.handlers.admin import ads, balance, broadcast, channel_gift_batch, channel_gifts, checks, reminders, roulette, channels, claims, home, nft, options, shop, staff, texts, users
from bot.handlers.admin.common import CommandResetsStateMiddleware, IsAdmin, ResetStateMiddleware


def build_admin_router() -> Router:
    router = Router(name="admin")
    router.message.filter(F.chat.type == "private", IsAdmin())
    router.callback_query.filter(IsAdmin())
    router.message.outer_middleware(CommandResetsStateMiddleware())
    router.callback_query.middleware(ResetStateMiddleware())

    @router.callback_query(A.filter(F.s == "noop"))
    async def noop(call: CallbackQuery) -> None:
        pass

    router.include_routers(
        home.router, balance.router, ads.router, checks.router, reminders.router, roulette.router, channels.router, channel_gifts.router, channel_gift_batch.router, nft.router, shop.router, claims.router, users.router,
        broadcast.router, options.router, texts.router, staff.router,
    )
    return router
