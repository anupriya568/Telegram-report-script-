import asyncio
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DEFAULT_INTRO_TEXT: str = (
    "🛡️ <b>MODGUARD — Group & VC Guard</b>\n\n"
    "Aapke group aur Voice Chat ko surakshit rakhne ke liye moderation assistant.\n\n"
    "• <b>VC Camera Protection:</b> VC mein non-admin ka camera on hote hi group se remove.\n"
    "• <b>Admin Safe:</b> Group admins aur creator par koi pabandi nahi.\n\n"
    "Neeche diye gaye button se bot ko apne group mein jodein ya <b>Features</b> aur <b>How to Use</b> dekhein."
)

DEFAULT_FEATURES_TEXT: str = (
    "🛡️ <b>MODGUARD Features</b>\n\n"
    "• <b>VC Camera Auto-Remove:</b> Non-admin user VC mein camera on kare toh turant group se remove.\n"
    "• <b>Admin & Creator Safe:</b> Group admins aur creator par koi rule apply nahi hota.\n"
    "• <b>Multi-VC Assistant:</b> VC start hote hi assistant join karta hai aur call khatam hone tak bana rehta hai."
)

DEFAULT_HOW_TO_USE_TEXT: str = (
    "📖 <b>How to Use MODGUARD</b>\n\n"
    "1. Neeche <b>➕ Add to Your Group</b> button dabayein aur apna group chunein.\n"
    "2. Bot ko group mein <b>Admin</b> banayein aur <b>Delete Messages</b>, <b>Restrict / Ban Users</b>, aur <b>Invite Users via Link</b> permissions dein.\n"
    "3. Group mein <code>/start</code> karein — aapko <code>ModGuard is now activated in your group.</code> dikhega.\n"
    "4. Bot invite link se assistant ko group mein join karwayega (agar assistant na aa paye toh use manually group mein add karein)."
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_channel_chat_id(channel_id: int) -> int:
    s = str(channel_id)
    if s.startswith("-100"):
        return int(s)
    return int(f"-100{abs(channel_id)}")


def extract_invite_hash(invite_link: str) -> Optional[str]:
    link = (invite_link or "").strip()
    if not link:
        return None
    m = re.search(r"(?:t\.me/\+|telegram\.me/\+|joinchat/)([A-Za-z0-9_-]+)", link)
    return m.group(1) if m else None


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    async def connect(self) -> None:
        async with self._lock:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._create_tables()
            self._seed_defaults()
            self._conn.commit()

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                    self._conn.commit()
                except Exception:
                    pass
                self._conn.close()
                self._conn = None

    async def flush_for_backup(self) -> str:
        async with self._lock:
            if self._conn is not None:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                self._conn.commit()
        return self.db_path

    def _create_tables(self) -> None:
        assert self._conn is not None
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_seen TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS groups_info (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                is_bot_admin INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS group_settings (
                chat_id INTEGER PRIMARY KEY,
                auto_camera_mute INTEGER NOT NULL DEFAULT 1,
                auto_vc_join INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (chat_id) REFERENCES groups_info(chat_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS moderation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                chat_title TEXT NOT NULL,
                user_id INTEGER,
                username TEXT,
                action_type TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notification_preferences (
                key TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                description TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS broadcast_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                message_text TEXT NOT NULL,
                target_scope TEXT NOT NULL,
                sent_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

    def _seed_defaults(self) -> None:
        assert self._conn is not None
        prefs = [
            ("vc_join_failure", 1, "Assistant VC join retries fail hone par owner ko alert"),
            ("bot_lost_admin", 1, "Main Bot ka admin role hatne par owner ko alert"),
            ("critical_error", 1, "Critical technical errors par owner ko alert"),
            ("system_failure", 1, "Important system failures par owner ko alert"),
        ]
        for key, enabled, desc in prefs:
            self._conn.execute(
                "INSERT OR IGNORE INTO notification_preferences (key, enabled, description) VALUES (?, ?, ?)",
                (key, enabled, desc),
            )
        self._conn.execute(
            "INSERT OR IGNORE INTO bot_config (key, value) VALUES ('maintenance_mode', '0')"
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO bot_config (key, value) VALUES ('intro_caption', ?)",
            (DEFAULT_INTRO_TEXT,),
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO bot_config (key, value) VALUES ('features_caption', ?)",
            (DEFAULT_FEATURES_TEXT,),
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO bot_config (key, value) VALUES ('how_to_use_caption', ?)",
            (DEFAULT_HOW_TO_USE_TEXT,),
        )

    async def _run(self, fn, *args, **kwargs) -> Any:
        async with self._lock:
            if self._conn is None:
                raise RuntimeError("Database connection is closed.")
            res = fn(self._conn, *args, **kwargs)
            self._conn.commit()
            return res

    async def get_config_text(self, key: str, fallback: str) -> str:
        def _op(conn: sqlite3.Connection):
            row = conn.execute("SELECT value FROM bot_config WHERE key = ?", (key,)).fetchone()
            if row and str(row["value"]).strip():
                return str(row["value"])
            return fallback

        return await self._run(_op)

    async def set_config_text(self, key: str, value_html: str) -> None:
        cleaned = value_html.strip()
        if not cleaned:
            raise ValueError("Text cannot be empty.")

        def _op(conn: sqlite3.Connection):
            conn.execute(
                """
                INSERT INTO bot_config (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, cleaned),
            )

        await self._run(_op)

    async def get_intro_caption(self) -> str:
        return await self.get_config_text("intro_caption", DEFAULT_INTRO_TEXT)

    async def set_intro_caption(self, caption_html: str) -> None:
        await self.set_config_text("intro_caption", caption_html)

    async def reset_intro_caption(self) -> None:
        await self.set_config_text("intro_caption", DEFAULT_INTRO_TEXT)

    async def get_features_caption(self) -> str:
        return await self.get_config_text("features_caption", DEFAULT_FEATURES_TEXT)

    async def set_features_caption(self, caption_html: str) -> None:
        await self.set_config_text("features_caption", caption_html)

    async def reset_features_caption(self) -> None:
        await self.set_config_text("features_caption", DEFAULT_FEATURES_TEXT)

    async def get_how_to_use_caption(self) -> str:
        return await self.get_config_text("how_to_use_caption", DEFAULT_HOW_TO_USE_TEXT)

    async def set_how_to_use_caption(self, caption_html: str) -> None:
        await self.set_config_text("how_to_use_caption", caption_html)

    async def reset_how_to_use_caption(self) -> None:
        await self.set_config_text("how_to_use_caption", DEFAULT_HOW_TO_USE_TEXT)

    async def upsert_user(self, user_id: int, username: Optional[str], first_name: Optional[str]) -> None:
        def _op(conn: sqlite3.Connection):
            conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_seen = excluded.last_seen
                """,
                (user_id, username or "", first_name or "", utc_now_iso()),
            )

        await self._run(_op)

    async def count_users(self) -> int:
        def _op(conn: sqlite3.Connection):
            row = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()
            return int(row["cnt"]) if row else 0

        return await self._run(_op)

    async def get_all_user_ids(self) -> List[int]:
        def _op(conn: sqlite3.Connection):
            rows = conn.execute("SELECT user_id FROM users").fetchall()
            return [int(r["user_id"]) for r in rows]

        return await self._run(_op)

    async def upsert_group(self, chat_id: int, title: str, is_bot_admin: bool) -> None:
        def _op(conn: sqlite3.Connection):
            conn.execute(
                """
                INSERT INTO groups_info (chat_id, title, is_bot_admin, active, updated_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = excluded.title,
                    is_bot_admin = excluded.is_bot_admin,
                    active = 1,
                    updated_at = excluded.updated_at
                """,
                (chat_id, title, 1 if is_bot_admin else 0, utc_now_iso()),
            )
            conn.execute(
                "INSERT OR IGNORE INTO group_settings (chat_id) VALUES (?)",
                (chat_id,),
            )

        await self._run(_op)

    async def mark_group_inactive(self, chat_id: int) -> None:
        def _op(conn: sqlite3.Connection):
            conn.execute(
                "UPDATE groups_info SET active = 0, updated_at = ? WHERE chat_id = ?",
                (utc_now_iso(), chat_id),
            )

        await self._run(_op)

    async def set_group_admin_status(self, chat_id: int, is_bot_admin: bool) -> None:
        def _op(conn: sqlite3.Connection):
            conn.execute(
                "UPDATE groups_info SET is_bot_admin = ?, updated_at = ? WHERE chat_id = ?",
                (1 if is_bot_admin else 0, utc_now_iso(), chat_id),
            )

        await self._run(_op)

    async def get_group(self, chat_id: int) -> Optional[Dict[str, Any]]:
        def _op(conn: sqlite3.Connection):
            row = conn.execute("SELECT * FROM groups_info WHERE chat_id = ?", (chat_id,)).fetchone()
            return dict(row) if row else None

        return await self._run(_op)

    async def count_groups(self) -> int:
        def _op(conn: sqlite3.Connection):
            row = conn.execute("SELECT COUNT(*) AS cnt FROM groups_info WHERE active = 1").fetchone()
            return int(row["cnt"]) if row else 0

        return await self._run(_op)

    async def get_all_group_ids(self) -> List[int]:
        def _op(conn: sqlite3.Connection):
            rows = conn.execute("SELECT chat_id FROM groups_info WHERE active = 1").fetchall()
            return [int(r["chat_id"]) for r in rows]

        return await self._run(_op)

    async def get_active_groups(self) -> List[Dict[str, Any]]:
        def _op(conn: sqlite3.Connection):
            rows = conn.execute("SELECT * FROM groups_info WHERE active = 1 ORDER BY title ASC").fetchall()
            return [dict(r) for r in rows]

        return await self._run(_op)

    async def get_group_settings(self, chat_id: int) -> Dict[str, Any]:
        def _op(conn: sqlite3.Connection):
            conn.execute("INSERT OR IGNORE INTO group_settings (chat_id) VALUES (?)", (chat_id,))
            row = conn.execute("SELECT * FROM group_settings WHERE chat_id = ?", (chat_id,)).fetchone()
            return (
                dict(row)
                if row
                else {
                    "chat_id": chat_id,
                    "auto_camera_mute": 1,
                    "auto_vc_join": 1,
                }
            )

        return await self._run(_op)

    async def add_moderation_log(
        self,
        chat_id: int,
        chat_title: str,
        user_id: Optional[int],
        username: Optional[str],
        action_type: str,
        reason: str,
    ) -> int:
        def _op(conn: sqlite3.Connection):
            cur = conn.execute(
                """
                INSERT INTO moderation_logs
                    (chat_id, chat_title, user_id, username, action_type, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (chat_id, chat_title, user_id, username or "", action_type, reason, utc_now_iso()),
            )
            return int(cur.lastrowid)

        return await self._run(_op)

    async def get_recent_logs(self, limit: int = 10) -> List[Dict[str, Any]]:
        def _op(conn: sqlite3.Connection):
            rows = conn.execute(
                "SELECT * FROM moderation_logs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._run(_op)

    async def is_notification_enabled(self, key: str) -> bool:
        def _op(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT enabled FROM notification_preferences WHERE key = ?",
                (key,),
            ).fetchone()
            return bool(row["enabled"]) if row else True

        return await self._run(_op)

    async def set_notification_pref(self, key: str, enabled: bool) -> None:
        def _op(conn: sqlite3.Connection):
            conn.execute(
                "UPDATE notification_preferences SET enabled = ? WHERE key = ?",
                (1 if enabled else 0, key),
            )

        await self._run(_op)

    async def get_notification_prefs(self) -> List[Dict[str, Any]]:
        def _op(conn: sqlite3.Connection):
            rows = conn.execute("SELECT * FROM notification_preferences ORDER BY key ASC").fetchall()
            return [dict(r) for r in rows]

        return await self._run(_op)

    async def record_broadcast(
        self,
        category: str,
        message_text: str,
        target_scope: str,
        sent_count: int,
        failed_count: int,
    ) -> int:
        def _op(conn: sqlite3.Connection):
            cur = conn.execute(
                """
                INSERT INTO broadcast_records
                    (category, message_text, target_scope, sent_count, failed_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (category, message_text, target_scope, sent_count, failed_count, utc_now_iso()),
            )
            return int(cur.lastrowid)

        return await self._run(_op)

    async def is_maintenance_mode(self) -> bool:
        def _op(conn: sqlite3.Connection):
            row = conn.execute("SELECT value FROM bot_config WHERE key = 'maintenance_mode'").fetchone()
            return bool(row and str(row["value"]) == "1")

        return await self._run(_op)

    async def set_maintenance_mode(self, enabled: bool) -> None:
        def _op(conn: sqlite3.Connection):
            conn.execute(
                """
                INSERT INTO bot_config (key, value) VALUES ('maintenance_mode', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("1" if enabled else "0",),
            )

        await self._run(_op)
