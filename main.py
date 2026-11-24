import ast
import asyncio
import json
import logging
import random
import re
import signal
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from telethon import TelegramClient, events
from telethon.errors import RPCError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.functions.phone import (
    EditGroupCallParticipantRequest,
    GetGroupCallRequest,
    JoinGroupCallRequest,
    LeaveGroupCallRequest,
)
from telethon.tl.types import (
    Channel,
    Chat,
    DataJSON,
    GroupCall,
    GroupCallDiscarded,
    InputGroupCall,
    PeerUser,
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

MUTE_CHAT_ON_CAMERA: bool = bool(getattr(cfg, "MUTE_CHAT_ON_CAMERA", False))


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
        self._lost_admin_notified: Set[int] = set()
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
                logger.info("User %s is admin/creator in %s; skipping chat restrict.", user_id, chat_id)
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
                f"{reason} (chat restricted until admin unmutes)",
            )
            logger.info("CHAT RESTRICTED user %s in '%s' (%s) | reason: %s", user_id, chat_title, chat_id, reason)
            return True
        except (Forbidden, BadRequest, TelegramError) as exc:
            logger.warning("Chat restrict failed for %s in %s: %s", user_id, chat_id, exc)
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
        self.assistant_id: Optional[int] = None
        self._active_joins: Dict[int, int] = {}
        self._known_calls: Dict[int, int] = {}
        self._chat_titles: Dict[int, str] = {}
        self._call_access_hashes: Dict[int, int] = {}
        self._join_locks: Dict[int, asyncio.Lock] = {}
        self._vc_muted_users: Set[Tuple[int, int]] = set()
        self._chat_muted_users: Set[Tuple[int, int]] = set()
        self._poll_task: Optional[asyncio.Task] = None
        self._running = False

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
            self.assistant_id = int(me.id)
            self._running = True
            self.client.add_event_handler(self._on_raw_update, events.Raw)
            logger.info("VC_DEBUG: Assistant connected as id=%s username=@%s", me.id, me.username or "")
            await self._sync_dialogs_and_calls(auto_join=True)
            self._poll_task = asyncio.create_task(self._poll_active_calls_loop())
        except Exception as exc:
            logger.error("VC_DEBUG: Assistant start failed (check SESSION_STRING): %s", exc)
            self.client = None

    async def _poll_active_calls_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(5)
                if not self.client or not self.client.is_connected():
                    continue
                # Join only if not already in active call; do NOT re-join already active VCs
                await self._sync_dialogs_and_calls(auto_join=True)
                for chat_id, call_id in list(self._active_joins.items()):
                    ahash = self._call_access_hashes.get(call_id, 0)
                    title = self._chat_titles.get(chat_id, str(chat_id))
                    settings = await self.db.get_group_settings(chat_id)
                    if settings.get("auto_camera_mute", 1):
                        await self._inspect_call_participants(chat_id, title, call_id, ahash)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("VC_DEBUG: Poll loop tick error: %s", exc)

    async def _sync_dialogs_and_calls(self, auto_join: bool = False) -> None:
        if not self.client or not self.client.is_connected():
            return
        try:
            async for dialog in self.client.iter_dialogs(limit=100):
                ent = dialog.entity
                title = getattr(ent, "title", None) or str(dialog.id)
                chat_id: Optional[int] = None
                call = None

                if isinstance(ent, Channel) and (getattr(ent, "megagroup", False) or getattr(ent, "gigagroup", False)):
                    chat_id = normalize_channel_chat_id(ent.id)
                    try:
                        full = await self.client(GetFullChannelRequest(ent))
                        call = getattr(full.full_chat, "call", None)
                    except Exception:
                        pass
                elif isinstance(ent, Chat):
                    chat_id = -abs(int(ent.id))
                    try:
                        full = await self.client(GetFullChatRequest(ent.id))
                        call = getattr(full.full_chat, "call", None)
                    except Exception:
                        pass

                if chat_id is None:
                    continue

                self._chat_titles[chat_id] = title
                if call is not None and getattr(call, "id", None):
                    cid = int(call.id)
                    ahash = int(getattr(call, "access_hash", 0) or 0)
                    self._known_calls[cid] = chat_id
                    if ahash:
                        self._call_access_hashes[cid] = ahash
                    if auto_join and chat_id not in self._active_joins:
                        logger.info("VC_DEBUG: Found active VC '%s' (chat_id=%s, call_id=%s)", title, chat_id, cid)
                        await self.join_vc(chat_id, title, cid, ahash)
        except Exception as exc:
            logger.warning("VC_DEBUG: Dialog sync error: %s", exc)
    async def _mute_user_in_vc(self, chat_id: int, chat_title: str, call_id: int, user_id: int) -> bool:
        if not self.client or not self.client.is_connected():
            return False
        ahash = self._call_access_hashes.get(call_id, 0)
        try:
            participant_peer = await self.client.get_input_entity(PeerUser(user_id))
            await self.client(
                EditGroupCallParticipantRequest(
                    call=InputGroupCall(id=call_id, access_hash=ahash),
                    participant=participant_peer,
                    muted=True,
                )
            )
            logger.info("VC_DEBUG: MUTED user=%s inside VC '%s' (chat_id=%s, call_id=%s)", user_id, chat_title, chat_id, call_id)
            return True
        except Exception as exc:
            logger.warning(
                "VC_DEBUG: Could not mute user=%s inside VC '%s' (%s): %s (Check Assistant/Bot VC admin rights)",
                user_id,
                chat_title,
                chat_id,
                exc,
            )
            return False

    async def _inspect_call_participants(self, chat_id: int, chat_title: str, call_id: int, access_hash: int) -> None:
        if not self.client or not self.client.is_connected():
            return
        try:
            call_resp = await self.client(
                GetGroupCallRequest(call=InputGroupCall(id=call_id, access_hash=access_hash), limit=100)
            )
            participants = getattr(call_resp, "participants", None) or []
            for p in participants:
                await self._evaluate_participant(chat_id, chat_title, call_id, p)
        except Exception as exc:
            logger.debug("VC_DEBUG: GetGroupCallRequest check in '%s': %s", chat_title, exc)

    async def _evaluate_participant(self, chat_id: int, chat_title: str, call_id: int, p: Any) -> None:
        if getattr(p, "left", False):
            return
        peer = getattr(p, "peer", None)
        user_id = (
            getattr(peer, "user_id", None)
            or getattr(peer, "chat_id", None)
            or getattr(peer, "channel_id", None)
        )
        if not user_id or (self.assistant_id and int(user_id) == self.assistant_id):
            return

        uid = int(user_id)
        video_attr = getattr(p, "video", None)
        camera_attr = getattr(p, "camera", None)
        video_paused = bool(getattr(p, "video_paused", False))
        video_stopped = bool(getattr(p, "video_stopped", False))

        has_video_obj = video_attr is not None and not isinstance(video_attr, bool)
        has_video_bool = video_attr is True
        has_camera = bool(camera_attr)

        is_video_active = (has_video_obj or has_video_bool or has_camera) and (not video_paused) and (not video_stopped)

        if is_video_active:
            logger.info(
                "VC_DEBUG: ACTIVE CAMERA/VIDEO in VC '%s' (chat_id=%s): user=%s video=%r camera=%r paused=%s stopped=%s",
                chat_title,
                chat_id,
                uid,
                video_attr,
                camera_attr,
                video_paused,
                video_stopped,
            )
            vc_key = (chat_id, uid)
            if vc_key not in self._vc_muted_users:
                vc_ok = await self._mute_user_in_vc(chat_id, chat_title, call_id, uid)
                if vc_ok:
                    self._vc_muted_users.add(vc_key)
                    await self.db.add_moderation_log(
                        chat_id,
                        chat_title,
                        uid,
                        None,
                        "VC_CAMERA_MUTE",
                        "Muted participant inside VC due to active camera/video",
                    )

            if MUTE_CHAT_ON_CAMERA and vc_key not in self._chat_muted_users:
                if await self.moderation.ensure_bot_is_admin(chat_id, chat_title):
                    chat_ok = await self.moderation.restrict_user_permanent(
                        chat_id=chat_id,
                        chat_title=chat_title,
                        user_id=uid,
                        username=None,
                        action_type="AUTO_CAMERA_CHAT_MUTE",
                        reason="Participant video/camera active in VC (MUTE_CHAT_ON_CAMERA=True)",
                    )
                    if chat_ok:
                        self._chat_muted_users.add(vc_key)

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
        for chat_id in list(self._active_joins.keys()):
            title = self._chat_titles.get(chat_id, str(chat_id))
            await self.leave_vc(chat_id, title, force=True)
        if self.client and self.client.is_connected():
            await self.client.disconnect()

    async def join_vc(self, chat_id: int, chat_title: str, call_id: int, access_hash: int) -> bool:
        if not self.client or not self.client.is_connected():
            logger.warning("VC_DEBUG: Assistant client not connected; cannot join VC in %s", chat_id)
            return False
        settings = await self.db.get_group_settings(chat_id)
        if not settings.get("auto_vc_join", 1):
            logger.info("VC_DEBUG: auto_vc_join disabled in '%s' (chat_id=%s).", chat_title, chat_id)
            return False

        lock = self._join_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            # Already joined this call_id; keep connection stable and avoid join/leave loop
            if self._active_joins.get(chat_id) == call_id:
                return True

            self._known_calls[call_id] = chat_id
            self._chat_titles[chat_id] = chat_title
            if access_hash:
                self._call_access_hashes[call_id] = access_hash
            last_err = ""

            for attempt in range(1, self.max_retries + 1):
                try:
                    ssrc_val = random.randint(10_000_000, 2_000_000_000)
                    call_params = DataJSON(
                        data=json.dumps({
                            "ufrag": "",
                            "pwd": "",
                            "fingerprints": [],
                            "ssrc": ssrc_val,
                        })
                    )
                    await self.client(
                        JoinGroupCallRequest(
                            call=InputGroupCall(id=call_id, access_hash=access_hash),
                            join_as=await self.client.get_input_entity("me"),
                            params=call_params,
                            muted=True,
                            video_stopped=True,
                        )
                    )
                    self._active_joins[chat_id] = call_id
                    logger.info(
                        "VC_DEBUG: Assistant JOINED and STAYING in VC '%s' (chat_id=%s, call_id=%s) on attempt %d.",
                        chat_title,
                        chat_id,
                        call_id,
                        attempt,
                    )
                    if settings.get("auto_camera_mute", 1):
                        await self._inspect_call_participants(chat_id, chat_title, call_id, access_hash)
                    return True
                except (RPCError, Exception) as exc:
                    last_err = str(exc)
                    logger.warning(
                        "VC_DEBUG: VC join attempt %d/%d failed in '%s' (chat_id=%s): %s",
                        attempt,
                        self.max_retries,
                        chat_title,
                        chat_id,
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
                    f"❌ <b>Assistant VC Join Failed</b>\nGroup: <b>{chat_title}</b> (<code>{chat_id}</code>)\nError: <code>{last_err}</code>",
                )
            return False

    async def leave_vc(self, chat_id: int, chat_title: str, call_id: Optional[int] = None, force: bool = False) -> None:
        active_id = self._active_joins.get(chat_id)
        if not active_id:
            return
        if not force and call_id is not None and int(call_id) != int(active_id):
            return
        ahash = self._call_access_hashes.get(active_id, 0)
        try:
            if self.client and self.client.is_connected():
                await self.client(LeaveGroupCallRequest(call=InputGroupCall(id=active_id, access_hash=ahash), source=0))
            logger.info("VC_DEBUG: Assistant left VC in '%s' (chat_id=%s, call_id=%s).", chat_title, chat_id, active_id)
        except RPCError as exc:
            logger.warning("VC_DEBUG: Leave VC error in %s: %s", chat_id, exc)
        finally:
            self._active_joins.pop(chat_id, None)
            self._known_calls.pop(active_id, None)
            self._call_access_hashes.pop(active_id, None)
            for k in [k for k in self._vc_muted_users if k[0] == chat_id]:
                self._vc_muted_users.discard(k)
            for k in [k for k in self._chat_muted_users if k[0] == chat_id]:
                self._chat_muted_users.discard(k)

    async def _resolve_chat_from_update(self, update: Any) -> Tuple[Optional[int], str]:
        raw_chat_id = getattr(update, "chat_id", None)
        call_obj = getattr(update, "call", None)
        call_id = getattr(call_obj, "id", None)

        if raw_chat_id:
            raw_int = int(raw_chat_id)
            chat_id = normalize_channel_chat_id(raw_int) if raw_int > 0 else raw_int
            if call_id:
                self._known_calls[int(call_id)] = chat_id
            title = self._chat_titles.get(chat_id)
            if not title:
                db_grp = await self.db.get_group(chat_id)
                title = db_grp["title"] if db_grp else str(chat_id)
            return chat_id, title

        if call_id is not None:
            cid_int = int(call_id)
            if cid_int in self._known_calls:
                chat_id = self._known_calls[cid_int]
                return chat_id, self._chat_titles.get(chat_id, str(chat_id))

            await self._sync_dialogs_and_calls(auto_join=False)
            if cid_int in self._known_calls:
                chat_id = self._known_calls[cid_int]
                return chat_id, self._chat_titles.get(chat_id, str(chat_id))

        return None, "Unknown Group"

    async def _on_raw_update(self, update: Any) -> None:
        if not isinstance(update, (UpdateGroupCall, UpdateGroupCallParticipants)):
            return

        chat_id, chat_title = await self._resolve_chat_from_update(update)
        call_obj = getattr(update, "call", None)
        cid = int(getattr(call_obj, "id", 0) or 0)
        logger.info("VC_DEBUG: Event=%s call_id=%s chat_id=%s title='%s'", type(update).__name__, cid, chat_id, chat_title)

        if chat_id is None:
            return

        if isinstance(update, UpdateGroupCall):
            # Only leave when call is genuinely discarded by Telegram
            if isinstance(call_obj, GroupCallDiscarded):
                await self.leave_vc(chat_id, chat_title, call_id=cid, force=True)
            elif isinstance(call_obj, GroupCall):
                ahash = int(getattr(call_obj, "access_hash", 0) or 0)
                if ahash:
                    self._call_access_hashes[cid] = ahash
                if chat_id not in self._active_joins:
                    await self.join_vc(chat_id, chat_title, cid, ahash)

        elif isinstance(update, UpdateGroupCallParticipants):
            settings = await self.db.get_group_settings(chat_id)
            if not settings.get("auto_camera_mute", 1):
                return
            active_cid = cid or self._active_joins.get(chat_id, 0)
            for p in getattr(update, "participants", []) or []:
                await self._evaluate_participant(chat_id, chat_title, active_cid, p)
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


def _verify_self_syntax() -> None:
    with open(__file__, "r", encoding="utf-8") as fh:
        ast.parse(fh.read(), filename=__file__)


if __name__ == "__main__":
    try:
        _verify_self_syntax()
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
