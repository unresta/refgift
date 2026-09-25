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
    last_seen       INTEGER NOT NULL
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
    processed_by INTEGER
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
            "UPDATE users SET verified_at = ? WHERE user_id = ? AND verified_at IS NULL", now(), user_id
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

    async def export_users(self) -> list[aiosqlite.Row]:
        return await self.all(f"SELECT *, {TOTAL} AS total FROM users ORDER BY created_at")

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
                COALESCE(SUM(referrer_id IS NOT NULL AND created_at >= ?), 0) AS invited_today
            FROM users
            """,
            day_start, ts - 7 * 86400, ts - 30 * 86400, ts - 86400, goal, day_start,
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
                           error: str | None = None, processed_by: int | None = None) -> int:
        ts = now()
        cur = await self.conn.execute(
            "INSERT INTO claims (user_id, status, method, gift_id, error, created_at, processed_at, processed_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, status, method, gift_id, error, ts, ts if status != "pending" else None, processed_by),
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
