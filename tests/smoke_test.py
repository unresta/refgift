"""Офлайн-прогон бота: фейковый Telegram API + реальные апдейты через диспетчер.

Запуск:  .venv/bin/python -m tests.smoke_test
"""
import asyncio
import itertools
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
from aiogram.methods import (CopyMessage, CreateChatInviteLink, GetAvailableGifts, GetChat, GetChatMember,
                             GetChatMemberCount, GetMe, GetMyStarBalance, SendDocument, SendGift, TelegramMethod)
from aiogram.types import ChatFullInfo, ChatInviteLink, Gifts, MessageId, StarAmount, User

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

    async def close(self) -> None:
        pass

    async def stream_content(self, *a, **kw):
        yield b""

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
            return StarAmount(amount=100)
        if isinstance(method, SendGift):
            if self.gift_error:
                raise TelegramBadRequest(method=method, message=self.gift_error)
            return True
        if isinstance(method, GetAvailableGifts):
            sticker = {"file_id": "f", "file_unique_id": "u", "type": "regular", "width": 1, "height": 1,
                       "is_animated": False, "is_video": False}
            return Gifts(gifts=[{"id": "g_bear", "star_count": 15, "sticker": {**sticker, "emoji": "🧸"}},
                                {"id": "g_rose", "star_count": 25, "sticker": {**sticker, "emoji": "🌹"},
                                 "remaining_count": 10}])
        if isinstance(method, GetChat):
            return ChatFullInfo(id=-1002, type="channel", title="Private Chan", accent_color_id=0,
                                max_reaction_count=0, accepted_gift_types={
                                    "unlimited_gifts": True, "limited_gifts": True, "unique_gifts": True,
                                    "premium_subscription": True, "gifts_from_channels": True})
        if isinstance(method, CreateChatInviteLink):
            return ChatInviteLink(invite_link="https://t.me/+secret", creator=User(id=BOT_ID, is_bot=True,
                                  first_name="Bot"), creates_join_request=False, is_primary=False, is_revoked=False)
        if isinstance(method, CopyMessage):
            return MessageId(message_id=next(ids))
        if name in ("SendMessage", "EditMessageText", "SendDocument", "SendInvoice"):
            chat_id = getattr(method, "chat_id", None) or ADMIN
            return _msg(chat_id, getattr(method, "text", None) or "doc", from_bot=True)
        return True


def _member(data):
    from pydantic import TypeAdapter
    from aiogram.types import ResultChatMemberUnion
    return TypeAdapter(ResultChatMemberUnion).validate_python(data)


def _msg(chat_id, text, from_bot=False, **extra):
    from aiogram.types import Message
    uid = BOT_ID if from_bot else chat_id
    return Message.model_validate({
        "message_id": next(ids), "date": int(time.time()),
        "chat": {"id": chat_id, "type": "private", "first_name": "U"},
        "from": {"id": uid, "is_bot": from_bot, "first_name": f"User{uid}"},
        "text": text, **extra,
    })


def msg_update(uid: int, text: str, **extra) -> dict:
    return {"update_id": next(ids), "message": {
        "message_id": next(ids), "date": int(time.time()),
        "chat": {"id": uid, "type": "private", "first_name": f"User{uid}"},
        "from": {"id": uid, "is_bot": False, "first_name": f"User{uid}", "username": f"user{uid}"},
        "text": text, **extra,
    }}


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


async def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    middlewares.ThrottlingMiddleware.__init__.__defaults__ = (0.0,)
    tmp = tempfile.mkdtemp()
    config = Config("0:fake", frozenset({ADMIN}), os.path.join(tmp, "t.db"), ZoneInfo("Europe/Moscow"))
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
        A(s="st", a="mode"), A(s="st", a="ttl"), A(s="st", a="topup"),
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
