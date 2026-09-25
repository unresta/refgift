from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from bot.utils import now

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    user_id         INTEGER PRIMARY KEY,
    username        TEXT,
    full_name       TEXT    NOT NULL DEFAULT '',
    referrer_id     INTEGER,
    ref_credited    INTEGER NOT NULL DEFAULT 0,  -- засчитан ли этот юзер своему пригласившему
    ref_count       INTEGER NOT NULL DEFAULT 0,  -- засчитанные рефералы
    bonus_refs      INTEGER NOT NULL DEFAULT 0,  -- ручная корректировка админом
    rewards_claimed INTEGER NOT NULL DEFAULT 0,
    is_banned       INTEGER NOT NULL DEFAULT 0,
    is_blocked      INTEGER NOT NULL DEFAULT 0,  -- пользователь заблокировал бота
    created_at      INTEGER NOT NULL,
    verified_at     INTEGER,                     -- когда впервые прошёл обязательную подписку
    last_seen       INTEGER NOT NULL,
    ad_link_id      INTEGER,                     -- рекламная ссылка, по которой пришёл
    source_check_id INTEGER,                     -- чек, по которому впервые запустил бота
    pending_check   TEXT,                        -- код чека, ждущего активации после подписки
    remind_step     INTEGER NOT NULL DEFAULT 0,  -- сколько напоминаний «забери подарок» уже отправлено
    remind_at       INTEGER                      -- когда отправить следующее (NULL — не нужно)
);
CREATE INDEX IF NOT EXISTS idx_users_referrer ON users(referrer_id);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_users_created  ON users(created_at);
CREATE INDEX IF NOT EXISTS idx_users_total    ON users(ref_count + bonus_refs);

