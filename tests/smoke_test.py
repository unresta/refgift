"""Офлайн-прогон бота: фейковый Telegram API + реальные апдейты через диспетчер.

Запуск:  .venv/bin/python -m tests.smoke_test
"""
import asyncio
import gzip
import hashlib
import hmac
import itertools
import json
import logging
import os
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (AnswerInlineQuery, CopyMessage, CreateChatInviteLink, EditMessageCaption,
                             GetAvailableGifts, GetChat, GetChatMember, GetChatMemberCount, GetFile, GetMe,
                             GetMyStarBalance, RefundStarPayment, SendDocument, SendGift, SendInvoice, SendPhoto,
                             TelegramMethod, AnswerPreCheckoutQuery)
from aiogram.types import ChatFullInfo, ChatInviteLink, File, Gifts, MessageId, StarAmount, User

from bot import middlewares
from bot.__main__ import build
from bot.callbacks import A, U
from bot.config import Config

ADMIN = 1
BOT_ID = 999
CHANNEL = -1001
ids = itertools.count(1)


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self.members: set[tuple[int, int]] = set()
        self.gift_error: str | None = None
        self.balance = 100
        self.reject_icons = False  # как у бота, владелец которого без Telegram Premium

    async def close(self) -> None:
        pass

    async def stream_content(self, url, *a, **kw):
        """Скачивание файлов: стикер подарка (tgs = gzip Lottie), превью (webp), картинки (jpg)."""
        if url.endswith("/sticker_anim"):
            yield gzip.compress(b'{"v":"5.5.2","fr":60,"ip":0,"op":60,"w":512,"h":512,"layers":[]}')
            return
        yield _image("WEBP" if "thumb" in url else "JPEG")

    def by_type(self, cls):
        return [c for c in self.calls if isinstance(c, cls)]

    def screen(self) -> TelegramMethod:
        """Последний показанный экран (сообщение или редактирование)."""
        return next(c for c in reversed(self.calls) if type(c).__name__ in ("SendMessage", "EditMessageText"))

    def texts_to(self, chat_id: int) -> list[str]:
        return [c.text for c in self.calls if getattr(c, "chat_id", None) == chat_id and getattr(c, "text", None)]

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = type(method).__name__
        markup = getattr(method, "reply_markup", None)
        if self.reject_icons and markup is not None and "icon_custom_emoji_id='" in str(markup):
            raise TelegramBadRequest(method=method, message="Bad Request: BUTTON_ICON_INVALID")
        if isinstance(method, GetMe):
            return User(id=BOT_ID, is_bot=True, first_name="Bot", username="test_bot")
        if isinstance(method, GetChatMember):
            user = {"id": method.user_id, "is_bot": False, "first_name": "U"}
            if method.user_id == BOT_ID:
                data = {"status": "administrator", "user": user, "can_be_edited": False, "is_anonymous": False,
                        "can_manage_chat": True, "can_delete_messages": True, "can_manage_video_chats": True,
                        "can_restrict_members": True, "can_promote_members": False, "can_change_info": True,
                        "can_invite_users": True, "can_post_stories": False, "can_edit_stories": False,
                        "can_delete_stories": False, "can_send_welcome_messages": False}
            elif (method.chat_id, method.user_id) in self.members:
                data = {"status": "member", "user": user}
            else:
                data = {"status": "left", "user": user}
            return _member(data)
        if isinstance(method, GetChatMemberCount):
            return 1234
        if isinstance(method, GetMyStarBalance):
            return StarAmount(amount=self.balance)
        if isinstance(method, SendGift):
            if self.gift_error:
                raise TelegramBadRequest(method=method, message=self.gift_error)
            return True
        if isinstance(method, GetAvailableGifts):
            thumb = {"file_id": "thumb", "file_unique_id": "t", "width": 64, "height": 64}
            sticker = {"file_id": "sticker_anim", "file_unique_id": "u", "type": "regular", "width": 512,
                       "height": 512, "is_animated": True, "is_video": False, "thumbnail": thumb}
            return Gifts(gifts=[{"id": "g_bear", "star_count": 15, "sticker": {**sticker, "emoji": "🧸"}},
                                {"id": "g_rose", "star_count": 25, "sticker": {**sticker, "emoji": "🌹"},
                                 "remaining_count": 10},
                                {"id": "g_premium", "star_count": 50, "is_premium": True,
                                 "sticker": {**sticker, "emoji": "💎"}},
                                {"id": "g_soldout", "star_count": 99, "remaining_count": 0,
                                 "sticker": {**sticker, "emoji": "🏆"}}])
        if isinstance(method, GetChat):
            return ChatFullInfo(id=-1002, type="channel", title="Private Chan", accent_color_id=0,
                                max_reaction_count=0, accepted_gift_types={
                                    "unlimited_gifts": True, "limited_gifts": True, "unique_gifts": True,
                                    "premium_subscription": True, "gifts_from_channels": True})
        if isinstance(method, CreateChatInviteLink):
            return ChatInviteLink(invite_link="https://t.me/+secret", creator=User(id=BOT_ID, is_bot=True,
                                  first_name="Bot"), creates_join_request=False, is_primary=False, is_revoked=False)
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="u", file_path=f"files/{method.file_id}")
        if isinstance(method, SendPhoto):
            n = next(ids)
            return _msg(method.chat_id, None, from_bot=True, photo=[
                {"file_id": f"photo{n}", "file_unique_id": f"p{n}", "width": 1280, "height": 720}])
        if name == "CreateInvoiceLink":
            return f"https://t.me/$invoice_{method.payload}"
        if isinstance(method, CopyMessage):
            return MessageId(message_id=next(ids))
        if name in ("SendMessage", "EditMessageText", "SendDocument", "SendInvoice"):
            chat_id = getattr(method, "chat_id", None) or ADMIN
            return _msg(chat_id, getattr(method, "text", None) or "doc", from_bot=True)
        return True


def _image(fmt: str) -> bytes:
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA" if fmt == "WEBP" else "RGB", (320, 180), (90, 60, 160)).save(buf, fmt)
    return buf.getvalue()


def _member(data):
    from pydantic import TypeAdapter
    from aiogram.types import ResultChatMemberUnion
    return TypeAdapter(ResultChatMemberUnion).validate_python(data)


def _msg(chat_id, text, from_bot=False, **extra):
    from aiogram.types import Message
    uid = BOT_ID if from_bot else chat_id
    data = {
        "message_id": next(ids), "date": int(time.time()),
        "chat": {"id": chat_id, "type": "private", "first_name": "U"},
        "from": {"id": uid, "is_bot": from_bot, "first_name": f"User{uid}"},
        **extra,
    }
    if text is not None:
        data["text"] = text
    return Message.model_validate(data)


def msg_update(uid: int, text: str, **extra) -> dict:
    return {"update_id": next(ids), "message": {
        "message_id": next(ids), "date": int(time.time()),
        "chat": {"id": uid, "type": "private", "first_name": f"User{uid}"},
        "from": {"id": uid, "is_bot": False, "first_name": f"User{uid}", "username": f"user{uid}"},
        "text": text, **extra,
    }}


def inline_update(uid: int, query: str) -> dict:
    return {"update_id": next(ids), "inline_query": {
        "id": str(next(ids)), "query": query, "offset": "",
        "from": {"id": uid, "is_bot": False, "first_name": f"User{uid}"},
    }}


