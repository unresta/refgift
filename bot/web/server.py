"""HTTP-сервер мини-аппа «Рулетка подарков»: статика + JSON API с проверкой подписи Telegram initData."""
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.web_app import WebAppUser, safe_parse_webapp_init_data
from aiohttp import web
from aiosqlite import Row

from bot.database import Database
from bot.services.admins import AdminRegistry
from bot.services.gifts import GiftCatalog
from bot.services.rewards import RewardService
from bot.services.roulette import RouletteService, chance
from bot.services.subscription import SubscriptionService
from bot.settings import Settings
from bot.views import channel_url
from bot.web.media import GiftMedia, media_kind

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
INIT_DATA_MAX_AGE = 24 * 3600
SPIN_COOLDOWN = 2.0  # секунд между созданием счетов одним пользователем
_last_invoice: dict[int, float] = {}


@dataclass(slots=True)
class WebContext:
    bot: Bot
    db: Database
    settings: Settings
    subs: SubscriptionService
    rewards: RewardService
    roulette: RouletteService
    catalog: GiftCatalog
    media: GiftMedia
    admins: AdminRegistry


def error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": code, "message": message}, status=status)


def ctx_of(request: web.Request) -> WebContext:
    return request.app["ctx"]


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Все /api/* (кроме картинок подарков) — только с валидной подписью Telegram."""
    if not request.path.startswith("/api/") or request.path.startswith("/api/gift/"):
        return await handler(request)
    ctx = ctx_of(request)
    raw = request.headers.get("Authorization", "").removeprefix("tma ").strip()
    try:
        data = safe_parse_webapp_init_data(ctx.bot.token, raw)
    except ValueError:
        return error(401, "unauthorized", "Откройте мини-апп из Telegram")
    if data.user is None or time.time() - data.auth_date.timestamp() > INIT_DATA_MAX_AGE:
        return error(401, "expired", "Сессия устарела — перезапустите мини-апп")

    tg: WebAppUser = data.user
    full_name = " ".join(x for x in (tg.first_name, tg.last_name) if x)
    user, _ = await ctx.db.upsert_user(tg.id, tg.username, full_name)
    is_admin = ctx.admins.is_admin(tg.id)
    if not is_admin:
        if user["is_banned"]:
            return error(403, "banned", "Доступ ограничен")
        if ctx.settings.flag("maintenance"):
            return error(503, "maintenance", "Бот на техническом обслуживании")
        if not ctx.roulette.enabled:
            return error(503, "disabled", "Рулетка временно недоступна")
    request["user"], request["tg"] = user, tg
    return await handler(request)


# ---------- сериализация ----------

async def prize_json(ctx: WebContext, prize: Row, total_weight: float | None = None) -> dict:
    gift = await ctx.catalog.any(prize["gift_id"])
    data = {
        "gift_id": prize["gift_id"],
        "emoji": prize["gift_emoji"] or "🎁",
        "price": prize["gift_price"],
        "media": media_kind(gift) if gift else None,
        "thumb": bool(gift and gift.sticker.thumbnail),
    }
    if total_weight is not None:
        data["id"] = prize["id"]
        data["chance"] = round(chance(prize, total_weight), 3)
    return data


async def spin_prize_json(ctx: WebContext, spin: Row) -> dict | None:
    if not spin["gift_id"]:
        return None
    gift = await ctx.catalog.any(spin["gift_id"])
    return {"gift_id": spin["gift_id"], "emoji": spin["gift_emoji"] or "🎁", "price": spin["gift_price"],
            "media": media_kind(gift) if gift else None, "thumb": bool(gift and gift.sticker.thumbnail)}


def short_name(full_name: str | None) -> str:
    parts = (full_name or "Игрок").split()
    return parts[0] if len(parts) == 1 else f"{parts[0]} {parts[1][0]}."


async def need_sub(ctx: WebContext, user_id: int, fresh: bool = False) -> list[dict]:
    if not ctx.settings.flag("roulette_require_sub"):
        return []
    missing = await ctx.subs.missing(user_id, use_cache=not fresh)
    if not missing:
        await ctx.rewards.complete_verification(user_id)
    return [{"title": ch["title"], "url": channel_url(ch)} for ch in missing if channel_url(ch)]


# ---------- API ----------

async def api_init(request: web.Request) -> web.Response:
    ctx, user, tg = ctx_of(request), request["user"], request["tg"]
    cases = []
    for case, prizes in await ctx.roulette.active_cases():
        total = sum(p["weight"] for p in prizes)
        cases.append({"id": case["id"], "name": case["name"], "price": case["price"],
                      "prizes": [await prize_json(ctx, p, total) for p in prizes]})
    return web.json_response({
        "user": {"id": user["user_id"], "name": user["full_name"], "photo_url": tg.photo_url},
        "cases": cases,
        "demo": ctx.settings.flag("roulette_demo"),
        "need_sub": await need_sub(ctx, user["user_id"]),
    })


async def api_check_sub(request: web.Request) -> web.Response:
    return web.json_response({"need_sub": await need_sub(ctx_of(request), request["user"]["user_id"], fresh=True)})


async def _active_case(ctx: WebContext, request: web.Request) -> tuple[Row, list[Row]] | None:
    try:
        case_id = int((await request.json()).get("case_id"))
    except (ValueError, TypeError, AttributeError):
        return None
    return next(((c, p) for c, p in await ctx.roulette.active_cases() if c["id"] == case_id), None)


async def api_spin(request: web.Request) -> web.Response:
    ctx, user = ctx_of(request), request["user"]
    now = time.monotonic()
    if now - _last_invoice.get(user["user_id"], 0) < SPIN_COOLDOWN:
        return error(429, "slow_down", "Секунду…")
    found = await _active_case(ctx, request)
    if not found:
        return error(404, "case", "Кейс недоступен")
    if await need_sub(ctx, user["user_id"], fresh=True):
        return error(403, "need_sub", "Сначала подпишитесь на каналы")
    try:
        spin_id, link = await ctx.roulette.create_invoice(user["user_id"], found[0])
    except TelegramAPIError as e:
        log.error("create_invoice_link: %s", e)
        return error(502, "invoice", "Не удалось создать счёт, попробуйте ещё раз")
    _last_invoice[user["user_id"]] = now
    return web.json_response({"spin_id": spin_id, "invoice": link})


async def api_spin_status(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    spin = await ctx.db.get_spin(int(request.match_info["spin_id"]))
    if not spin or spin["user_id"] != request["user"]["user_id"]:
        return error(404, "spin", "Прокрутка не найдена")
    return web.json_response({"status": spin["status"], "prize": await spin_prize_json(ctx, spin)})


async def api_spin_reveal(request: web.Request) -> web.Response:
    """Рулетка остановилась на призе — теперь отправляем подарок (не раньше, чтобы сохранить интригу)."""
    ctx = ctx_of(request)
    spin = await ctx.db.get_spin(int(request.match_info["spin_id"]))
    if not spin or spin["user_id"] != request["user"]["user_id"]:
        return error(404, "spin", "Прокрутка не найдена")
    if spin["status"] == "paid":
        spin = await ctx.roulette.deliver(spin["id"]) or spin
    return web.json_response({"status": spin["status"], "prize": await spin_prize_json(ctx, spin)})


async def api_demo(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    if not ctx.settings.flag("roulette_demo"):
        return error(403, "demo", "Демо-режим выключен")
    found = await _active_case(ctx, request)
    if not found:
        return error(404, "case", "Кейс недоступен")
    prize = ctx.roulette.roll(found[1])
    return web.json_response({"prize": await prize_json(ctx, prize)})


async def api_top(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    recent = [{"name": short_name(s["full_name"]), "case": s["case_name"], "at": s["paid_at"],
               **(await spin_prize_json(ctx, s) or {})} for s in await ctx.db.recent_wins(20)]
    top = [{"name": short_name(r["full_name"]), "spins": r["spins"], "won": r["won"] or 0, "best": r["best"] or 0,
            "me": r["user_id"] == request["user"]["user_id"]}
           for r in await ctx.db.top_winners(int(time.time()) - 7 * 86400, 20)]
    return web.json_response({"recent": recent, "top": top})


async def api_profile(request: web.Request) -> web.Response:
    ctx, user = ctx_of(request), request["user"]
    spins = await ctx.db.user_spins(user["user_id"], 50)
    won = [s for s in spins if s["status"] != "refunded"]
    best = max(won, key=lambda s: s["gift_price"] or 0, default=None)
    return web.json_response({
        "spins": len(spins),
        "won": sum(s["gift_price"] or 0 for s in won),
        "spent": sum(s["price"] for s in won),
        "best": await spin_prize_json(ctx, best) if best else None,
        "history": [{"id": s["id"], "case": s["case_name"], "price": s["price"], "status": s["status"],
                     "at": s["paid_at"], "prize": await spin_prize_json(ctx, s)} for s in spins],
    })


async def gift_media(request: web.Request) -> web.Response:
    ctx = ctx_of(request)
    result = await ctx.media.get(request.match_info["gift_id"], thumb=request.match_info.get("thumb") == "thumb")
    if result is None:
        raise web.HTTPNotFound()
    body, content_type = result
    return web.Response(body=body, content_type=content_type,
                        headers={"Cache-Control": "public, max-age=604800, immutable"})


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


def create_app(ctx: WebContext) -> web.Application:
    app = web.Application(middlewares=[auth_middleware], client_max_size=64 * 1024)
    app["ctx"] = ctx
    app.router.add_get("/", index)
    app.router.add_static("/static/", STATIC_DIR, append_version=True)
    app.router.add_post("/api/init", api_init)
    app.router.add_post("/api/check_sub", api_check_sub)
    app.router.add_post("/api/spin", api_spin)
    app.router.add_get("/api/spin/{spin_id:\\d+}", api_spin_status)
    app.router.add_post("/api/spin/{spin_id:\\d+}/reveal", api_spin_reveal)
    app.router.add_post("/api/demo", api_demo)
    app.router.add_get("/api/top", api_top)
    app.router.add_get("/api/profile", api_profile)
    app.router.add_get("/api/gift/{gift_id:\\w+}", gift_media)
    app.router.add_get("/api/gift/{gift_id:\\w+}/{thumb:thumb}", gift_media)
    return app
