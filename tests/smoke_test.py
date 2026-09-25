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
from aiogram.methods import (AnswerInlineQuery, CopyMessage, CreateChatInviteLink, EditMessageCaption,
                             GetAvailableGifts, GetChat, GetChatMember, GetChatMemberCount, GetFile, GetMe,
                             GetMyStarBalance, SendDocument, SendGift, SendPhoto, TelegramMethod)
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

    async def close(self) -> None:
        pass

    async def stream_content(self, url, *a, **kw):
        """Скачивание файлов: картинка чека (jpg) и превью подарков (webp)."""
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
            thumb = {"file_id": "thumb", "file_unique_id": "t", "width": 64, "height": 64}
            sticker = {"file_id": "f", "file_unique_id": "u", "type": "regular", "width": 1, "height": 1,
                       "is_animated": False, "is_video": False, "thumbnail": thumb}
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

    await settings.set("reward_mode", "manual")
    await feed(msg_update(503, f"/start c_{rose['code']}"))
    claim = (await db.list_claims("pending", 10, 0))[0]
    check(claim["gift_id"] == "g_rose" and claim["check_id"] == rose["id"], "ручной режим: заявка с подарком чека")
    await feed(cb_update(ADMIN, A(s="cl", a="send", id=claim["id"]).pack()))
    check(session.by_type(SendGift)[-1].gift_id == "g_rose", "админ выдал именно подарок из чека 🌹")
    await settings.set("reward_mode", "auto")

    await feed(msg_update(504, "/start c_nonexistent"))
    check("не найден" in session.texts_to(504)[-1] or "Я подписался" in str(session.screen().reply_markup),
          "несуществующий чек")

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
