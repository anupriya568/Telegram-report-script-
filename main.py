import asyncio
import json
import logging
import re
import signal
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from telethon import TelegramClient, events
from telethon.errors import RPCError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.functions.phone import JoinGroupCallRequest, LeaveGroupCallRequest
from telethon.tl.types import (
    Channel,
    Chat,
    DataJSON,
    GroupCall,
    GroupCallDiscarded,
    InputGroupCall,
    UpdateGroupCall,
    UpdateGroupCallParticipants,
)
from telegram import Bot, ChatPermissions, Message, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config as cfg

logging.basicConfig(
    level=getattr(logging, getattr(cfg, "LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("modguard")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_channel_chat_id(channel_id: int) -> int:
    s = str(channel_id)
    if s.startswith("-100"):
        return int(s)
    return int(f"-100{abs(channel_id)}")


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
                self._conn.close()
                self._conn = None

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
                auto_abuse_mute INTEGER NOT NULL DEFAULT 1,
                auto_delete_abuse INTEGER NOT NULL DEFAULT 1,
                auto_camera_mute INTEGER NOT NULL DEFAULT 1,
                auto_vc_join INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (chat_id) REFERENCES groups_info(chat_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS custom_badwords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                word TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT 'multi',
                added_by INTEGER,
                created_at TEXT NOT NULL,
                UNIQUE(chat_id, word),
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

    async def _run(self, fn, *args, **kwargs) -> Any:
        async with self._lock:
            if self._conn is None:
                raise RuntimeError("Database connection is closed.")
            res = fn(self._conn, *args, **kwargs)
            self._conn.commit()
            return res

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

    async def get_group_settings(self, chat_id: int) -> Dict[str, Any]:
        def _op(conn: sqlite3.Connection):
            conn.execute("INSERT OR IGNORE INTO group_settings (chat_id) VALUES (?)", (chat_id,))
            row = conn.execute("SELECT * FROM group_settings WHERE chat_id = ?", (chat_id,)).fetchone()
            return (
                dict(row)
                if row
                else {
                    "chat_id": chat_id,
                    "auto_abuse_mute": 1,
                    "auto_delete_abuse": 1,
                    "auto_camera_mute": 1,
                    "auto_vc_join": 1,
                }
            )

        return await self._run(_op)

    async def add_badword(
        self, chat_id: int, word: str, language: str = "multi", added_by: Optional[int] = None
    ) -> bool:
        normalized = word.strip().lower()
        if not normalized:
            return False

        def _op(conn: sqlite3.Connection):
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO custom_badwords (chat_id, word, language, added_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, normalized, language, added_by, utc_now_iso()),
            )
            return cur.rowcount > 0

        return await self._run(_op)

    async def remove_badword(self, chat_id: int, word: str) -> bool:
        normalized = word.strip().lower()

        def _op(conn: sqlite3.Connection):
            cur = conn.execute(
                "DELETE FROM custom_badwords WHERE chat_id = ? AND word = ?",
                (chat_id, normalized),
            )
            return cur.rowcount > 0

        return await self._run(_op)

    async def list_badwords(self, chat_id: int) -> List[str]:
        def _op(conn: sqlite3.Connection):
            rows = conn.execute(
                "SELECT word FROM custom_badwords WHERE chat_id = ? ORDER BY word ASC",
                (chat_id,),
            ).fetchall()
            return [str(r["word"]) for r in rows]

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


DEFAULT_BADWORDS: List[str] = [
    "idiot",
    "moron",
    "stupid",
    "dumb",
    "bastard",
    "asshole",
    "चूतिया",
    "भड़वा",
    "हरामी",
    "कमीना",
    "बेवकूफ",
    "chutiya",
    "bhosdike",
    "harami",
    "kamina",
    "bewakoof",
]


def normalize_text(text: str) -> str:
    if not text:
        return ""
    norm = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"\s+", " ", norm).strip()


def detect_abuse(text: Optional[str], badwords: Iterable[str]) -> Optional[List[str]]:
    normalized = normalize_text(text or "")
    if not normalized:
        return None

    matches: List[str] = []
    for raw_word in badwords:
        word = normalize_text(raw_word)
        if not word:
            continue
        escaped = re.escape(word)
        if re.fullmatch(r"[a-z0-9_ ]+", word):
            pattern = rf"(?<!\w){escaped}(?!\w)"
        else:
            pattern = rf"(?<![^\s]){escaped}(?![^\s])"
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            matches.append(word)

    return sorted(set(matches)) if matches else None
        class ModerationEngine:
    def __init__(self, bot: Bot, db: Database, notify_owner_cb=None) -> None:
        self.bot = bot
        self.db = db
        self.notify_owner_cb = notify_owner_cb
        self._recent_actions: Dict[Tuple[int, Optional[int], str, str], float] = {}
        self._lost_admin_notified: set[int] = set()
        self._lock = asyncio.Lock()

    async def _is_duplicate(
        self, chat_id: int, user_id: Optional[int], action_type: str, dedup_key: str, ttl: float = 25.0
    ) -> bool:
        async with self._lock:
            now = time.monotonic()
            for k, ts in [k_ts for k_ts in self._recent_actions.items() if now - k_ts[1] > ttl]:
                del self._recent_actions[k]
            full_key = (chat_id, user_id, action_type, dedup_key)
            if full_key in self._recent_actions:
                return True
            self._recent_actions[full_key] = now
            return False

    async def ensure_bot_is_admin(self, chat_id: int, chat_title: str) -> bool:
        try:
            me = await self.bot.get_me()
            member = await self.bot.get_chat_member(chat_id=chat_id, user_id=me.id)
            is_admin = member.status in ("administrator", "creator")
        except (Forbidden, BadRequest):
            is_admin = False
        except TelegramError as exc:
            logger.error("Admin check error in %s: %s", chat_id, exc)
            return False

        await self.db.upsert_group(chat_id, chat_title, is_admin)

        if is_admin:
            self._lost_admin_notified.discard(chat_id)
            return True

        await self.db.set_group_admin_status(chat_id, False)
        if chat_id not in self._lost_admin_notified:
            self._lost_admin_notified.add(chat_id)
            if self.notify_owner_cb:
                await self.notify_owner_cb(
                    "bot_lost_admin",
                    f"⚠️ <b>MODGUARD Admin Permission Lost</b>\nGroup: <b>{chat_title}</b> (<code>{chat_id}</code>)",
                )
        return False

    async def delete_message_safe(self, message: Message, chat_title: str, reason: str) -> bool:
        chat_id = message.chat_id
        user_id = message.from_user.id if message.from_user else None
        username = message.from_user.username if message.from_user else None
        if await self._is_duplicate(chat_id, user_id, "delete_message", str(message.message_id)):
            return False
        try:
            await self.bot.delete_message(chat_id=chat_id, message_id=message.message_id)
            await self.db.add_moderation_log(chat_id, chat_title, user_id, username, "DELETE_MESSAGE", reason)
            return True
        except (Forbidden, BadRequest, TelegramError) as exc:
            logger.warning("Delete failed in %s: %s", chat_id, exc)
            return False

    async def restrict_user_permanent(
        self,
        chat_id: int,
        chat_title: str,
        user_id: int,
        username: Optional[str],
        action_type: str,
        reason: str,
    ) -> bool:
        bucket = str(int(time.time() // 20))
        if await self._is_duplicate(chat_id, user_id, action_type, bucket, ttl=20.0):
            return False
        try:
            member = await self.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in ("administrator", "creator"):
                logger.info("User %s is admin/creator in %s; skipping mute.", user_id, chat_id)
                return False

            perms = ChatPermissions(
                can_send_messages=False,
                can_send_audios=False,
                can_send_documents=False,
                can_send_photos=False,
                can_send_videos=False,
                can_send_video_notes=False,
                can_send_voice_notes=False,
                can_send_polls=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
                can_invite_users=False,
                can_change_info=False,
                can_pin_messages=False,
                can_manage_topics=False,
            )
            await self.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=perms,
                until_date=None,
            )
            await self.db.add_moderation_log(
                chat_id,
                chat_title,
                user_id,
                username,
                action_type,
                f"{reason} (permanent mute until admin unmutes)",
            )
            logger.info("PERMANENT MUTED user %s in '%s' (%s) | reason: %s", user_id, chat_title, chat_id, reason)
            return True
        except (Forbidden, BadRequest, TelegramError) as exc:
            logger.warning("Permanent mute failed for %s in %s: %s", user_id, chat_id, exc)
            return False


class AssistantVCController:
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session_string: str,
        db: Database,
        moderation: ModerationEngine,
        max_retries: int = 3,
        retry_delay: int = 5,
        notify_owner_cb=None,
    ) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_string = session_string
        self.db = db
        self.moderation = moderation
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.notify_owner_cb = notify_owner_cb

        self.client: Optional[TelegramClient] = None
        self._active_joins: Dict[int, int] = {}
        self._known_calls: Dict[int, int] = {}
        self._join_locks: Dict[int, asyncio.Lock] = {}
        self._muted_in_call: Dict[Tuple[int, int], bool] = {}

    async def start(self) -> None:
        cleaned = (self.session_string or "").strip()
        if not cleaned:
            logger.warning("VC_DEBUG: SESSION_STRING empty; Assistant VC monitor disabled.")
            return
        try:
            self.client = TelegramClient(StringSession(cleaned), self.api_id, self.api_hash)
            await self.client.connect()
            if not await self.client.is_user_authorized():
                logger.error("VC_DEBUG: SESSION_STRING unauthorized / invalid session.")
                await self.client.disconnect()
                self.client = None
                return
            me = await self.client.get_me()
            self.client.add_event_handler(self._on_raw_update, events.Raw)
            logger.info("VC_DEBUG: Assistant connected as id=%s username=@%s", me.id, me.username or "")
            asyncio.create_task(self._scan_existing_calls())
        except Exception as exc:
            logger.error("VC_DEBUG: Assistant start failed (check SESSION_STRING): %s", exc)
            self.client = None

    async def _scan_existing_calls(self) -> None:
        if not self.client or not self.client.is_connected():
            return
        try:
            async for dialog in self.client.iter_dialogs(limit=50):
                ent = dialog.entity
                title = getattr(ent, "title", None) or str(dialog.id)
                if isinstance(ent, Channel) and getattr(ent, "megagroup", False):
                    chat_id = normalize_channel_chat_id(ent.id)
                    try:
                        full = await self.client(GetFullChannelRequest(ent))
                        call = getattr(full.full_chat, "call", None)
                        if isinstance(call, GroupCall):
                            logger.info("VC_DEBUG: Found active VC in '%s' (%s), joining...", title, chat_id)
                            await self.join_vc(chat_id, title, int(call.id), int(call.access_hash))
                    except Exception:
                        pass
                elif isinstance(ent, Chat):
                    chat_id = -abs(int(ent.id))
                    try:
                        full = await self.client(GetFullChatRequest(ent.id))
                        call = getattr(full.full_chat, "call", None)
                        if isinstance(call, GroupCall):
                            logger.info("VC_DEBUG: Found active chat VC in '%s' (%s), joining...", title, chat_id)
                            await self.join_vc(chat_id, title, int(call.id), int(call.access_hash))
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning("VC_DEBUG: Dialog VC scan error: %s", exc)

    async def stop(self) -> None:
        for chat_id in list(self._active_joins.keys()):
            group = await self.db.get_group(chat_id)
            await self.leave_vc(chat_id, group["title"] if group else str(chat_id))
        if self.client and self.client.is_connected():
            await self.client.disconnect()

    async def join_vc(self, chat_id: int, chat_title: str, call_id: int, access_hash: int) -> bool:
        if not self.client or not self.client.is_connected():
            logger.warning("VC_DEBUG: Assistant client not connected; cannot join VC in %s", chat_id)
            return False
        if not await self.moderation.ensure_bot_is_admin(chat_id, chat_title):
            logger.warning("VC_DEBUG: Main Bot is not admin in '%s' (%s); skipping VC join.", chat_title, chat_id)
            return False
        settings = await self.db.get_group_settings(chat_id)
        if not settings.get("auto_vc_join", 1):
            logger.info("VC_DEBUG: auto_vc_join disabled in '%s' (%s).", chat_title, chat_id)
            return False

        lock = self._join_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            if self._active_joins.get(chat_id) == call_id:
                return True

            self._known_calls[call_id] = chat_id
            last_err = ""
            silent_params = DataJSON(data=json.dumps({"ufrag": "", "pwd": "", "fingerprints": [], "ssrc": 0}))

            for attempt in range(1, self.max_retries + 1):
                try:
                    await self.client(
                        JoinGroupCallRequest(
                            call=InputGroupCall(id=call_id, access_hash=access_hash),
                            join_as=await self.client.get_input_entity("me"),
                            params=silent_params,
                            muted=True,
                            video_stopped=True,
                        )
                    )
                    self._active_joins[chat_id] = call_id
                    logger.info(
                        "VC_DEBUG: Assistant silently joined VC in '%s' (%s) on attempt %d.",
                        chat_title,
                        chat_id,
                        attempt,
                    )
                    return True
                except (RPCError, Exception) as exc:
                    last_err = str(exc)
                    logger.warning(
                        "VC_DEBUG: VC join attempt %d/%d failed in '%s': %s",
                        attempt,
                        self.max_retries,
                        chat_title,
                        last_err,
                    )
                    if attempt < self.max_retries:
                        await asyncio.sleep(self.retry_delay)

            await self.db.add_moderation_log(
                chat_id,
                chat_title,
                None,
                None,
                "VC_JOIN_FAILED",
                f"Failed after {self.max_retries} retries: {last_err}",
            )
            if self.notify_owner_cb:
                await self.notify_owner_cb(
                    "vc_join_failure",
                    f"❌ <b>Assistant VC Join Failed</b>\nGroup: <b>{chat_title}</b>\nError: <code>{last_err}</code>",
                )
            return False

    async def leave_vc(self, chat_id: int, chat_title: str, call_id: Optional[int] = None) -> None:
        active_id = call_id or self._active_joins.get(chat_id)
        if not active_id:
            return
        try:
            if self.client and self.client.is_connected():
                await self.client(LeaveGroupCallRequest(call=InputGroupCall(id=active_id, access_hash=0), source=0))
            logger.info("VC_DEBUG: Assistant left VC in '%s' (%s).", chat_title, chat_id)
        except RPCError as exc:
            logger.warning("VC_DEBUG: Leave VC error in %s: %s", chat_id, exc)
        finally:
            self._active_joins.pop(chat_id, None)
            self._known_calls.pop(active_id, None)
            for k in [k for k in self._muted_in_call if k[0] == chat_id]:
                del self._muted_in_call[k]

    async def _resolve_chat(self, update: Any) -> Tuple[Optional[int], str]:
        raw_id = getattr(update, "chat_id", None)
        chat_id: Optional[int] = None

        if raw_id:
            raw_int = int(raw_id)
            chat_id = normalize_channel_chat_id(raw_int) if raw_int > 0 else raw_int
        else:
            call_obj = getattr(update, "call", None)
            call_id = getattr(call_obj, "id", None)
            if call_id and int(call_id) in self._known_calls:
                chat_id = self._known_calls[int(call_id)]

        if chat_id is None:
            return None, "Unknown Group"

        db_group = await self.db.get_group(chat_id)
        if db_group:
            return chat_id, db_group["title"]

        if self.client:
            try:
                ent = await self.client.get_entity(chat_id)
                if isinstance(ent, (Channel, Chat)):
                    return chat_id, getattr(ent, "title", None) or str(chat_id)
            except Exception:
                pass

        return chat_id, str(chat_id)

    async def _on_raw_update(self, update: Any) -> None:
        if not isinstance(update, (UpdateGroupCall, UpdateGroupCallParticipants)):
            return

        chat_id, chat_title = await self._resolve_chat(update)
        logger.info("VC_DEBUG: Event=%s chat_id=%s title='%s'", type(update).__name__, chat_id, chat_title)
        if chat_id is None:
            return

        if isinstance(update, UpdateGroupCall):
            call_obj = update.call
            if isinstance(call_obj, GroupCallDiscarded):
                await self.leave_vc(chat_id, chat_title, call_id=int(call_obj.id))
            elif isinstance(call_obj, GroupCall):
                await self.join_vc(chat_id, chat_title, int(call_obj.id), int(call_obj.access_hash))

        elif isinstance(update, UpdateGroupCallParticipants):
            if not await self.moderation.ensure_bot_is_admin(chat_id, chat_title):
                return
            settings = await self.db.get_group_settings(chat_id)
            if not settings.get("auto_camera_mute", 1):
                return

            for p in getattr(update, "participants", []) or []:
                if getattr(p, "left", False):
                    continue
                user_id = getattr(getattr(p, "peer", None), "user_id", None)
                if not user_id:
                    continue

                has_video = bool(getattr(p, "video", False))
                has_camera = bool(getattr(p, "camera", False))
                paused = bool(getattr(p, "video_paused", False))
                logger.info(
                    "VC_DEBUG: Participant in '%s' (%s): user=%s video=%s camera=%s paused=%s",
                    chat_title,
                    chat_id,
                    user_id,
                    has_video,
                    has_camera,
                    paused,
                )

                if (has_video or has_camera) and not paused:
                    key = (chat_id, int(user_id))
                    if not self._muted_in_call.get(key):
                        muted = await self.moderation.restrict_user_permanent(
                            chat_id=chat_id,
                            chat_title=chat_title,
                            user_id=int(user_id),
                            username=None,
                            action_type="AUTO_CAMERA_MUTE",
                            reason="Participant video/camera flag active in group call",
                        )
                        if muted:
                            self._muted_in_call[key] = True
                            def format_uptime(start_mono: float) -> str:
    sec = max(0, int(time.monotonic() - start_mono))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return f"{d}d {h}h {m}m {s}s"


async def main() -> None:
    db = Database(getattr(cfg, "DB_PATH", "modguard.db"))
    await db.connect()

    start_mono = time.monotonic()
    app = Application.builder().token(cfg.BOT_TOKEN).build()

    async def notify_owner(event_key: str, text: str) -> None:
        if not await db.is_notification_enabled(event_key):
            return
        try:
            await app.bot.send_message(chat_id=cfg.OWNER_ID, text=text, parse_mode="HTML")
        except Exception as exc:
            logger.error("Owner alert failed (%s): %s", event_key, exc)

    moderation = ModerationEngine(
        bot=app.bot,
        db=db,
        notify_owner_cb=notify_owner,
    )
    vc_controller = AssistantVCController(
        api_id=int(cfg.API_ID),
        api_hash=str(cfg.API_HASH),
        session_string=str(cfg.SESSION_STRING),
        db=db,
        moderation=moderation,
        max_retries=int(getattr(cfg, "MAX_VC_JOIN_RETRIES", 3)),
        retry_delay=int(getattr(cfg, "VC_RETRY_DELAY_SECONDS", 5)),
        notify_owner_cb=notify_owner,
    )

    def is_owner(user_id: Optional[int]) -> bool:
        return user_id is not None and int(user_id) == int(cfg.OWNER_ID)

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user:
            await db.upsert_user(
                update.effective_user.id,
                update.effective_user.username,
                update.effective_user.first_name,
            )
        if not is_owner(update.effective_user.id if update.effective_user else None) and await db.is_maintenance_mode():
            if update.message:
                await update.message.reply_text("🛠️ MODGUARD maintenance mode mein hai.")
            return

        chat = update.effective_chat
        if chat and chat.type in ("group", "supergroup"):
            is_adm = await moderation.ensure_bot_is_admin(chat.id, chat.title or str(chat.id))
            if update.message:
                status_text = "Admin hai (DB synced: is_bot_admin=1)" if is_adm else "Admin nahi hai (is_bot_admin=0)"
                await update.message.reply_text(
                    f"🛡️ <b>MODGUARD</b> group mein active hai. {status_text}",
                    parse_mode="HTML",
                )
            return

        if update.message:
            await update.message.reply_text("🛡️ <b>MODGUARD</b> active hai.", parse_mode="HTML")

    async def cmd_addbadword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat, user = update.effective_chat, update.effective_user
        if not chat or chat.type not in ("group", "supergroup") or not user:
            return
        member = await app.bot.get_chat_member(chat.id, user.id)
        if member.status not in ("administrator", "creator") and not is_owner(user.id):
            return
        if not context.args:
            await update.message.reply_text("Usage: <code>/addbadword word</code>", parse_mode="HTML")
            return
        word = " ".join(context.args)
        ok = await db.add_badword(chat.id, word, added_by=user.id)
        await update.message.reply_text("✅ Word added." if ok else "Already exists.", parse_mode="HTML")

    async def cmd_delbadword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat, user = update.effective_chat, update.effective_user
        if not chat or chat.type not in ("group", "supergroup") or not user:
            return
        member = await app.bot.get_chat_member(chat.id, user.id)
        if member.status not in ("administrator", "creator") and not is_owner(user.id):
            return
        if not context.args:
            await update.message.reply_text("Usage: <code>/delbadword word</code>", parse_mode="HTML")
            return
        ok = await db.remove_badword(chat.id, " ".join(context.args))
        await update.message.reply_text("🗑️ Word removed." if ok else "Not found.", parse_mode="HTML")

    async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_owner(update.effective_user.id):
            return
        users_cnt = await db.count_users()
        groups_cnt = await db.count_groups()
        maint = await db.is_maintenance_mode()
        await update.message.reply_text(
            f"🛡️ <b>MODGUARD Admin Panel</b>\n"
            f"• Users: <b>{users_cnt}</b>\n"
            f"• Groups: <b>{groups_cnt}</b>\n"
            f"• Maintenance: <b>{'ON' if maint else 'OFF'}</b>\n"
            f"• Uptime: <b>{format_uptime(start_mono)}</b>\n\n"
            f"/maintenance on|off | /broadcast [users|groups|all] msg | /updatecast [users|groups|all] msg | /logs [n] | /alerts",
            parse_mode="HTML",
        )

    async def cmd_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_owner(update.effective_user.id):
            return
        if not context.args or context.args[0].lower() not in ("on", "off"):
            await update.message.reply_text("Usage: <code>/maintenance on|off</code>", parse_mode="HTML")
            return
        enabled = context.args[0].lower() == "on"
        await db.set_maintenance_mode(enabled)
        await update.message.reply_text(f"🔧 Maintenance mode: <b>{'ON' if enabled else 'OFF'}</b>", parse_mode="HTML")

    async def do_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, category: str) -> None:
        if not update.effective_user or not is_owner(update.effective_user.id):
            return
        args = context.args or []
        if not args:
            return
        scope = "all"
        if args[0].lower() in ("users", "groups", "all"):
            scope = args[0].lower()
            text = " ".join(args[1:]).strip()
        else:
            text = " ".join(args).strip()
        if not text:
            return

        targets: List[int] = []
        if scope in ("users", "all"):
            targets.extend(await db.get_all_user_ids())
        if scope in ("groups", "all"):
            targets.extend(await db.get_all_group_ids())
        unique_targets = list(dict.fromkeys(targets))

        sent = failed = 0
        for tid in unique_targets:
            try:
                await app.bot.send_message(chat_id=tid, text=text, parse_mode="HTML")
                sent += 1
            except TelegramError:
                failed += 1
            await asyncio.sleep(0.05)

        await db.record_broadcast(category, text, scope, sent, failed)
        await update.message.reply_text(f"📢 Broadcast ({category}): sent={sent}, failed={failed}")

    async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await do_broadcast(update, context, "admin_broadcast")

    async def cmd_updatecast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await do_broadcast(update, context, "update_broadcast")

    async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_owner(update.effective_user.id):
            return
        limit = int(context.args[0]) if (context.args and context.args[0].isdigit()) else 10
        rows = await db.get_recent_logs(max(1, min(30, limit)))
        if not rows:
            await update.message.reply_text("No logs.")
            return
        lines = [f"• [<code>{r['action_type']}</code>] <b>{r['chat_title']}</b>: {r['reason']}" for r in rows]
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_owner(update.effective_user.id):
            return
        if len(context.args) >= 2 and context.args[1].lower() in ("on", "off"):
            await db.set_notification_pref(context.args[0], context.args[1].lower() == "on")
        prefs = await db.get_notification_prefs()
        lines = [f"• <code>{p['key']}</code>: <b>{'ON' if p['enabled'] else 'OFF'}</b>" for p in prefs]
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat and chat.type in ("group", "supergroup"):
            await moderation.ensure_bot_is_admin(chat.id, chat.title or str(chat.id))

    async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
        if not msg or not chat or chat.type not in ("group", "supergroup"):
            return
        if user:
            await db.upsert_user(user.id, user.username, user.first_name)
        if await db.is_maintenance_mode():
            return
        title = chat.title or str(chat.id)
        if not await moderation.ensure_bot_is_admin(chat.id, title):
            return
        text = msg.text or msg.caption
        if not text:
            return
        custom = await db.list_badwords(chat.id)
        matched = detect_abuse(text, set(DEFAULT_BADWORDS) | set(custom))
        if not matched:
            return
        settings = await db.get_group_settings(chat.id)
        reason = f"Abuse detected: {', '.join(matched)}"
        if settings.get("auto_delete_abuse", 1):
            await moderation.delete_message_safe(msg, title, reason)
        if settings.get("auto_abuse_mute", 1) and user:
            await moderation.restrict_user_permanent(
                chat_id=chat.id,
                chat_title=title,
                user_id=user.id,
                username=user.username,
                action_type="AUTO_ABUSE_MUTE",
                reason=reason,
            )

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.exception("Main Bot error: %s", context.error)
        await notify_owner("critical_error", f"🚨 <b>Critical Error</b>\n<code>{context.error}</code>")

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("addbadword", cmd_addbadword))
    app.add_handler(CommandHandler("delbadword", cmd_delbadword))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("maintenance", cmd_maintenance))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("updatecast", cmd_updatecast))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_group_message))
    app.add_error_handler(on_error)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    try:
        await app.initialize()
        await app.start()
        assert app.updater is not None
        await app.updater.start_polling(drop_pending_updates=True)

        for gid in await db.get_all_group_ids():
            grp = await db.get_group(gid)
            await moderation.ensure_bot_is_admin(gid, grp["title"] if grp else str(gid))

        await vc_controller.start()
        logger.info("MODGUARD running.")
        await stop_event.wait()
    except Exception as exc:
        logger.exception("System failure: %s", exc)
        await notify_owner("system_failure", f"💥 <b>System Failure</b>\n<code>{exc}</code>")
    finally:
        await vc_controller.stop()
        if app.updater and app.updater.running:
            await app.updater.stop()
        if app.running:
            await app.stop()
        await app.shutdown()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
        
