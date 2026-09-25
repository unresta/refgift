from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, MessageOriginUser
from aiogram.utils.callback_answer import CallbackAnswer

from bot.callbacks import A
from bot.database import Database
from bot.handlers.admin.common import Input, back, btn, drop_prompt, kb, prompt
from bot.services.admins import AdminRegistry
from bot.utils import esc, show

router = Router(name="admin_staff")


async def list_screen(db: Database, admins: AdminRegistry, viewer_id: int):
    is_super = admins.is_super(viewer_id)
    lines = ["👮 <b>Администраторы</b>\n"]
    rows = []
    for uid in admins.all_ids:
        user = await db.get_user(uid)
        name = esc(user["full_name"]) if user else "не запускал бота"
        crown = "👑" if admins.is_super(uid) else "👮"
        lines.append(f"{crown} {name} · <code>{uid}</code>")
        if is_super and not admins.is_super(uid):
            rows.append([btn(f"❌ Убрать {user['full_name'] if user else uid}", "ad", "del", id=uid)])
    lines.append("\n👑 — главные админы из <code>.env</code>, управляют списком админов.")
    if is_super:
        rows.insert(0, [btn("➕ Добавить админа", "ad", "add", style="success")])
    else:
        lines.append("<i>Добавлять и удалять админов могут только главные админы.</i>")
    rows.append(back())
    return "\n".join(lines), kb(*rows)


@router.callback_query(A.filter((F.s == "ad") & (F.a == "open")))
async def cb_list(call: CallbackQuery, db: Database, admins: AdminRegistry) -> None:
    await show(call, *await list_screen(db, admins, call.from_user.id))


@router.callback_query(A.filter((F.s == "ad") & (F.a == "add")))
async def cb_add(call: CallbackQuery, callback_answer: CallbackAnswer, state: FSMContext,
                 admins: AdminRegistry) -> None:
    if not admins.is_super(call.from_user.id):
        callback_answer.text = "Только для главных админов"
        return
    await prompt(call, state, Input.admin_add,
                 "➕ <b>Новый администратор</b>\n\nПришлите ID, @username или перешлите сообщение пользователя.\n"
                 "<i>Пользователь должен хотя бы раз запустить бота.</i>",
                 back("ad", text="✖️ Отмена"))


@router.message(Input.admin_add)
async def on_add(message: Message, state: FSMContext, db: Database, admins: AdminRegistry) -> None:
    if isinstance(message.forward_origin, MessageOriginUser):
        query = str(message.forward_origin.sender_user.id)
    else:
        query = message.text or ""
    user = await db.find_user(query)
    if not user:
        await message.answer("❌ Не найден. Попросите его запустить бота и повторите.")
        return
    await drop_prompt(message, state)
    await state.clear()
    await admins.add(user["user_id"], message.from_user.id)
    await message.answer(f"✅ {esc(user['full_name'])} теперь администратор")
    try:
        await message.bot.send_message(user["user_id"], "👮 Вам выданы права администратора. Панель: /admin")
    except Exception:
        pass
    await show(message, *await list_screen(db, admins, message.from_user.id))


@router.callback_query(A.filter((F.s == "ad") & (F.a == "del")))
async def cb_del(call: CallbackQuery, callback_data: A, callback_answer: CallbackAnswer, db: Database,
                 admins: AdminRegistry) -> None:
    if not admins.is_super(call.from_user.id) or admins.is_super(callback_data.id):
        callback_answer.text = "Недостаточно прав"
        return
    await admins.remove(callback_data.id)
    callback_answer.text = "Админ удалён"
    await show(call, *await list_screen(db, admins, call.from_user.id))