def init_data(uid: int, token: str, auth_date: int | None = None) -> str:
    """Подписанный initData, как его формирует Telegram для мини-аппа."""
    from urllib.parse import urlencode
    fields = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAH",
              "user": json.dumps({"id": uid, "first_name": f"User{uid}", "username": f"user{uid}"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def payment_update(uid: int, payload: str, amount: int, charge: str) -> dict:
    return {"update_id": next(ids), "message": {
        "message_id": next(ids), "date": int(time.time()),
        "chat": {"id": uid, "type": "private", "first_name": f"User{uid}"},
        "from": {"id": uid, "is_bot": False, "first_name": f"User{uid}"},
        "successful_payment": {"currency": "XTR", "total_amount": amount, "invoice_payload": payload,
                               "telegram_payment_charge_id": charge, "provider_payment_charge_id": ""}}}


def cb_update(uid: int, data: str) -> dict:
    return {"update_id": next(ids), "callback_query": {
        "id": str(next(ids)), "chat_instance": "x", "data": data,
        "from": {"id": uid, "is_bot": False, "first_name": f"User{uid}", "username": f"user{uid}"},
        "message": {"message_id": next(ids), "date": int(time.time()), "text": "panel",
                    "chat": {"id": uid, "type": "private", "first_name": "U"},
                    "from": {"id": BOT_ID, "is_bot": True, "first_name": "Bot"}},
    }}


passed = 0


def check(cond: bool, label: str) -> None:
    global passed
    if not cond:
        raise AssertionError(label)
    passed += 1
    print(f"  ✓ {label}")


async def check_migration(tmp: str) -> None:
    import sqlite3

    from bot.database import Database
    path = os.path.join(tmp, "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT NOT NULL DEFAULT '', "
                "referrer_id INTEGER, ref_credited INTEGER NOT NULL DEFAULT 0, ref_count INTEGER NOT NULL DEFAULT 0, "
                "bonus_refs INTEGER NOT NULL DEFAULT 0, rewards_claimed INTEGER NOT NULL DEFAULT 0, "
                "is_banned INTEGER NOT NULL DEFAULT 0, is_blocked INTEGER NOT NULL DEFAULT 0, "
                "created_at INTEGER NOT NULL, verified_at INTEGER, last_seen INTEGER NOT NULL)")
    con.execute("INSERT INTO users (user_id, created_at, last_seen) VALUES (1, 0, 0)")
    con.commit()
    con.close()
    db = Database(path)
    await db.connect()
    user = await db.get_user(1)
    await db.close()
    check(user is not None and user["ad_link_id"] is None, "миграция старой базы: колонка ad_link_id добавлена")


async def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    middlewares.ThrottlingMiddleware.__init__.__defaults__ = (0.0,)
    tmp = tempfile.mkdtemp()
    print("Миграция")
    await check_migration(tmp)
    config = Config("0:fake", frozenset({ADMIN}), os.path.join(tmp, "t.db"), ZoneInfo("Europe/Moscow"),
                    webapp_url="https://app.test")
    session = FakeSession()
    bot = Bot(f"{BOT_ID}:fake", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    dp, db, admins = await build(config, bot)
    try:
        await scenario(dp, db, bot, session)
    finally:
        await db.close()


async def scenario(dp, db, bot, session) -> None:
    settings = dp["settings"]
    await settings.set("sub_cache_ttl", 0)
    feed = lambda upd: dp.feed_raw_update(bot, upd)  # noqa: E731

    await db.add_channel(CHANNEL, "Test Channel", "testchan", None)

    print("Пользовательский сценарий")
    await feed(msg_update(100, "/start"))
    check("Я подписался" in str(session.screen().reply_markup), "без подписки — экран подписки")
    await feed(cb_update(100, U(a="invite").pack()))
    check("Я подписался" in str(session.screen().reply_markup), "кнопки меню закрыты до подписки")
    session.members.add((CHANNEL, 100))
    await feed(cb_update(100, U(a="check").pack()))
    check((await db.get_user(100))["verified_at"] is not None, "подписка подтверждена")
    check("Мишка за друзей" in session.texts_to(100)[-1], "после подписки — главное меню")

    for friend in range(101, 106):
        await feed(msg_update(friend, "/start r100"))
    a = await db.get_user(100)
    check(a["ref_count"] == 0, "реферал НЕ засчитан до подписки друзей")
    check((await db.referral_counts(100)) == (0, 5), "5 друзей в ожидании")

    for friend in range(101, 106):
        session.members.add((CHANNEL, friend))
        await feed(cb_update(friend, U(a="check").pack()))
    a = await db.get_user(100)
    check(a["ref_count"] == 5, "после подписки засчитано 5 рефералов")
    check(any("Забирай своего мишку" in t for t in session.texts_to(100)), "пригласившему пришло уведомление")

    await feed(cb_update(101, U(a="check").pack()))
    check((await db.get_user(100))["ref_count"] == 5, "повторная проверка не засчитывает дважды")

    await feed(cb_update(100, U(a="menu").pack()))
    check("Забрать мишку" in str(session.screen().reply_markup), "в меню кнопка «Забрать мишку»")
    await feed(cb_update(100, U(a="claim").pack()))
    gifts = session.by_type(SendGift)
    check(len(gifts) == 1 and gifts[0].user_id == 100, "подарок отправлен автоматически")
    check(gifts[0].text == settings.get("gift_text"), "реферальный подарок — с подписью из настроек")
    await feed(cb_update(100, U(a="claim").pack()))
    check(len(session.by_type(SendGift)) == 1, "повторно забрать нельзя")

    await feed(msg_update(200, "/start r200"))
    check((await db.get_user(200))["referrer_id"] is None, "сам себя пригласить нельзя")
    await feed(msg_update(201, "/start r99999"))
    check((await db.get_user(201))["referrer_id"] is None, "несуществующий реферер игнорируется")

    for uid in ("friends", "top", "rules", "invite"):
        await feed(cb_update(100, U(a=uid).pack()))
    check("t.me/test_bot?start=r100" in session.texts_to(100)[-1], "ссылка-приглашение корректна")
    await feed(msg_update(100, "привет"))
    check("Мишка за друзей" in session.texts_to(100)[-1], "любой текст — показываем меню")

    session.members.discard((CHANNEL, 100))
    await feed(cb_update(100, U(a="top").pack()))
    check("Я подписался" in str(session.screen().reply_markup), "отписался — снова экран подписки")
    session.members.add((CHANNEL, 100))

    print("Ручная выдача и ошибка звёзд")
    await settings.set("reward_mode", "manual")
    await settings.set("ref_goal", 1)
    await feed(msg_update(300, "/start r102"))
    session.members.add((CHANNEL, 300))
    await feed(cb_update(300, U(a="check").pack()))
    await feed(cb_update(102, U(a="claim").pack()))
    pending = await db.list_claims("pending", 10, 0)
    check(len(pending) == 1 and pending[0]["user_id"] == 102, "ручной режим — создана заявка")
    check(any("Заявка #" in t for t in session.texts_to(ADMIN)), "админ получил уведомление о заявке")
    await feed(cb_update(ADMIN, A(s="cl", a="send", id=pending[0]["id"]).pack()))
    check((await db.get_claim(pending[0]["id"]))["status"] == "sent", "админ отправил подарок из заявки")

    await settings.set("reward_mode", "auto")
    session.gift_error = "BALANCE_TOO_LOW"
    await feed(msg_update(301, "/start r103"))
    session.members.add((CHANNEL, 301))
    await feed(cb_update(301, U(a="check").pack()))
    await feed(cb_update(103, U(a="claim").pack()))
    claim = (await db.list_claims("pending", 10, 0))[0]
    check(claim["user_id"] == 103 and "BALANCE" in claim["error"], "нет звёзд — заявка ушла в очередь")
    session.gift_error = None
    await feed(cb_update(ADMIN, A(s="cl", a="all_ok").pack()))
    check(await db.count_claims("pending") == 0, "«Отправить все» разобрал очередь")

    print("Админ-панель: все экраны")
    screens = [
        A(s="home"), A(s="home", a="refresh"), A(s="stats"), A(s="stats", a="export"),
        A(s="ch"), A(s="ch", a="card", id=CHANNEL), A(s="ch", a="toggle", id=CHANNEL),
        A(s="ch", a="toggle", id=CHANNEL), A(s="ch", a="up", id=CHANNEL), A(s="ch", a="del", id=CHANNEL),
        A(s="cl"), A(s="cl", v="sent"), A(s="cl", v="rejected"), A(s="cl", a="card", id=1),
        A(s="us"), A(s="us", a="list", v="top"), A(s="us", a="list", v="new"), A(s="us", a="list", v="banned"),
        A(s="us", a="card", id=100), A(s="us", a="refs", id=100), A(s="us", a="bonus", id=100, p=1),
        A(s="us", a="rreset", id=100), A(s="us", a="gift", id=100),
        A(s="bc"), A(s="st"), A(s="st", a="goal"), A(s="st", a="setgoal", id=7), A(s="st", a="t", v="repeatable"),
        A(s="st", a="mode"), A(s="st", a="ttl"), A(s="bal"), A(s="bal", v="gifts"),
        A(s="tx"), *[A(s="tx", a="card", v=k) for k in ("text_menu", "text_subscribe", "text_rules")],
        A(s="ad"),
    ]
    for cb in screens:
        await feed(cb_update(ADMIN, cb.pack()))
    check(True, f"открыто {len(screens)} экранов без ошибок")
    check(settings.goal == 7 and settings.repeatable, "настройки сохраняются")
    check((await db.get_user(100))["bonus_refs"] == 1, "бонусный реферал начислен")
    check(len(session.by_type(SendDocument)) == 1, "CSV-выгрузка отправлена")

    await feed(cb_update(ADMIN, A(s="st", a="gifts").pack()))
    await feed(cb_update(ADMIN, A(s="st", a="gift", v="g_rose").pack()))
    check(settings.get("gift_id") == "g_rose" and settings.get("gift_price") == "25", "выбор подарка из списка")

    await feed(cb_update(ADMIN, A(s="ch", a="add").pack()))
    await feed(msg_update(ADMIN, "", chat_shared={"request_id": 1, "chat_id": -1002}))
    ch = await db.get_channel(-1002)
    check(ch is not None and ch["invite_link"] == "https://t.me/+secret",
          "канал добавлен через кнопку выбора, создана инвайт-ссылка")
    await feed(msg_update(301, "/start"))
    check("Private Chan" in str(session.screen().reply_markup), "новый канал сразу требуется у пользователей")
    await feed({"update_id": next(ids), "chat_join_request": {
        "chat": {"id": -1002, "type": "channel", "title": "Private Chan"},
        "from": {"id": 301, "is_bot": False, "first_name": "U"}, "user_chat_id": 301, "date": int(time.time())}})
    await feed(cb_update(301, U(a="check").pack()))
    check("Мишка за друзей" in session.texts_to(301)[-1], "заявка на вступление засчитывается как подписка")

    print("Пополнение баланса")
    await feed(cb_update(ADMIN, A(s="home").pack()))
    check("⭐ Баланс: 100 — пополнить" in str(session.screen().reply_markup), "кнопка баланса на дашборде")
    await feed(cb_update(ADMIN, A(s="bal").pack()))
    check("Хватит на" in session.screen().text and "Своя сумма" in str(session.screen().reply_markup),
          "экран баланса с готовыми суммами и своей")
    await feed(cb_update(ADMIN, A(s="bal", a="pay", id=250).pack()))
    inv = session.by_type(SendInvoice)[-1]
    check(inv.currency == "XTR" and inv.prices[0].amount == 250, "готовая сумма — счёт на 250 ⭐")
    await feed(cb_update(ADMIN, A(s="bal", a="custom").pack()))
    await feed(msg_update(ADMIN, "0"))
    await feed(msg_update(ADMIN, "20000"))
    check(session.by_type(SendInvoice)[-1] is inv, "некорректная сумма отклонена")
    await feed(msg_update(ADMIN, "1 337"))
    inv = session.by_type(SendInvoice)[-1]
    check(inv.prices[0].amount == 1337 and inv.payload == "topup:1337", "своя сумма — счёт на 1337 ⭐")
    await feed(msg_update(ADMIN, None, successful_payment={
        "currency": "XTR", "total_amount": 1337, "invoice_payload": "topup:1337",
        "telegram_payment_charge_id": "ch1", "provider_payment_charge_id": ""}))
    check("пополнен на <b>1337</b>" in session.texts_to(ADMIN)[-1] and "К балансу" in str(session.screen().reply_markup),
          "после оплаты — подтверждение с балансом")

    await feed(cb_update(ADMIN, A(s="us").pack()))
    await feed(msg_update(ADMIN, "@user101"))
    check("<code>101</code>" in session.texts_to(ADMIN)[-1], "поиск пользователя по @username")

    await feed(cb_update(ADMIN, A(s="tx", a="edit", v="text_menu").pack()))
    await feed(msg_update(ADMIN, "Привет {name}! {progress}", entities=[{"type": "bold", "offset": 0, "length": 6}]))
    check(settings.get("text_menu").startswith("<b>Привет</b>"), "редактор текстов сохраняет форматирование")

    await feed(cb_update(ADMIN, A(s="us", a="ban", id=105).pack()))
    before = len(session.calls)
    await feed(msg_update(105, "/start"))
    check((await db.get_user(105))["is_banned"] == 1 and len(session.calls) == before + 1,
          "забаненный получает отказ")

    await feed(cb_update(ADMIN, A(s="ad", a="add").pack()))
    await feed(msg_update(ADMIN, "/start"))
    check(await dp.fsm.get_context(bot, ADMIN, ADMIN).get_state() is None, "/start прерывает ввод админа")

    print("Подарки на каналы")
    await feed(cb_update(ADMIN, A(s="home").pack()))
    check("Подарок на канал" in str(session.screen().reply_markup), "кнопка на дашборде")
    await feed(cb_update(ADMIN, A(s="cgift").pack()))
    check("Private Chan" in str(session.screen().reply_markup), "каналы из подписки — в списке получателей")
    await feed(cb_update(ADMIN, A(s="cgift", a="add").pack()))
    await feed(msg_update(ADMIN, "@private_chan"))
    check("🌹 25⭐" in str(session.screen().reply_markup) and "💎" not in str(session.screen().reply_markup),
          "канал по @username — экран выбора подарка без premium")
    check(settings.get("cgift_recent").startswith("[[-1002"), "канал запомнен в недавних")
    await feed(cb_update(ADMIN, A(s="cgift", a="pick", id=-1002, p=3, v="g_rose").pack()))
    check("Итого: <b>75</b>" in session.screen().text, "подтверждение: 3 × 25 ⭐")
    await feed(cb_update(ADMIN, A(s="cgift", a="text", id=-1002, p=3, v="g_rose").pack()))
    await feed(msg_update(ADMIN, "С любовью <3"))
    check("Количество: <b>3</b>" in session.screen().text and "&lt;3" in session.screen().text,
          "после подписи — снова подтверждение")
    before = len(session.by_type(SendGift))
    await feed(cb_update(ADMIN, A(s="cgift", a="send", id=-1002, p=3, v="g_rose").pack()))
    sent = session.by_type(SendGift)[before:]
    check(len(sent) == 3 and all(g.chat_id == -1002 and g.user_id is None and g.gift_id == "g_rose"
                                 and g.text == "С любовью &lt;3" for g in sent), "3 подарка ушли на канал с подписью")
    check("Отправлено: <b>3</b>" in session.screen().text, "итог отправки")
    session.gift_error = "BALANCE_TOO_LOW"
    await feed(cb_update(ADMIN, A(s="cgift", a="send", id=-1002, p=2, v="g_bear").pack()))
    session.gift_error = None
    check("Отправлено: <b>0</b>" in session.screen().text and "BALANCE_TOO_LOW" in session.screen().text,
          "ошибка отправки показана админу")

    print("НФТ подарки")
    session.members.add((-1002, 100))  # канал добавлен выше — пользователь подписывается и на него
    await feed(cb_update(100, U(a="menu").pack()))
    check("НФТ подарки" not in str(session.screen().reply_markup), "без подарков кнопки в меню нет")
    await feed(cb_update(ADMIN, A(s="home").pack()))
    check("НФТ подарки" in str(session.screen().reply_markup), "раздел на дашборде")
    await feed(cb_update(ADMIN, A(s="nft").pack()))
    check("не подключён" in session.screen().text and "Запасная ссылка: —" in session.screen().text,
          "юзербот не настроен, запасной ссылки нет")
    await feed(cb_update(ADMIN, A(s="nft", a="add").pack()))
    await feed(msg_update(ADMIN, "https://t.me/nft/PlushPepe-1234"))
    nft = (await db.nft_gifts())[-1]
    check(nft["title"] == "Plush Pepe #1234" and nft["link"] == "https://t.me/nft/PlushPepe-1234",
          "подарок по ссылке: название из ссылки")
    await feed(cb_update(ADMIN, A(s="nft", a="edit", id=nft["id"], v="price").pack()))
    await feed(msg_update(ADMIN, "1 500 ⭐"))
    await feed(cb_update(ADMIN, A(s="nft", a="edit", id=nft["id"], v="link").pack()))
    await feed(msg_update(ADMIN, "не ссылка"))
    check("не похоже на ссылку" in session.texts_to(ADMIN)[-1], "неверная ссылка отклонена")
    await feed(cb_update(ADMIN, A(s="nft", a="contact").pack()))
    await feed(msg_update(ADMIN, "@nft_admin"))
    check(settings.get("nft_contact_url") == "https://t.me/nft_admin", "ссылка для связи из @username")
    await feed(cb_update(ADMIN, A(s="nft", a="add").pack()))
    await feed(msg_update(ADMIN, "Durov's Cap"))
    cap = (await db.nft_gifts())[-1]

    await feed(cb_update(100, U(a="menu").pack()))
    check("НФТ подарки" in str(session.screen().reply_markup), "в меню появилась кнопка «НФТ подарки»")
    await feed(cb_update(100, U(a="nft").pack()))
    markup = str(session.screen().reply_markup)
    check("Plush Pepe #1234 · 1 500 ⭐" in markup and "Durov's Cap" in markup, "список подарков с ценами")
    check("💎" not in markup, "на кнопках подарков нет 💎")

    await feed(cb_update(ADMIN, A(s="nft", a="edit", id=nft["id"], v="emoji").pack()))
    await feed(msg_update(ADMIN, "🔥"))
    check("не премиум-эмодзи" in session.texts_to(ADMIN)[-1], "обычный эмодзи не подходит")
    await feed(msg_update(ADMIN, "🐸", entities=[{"type": "custom_emoji", "offset": 0, "length": 2,
                                                   "custom_emoji_id": "5368324170671202286"}]))
    check((await db.get_nft_gift(nft["id"]))["emoji_id"] == "5368324170671202286"
          and "Премиум-эмодзи на кнопке: задан" in session.screen().text, "премиум-эмодзи сохранён")
    await feed(cb_update(100, U(a="nft").pack()))
    first = session.screen().reply_markup.inline_keyboard[0][0]
    check(first.icon_custom_emoji_id == "5368324170671202286" and first.text == "Plush Pepe #1234 · 1 500 ⭐",
          "кнопка подарка с премиум-эмодзи")
    def custom(offset, emoji_id):
        return {"type": "custom_emoji", "offset": offset, "length": 2, "custom_emoji_id": emoji_id}

    await feed(cb_update(ADMIN, A(s="nft", a="add").pack()))
    await feed(msg_update(ADMIN, "🐸 Жаба Кепка", entities=[custom(0, "111")]))
    frog = (await db.nft_gifts())[-1]
    check(frog["title"] == "Жаба Кепка" and frog["emoji_id"] == "111"
          and any("стал иконкой" in t for t in session.texts_to(ADMIN)[-2:]),
          "добавление с премиум-эмодзи: эмодзи — иконка, в названии его нет")
    await feed(cb_update(ADMIN, A(s="nft", a="edit", id=frog["id"], v="title").pack()))
    await feed(msg_update(ADMIN, "Жаба 🎩 Кепка 🐸", entities=[custom(5, "222"), custom(14, "333")]))
    frog = await db.get_nft_gift(frog["id"])
    check(frog["title"] == "Жаба Кепка" and frog["emoji_id"] == "222", "переименование: первый премиум-эмодзи — иконка")
    await feed(cb_update(ADMIN, A(s="nft", a="edit", id=frog["id"], v="title").pack()))
    await feed(msg_update(ADMIN, "Жаба в кепке"))
    check((await db.get_nft_gift(frog["id"]))["emoji_id"] == "222", "название без эмодзи — иконка прежняя")
    await feed(cb_update(ADMIN, A(s="nft", a="add").pack()))
    await feed(msg_update(ADMIN, "🐸", entities=[custom(0, "444")]))
    check("нужно название" in session.texts_to(ADMIN)[-1], "только эмодзи без названия — просим название")
    await feed(msg_update(ADMIN, "🐸 https://t.me/nft/SwissWatch-55", entities=[custom(0, "555")]))
    watch = (await db.nft_gifts())[-1]
    check(watch["title"] == "Swiss Watch #55" and watch["emoji_id"] == "555"
          and watch["link"] == "https://t.me/nft/SwissWatch-55", "ссылка с премиум-эмодзи тоже работает")
    for g in (frog, watch):
        await db.delete_nft_gift(g["id"])

    session.reject_icons = True
    await feed(cb_update(100, U(a="nft").pack()))
    session.reject_icons = False
    first = session.screen().reply_markup.inline_keyboard[0][0]
    check(first.icon_custom_emoji_id is None and "Plush Pepe" in first.text,
          "Telegram не принял иконки — список без них, а не ошибка")
    await feed(cb_update(100, U(a="nftg", p=nft["id"]).pack()))
    card = session.screen()
    urls = [b.url for row in card.reply_markup.inline_keyboard for b in row if b.url]
    check("Напиши админу" in card.text and "«Plush Pepe #1234»" in card.text, "карточка с инструкцией")
    check(urls[0] == "https://t.me/nft_admin?text=" + "%D0%9F%D1%80%D0%B8%D0%B2%D0%B5%D1%82%2C%20%D0%BE%D0%BF%D0%BB"
          "%D0%B0%D1%82%D0%B0%20%D0%B7%D0%B0%20Plush%20Pepe%20%231234",
          "без ссылки на чат — запасная ссылка с тем же готовым текстом")
    check(card.link_preview_options.url == nft["link"], "превью НФТ подарка по ссылке")
    check((await db.get_nft_gift(nft["id"]))["views"] == 1, "просмотр засчитан")

    await feed(cb_update(ADMIN, A(s="nft", a="edit", id=cap["id"], v="photo").pack()))
    await feed(msg_update(ADMIN, "", photo=[{"file_id": "cap_photo", "file_unique_id": "c", "width": 512,
                                             "height": 512}]))
    await feed(cb_update(100, U(a="nftg", p=cap["id"]).pack()))
    photo = session.by_type(SendPhoto)[-1]
    check(photo.chat_id == 100 and photo.photo == "cap_photo" and "Durov" in photo.caption,
          "карточка с картинкой — фото с подписью")
    await feed(cb_update(ADMIN, A(s="nft", a="preview", id=cap["id"]).pack()))
    check(session.by_type(SendPhoto)[-1].chat_id == ADMIN, "админ видит карточку как пользователь")
    await feed(cb_update(ADMIN, A(s="nft", a="toggle", id=cap["id"]).pack()))
    await feed(cb_update(100, U(a="nftg", p=cap["id"]).pack()))
    check("Durov" not in str(session.screen().reply_markup), "скрытый подарок недоступен пользователю")
    await feed(cb_update(ADMIN, A(s="nft", a="del_ok", id=cap["id"]).pack()))
    check(await db.get_nft_gift(cap["id"]) is None, "подарок удалён")

    print("Юзербот")
    from types import SimpleNamespace
    from telethon.tl import types as tl
    from bot.services.userbot import UbStatus
    userbot, replies = dp["userbot"], []

    def ub_event(uid, stars=0, **flags):
        sender = tl.User(id=uid, first_name="Покупатель", **flags)

        async def get_sender():
            return sender

        async def respond(text, **kw):
            replies.append((uid, text, kw))
        return SimpleNamespace(get_sender=get_sender, respond=respond,
                               message=SimpleNamespace(paid_message_stars=stars))

    await feed(cb_update(ADMIN, A(s="ub").pack()))
    check("не настроен" in session.screen().text and "my.telegram.org" in session.screen().text,
          "без api_id — инструкция по подключению")
    await feed(cb_update(ADMIN, A(s="ub", a="reply").pack()))
    await feed(msg_update(ADMIN, "{name}, админ скоро отправит вам подарок!"))
    check("Иван, админ скоро" in session.screen().text, "автоответ сохранён, предпросмотр с именем")

    await userbot.on_message(ub_event(800, stars=100))
    check(replies[-1][:2] == (800, "Покупатель, админ скоро отправит вам подарок!")
          and replies[-1][2]["parse_mode"] == "html", "на платное сообщение — автоответ")
    await userbot.on_message(ub_event(800, stars=100))
    check(len(replies) == 1, "альбом/спам в ту же секунду — один ответ")
    userbot._last.clear()
    await userbot.on_message(ub_event(800, stars=100))
    check(len(replies) == 2, "повторное сообщение — снова тот же ответ")
    await userbot.on_message(ub_event(801, contact=True))
    await userbot.on_message(ub_event(802, bot=True))
    check(len(replies) == 2, "контактам и ботам не отвечает")
    await feed(cb_update(ADMIN, A(s="ub", a="t", v="userbot_paid_only").pack()))
    await userbot.on_message(ub_event(803))
    await userbot.on_message(ub_event(804, stars=50))
    check([r[0] for r in replies[2:]] == [804], "режим «только платные»")
    stats = await db.userbot_stats()
    check(stats["messages"] == 4 and stats["stars"] == 350 and stats["users"] == 2, "статистика сообщений и звёзд")
    await feed(cb_update(ADMIN, A(s="ub", a="t", v="userbot_enabled").pack()))
    await userbot.on_message(ub_event(805, stars=50))
    check(len(replies) == 3, "автоответ выключен — молчит")

    print("  — ссылки на чат для НФТ подарков")
    from telethon.errors import RPCError

    class FakeTelethon:
        def __init__(self):
            self.requests, self.links, self.premium = [], {}, True

        def is_connected(self):
            return True

        async def disconnect(self):
            pass

        def _link(self, slug, link, views=0):
            return tl.BusinessChatLink(link=f"https://t.me/m/{slug}", message=link.message, views=views,
                                       title=link.title)

        async def __call__(self, req):
            self.requests.append(req)
            name = type(req).__name__
            if name == "CreateBusinessChatLinkRequest":
                if not self.premium:
                    raise RPCError(req, "PREMIUM_ACCOUNT_REQUIRED", 403)
                slug = f"L{len(self.requests)}"
                self.links[slug] = req.link
                return self._link(slug, req.link)
            if name == "EditBusinessChatLinkRequest":
                if req.slug not in self.links:
                    raise RPCError(req, "CHATLINK_SLUG_EMPTY", 400)
                self.links[req.slug] = req.link
                return self._link(req.slug, req.link)
            if name == "DeleteBusinessChatLinkRequest":
                self.links.pop(req.slug, None)
                return True
            if name == "GetBusinessChatLinksRequest":
                return tl.account.BusinessChatLinks(
                    links=[self._link(slug, link, views=7) for slug, link in self.links.items()], chats=[], users=[])
            if name == "GetGlobalPrivacySettingsRequest":
                return tl.GlobalPrivacySettings(noncontact_peers_paid_stars=100)
            raise AssertionError(name)

    fake = FakeTelethon()
    userbot.client, userbot.me = fake, tl.User(id=555, first_name="NFT", username="GiveNFTRobot")
    userbot.status = UbStatus.ONLINE
    await userbot.sync_nft_links(only_missing=True)  # как при подключении юзербота
    pepe = await db.get_nft_gift(nft["id"])
    check(pepe["chat_link"].startswith("https://t.me/m/")
          and fake.links[pepe["chat_link_slug"]].message == "Привет, оплата за Plush Pepe #1234"
          and fake.links[pepe["chat_link_slug"]].title == "💎 Plush Pepe #1234",
          "при подключении юзербота у подарка появилась ссылка на чат с готовым сообщением")
    await feed(cb_update(100, U(a="nftg", p=nft["id"]).pack()))
    urls = [b.url for row in session.screen().reply_markup.inline_keyboard for b in row if b.url]
    check(urls[0] == pepe["chat_link"], "«Написать админу» ведёт на ссылку подарка")
    await feed(cb_update(ADMIN, A(s="nft", a="card", id=nft["id"]).pack()))
    check("переходов: <b>7</b>" in session.screen().text, "в карточке — ссылка и число переходов")
    await feed(cb_update(ADMIN, A(s="nft", a="add").pack()))
    await feed(msg_update(ADMIN, "Swiss Watch"))
    watch = (await db.nft_gifts())[-1]
    check(watch["chat_link"] and "Ссылка на чат создана" in "".join(session.texts_to(ADMIN)[-2:]),
          "новый подарок сразу получает ссылку")

    await feed(cb_update(ADMIN, A(s="nft", a="edit", id=watch["id"], v="title").pack()))
    before = len(fake.requests)
    await feed(msg_update(ADMIN, "Swiss Watch #7"))
    check("EditBusinessChatLinkRequest" in [type(r).__name__ for r in fake.requests[before:]]
          and fake.links[watch["chat_link_slug"]].message == "Привет, оплата за Swiss Watch #7",
          "переименование подарка обновляет ссылку")
    await feed(cb_update(ADMIN, A(s="nft", a="msg").pack()))
    await feed(msg_update(ADMIN, "Здравствуйте! Хочу оплатить {gift}"))
    check(all(link.message.startswith("Здравствуйте! Хочу оплатить ") for link in fake.links.values())
          and len(fake.links) == 2, "новый текст сообщения — обновлены все ссылки")

    fake.links.pop(watch["chat_link_slug"])  # ссылку удалили в Telegram вручную
    await feed(cb_update(ADMIN, A(s="nft", a="sync1", id=watch["id"]).pack()))
    watch = await db.get_nft_gift(watch["id"])
    check(watch["chat_link_slug"] in fake.links, "удалённая вручную ссылка пересоздаётся")
    await feed(cb_update(ADMIN, A(s="nft", a="del_ok", id=watch["id"]).pack()))
    check(watch["chat_link_slug"] not in fake.links, "удаление подарка удаляет его ссылку")

    fake.premium = False
    await feed(cb_update(ADMIN, A(s="nft", a="add").pack()))
    await feed(msg_update(ADMIN, "Lol Pop"))
    pop = (await db.nft_gifts())[-1]
    await feed(cb_update(ADMIN, A(s="nft", a="card", id=pop["id"]).pack()))
    check(pop["chat_link"] is None and "нужен Telegram Premium" in session.screen().text,
          "без Premium — понятная ошибка в карточке")
    await feed(cb_update(100, U(a="nftg", p=pop["id"]).pack()))
    urls = [b.url for row in session.screen().reply_markup.inline_keyboard for b in row if b.url]
    check(urls[0].startswith("https://t.me/nft_admin?text="), "без ссылки на чат — запасная ссылка")
    await feed(cb_update(ADMIN, A(s="ub").pack()))
    check("@GiveNFTRobot" in session.screen().text and "<b>100</b> ⭐" in session.screen().text,
          "экран юзербота: аккаунт и цена сообщения")

    await feed(cb_update(ADMIN, A(s="ub", a="reset").pack()))
    await feed(cb_update(ADMIN, A(s="ub", a="t", v="userbot_enabled").pack()))
    await feed(cb_update(ADMIN, A(s="ub", a="reconnect").pack()))
    check(settings.get("userbot_reply") == "🎁 Админ скоро отправит вам подарок!"
          and "Всего: <b>4</b>" in session.screen().text, "сброс текста и переподключение без настроек")

    print("Магазин подарков")
    from bot.callbacks import Shop, ShopBuy

    async def shop_checkout(payload, amount):
        await feed({"update_id": next(ids), "pre_checkout_query": {
            "id": str(next(ids)), "currency": "XTR", "total_amount": amount, "invoice_payload": payload,
            "from": {"id": 100, "is_bot": False, "first_name": "U"}}})
        answer = session.by_type(AnswerPreCheckoutQuery)[-1]
        return answer.ok, answer.error_message or ""

    await feed(cb_update(100, U(a="menu").pack()))
    check("Купить подарок" in str(session.screen().reply_markup), "в меню кнопка магазина")
    await feed(cb_update(100, U(a="shop").pack()))
    markup = str(session.screen().reply_markup)
    check("🧸 мишка - 15 ⭐" in markup and "🌹" not in markup,
          "по умолчанию — обычные подарки по цене Telegram, лимитированные скрыты")

    await feed(cb_update(ADMIN, A(s="home").pack()))
    check("Магазин подарков" in str(session.screen().reply_markup), "раздел магазина на дашборде")
    await feed(cb_update(ADMIN, A(s="shop", a="price", id=8, v="g_bear").pack()))
    check("Цена с нашим комментарием: <b>8</b>" in session.screen().text and "продаётся в минус" in session.screen().text,
          "цена ниже себестоимости — предупреждение в карточке")
    await feed(cb_update(ADMIN, A(s="shop", a="toggle", v="g_rose").pack()))
    await feed(cb_update(ADMIN, A(s="shop", a="name", v="g_rose").pack()))
    await feed(msg_update(ADMIN, "розочка"))
    await feed(cb_update(ADMIN, A(s="shop", a="pricein", v="g_rose").pack()))
    await feed(msg_update(ADMIN, "abc"))
    check("целое число" in session.texts_to(ADMIN)[-1], "неверная цена отклонена")
    await feed(msg_update(ADMIN, "30"))
    check("<b>+5</b> ⭐" in session.screen().text, "своя цена: прибыль +5 ⭐")
    await feed(cb_update(ADMIN, A(s="shop").pack()))
    check("в минус" in session.screen().text and "мишка — 8 ⭐ · −7" in str(session.screen().reply_markup),
          "общий экран: прибыль по каждому подарку и предупреждение")

    await feed(cb_update(100, U(a="shop").pack()))
    rows = session.screen().reply_markup.inline_keyboard
    check([b.text for b in rows[0]] == ["🧸 мишка - 8 ⭐", "🌹 розочка - 30 ⭐"], "кнопки по две в ряд, по цене")
    await feed(cb_update(100, Shop(g="g_bear").pack()))
    card = session.screen()
    check("С нашим комментарием · 8 ⭐" in str(card.reply_markup) and "Свой комментарий" not in str(card.reply_markup)
          and "Подарки дешевле, чем в Telegram — @test_bot" in card.text,
          "карточка: наш комментарий виден заранее, без вариантов — только он")
    await feed(cb_update(100, ShopBuy(g="g_bear", c=0).pack()))
    inv = session.by_type(SendInvoice)[-1]
    check(inv.currency == "XTR" and inv.prices[0].amount == 8 and inv.payload == "shop:g_bear:8:0"
          and inv.chat_id == 100 and "Подарки дешевле" in inv.description, "счёт на 8 ⭐ с нашим комментарием")
    check((await shop_checkout("shop:g_bear:8:0", 8))[0] and not (await shop_checkout("shop:g_bear:8:0", 15))[0]
          and (await shop_checkout("shop:g_bear:8", 8))[0], "pre_checkout: верная цена — ок, иначе отказ; старые счета работают")
    session.balance = 10
    ok, error = await shop_checkout("shop:g_bear:8:0", 8)
    session.balance = 100
    check(not ok and "временно недоступен" in error and any("не хватает звёзд" in t for t in session.texts_to(ADMIN)),
          "у бота мало звёзд — отказ до оплаты и уведомление админам")

    gifts_before = len(session.by_type(SendGift))
    await feed(payment_update(100, "shop:g_bear:8:0", 8, "shop_c1"))
    gift = session.by_type(SendGift)[-1]
    check(len(session.by_type(SendGift)) == gifts_before + 1 and gift.user_id == 100 and gift.gift_id == "g_bear"
          and gift.text == "🎁 Подарки дешевле, чем в Telegram — @test_bot", "после оплаты подарок с нашим комментарием")
    check("Подарок отправлен" in session.texts_to(100)[-1], "покупателю — сообщение с кнопкой «Купить ещё»")
    await feed(payment_update(100, "shop:g_bear:8:0", 8, "shop_c1"))
    check(len(session.by_type(SendGift)) == gifts_before + 1, "повтор того же платежа не дарит второй раз")

    session.gift_error = "BALANCE_TOO_LOW"
    await feed(payment_update(100, "shop:g_rose:30:0", 30, "shop_c2"))
    session.gift_error = None
    refund = session.by_type(RefundStarPayment)[-1]
    check(refund.telegram_payment_charge_id == "shop_c2" and "30 ⭐ уже вернулись" in session.texts_to(100)[-1],
          "не отправилось — звёзды вернулись автоматически")
    check(any("подарок не отправлен" in t for t in session.texts_to(ADMIN)), "админы узнали об ошибке")

    await feed(cb_update(ADMIN, A(s="shop", a="price", id=20, v="g_bear").pack()))
    check(not (await shop_checkout("shop:g_bear:8:0", 8))[0], "цену изменили — старый счёт не оплатить")

    print("  — свой комментарий по себестоимости")
    await feed(cb_update(ADMIN, A(s="shop", a="comment").pack()))
    await feed(msg_update(ADMIN, "Купил в {bot} <дёшево>"))
    check(settings.get("shop_comment") == "Купил в {bot} &lt;дёшево&gt;"
          and "Купил в @test_bot &lt;дёшево&gt;" in session.screen().text, "наш комментарий изменён, {bot} подставлен")
    await feed(cb_update(ADMIN, A(s="shop", a="cmadd").pack()))
    await feed(msg_update(ADMIN, "x" * 129))
    check("129/128" in session.texts_to(ADMIN)[-1], "длиннее 128 символов нельзя")
    await feed(msg_update(ADMIN, "С днём рождения! 🎉"))
    await feed(cb_update(ADMIN, A(s="shop", a="cmadd").pack()))
    await feed(msg_update(ADMIN, "Люблю тебя ❤️"))
    comments = await db.shop_comments()
    check([c["text"] for c in comments] == ["С днём рождения! 🎉", "Люблю тебя ❤️"]
          and "🗑 2. Люблю тебя" in str(session.screen().reply_markup), "варианты своего комментария добавлены")

    await feed(cb_update(100, Shop(g="g_bear").pack()))
    check("Свой комментарий · 15 ⭐" in str(session.screen().reply_markup)
          and "С нашим комментарием · 20 ⭐" in str(session.screen().reply_markup), "две цены: наша и себестоимость")
    await feed(cb_update(100, ShopBuy(g="g_bear", c=-1).pack()))
    check("Люблю тебя ❤️" in session.screen().text and "15 ⭐" in session.screen().text, "список вариантов")
    await feed(cb_update(100, ShopBuy(g="g_bear", c=comments[1]["id"]).pack()))
    inv = session.by_type(SendInvoice)[-1]
    payload = f"shop:g_bear:15:{comments[1]['id']}"
    check(inv.prices[0].amount == 15 and inv.payload == payload and "Люблю тебя" in inv.description,
          "свой комментарий — счёт по себестоимости")
    check((await shop_checkout(payload, 15))[0] and not (await shop_checkout(f"shop:g_bear:20:{comments[1]['id']}", 20))[0]
          and not (await shop_checkout("shop:g_bear:15:999", 15))[0], "pre_checkout: свой — только по себестоимости")
    await feed(payment_update(100, payload, 15, "shop_c3"))
    check(session.by_type(SendGift)[-1].text == "Люблю тебя ❤️" and "Люблю тебя" in session.texts_to(100)[-1],
          "подарок ушёл с выбранным комментарием")
    await feed(cb_update(ADMIN, A(s="shop", a="cmdel", id=comments[1]["id"]).pack()))
    check(not (await shop_checkout(payload, 15))[0], "удалённый вариант больше не оплатить")
    await feed(cb_update(ADMIN, A(s="shop").pack()))
    check("продаж <b>2</b> · выручка <b>23</b> ⭐ · прибыль <b>−7</b>" in session.screen().text
          and "возвратов 1" in session.screen().text, "статистика продаж")
    await feed(cb_update(ADMIN, A(s="shop", a="t").pack()))
    await feed(cb_update(100, U(a="menu").pack()))
    check("Купить подарок" not in str(session.screen().reply_markup)
          and not (await shop_checkout("shop:g_bear:20:0", 20))[0], "магазин закрыт — ни кнопки, ни оплаты")
    await feed(cb_update(ADMIN, A(s="shop", a="t").pack()))

    print("Рекламные ссылки")
    await feed(cb_update(ADMIN, A(s="lk").pack()))
    await feed(cb_update(ADMIN, A(s="lk", a="new").pack()))
    await feed(msg_update(ADMIN, "Канал @news, пост 25.09"))
    await feed(msg_update(ADMIN, "bad code!"))
    check(await db.get_ad_link_by_code("bad code!") is None, "невалидный код отклонён")
    await feed(msg_update(ADMIN, "promo1"))
    link = await db.get_ad_link_by_code("promo1")
    check(link is not None and link["name"] == "Канал @news, пост 25.09", "ссылка создана с названием и кодом")
    check("t.me/test_bot?start=ad_promo1" in session.texts_to(ADMIN)[-1], "карточка показывает ссылку")

    await feed(msg_update(400, "/start ad_promo1"))
    await feed(msg_update(400, "/start ad_promo1"))
    await feed(msg_update(100, "/start ad_promo1"))
    await feed(msg_update(401, "/start ad_unknown"))
    session.members.update({(CHANNEL, 400), (-1002, 400)})
    await feed(cb_update(400, U(a="check").pack()))
    st = await db.ad_link_stats(link["id"], settings.goal, 0)
    check(st["new_users"] == 1 and st["clicks"] == 3 and st["unique_clicks"] == 2 and st["returning_users"] == 1,
          f"переходы: всего {st['clicks']}, уник. {st['unique_clicks']}, новых {st['new_users']}, "
          f"вернувшихся {st['returning_users']}")
    check(st["verified"] == 1, "подписка пришедших по ссылке учтена")
    check((await db.get_user(100))["ad_link_id"] is None, "старый пользователь не переписан на рекламу")
    check((await db.get_user(401))["ad_link_id"] is None, "неизвестный код игнорируется")

    lid = link["id"]
    await feed(cb_update(ADMIN, A(s="lk", a="cost", id=lid).pack()))
    await feed(msg_update(ADMIN, "5 000 ₽"))
    check((await db.get_ad_link(lid))["cost"] == 5000, "стоимость «5 000 ₽» распознана")
    check("Подписчик: <b>5 000 ₽</b>" in session.texts_to(ADMIN)[-1], "цена подписчика посчитана")
    await feed(cb_update(ADMIN, A(s="lk", a="rename", id=lid).pack()))
    await feed(msg_update(ADMIN, "TikTok"))
    check((await db.get_ad_link(lid))["name"] == "TikTok", "переименование")
    for cb in (A(s="lk", a="card", id=lid, p=14), A(s="lk", a="card", id=lid, p=30), A(s="lk", a="csv", id=lid),
               A(s="lk", a="arch", id=lid), A(s="lk", v="arch"), A(s="lk", a="arch", id=lid),
               A(s="us", a="card", id=400), A(s="home"), A(s="stats")):
        await feed(cb_update(ADMIN, cb.pack()))
    check("Пришёл по рекламе" in session.texts_to(ADMIN)[-3], "источник виден в карточке пользователя")
    check(session.by_type(SendDocument)[-1].document.filename.startswith("users_promo1_"), "CSV по ссылке")
    await feed(cb_update(ADMIN, A(s="lk", a="new").pack()))
    await feed(msg_update(ADMIN, "Случайная"))
    await feed(cb_update(ADMIN, A(s="lk", a="rnd").pack()))
    check(await db.count_ad_links(False) == 2, "ссылка со случайным кодом")
    await feed(cb_update(ADMIN, A(s="lk", a="del", id=lid).pack()))
    await feed(cb_update(ADMIN, A(s="lk", a="del_ok", id=lid).pack()))
    check(await db.get_ad_link(lid) is None and (await db.get_user(400))["ad_link_id"] is None,
          "удаление ссылки отвязывает пользователей")

    print("Чеки на подарки")
    await settings.set("reward_mode", "auto")
    await settings.set("gift_id", "g_bear")
    gift_images = dp["gift_images"]

    await feed(inline_update(ADMIN, "3 Тестовый чек"))
    results = session.by_type(AnswerInlineQuery)[-1].results
    check([r.title.split()[0] for r in results] == ["🧸", "🌹"],
          "inline показывает все подарки (без premium и распроданных), по умолчанию — первым")
    check("по умолчанию" in results[0].title and type(results[0]).__name__ == "InlineQueryResultArticle",
          "без картинки — список текстовых чеков")
    drafts = await db.val("SELECT COUNT(*) FROM checks")
    await feed(inline_update(ADMIN, "3 Тестовый чек"))
    check(await db.val("SELECT COUNT(*) FROM checks") == drafts, "повторный запрос не плодит черновики")

    await feed(cb_update(ADMIN, A(s="ck", a="photo").pack()))
    await feed(msg_update(ADMIN, None, photo=[{"file_id": "base_photo", "file_unique_id": "b",
                                               "width": 1280, "height": 720}]))
    check(settings.get("check_photo") == "base_photo", "картинка чека загружена фото")
    await gift_images.wait()
    check(await db.val("SELECT COUNT(*) FROM gift_images WHERE base_file_id = 'base_photo'") == 2,
          "сгенерированы картинки со значком для каждого подарка")

    await feed(inline_update(ADMIN, "3"))
    results = session.by_type(AnswerInlineQuery)[-1].results
    photo_ids = {r.photo_file_id for r in results}
    check(all(type(r).__name__ == "InlineQueryResultCachedPhoto" for r in results) and len(photo_ids) == 2
          and "base_photo" not in photo_ids, "с картинкой — у каждого подарка своя картинка")
    bear = await db.get_check(int(results[0].id.removeprefix("chk:")))
    check(bear["gift_id"] == "g_bear" and bear["total"] == 3, "чек привязан к выбранному подарку")
    rose = await db.get_check(int(results[1].id.removeprefix("chk:")))

    await feed({"update_id": next(ids), "chosen_inline_result": {
        "result_id": f"chk:{bear['id']}", "from": {"id": ADMIN, "is_bot": False, "first_name": "A"},
        "query": "3", "inline_message_id": "imsg1"}})
    check((await db.get_check(bear["id"]))["is_sent"] == 1, "отправленный чек отмечен (inline feedback)")

    code = bear["code"]
    gifts_before = len(session.by_type(SendGift))
    await feed(msg_update(500, f"/start c_{code}"))
    check("по чеку" in session.texts_to(500)[-1] and (await db.get_user(500))["pending_check"] == code,
          "без подписки — экран подписки, чек ждёт")
    check(len(session.by_type(SendGift)) == gifts_before, "без подписки подарок не выдан")
    session.members.update({(CHANNEL, 500), (-1002, 500)})
    await feed(cb_update(500, U(a="check").pack()))
    gift = session.by_type(SendGift)[-1]
    check(gift.user_id == 500 and gift.gift_id == "g_bear", "после подписки чек активирован — отправлен 🧸")
    check("Чек активирован" in session.texts_to(500)[-1], "пользователь видит результат")
    check((await db.get_user(500))["source_check_id"] == bear["id"], "новый пользователь привязан к чеку")

    await feed(msg_update(500, f"/start c_{code}"))
    check("уже активировал" in session.texts_to(500)[-1] and len(session.by_type(SendGift)) == gifts_before + 1,
          "повторно активировать нельзя")

    session.members.discard((CHANNEL, 100))
    await feed(msg_update(100, f"/start c_{code}"))
    check("по чеку" in session.texts_to(100)[-1] and len(session.by_type(SendGift)) == gifts_before + 1,
          "подписка перепроверяется перед активацией (отписался — не выдали)")
    session.members.add((CHANNEL, 100))
    session.members.add((-1002, 100))

    for uid in (501, 502):
        session.members.update({(CHANNEL, uid), (-1002, uid)})
        await feed(msg_update(uid, f"/start c_{code}"))
    check((await db.get_check(bear["id"]))["used"] == 3, "3 активации из 3")
    closed = session.by_type(EditMessageCaption)
    check(not closed, "сообщение чека в чате не меняется — люди продолжают заходить в бота")
    session.members.update({(CHANNEL, 503), (-1002, 503)})
    await feed(msg_update(503, f"/start c_{code}"))
    check("уже разобрали" in session.texts_to(503)[-1] and (await db.get_check(bear["id"]))["used"] == 3,
          "лимит активаций соблюдается")

    gifts_before = len(session.by_type(SendGift))
    await feed(msg_update(506, f"/start c_{code}"))
    check("Я подписался" in str(session.screen().reply_markup) and "по чеку" not in session.texts_to(506)[-1],
          "закончившийся чек без подписки — просит подписаться, без обещания подарка")
    session.members.update({(CHANNEL, 506), (-1002, 506)})
    await feed(cb_update(506, U(a="check").pack()))
    check("уже разобрали" in session.texts_to(506)[-1] and len(session.by_type(SendGift)) == gifts_before
          and (await db.get_user(506))["verified_at"] is not None,
          "после подписки — «уже разобрали», подписка засчитана, подарок не выдан")
    await feed(msg_update(507, "/start c_nonexistent"))
    check("Я подписался" in str(session.screen().reply_markup), "несуществующий чек без подписки — тоже просит подписаться")

    await settings.set("reward_mode", "manual")
    await feed(msg_update(503, f"/start c_{rose['code']}"))
    claim = (await db.list_claims("pending", 10, 0))[0]
    check(claim["gift_id"] == "g_rose" and claim["check_id"] == rose["id"], "ручной режим: заявка с подарком чека")
    await feed(cb_update(ADMIN, A(s="cl", a="send", id=claim["id"]).pack()))
    check(session.by_type(SendGift)[-1].gift_id == "g_rose", "админ выдал именно подарок из чека 🌹")
    await settings.set("reward_mode", "auto")

    session.members.update({(CHANNEL, 504), (-1002, 504)})
    await feed(msg_update(504, "/start c_nonexistent"))
    check("не найден" in session.texts_to(504)[-1], "несуществующий чек у подписанного — «не найден»")

    for cb in (A(s="ck"), A(s="ck", a="card", id=bear["id"]), A(s="ck", a="acts", id=bear["id"]),
               A(s="ck", a="preview"), A(s="ck", a="toggle", id=rose["id"]), A(s="ck", a="toggle", id=rose["id"]),
               A(s="ck", a="del", id=rose["id"])):
        await feed(cb_update(ADMIN, cb.pack()))
    check("🧸 · 15 ⭐" in [t for t in session.texts_to(ADMIN) if "🎟 <b>Чек</b>" in t][0], "карточка чека")
    await feed(cb_update(ADMIN, A(s="ck", a="del_ok", id=rose["id"]).pack()))
    check(await db.get_check(rose["id"]) is None, "чек удалён")

    await feed(cb_update(ADMIN, A(s="ck", a="photo").pack()))
    await feed(msg_update(ADMIN, None, document={"file_id": "png_doc", "file_unique_id": "d",
                                                 "file_name": "check.png", "mime_type": "image/png",
                                                 "file_size": 2048}))
    check(settings.get("check_photo").startswith("photo"), "картинка файлом PNG перезалита как фото")
    await gift_images.wait()

    print("Баннеры подарков")
    for cb in (A(s="ck"), A(s="ck", a="banners"), A(s="ck", a="banner", v="g_rose")):
        await feed(cb_update(ADMIN, cb.pack()))
    check("Своих: <b>0</b> из 2" in [t for t in session.texts_to(ADMIN) if "Баннеры подарков</b>" in t][-1],
          "список подарков для баннеров")
    await feed(cb_update(ADMIN, A(s="ck", a="bn_up", v="g_rose").pack()))
    await feed(msg_update(ADMIN, None, photo=[{"file_id": "rose_banner", "file_unique_id": "r",
                                               "width": 1280, "height": 720}]))
    check(await db.get_gift_banner("g_rose") == "rose_banner", "свой баннер для 🌹 сохранён")

    await feed(inline_update(ADMIN, "2"))
    res = {r.title.split()[0]: r for r in session.by_type(AnswerInlineQuery)[-1].results}
    check(res["🌹"].photo_file_id == "rose_banner", "у 🌹 в inline свой баннер")
    check(res["🧸"].photo_file_id not in ("rose_banner", settings.get("check_photo")),
          "у 🧸 — общий баннер со значком")

    await feed(cb_update(ADMIN, A(s="ck", a="nophoto").pack()))
    await feed(inline_update(ADMIN, "2"))
    res = {r.title.split()[0]: r for r in session.by_type(AnswerInlineQuery)[-1].results}
    check(type(res["🌹"]).__name__ == "InlineQueryResultCachedPhoto"
          and type(res["🧸"]).__name__ == "InlineQueryResultArticle",
          "без общего баннера: 🌹 с баннером, 🧸 текстом")
    rose2 = await db.get_check(int(res["🌹"].id.removeprefix("chk:")))
    bear2 = await db.get_check(int(res["🧸"].id.removeprefix("chk:")))
    check(rose2["with_photo"] == 1 and bear2["with_photo"] == 0, "чек помнит, с картинкой ли он")

    await feed(cb_update(ADMIN, A(s="ck", a="bn_view", v="g_rose").pack()))
    check(session.by_type(SendPhoto)[-1].photo == "rose_banner", "превью чека с баннером подарка")
    await feed(cb_update(ADMIN, A(s="ck", a="bn_del", v="g_rose").pack()))
    check(await db.get_gift_banner("g_rose") is None, "свой баннер убран")

    await feed(inline_update(100, ""))
    res = session.by_type(AnswerInlineQuery)[-1].results
    check(len(res) == 1 and "start=r100" in res[0].input_message_content.message_text,
          "обычный пользователь в inline делится реф-ссылкой")

    print("Напоминания")
    import bot.services.reminders as rem_mod
    reminders = dp["reminders"]
    clock = {"offset": 0}
    real_now = rem_mod.now
    rem_mod.now = lambda: real_now() + clock["offset"]

    async def tick_at(minutes: int) -> int:
        clock["offset"] = minutes * 60 + 1
        return await reminders.tick()

    try:
        await feed(msg_update(600, "/start"))
        u = await db.get_user(600)
        check(u["remind_at"] is not None and u["remind_step"] == 0, "не прошёл подписку — напоминание запланировано")
        check(await tick_at(0) == 0, "раньше интервала ничего не шлём")
        await tick_at(5)
        first = session.texts_to(600)[-1]
        check("твой подарок ждёт" in first and "ЗАБРАТЬ ПОДАРОК" in str(session.screen().reply_markup),
              "через 5 мин — напоминание №1 с кнопкой")
        check(await tick_at(6) == 0, "до следующего интервала — тишина")
        await tick_at(10)
        check("пока что" in session.texts_to(600)[-1], "через 10 мин — другой вариант текста")

        await feed(cb_update(600, U(a="gift_cta").pack()))
        check("Я подписался" in str(session.screen().reply_markup), "кнопка «ЗАБРАТЬ ПОДАРОК» ведёт на подписку")
        session.members.update({(CHANNEL, 600), (-1002, 600)})
        await feed(cb_update(600, U(a="check").pack()))
        u = await db.get_user(600)
        check(u["verified_at"] is not None and u["remind_at"] is None, "подписался — напоминания остановлены")
        before = len(session.texts_to(600))
        await tick_at(15)
        check(len(session.texts_to(600)) == before, "подписавшемуся больше не пишем")

        await feed(msg_update(601, "/start"))
        clock["offset"] = 0
        await reminders.db.run("UPDATE users SET remind_at = ? WHERE user_id = 601", real_now())
        for m in (1, 7, 13, 19, 25):
            await tick_at(m)
        u = await db.get_user(601)
        check(u["remind_step"] == 3 and u["remind_at"] is None, "ровно 3 напоминания, дальше цепочка закрыта")

        await feed(msg_update(100, "/start"))
        check((await db.get_user(100))["remind_at"] is None, "подписанному напоминания не ставятся")

        st = await db.reminder_stats()
        log600 = [r[0] for r in await db.all("SELECT step FROM reminder_log WHERE user_id = 600 ORDER BY step")]
        others = await db.val("SELECT COUNT(DISTINCT user_id) FROM reminder_log WHERE user_id NOT IN (600, 601)")
        check(log600 == [1, 2] and st["converted"].get(2) == 1 and st["reached"] == 2 + others,
              f"статистика: {st['reached']} получили, подписались после №2: {st['converted'].get(2)}")

        for cb in (A(s="rm"), A(s="rm", a="count", id=5), A(s="rm", a="interval", id=10), A(s="rm", a="texts"),
                   A(s="rm", a="text", id=1)):
            await feed(cb_update(ADMIN, cb.pack()))
        check(reminders.count == 5 and reminders.interval == 600, "количество и интервал меняются из админки")
        await feed(cb_update(ADMIN, A(s="rm", a="text_edit", id=1).pack()))
        await feed(msg_update(ADMIN, "Эй {name}, забери {gift}!", entities=[{"type": "bold", "offset": 0, "length": 2}]))
        check(reminders.texts()[0] == "<b>Эй</b> {name}, забери {gift}!", "вариант отредактирован с форматированием")
        await feed(cb_update(ADMIN, A(s="rm", a="text_add").pack()))
        await feed(msg_update(ADMIN, "Шестой вариант"))
        check(len(reminders.texts()) == 6, "вариант добавлен")
        await feed(cb_update(ADMIN, A(s="rm", a="text_del", id=6).pack()))
        await feed(cb_update(ADMIN, A(s="rm", a="text_reset").pack()))
        check(len(reminders.texts()) == 5 and "твой подарок ждёт" in reminders.texts()[0], "стандартные тексты")
        await feed(cb_update(ADMIN, A(s="rm", a="button").pack()))
        await feed(msg_update(ADMIN, "🔥 ХОЧУ ПОДАРОК"))
        check(settings.get("remind_button") == "🔥 ХОЧУ ПОДАРОК", "текст кнопки изменён")
        await feed(cb_update(ADMIN, A(s="rm", a="photo").pack()))
        await feed(msg_update(ADMIN, None, photo=[{"file_id": "remind_pic", "file_unique_id": "rp",
                                                   "width": 800, "height": 400}]))
        photos_before = len(session.by_type(SendPhoto))
        await feed(cb_update(ADMIN, A(s="rm", a="preview").pack()))
        previews = session.by_type(SendPhoto)[photos_before:]
        check(len(previews) == 5 and previews[0].photo == "remind_pic" and "ХОЧУ" in str(previews[0].reply_markup),
              "превью: 5 напоминаний с картинкой и новой кнопкой")

        await feed(cb_update(ADMIN, A(s="rm", a="toggle").pack()))
        await feed(msg_update(602, "/start"))
        check(not reminders.enabled and (await db.get_user(602))["remind_at"] is None,
              "выключены — новым пользователям не ставятся")
        await feed(cb_update(ADMIN, A(s="rm", a="toggle").pack()))
    finally:
        rem_mod.now = real_now

    print("Рулетка (мини-апп)")
    from aiohttp.test_utils import TestClient, TestServer

    import bot.services.roulette as roulette_mod
    import bot.web.server as server_mod
    from bot.web.server import create_app
    roulette = dp["roulette"]
    await roulette.seed_defaults()
    cases = await db.roulette_cases()
    check([c["name"] for c in cases] == ["Все", "Романтика"] and [c["price"] for c in cases] == [25, 42],
          "кейсы по умолчанию: «Все» 25 ⭐ и «Романтика» 42 ⭐")
    all_case = cases[0]
    check({p["gift_emoji"] for p in await db.roulette_prizes(all_case["id"])} == {"🌹", "🧸"},
          "призы подобраны из каталога по эмодзи и цене")
    for c in cases:
        prizes = await db.roulette_prizes(c["id"])
        top_weight = max(p["weight"] for p in prizes)
        check(all(p["weight"] == top_weight for p in prizes if p["gift_price"] == 15)
              and all(p["weight"] < top_weight for p in prizes if p["gift_price"] != 15),
              f"«{c['name']}»: у подарков за 15 ⭐ самый высокий шанс")

    client = TestClient(TestServer(create_app(dp["web"])))
    await client.start_server()
    try:
        def auth(uid):
            return {"Authorization": f"tma {init_data(uid, bot.token)}"}

        r = await client.post("/api/init", json={})
        check(r.status == 401, "API без подписи Telegram — 401")
        r = await client.post("/api/init", json={}, headers={"Authorization": f"tma {init_data(700, 'wrong:token')}"})
        check(r.status == 401, "поддельная подпись — 401")
        r = await client.post("/api/init", json={}, headers={
            "Authorization": f"tma {init_data(700, bot.token, int(time.time()) - 3 * 86400)}"})
        check(r.status == 401, "устаревшая сессия — 401")

        data = await (await client.post("/api/init", json={}, headers=auth(700))).json()
        check(len(data["cases"]) == 2 and data["need_sub"] and data["demo"], "init: кейсы, демо, нужна подписка")
        chances = [p["chance"] for p in data["cases"][0]["prizes"]]
        check(abs(sum(chances) - 100) < 0.01 and all(p["media"] == "json" for p in data["cases"][0]["prizes"]),
              "шансы нормализованы до 100%, анимации — Lottie")
        r = await client.post("/api/spin", json={"case_id": all_case["id"]}, headers=auth(700))
        check(r.status == 403, "без подписки крутить нельзя")

        session.members.update({(CHANNEL, 700), (-1002, 700)})
        data = await (await client.post("/api/check_sub", json={}, headers=auth(700))).json()
        check(data["need_sub"] == [] and (await db.get_user(700))["verified_at"] is not None,
              "после подписки — можно играть, подписка засчитана")

        demo = await (await client.post("/api/demo", json={"case_id": all_case["id"]}, headers=auth(700))).json()
        gifts_before = len(session.by_type(SendGift))
        check(demo["prize"]["emoji"] in ("🌹", "🧸") and len(session.by_type(SendGift)) == gifts_before,
              "демо-прокрутка без оплаты и без подарка")

        spin = await (await client.post("/api/spin", json={"case_id": all_case["id"]}, headers=auth(700))).json()
        check(spin["invoice"].endswith(f"spin:{spin['spin_id']}"), "счёт на прокрутку создан")
        r = await client.post("/api/spin", json={"case_id": all_case["id"]}, headers=auth(700))
        check(r.status == 429, "частые счета ограничены")
        payload = f"spin:{spin['spin_id']}"

        async def pre_checkout(uid, amount):
            await feed({"update_id": next(ids), "pre_checkout_query": {
                "id": str(next(ids)), "currency": "XTR", "total_amount": amount, "invoice_payload": payload,
                "from": {"id": uid, "is_bot": False, "first_name": "U"}}})
            return session.by_type(AnswerPreCheckoutQuery)[-1].ok

        check(await pre_checkout(700, 25) and not await pre_checkout(700, 1) and not await pre_checkout(701, 25),
              "pre_checkout: верная сумма и владелец — ок, иначе отказ")

        chat_before = len(session.texts_to(700))
        await feed(payment_update(700, payload, 25, "charge_1"))
        st = await (await client.get(f"/api/spin/{spin['spin_id']}", headers=auth(700))).json()
        check(st["status"] == "paid" and st["prize"] and len(session.by_type(SendGift)) == gifts_before
              and len(session.texts_to(700)) == chat_before,
              "после оплаты приз известен, но подарок и сообщение ещё не отправлены (крутится рулетка)")
        await feed(payment_update(700, payload, 25, "charge_1"))
        r = await client.post(f"/api/spin/{spin['spin_id']}/reveal", json={}, headers=auth(701))
        check(r.status == 404, "чужой спин не раскрыть")
        st = await (await client.post(f"/api/spin/{spin['spin_id']}/reveal", json={}, headers=auth(700))).json()
        gift = session.by_type(SendGift)[-1]
        check(st["status"] == "sent" and gift.user_id == 700 and gift.gift_id in ("g_rose", "g_bear")
              and st["prize"]["gift_id"] == gift.gift_id,
              f"рулетка остановилась → отправлен {st['prize']['emoji']}")
        check(gift.text is None, "выигрыш рулетки — без подписи")
        check("Рулетка «Все»" in session.texts_to(700)[-1], "в чат пришло сообщение о выигрыше — после прокрутки")
        await client.post(f"/api/spin/{spin['spin_id']}/reveal", json={}, headers=auth(700))
        await roulette.deliver(spin["spin_id"])
        check(len(session.by_type(SendGift)) == gifts_before + 1,
              "повторная оплата/раскрытие/таймаут не выдают второй приз")
        r = await client.get(f"/api/spin/{spin['spin_id']}", headers=auth(701))
        check(r.status == 404, "чужой спин не виден")

        prof = await (await client.get("/api/profile", headers=auth(700))).json()
        r = await client.get("/api/top", headers=auth(700))
        check(prof["spins"] == 1 and prof["history"][0]["status"] == "sent" and r.status == 404,
              "профиль показывает выигрыш, вкладки «Топ» больше нет")

        r = await client.get(f"/api/gift/{gift.gift_id}")
        body = await r.json()
        check(r.status == 200 and body["fr"] == 60, "анимация подарка: .tgs распакован в Lottie JSON")
        r = await client.get("/")
        page = await r.text()
        check(r.status == 200 and "Мне повезёт" in page, "страница мини-аппа отдаётся")
        import re as re_mod
        js = re_mod.search(r'src="(/static/app\.js\?v=\w+)"', page)
        css = re_mod.search(r'href="(/static/app\.css\?v=\w+)"', page)
        check(js is not None and css is not None and r.headers["Cache-Control"] == "no-cache",
              "скрипт и стили подключены с версией — после обновления кэш не мешает")
        r = await client.get(js.group(1))
        plain_js = await client.get("/static/app.js")
        check(r.status == 200 and "immutable" in r.headers["Cache-Control"]
              and plain_js.headers["Cache-Control"] == "no-cache", "версионная статика кэшируется, без версии — нет")

        print("  — выдача не удалась и возврат звёзд")
        session.gift_error = "BALANCE_TOO_LOW"
        server_mod._last_invoice.clear()
        spin2 = await (await client.post("/api/spin", json={"case_id": all_case["id"]}, headers=auth(700))).json()
        roulette_mod.REVEAL_TIMEOUT = 0.2  # мини-апп закрыли посреди прокрутки — сработает таймаут
        await feed(payment_update(700, f"spin:{spin2['spin_id']}", 25, "charge_2"))
        await asyncio.sleep(0.5)
        roulette_mod.REVEAL_TIMEOUT = 30.0
        session.gift_error = None
        check((await db.get_spin(spin2["spin_id"]))["status"] == "pending", "нет звёзд у бота — выигрыш в очереди")
        claim = (await db.list_claims("pending", 10, 0))[0]
        check(claim["spin_id"] == spin2["spin_id"], "заявка связана с прокруткой")
        await feed(cb_update(ADMIN, A(s="cl", a="card", id=claim["id"]).pack()))
        check("Вернуть 25 ⭐" in str(session.screen().reply_markup), "в заявке есть кнопка возврата звёзд")
        await feed(cb_update(ADMIN, A(s="cl", a="send", id=claim["id"]).pack()))
        check(session.by_type(SendGift)[-1].text is None and (await db.get_claim(claim["id"]))["status"] == "sent",
              "из очереди выигрыш рулетки тоже уходит без подписи")
        claim = (await db.list_claims("sent", 1, 0))[0]
        await db.run("UPDATE claims SET status = 'pending' WHERE id = ?", claim["id"])
        await db.set_spin_status(spin2["spin_id"], "pending")
        await feed(cb_update(ADMIN, A(s="cl", a="refund_ok", id=claim["id"]).pack()))
        refund = session.by_type(RefundStarPayment)[-1]
        check(refund.telegram_payment_charge_id == "charge_2"
              and (await db.get_spin(spin2["spin_id"]))["status"] == "refunded", "звёзды возвращены")

        server_mod._last_invoice.clear()
        spin3 = await (await client.post("/api/spin", json={"case_id": all_case["id"]}, headers=auth(700))).json()
        await feed(payment_update(700, f"spin:{spin3['spin_id']}", 25, "charge_3"))
        for task in list(roulette._tasks):
            task.cancel()  # имитация перезапуска: отложенная выдача потерялась
        check((await db.get_spin(spin3["spin_id"]))["status"] == "paid", "перезапуск между оплатой и выдачей")
        await roulette.recover()
        await asyncio.sleep(0.1)
        check((await db.get_spin(spin3["spin_id"]))["status"] == "sent", "после перезапуска выигрыш доотправлен")

        print("  — админка рулетки")
        for cb in (A(s="rl"), A(s="rl", a="case", id=all_case["id"]), A(s="rl", a="add", id=all_case["id"])):
            await feed(cb_update(ADMIN, cb.pack()))
        check("RTP" in [t for t in session.texts_to(ADMIN) if "Рулетка подарков" in t][-1], "главный экран рулетки")
        await feed(cb_update(ADMIN, A(s="rl", a="pick", id=all_case["id"], v="g_rose").pack()))
        await feed(msg_update(ADMIN, "150"))
        await feed(msg_update(ADMIN, "0,5%"))
        prizes = await db.roulette_prizes(all_case["id"])
        check(len(prizes) == 3 and any(p["weight"] == 0.5 for p in prizes),
              "приз добавлен с шансом «0,5%» (150 отклонено)")
        added = next(p for p in prizes if p["weight"] == 0.5)
        await feed(cb_update(ADMIN, A(s="rl", a="weight", id=added["id"]).pack()))
        await feed(msg_update(ADMIN, "2"))
        check((await db.get_roulette_prize(added["id"]))["weight"] == 2, "шанс приза изменён")
        await feed(cb_update(ADMIN, A(s="rl", a="prize_del", id=added["id"]).pack()))
        await feed(cb_update(ADMIN, A(s="rl", a="new").pack()))
        await feed(msg_update(ADMIN, "VIP"))
        await feed(msg_update(ADMIN, "100"))
        check(any(c["name"] == "VIP" and c["price"] == 100 for c in await db.roulette_cases()), "новый кейс создан")
        vip = next(c for c in await db.roulette_cases() if c["name"] == "VIP")
        data = await (await client.post("/api/init", json={}, headers=auth(700))).json()
        check(len(data["cases"]) == 2, "кейс без призов не показывается в мини-аппе")
        await feed(cb_update(ADMIN, A(s="rl", a="del_ok", id=vip["id"]).pack()))

        await feed(cb_update(ADMIN, A(s="rl", a="t", v="roulette_enabled").pack()))
        r = await client.post("/api/init", json={}, headers=auth(700))
        check(r.status == 503 and not await pre_checkout(700, 25), "рулетка выключена — мини-апп и оплата закрыты")
        await feed(cb_update(ADMIN, A(s="rl", a="t", v="roulette_enabled").pack()))

        print("  — обновление шансов в уже созданных кейсах")
        await settings.set("roulette_defaults_version", "1")
        untouched = await db.create_roulette_case("Все", 25)
        await db.add_roulette_prize(untouched, "g_bear", "🧸", 15, 21.37)
        await db.add_roulette_prize(untouched, "g_rose", "🌹", 25, 25)
        custom = await db.create_roulette_case("Романтика", 42)
        await db.add_roulette_prize(custom, "g_bear", "🧸", 15, 11.05)
        await db.add_roulette_prize(custom, "g_rose", "🌹", 25, 50)  # админ поменял шанс
        await roulette.upgrade_default_weights()
        w_untouched = {p["gift_emoji"]: p["weight"] for p in await db.roulette_prizes(untouched)}
        w_custom = {p["gift_emoji"]: p["weight"] for p in await db.roulette_prizes(custom)}
        check(w_untouched == {"🧸": 33, "🌹": 14}, "нетронутый кейс получил новые шансы")
        check(w_custom == {"🧸": 11.05, "🌹": 50}, "кейс с ручными шансами не тронут")
        await db.set_prize_weight((await db.roulette_prizes(untouched))[0]["id"], 21.37)
        await roulette.upgrade_default_weights()
        check((await db.roulette_prizes(untouched))[0]["weight"] == 21.37, "миграция выполняется один раз")
        await db.delete_roulette_case(untouched)
        await db.delete_roulette_case(custom)

        await feed(cb_update(100, U(a="menu").pack()))
        check("web_app" in str(session.screen().reply_markup.model_dump(exclude_none=True)),
              "в меню бота есть кнопка мини-аппа")
    finally:
        await client.close()

    print("Рассылка")
    await feed(cb_update(ADMIN, A(s="bc").pack()))
    await feed(msg_update(ADMIN, "Новость!"))
    await feed(cb_update(ADMIN, A(s="bc", a="skip").pack()))
    await feed(cb_update(ADMIN, A(s="bc", a="aud", v="all").pack()))
    await feed(cb_update(ADMIN, A(s="bc", a="go").pack()))
    await dp["broadcaster"].wait(timeout=10)
    stats = dp["broadcaster"].stats
    check(stats and stats.finished and stats.sent == stats.total and stats.total > 5,
          f"рассылка завершена: {stats.sent}/{stats.total}")

    await asyncio.sleep(0.1)
    print(f"\n✅ Все проверки пройдены: {passed}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\n❌ ПРОВАЛ: {e}")
        sys.exit(1)