CREATE TABLE IF NOT EXISTS channels (
    chat_id     INTEGER PRIMARY KEY,
    title       TEXT    NOT NULL,
    username    TEXT,
    invite_link TEXT,
    is_active   INTEGER NOT NULL DEFAULT 1,
    position    INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS join_requests (
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS claims (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    status       TEXT    NOT NULL,  -- pending | sent | rejected
    method       TEXT,              -- auto | manual | admin
    gift_id      TEXT,
    error        TEXT,
    created_at   INTEGER NOT NULL,
    processed_at INTEGER,
    processed_by INTEGER,
    check_id     INTEGER            -- заявка создана активацией чека
);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status, id);
CREATE INDEX IF NOT EXISTS idx_claims_user   ON claims(user_id);

CREATE TABLE IF NOT EXISTS admins (
    user_id  INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ad_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    cost        REAL    NOT NULL DEFAULT 0,
    is_archived INTEGER NOT NULL DEFAULT 0,
    created_by  INTEGER,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ad_clicks (
    link_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    is_new     INTEGER NOT NULL,  -- 1 — пользователь впервые запустил бота по этой ссылке
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ad_clicks_link ON ad_clicks(link_id, created_at);

CREATE TABLE IF NOT EXISTS checks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    code              TEXT    NOT NULL UNIQUE,
    total             INTEGER NOT NULL,          -- сколько всего активаций
    caption           TEXT,                      -- своя подпись (NULL — шаблон из текстов)
    gift_id           TEXT,                      -- подарок чека (NULL — из настроек)
    gift_emoji        TEXT,
    gift_price        INTEGER,
    with_photo        INTEGER NOT NULL DEFAULT 0,
    is_active         INTEGER NOT NULL DEFAULT 1,
    is_sent           INTEGER NOT NULL DEFAULT 0, -- админ отправил чек (chosen_inline_result)
    inline_message_id TEXT,
    created_by        INTEGER NOT NULL,
    created_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checks_draft ON checks(created_by, is_sent, created_at);

CREATE TABLE IF NOT EXISTS gift_images (
    base_file_id TEXT NOT NULL,   -- картинка чека
    gift_id      TEXT NOT NULL,
    file_id      TEXT NOT NULL,   -- картинка чека со значком подарка
    PRIMARY KEY (base_file_id, gift_id)
);

CREATE TABLE IF NOT EXISTS gift_banners (
    gift_id    TEXT    PRIMARY KEY,  -- свой баннер чека для конкретного подарка
    file_id    TEXT    NOT NULL,
    gift_emoji TEXT,
    gift_price INTEGER,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reminder_log (
    user_id INTEGER NOT NULL,
    step    INTEGER NOT NULL,
    sent_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminder_log_user ON reminder_log(user_id);

CREATE TABLE IF NOT EXISTS check_activations (
    check_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (check_id, user_id)
);
"""

# Колонки, добавленные после первого релиза: (таблица, колонка, определение)
MIGRATIONS = [
    ("users", "ad_link_id", "INTEGER"),
    ("users", "source_check_id", "INTEGER"),
    ("users", "pending_check", "TEXT"),
    ("claims", "check_id", "INTEGER"),
    ("checks", "gift_id", "TEXT"),
    ("checks", "gift_emoji", "TEXT"),
    ("checks", "gift_price", "INTEGER"),
    ("users", "remind_step", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "remind_at", "INTEGER"),
]
POST_MIGRATION_SQL = """
CREATE INDEX IF NOT EXISTS idx_users_ad_link ON users(ad_link_id, created_at);
CREATE INDEX IF NOT EXISTS idx_users_check ON users(source_check_id);
CREATE INDEX IF NOT EXISTS idx_users_remind ON users(remind_at) WHERE remind_at IS NOT NULL;
"""

TOTAL = "(ref_count + bonus_refs)"

AUDIENCES = {
    "all": "1",
    "verified": "verified_at IS NOT NULL",
    "unverified": "verified_at IS NULL",
    "goal": f"{TOTAL} >= :goal",
    "active": "last_seen >= :since",
}


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database is not connected"
        return self._conn

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        for table, column, definition in MIGRATIONS:
            columns = {r["name"] for r in await self.all(f"PRAGMA table_info({table})")}
            if column not in columns:
                await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        await self._conn.executescript(POST_MIGRATION_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    # ---------- low level ----------
    async def one(self, sql: str, *args: Any) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchone()

    async def all(self, sql: str, *args: Any) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, args) as cur:
            return list(await cur.fetchall())

    async def val(self, sql: str, *args: Any, default: Any = 0) -> Any:
        row = await self.one(sql, *args)
        return row[0] if row and row[0] is not None else default

    async def run(self, sql: str, *args: Any) -> int:
        cur = await self.conn.execute(sql, args)
        await self.conn.commit()
        return cur.rowcount

    # ---------- users ----------
    async def upsert_user(self, user_id: int, username: str | None, full_name: str) -> tuple[aiosqlite.Row, bool]:
        ts = now()
        cur = await self.conn.execute(
            "INSERT INTO users (user_id, username, full_name, created_at, last_seen) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO NOTHING",
            (user_id, username, full_name, ts, ts),
        )
        is_new = cur.rowcount > 0
        if not is_new:
            await self.conn.execute(
                "UPDATE users SET username = ?, full_name = ?, last_seen = ?, is_blocked = 0 WHERE user_id = ?",
                (username, full_name, ts, user_id),
            )
        await self.conn.commit()
        row = await self.get_user(user_id)
        assert row is not None
        return row, is_new

    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        return await self.one("SELECT * FROM users WHERE user_id = ?", user_id)

    async def find_user(self, query: str) -> aiosqlite.Row | None:
        query = query.strip().removeprefix("https://t.me/").removeprefix("@")
        if query.lstrip("-").isdigit():
            return await self.get_user(int(query))
        return await self.one("SELECT * FROM users WHERE username = ? COLLATE NOCASE", query)

    async def set_referrer(self, user_id: int, referrer_id: int) -> bool:
        """Привязывает пригласившего, пока пользователь ещё не прошёл подписку."""
        return bool(await self.run(
            "UPDATE users SET referrer_id = ? "
            "WHERE user_id = ? AND referrer_id IS NULL AND verified_at IS NULL AND user_id != ?",
            referrer_id, user_id, referrer_id,
        ))

    async def mark_verified(self, user_id: int) -> bool:
        return bool(await self.run(
            "UPDATE users SET verified_at = ?, remind_at = NULL WHERE user_id = ? AND verified_at IS NULL",
            now(), user_id,
        ))

    async def credit_referral(self, user_id: int) -> int | None:
        """Атомарно засчитывает реферала. Возвращает ID пригласившего, если засчитан сейчас."""
        async with self.conn.execute(
            "UPDATE users SET ref_credited = 1 "
            "WHERE user_id = ? AND ref_credited = 0 AND referrer_id IS NOT NULL RETURNING referrer_id",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            await self.conn.commit()
            return None
        referrer_id = row[0]
        await self.conn.execute("UPDATE users SET ref_count = ref_count + 1 WHERE user_id = ?", (referrer_id,))
        await self.conn.commit()
        return referrer_id

    async def referral_counts(self, referrer_id: int) -> tuple[int, int]:
        row = await self.one(
            "SELECT COALESCE(SUM(ref_credited = 1), 0), COALESCE(SUM(ref_credited = 0), 0) "
            "FROM users WHERE referrer_id = ?",
            referrer_id,
        )
        return (row[0], row[1]) if row else (0, 0)

    async def list_referrals(self, referrer_id: int, limit: int, offset: int) -> list[aiosqlite.Row]:
        return await self.all(
            "SELECT * FROM users WHERE referrer_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            referrer_id, limit, offset,
        )

    async def top_referrers(self, limit: int = 10, offset: int = 0) -> list[aiosqlite.Row]:
        return await self.all(
            f"SELECT *, {TOTAL} AS total FROM users WHERE {TOTAL} > 0 AND is_banned = 0 "
            f"ORDER BY total DESC, user_id LIMIT ? OFFSET ?",
            limit, offset,
        )

    async def user_rank(self, user_id: int) -> int | None:
        user = await self.get_user(user_id)
        if not user or user["ref_count"] + user["bonus_refs"] <= 0:
            return None
        total = user["ref_count"] + user["bonus_refs"]
        better = await self.val(
            f"SELECT COUNT(*) FROM users WHERE is_banned = 0 AND ({TOTAL} > ? OR ({TOTAL} = ? AND user_id < ?))",
            total, total, user_id,
        )
        return better + 1

    async def list_users(self, kind: str, limit: int, offset: int) -> list[aiosqlite.Row]:
        where = {"banned": "is_banned = 1", "new": "1"}[kind]
        return await self.all(
            f"SELECT * FROM users WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?", limit, offset
        )

    async def count_users(self, kind: str) -> int:
        where = {"banned": "is_banned = 1", "new": "1"}[kind]
        return await self.val(f"SELECT COUNT(*) FROM users WHERE {where}")

    async def set_banned(self, user_id: int, banned: bool) -> None:
        await self.run("UPDATE users SET is_banned = ? WHERE user_id = ?", int(banned), user_id)

    async def set_blocked(self, user_id: int, blocked: bool = True) -> None:
        await self.run("UPDATE users SET is_blocked = ? WHERE user_id = ?", int(blocked), user_id)

    async def mark_blocked_many(self, user_ids: Iterable[int]) -> None:
        await self.conn.executemany("UPDATE users SET is_blocked = 1 WHERE user_id = ?", [(u,) for u in user_ids])
        await self.conn.commit()

    async def add_bonus(self, user_id: int, delta: int) -> None:
        await self.run("UPDATE users SET bonus_refs = bonus_refs + ? WHERE user_id = ?", delta, user_id)

    async def try_consume_reward(self, user_id: int, allowed_total: int) -> bool:
        """Атомарно резервирует награду — защищает от двойного нажатия."""
        return bool(await self.run(
            "UPDATE users SET rewards_claimed = rewards_claimed + 1 WHERE user_id = ? AND rewards_claimed < ?",
            user_id, allowed_total,
        ))

    async def set_rewards_claimed(self, user_id: int, value: int) -> None:
        await self.run("UPDATE users SET rewards_claimed = ? WHERE user_id = ?", max(0, value), user_id)

    async def audience_ids(self, audience: str, goal: int, since: int) -> list[int]:
        rows = await self.conn.execute_fetchall(
            f"SELECT user_id FROM users WHERE is_banned = 0 AND is_blocked = 0 AND {AUDIENCES[audience]}",
            {"goal": goal, "since": since},
        )
        return [r[0] for r in rows]

    async def audience_count(self, audience: str, goal: int, since: int) -> int:
        async with self.conn.execute(
            f"SELECT COUNT(*) FROM users WHERE is_banned = 0 AND is_blocked = 0 AND {AUDIENCES[audience]}",
            {"goal": goal, "since": since},
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def export_users(self, ad_link_id: int | None = None) -> list[aiosqlite.Row]:
        if ad_link_id is None:
            return await self.all(f"SELECT *, {TOTAL} AS total FROM users ORDER BY created_at")
        return await self.all(
            f"SELECT *, {TOTAL} AS total FROM users WHERE ad_link_id = ? ORDER BY created_at", ad_link_id
        )

    # ---------- statistics ----------
    async def stats(self, day_start: int, goal: int) -> dict[str, int]:
        ts = now()
        row = await self.one(
            f"""
            SELECT
                COUNT(*)                                        AS total,
                COALESCE(SUM(created_at >= ?), 0)               AS today,
                COALESCE(SUM(created_at >= ?), 0)               AS week,
                COALESCE(SUM(created_at >= ?), 0)               AS month,
                COALESCE(SUM(last_seen  >= ?), 0)               AS active24,
                COALESCE(SUM(verified_at IS NOT NULL), 0)       AS verified,
                COALESCE(SUM(is_blocked), 0)                    AS blocked,
                COALESCE(SUM(is_banned), 0)                     AS banned,
                COALESCE(SUM(referrer_id IS NOT NULL), 0)       AS invited,
                COALESCE(SUM(ref_credited), 0)                  AS credited,
                COALESCE(SUM(referrer_id IS NOT NULL AND ref_credited = 0), 0) AS ref_pending,
                COALESCE(SUM({TOTAL} >= ?), 0)                  AS reached_goal,
                COALESCE(SUM(referrer_id IS NOT NULL AND created_at >= ?), 0) AS invited_today,
                COALESCE(SUM(ad_link_id IS NOT NULL), 0)        AS from_ads,
                COALESCE(SUM(ad_link_id IS NOT NULL AND created_at >= ?), 0) AS from_ads_today
            FROM users
            """,
            day_start, ts - 7 * 86400, ts - 30 * 86400, ts - 86400, goal, day_start, day_start,
        )
        result = dict(row) if row else {}
        claims = await self.all("SELECT status, COALESCE(method, ''), COUNT(*) FROM claims GROUP BY 1, 2")
        for key in ("claims_pending", "claims_sent", "claims_rejected", "sent_auto", "sent_manual"):
            result[key] = 0
        for status, method, count in claims:
            result[f"claims_{status}"] = result.get(f"claims_{status}", 0) + count
            if status == "sent":
                result["sent_auto" if method == "auto" else "sent_manual"] += count
        return result

    async def registrations_since(self, since: int) -> list[int]:
        rows = await self.all("SELECT created_at FROM users WHERE created_at >= ?", since)
        return [r[0] for r in rows]

    # ---------- channels ----------
    async def add_channel(self, chat_id: int, title: str, username: str | None, invite_link: str | None) -> None:
        position = await self.val("SELECT COALESCE(MAX(position), 0) + 1 FROM channels")
        await self.run(
            "INSERT INTO channels (chat_id, title, username, invite_link, position, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
            "title = excluded.title, username = excluded.username, invite_link = excluded.invite_link, is_active = 1",
            chat_id, title, username, invite_link, position, now(),
        )

    async def channels(self, only_active: bool = False) -> list[aiosqlite.Row]:
        where = "WHERE is_active = 1" if only_active else ""
        return await self.all(f"SELECT * FROM channels {where} ORDER BY position, created_at")

    async def get_channel(self, chat_id: int) -> aiosqlite.Row | None:
        return await self.one("SELECT * FROM channels WHERE chat_id = ?", chat_id)

    async def toggle_channel(self, chat_id: int) -> None:
        await self.run("UPDATE channels SET is_active = 1 - is_active WHERE chat_id = ?", chat_id)

    async def set_channel_link(self, chat_id: int, link: str) -> None:
        await self.run("UPDATE channels SET invite_link = ? WHERE chat_id = ?", link, chat_id)

    async def delete_channel(self, chat_id: int) -> None:
        await self.conn.execute("DELETE FROM channels WHERE chat_id = ?", (chat_id,))
        await self.conn.execute("DELETE FROM join_requests WHERE chat_id = ?", (chat_id,))
        await self.conn.commit()

    async def move_channel_up(self, chat_id: int) -> None:
        items = [r["chat_id"] for r in await self.channels()]
        i = items.index(chat_id) if chat_id in items else 0
        if i == 0:
            return
        items[i - 1], items[i] = items[i], items[i - 1]
        await self.conn.executemany(
            "UPDATE channels SET position = ? WHERE chat_id = ?", [(pos, cid) for pos, cid in enumerate(items)]
        )
        await self.conn.commit()

    async def add_join_request(self, chat_id: int, user_id: int) -> None:
        await self.run(
            "INSERT OR IGNORE INTO join_requests (chat_id, user_id, created_at) VALUES (?, ?, ?)",
            chat_id, user_id, now(),
        )

    async def has_join_request(self, chat_id: int, user_id: int) -> bool:
        return bool(await self.val(
            "SELECT 1 FROM join_requests WHERE chat_id = ? AND user_id = ?", chat_id, user_id
        ))

    # ---------- claims ----------
    async def create_claim(self, user_id: int, status: str, method: str | None, gift_id: str | None,
                           error: str | None = None, processed_by: int | None = None,
                           check_id: int | None = None) -> int:
        ts = now()
        cur = await self.conn.execute(
            "INSERT INTO claims (user_id, status, method, gift_id, error, created_at, processed_at, processed_by, "
            "check_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, status, method, gift_id, error, ts, ts if status != "pending" else None, processed_by,
             check_id),
        )
        await self.conn.commit()
        return cur.lastrowid or 0

    async def get_claim(self, claim_id: int) -> aiosqlite.Row | None:
        return await self.one(
            "SELECT c.*, u.full_name, u.username FROM claims c LEFT JOIN users u USING(user_id) WHERE c.id = ?",
            claim_id,
        )

    async def list_claims(self, status: str, limit: int, offset: int) -> list[aiosqlite.Row]:
        order = "ASC" if status == "pending" else "DESC"
        return await self.all(
            "SELECT c.*, u.full_name, u.username FROM claims c LEFT JOIN users u USING(user_id) "
            f"WHERE c.status = ? ORDER BY c.id {order} LIMIT ? OFFSET ?",
            status, limit, offset,
        )

    async def count_claims(self, status: str) -> int:
        return await self.val("SELECT COUNT(*) FROM claims WHERE status = ?", status)

    async def pending_claim_ids(self) -> list[int]:
        return [r[0] for r in await self.all("SELECT id FROM claims WHERE status = 'pending' ORDER BY id")]

    async def user_pending_claim(self, user_id: int) -> aiosqlite.Row | None:
        return await self.one(
            "SELECT * FROM claims WHERE user_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1", user_id
        )

    async def finish_claim(self, claim_id: int, status: str, method: str | None, admin_id: int | None,
                           error: str | None = None) -> bool:
        """Меняет статус только у ожидающей заявки — повторная обработка невозможна."""
        return bool(await self.run(
            "UPDATE claims SET status = ?, method = ?, processed_at = ?, processed_by = ?, error = ? "
            "WHERE id = ? AND status = 'pending'",
            status, method, now(), admin_id, error, claim_id,
        ))

    async def set_claim_error(self, claim_id: int, error: str) -> None:
        await self.run("UPDATE claims SET error = ? WHERE id = ?", error, claim_id)

    # ---------- admins ----------
    async def admin_ids(self) -> list[int]:
        return [r[0] for r in await self.all("SELECT user_id FROM admins ORDER BY added_at")]

    async def add_admin(self, user_id: int, added_by: int) -> None:
        await self.run(
            "INSERT OR IGNORE INTO admins (user_id, added_by, added_at) VALUES (?, ?, ?)", user_id, added_by, now()
        )

    async def remove_admin(self, user_id: int) -> None:
        await self.run("DELETE FROM admins WHERE user_id = ?", user_id)

    # ---------- рекламные ссылки ----------
    async def create_ad_link(self, code: str, name: str, created_by: int) -> int:
        cur = await self.conn.execute(
            "INSERT INTO ad_links (code, name, created_by, created_at) VALUES (?, ?, ?, ?)",
            (code, name, created_by, now()),
        )
        await self.conn.commit()
        return cur.lastrowid or 0

    async def get_ad_link(self, link_id: int) -> aiosqlite.Row | None:
        return await self.one("SELECT * FROM ad_links WHERE id = ?", link_id)

    async def get_ad_link_by_code(self, code: str) -> aiosqlite.Row | None:
        return await self.one("SELECT * FROM ad_links WHERE code = ?", code)

    async def list_ad_links(self, archived: bool, limit: int, offset: int) -> list[aiosqlite.Row]:
        return await self.all(
            "SELECT l.*, "
            "  (SELECT COUNT(*) FROM users u WHERE u.ad_link_id = l.id) AS new_users, "
            "  (SELECT COUNT(*) FROM users u WHERE u.ad_link_id = l.id AND u.verified_at IS NOT NULL) AS verified, "
            "  (SELECT COUNT(*) FROM ad_clicks c WHERE c.link_id = l.id) AS clicks "
            "FROM ad_links l WHERE l.is_archived = ? ORDER BY l.id DESC LIMIT ? OFFSET ?",
            int(archived), limit, offset,
        )

    async def count_ad_links(self, archived: bool) -> int:
        return await self.val("SELECT COUNT(*) FROM ad_links WHERE is_archived = ?", int(archived))

    async def update_ad_link(self, link_id: int, **fields: Any) -> None:
        allowed = {"name", "cost", "is_archived"}
        assert set(fields) <= allowed, fields
        sets = ", ".join(f"{k} = ?" for k in fields)
        await self.run(f"UPDATE ad_links SET {sets} WHERE id = ?", *fields.values(), link_id)

    async def delete_ad_link(self, link_id: int) -> None:
        await self.conn.execute("UPDATE users SET ad_link_id = NULL WHERE ad_link_id = ?", (link_id,))
        await self.conn.execute("DELETE FROM ad_clicks WHERE link_id = ?", (link_id,))
        await self.conn.execute("DELETE FROM ad_links WHERE id = ?", (link_id,))
        await self.conn.commit()

    async def track_ad_click(self, link_id: int, user_id: int, is_new: bool) -> None:
        """Фиксирует переход; новому пользователю навсегда присваивает источник."""
        await self.conn.execute(
            "INSERT INTO ad_clicks (link_id, user_id, is_new, created_at) VALUES (?, ?, ?, ?)",
            (link_id, user_id, int(is_new), now()),
        )
        if is_new:
            await self.conn.execute(
                "UPDATE users SET ad_link_id = ? WHERE user_id = ? AND ad_link_id IS NULL", (link_id, user_id)
            )
        await self.conn.commit()

    async def ad_link_stats(self, link_id: int, goal: int, day_start: int) -> dict[str, int]:
        ts = now()
        users = await self.one(
            f"""
            SELECT
                COUNT(*)                                     AS new_users,
                COALESCE(SUM(created_at >= ?), 0)            AS new_today,
                COALESCE(SUM(verified_at IS NOT NULL), 0)    AS verified,
                COALESCE(SUM(ref_count > 0), 0)              AS inviters,
                COALESCE(SUM(ref_count), 0)                  AS referrals,
                COALESCE(SUM({TOTAL} >= ?), 0)               AS reached_goal,
                COALESCE(SUM(rewards_claimed), 0)            AS rewards,
                COALESCE(SUM(last_seen >= ?), 0)             AS active24,
                COALESCE(SUM(last_seen >= ?), 0)             AS active7,
                COALESCE(SUM(is_blocked), 0)                 AS blocked,
                COALESCE(SUM(is_banned), 0)                  AS banned
            FROM users WHERE ad_link_id = ?
            """,
            day_start, goal, ts - 86400, ts - 7 * 86400, link_id,
        )
        clicks = await self.one(
            """
            SELECT
                COUNT(*)                                             AS clicks,
                COUNT(DISTINCT user_id)                              AS unique_clicks,
                COUNT(DISTINCT user_id)
                  - COUNT(DISTINCT CASE WHEN is_new = 1 THEN user_id END) AS returning_users,
                COALESCE(SUM(created_at >= ?), 0)                    AS clicks_today,
                MIN(created_at)                                      AS first_click,
                MAX(created_at)                                      AS last_click
            FROM ad_clicks WHERE link_id = ?
            """,
            day_start, link_id,
        )
        return {**dict(users or {}), **dict(clicks or {})}

    async def ad_link_daily(self, link_id: int, since: int) -> tuple[list[int], list[int]]:
        """Метки времени новых пользователей и переходов по ссылке начиная с since."""
        new = await self.all("SELECT created_at FROM users WHERE ad_link_id = ? AND created_at >= ?", link_id, since)
        clicks = await self.all("SELECT created_at FROM ad_clicks WHERE link_id = ? AND created_at >= ?",
                                link_id, since)
        return [r[0] for r in new], [r[0] for r in clicks]

    # ---------- чеки ----------
    async def create_check(self, code: str, total: int, caption: str | None, with_photo: bool,
                           created_by: int, gift_id: str, gift_emoji: str, gift_price: int) -> int:
        cur = await self.conn.execute(
            "INSERT INTO checks (code, total, caption, with_photo, created_by, created_at, gift_id, gift_emoji, "
            "gift_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (code, total, caption, int(with_photo), created_by, now(), gift_id, gift_emoji, gift_price),
        )
        await self.conn.commit()
        return cur.lastrowid or 0

    async def find_draft_check(self, created_by: int, total: int, caption: str | None, with_photo: bool,
                               gift_id: str, max_age: int = 600) -> aiosqlite.Row | None:
        """Неотправленный чек с теми же параметрами — чтобы не плодить черновики на каждое нажатие клавиши."""
        return await self.one(
            "SELECT * FROM checks WHERE created_by = ? AND is_sent = 0 AND total = ? AND caption IS ? "
            "AND with_photo = ? AND gift_id = ? AND created_at >= ? "
            "AND NOT EXISTS (SELECT 1 FROM check_activations a WHERE a.check_id = checks.id) "
            "ORDER BY id DESC LIMIT 1",
            created_by, total, caption, int(with_photo), gift_id, now() - max_age,
        )

    async def cleanup_check_drafts(self, max_age: int = 86400) -> int:
        """Удаляет старые неотправленные черновики — только если отметка «отправлен» точно работает
        (в @BotFather включён /setinlinefeedback и хотя бы один чек уже отмечен отправленным)."""
        return await self.run(
            "DELETE FROM checks WHERE is_sent = 0 AND created_at < ? "
            "AND NOT EXISTS (SELECT 1 FROM check_activations a WHERE a.check_id = checks.id) "
            "AND EXISTS (SELECT 1 FROM checks s WHERE s.is_sent = 1)",
            now() - max_age,
        )

    async def get_check(self, check_id: int) -> aiosqlite.Row | None:
        return await self.one(
            "SELECT c.*, (SELECT COUNT(*) FROM check_activations a WHERE a.check_id = c.id) AS used "
            "FROM checks c WHERE c.id = ?", check_id,
        )

    async def get_check_by_code(self, code: str) -> aiosqlite.Row | None:
        return await self.one(
            "SELECT c.*, (SELECT COUNT(*) FROM check_activations a WHERE a.check_id = c.id) AS used "
            "FROM checks c WHERE c.code = ?", code,
        )

    VISIBLE_CHECKS = "(c.is_sent = 1 OR EXISTS (SELECT 1 FROM check_activations a WHERE a.check_id = c.id))"

    async def list_checks(self, limit: int, offset: int) -> list[aiosqlite.Row]:
        return await self.all(
            "SELECT c.*, (SELECT COUNT(*) FROM check_activations a WHERE a.check_id = c.id) AS used "
            f"FROM checks c WHERE {self.VISIBLE_CHECKS} ORDER BY c.id DESC LIMIT ? OFFSET ?",
            limit, offset,
        )

    async def count_checks(self) -> int:
        return await self.val(f"SELECT COUNT(*) FROM checks c WHERE {self.VISIBLE_CHECKS}")

    async def mark_check_sent(self, check_id: int, inline_message_id: str | None) -> None:
        await self.run(
            "UPDATE checks SET is_sent = 1, inline_message_id = COALESCE(?, inline_message_id) WHERE id = ?",
            inline_message_id, check_id,
        )

    async def set_check_active(self, check_id: int, active: bool) -> None:
        await self.run("UPDATE checks SET is_active = ? WHERE id = ?", int(active), check_id)

    async def delete_check(self, check_id: int) -> None:
        await self.conn.execute("DELETE FROM check_activations WHERE check_id = ?", (check_id,))
        await self.conn.execute("UPDATE users SET source_check_id = NULL WHERE source_check_id = ?", (check_id,))
        await self.conn.execute("DELETE FROM checks WHERE id = ?", (check_id,))
        await self.conn.commit()

    async def try_activate_check(self, check_id: int, user_id: int) -> bool:
        """Одним атомарным запросом: чек активен, лимит не исчерпан, пользователь ещё не активировал."""
        return bool(await self.run(
            "INSERT OR IGNORE INTO check_activations (check_id, user_id, created_at) "
            "SELECT ?, ?, ? FROM checks c WHERE c.id = ? AND c.is_active = 1 "
            "AND (SELECT COUNT(*) FROM check_activations a WHERE a.check_id = c.id) < c.total",
            check_id, user_id, now(), check_id,
        ))

    async def has_activated(self, check_id: int, user_id: int) -> bool:
        return bool(await self.val(
            "SELECT 1 FROM check_activations WHERE check_id = ? AND user_id = ?", check_id, user_id
        ))

    async def check_activations(self, check_id: int, limit: int, offset: int) -> list[aiosqlite.Row]:
        return await self.all(
            "SELECT a.*, u.full_name, u.username, "
            "  (SELECT status FROM claims cl WHERE cl.check_id = a.check_id AND cl.user_id = a.user_id "
            "   ORDER BY cl.id DESC LIMIT 1) AS gift_status "
            "FROM check_activations a LEFT JOIN users u USING(user_id) "
            "WHERE a.check_id = ? ORDER BY a.created_at DESC LIMIT ? OFFSET ?",
            check_id, limit, offset,
        )

    async def check_stats(self, check_id: int) -> dict[str, int]:
        gifts = await self.one(
            "SELECT COALESCE(SUM(status = 'sent'), 0) AS sent, COALESCE(SUM(status = 'pending'), 0) AS pending, "
            "COALESCE(SUM(status = 'rejected'), 0) AS rejected FROM claims WHERE check_id = ?", check_id,
        )
        times = await self.one(
            "SELECT MIN(created_at) AS first_at, MAX(created_at) AS last_at FROM check_activations WHERE check_id = ?",
            check_id,
        )
        new_users = await self.val("SELECT COUNT(*) FROM users WHERE source_check_id = ?", check_id)
        return {**dict(gifts or {}), **dict(times or {}), "new_users": new_users}

    async def get_gift_image(self, base_file_id: str, gift_id: str) -> str | None:
        return await self.val("SELECT file_id FROM gift_images WHERE base_file_id = ? AND gift_id = ?",
                              base_file_id, gift_id, default=None)

    async def save_gift_image(self, base_file_id: str, gift_id: str, file_id: str) -> None:
        await self.run("INSERT OR REPLACE INTO gift_images (base_file_id, gift_id, file_id) VALUES (?, ?, ?)",
                       base_file_id, gift_id, file_id)

    async def get_gift_banner(self, gift_id: str) -> str | None:
        return await self.val("SELECT file_id FROM gift_banners WHERE gift_id = ?", gift_id, default=None)

    async def set_gift_banner(self, gift_id: str, file_id: str, emoji: str, price: int) -> None:
        await self.run(
            "INSERT OR REPLACE INTO gift_banners (gift_id, file_id, gift_emoji, gift_price, updated_at) "
            "VALUES (?, ?, ?, ?, ?)", gift_id, file_id, emoji, price, now(),
        )

    async def delete_gift_banner(self, gift_id: str) -> None:
        await self.run("DELETE FROM gift_banners WHERE gift_id = ?", gift_id)

    async def gift_banners(self) -> list[aiosqlite.Row]:
        return await self.all("SELECT * FROM gift_banners ORDER BY gift_price, gift_id")

    async def set_pending_check(self, user_id: int, code: str | None) -> None:
        await self.run("UPDATE users SET pending_check = ? WHERE user_id = ?", code, user_id)

    async def set_source_check(self, user_id: int, check_id: int) -> None:
        await self.run("UPDATE users SET source_check_id = ? WHERE user_id = ? AND source_check_id IS NULL",
                       check_id, user_id)

    # ---------- напоминания ----------
    async def schedule_first_reminder(self, user_id: int, at: int) -> bool:
        """Ставит первое напоминание, только если цепочка для пользователя ещё не запускалась."""
        return bool(await self.run(
            "UPDATE users SET remind_at = ? WHERE user_id = ? AND verified_at IS NULL "
            "AND remind_step = 0 AND remind_at IS NULL",
            at, user_id,
        ))

    async def due_reminders(self, ts: int, limit: int) -> list[aiosqlite.Row]:
        return await self.all(
            "SELECT * FROM users WHERE remind_at IS NOT NULL AND remind_at <= ? "
            "AND verified_at IS NULL AND is_banned = 0 AND is_blocked = 0 ORDER BY remind_at LIMIT ?",
            ts, limit,
        )

    async def reminder_sent(self, user_id: int, step: int, next_at: int | None) -> None:
        await self.conn.execute("UPDATE users SET remind_step = ?, remind_at = ? WHERE user_id = ?",
                                (step, next_at, user_id))
        await self.conn.execute("INSERT INTO reminder_log (user_id, step, sent_at) VALUES (?, ?, ?)",
                                (user_id, step, now()))
        await self.conn.commit()

    async def cancel_reminder(self, user_id: int) -> None:
        await self.run("UPDATE users SET remind_at = NULL WHERE user_id = ?", user_id)

    async def reminder_stats(self) -> dict[str, object]:
        queued = await self.val(
            "SELECT COUNT(*) FROM users WHERE remind_at IS NOT NULL AND verified_at IS NULL "
            "AND is_banned = 0 AND is_blocked = 0"
        )
        sent = {r[0]: r[1] for r in await self.all("SELECT step, COUNT(*) FROM reminder_log GROUP BY step")}
        # Прошли подписку после последнего полученного напоминания — по номеру этого напоминания.
        converted = {r[0]: r[1] for r in await self.all(
            "SELECT r.step, COUNT(*) FROM (SELECT user_id, MAX(step) AS step, MAX(sent_at) AS last "
            "FROM reminder_log GROUP BY user_id) r JOIN users u USING(user_id) "
            "WHERE u.verified_at IS NOT NULL AND u.verified_at >= r.last GROUP BY r.step"
        )}
        reached = await self.val("SELECT COUNT(DISTINCT user_id) FROM reminder_log")
        return {"queued": queued, "sent": sent, "converted": converted, "reached": reached}

    # ---------- settings ----------
    async def load_settings(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in await self.all("SELECT key, value FROM settings")}

    async def set_setting(self, key: str, value: str) -> None:
        await self.run(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            key, value,
        )

    async def delete_setting(self, key: str) -> None:
        await self.run("DELETE FROM settings WHERE key = ?", key)
