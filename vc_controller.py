import asyncio
import logging
from typing import Any, Dict, List, Optional, Set

from pytgcalls import PyTgCalls
from pytgcalls.types import ChatUpdate, MediaStream, UpdatedGroupCallParticipants
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError, UserAlreadyParticipantError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest, ImportChatInviteRequest
from telethon.tl.types import Channel, Chat

from database import Database, extract_invite_hash, normalize_channel_chat_id
from moderation import ModerationEngine

logger = logging.getLogger("modguard.vc")


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
        self.calls: Optional[PyTgCalls] = None
        self.assistant_id: Optional[int] = None

        self._active_joins: Dict[int, int] = {}
        self._known_calls: Dict[int, int] = {}
        self._chat_titles: Dict[int, str] = {}
        self._seen_group_users: Dict[int, Set[int]] = {}
        self._join_locks: Dict[int, asyncio.Lock] = {}
        self._poll_task: Optional[asyncio.Task] = None
        self._running = False

    async def collect_group_participants(self, chat_id: int) -> List[int]:
        collected: Set[int] = set(self._seen_group_users.get(chat_id, set()))
        if not self.client or not self.client.is_connected():
            return list(collected)
        try:
            entity = await self.client.get_entity(chat_id)
            async for member in self.client.iter_participants(entity, limit=2000):
                if getattr(member, "id", None):
                    collected.add(int(member.id))
        except Exception as exc:
            logger.debug("Participant list fetch for %s: %s", chat_id, exc)
        return list(collected)

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

            self.calls = PyTgCalls(self.client)
            self._register_call_handlers()
            await self.calls.start()

            self._running = True
            logger.info("VC_DEBUG: Assistant + PyTgCalls started as id=%s username=@%s", me.id, me.username or "")
            await self._sync_dialogs_and_calls(auto_join=True)
            self._poll_task = asyncio.create_task(self._poll_active_calls_loop())
        except Exception as exc:
            logger.error("VC_DEBUG: Assistant/PyTgCalls start failed: %s", exc)
            self.client = None
            self.calls = None

    def _register_call_handlers(self) -> None:
        assert self.calls is not None

        @self.calls.on_update()
        async def _on_call_update(_: PyTgCalls, update: Any) -> None:
            if isinstance(update, ChatUpdate):
                chat_id = normalize_channel_chat_id(int(update.chat_id)) if int(update.chat_id) > 0 else int(update.chat_id)
                title = self._chat_titles.get(chat_id, str(chat_id))
                status_str = str(getattr(update, "status", "")).upper()
                if "CLOSED_VOICE_CHAT" in status_str or "DISCARDED" in status_str or "LEFT" in status_str:
                    await self.leave_vc(chat_id, title, reason=f"CHAT_UPDATE_{status_str}", force=True)
                elif "CREATED_VOICE_CHAT" in status_str or "ACTIVE" in status_str:
                    await self._sync_dialogs_and_calls(auto_join=True)

            elif isinstance(update, UpdatedGroupCallParticipants):
                raw_cid = int(getattr(update, "chat_id", 0) or 0)
                if not raw_cid:
                    return
                chat_id = normalize_channel_chat_id(raw_cid) if raw_cid > 0 else raw_cid
                title = self._chat_titles.get(chat_id, str(chat_id))
                settings = await self.db.get_group_settings(chat_id)
                if not settings.get("auto_camera_mute", 1):
                    return
                for p in getattr(update, "participants", []) or []:
                    await self._evaluate_participant_obj(chat_id, title, p)

    async def join_group_via_invite(self, invite_link: str, chat_id: int, chat_title: str) -> bool:
        if not self.client or not self.client.is_connected():
            return False
        invite_hash = extract_invite_hash(invite_link)
        if not invite_hash:
            logger.warning("VC_DEBUG: Invalid invite link for group '%s' (%s)", chat_title, chat_id)
            return False
        try:
            await self.client(ImportChatInviteRequest(invite_hash))
            self._chat_titles[chat_id] = chat_title
            logger.info("VC_DEBUG: Assistant JOINED group '%s' (%s) via invite link.", chat_title, chat_id)
            await asyncio.sleep(1.0)
            await self._sync_dialogs_and_calls(auto_join=True)
            return True
        except UserAlreadyParticipantError:
            self._chat_titles[chat_id] = chat_title
            logger.info("VC_DEBUG: Assistant already a member of group '%s' (%s).", chat_title, chat_id)
            await self._sync_dialogs_and_calls(auto_join=True)
            return True
        except FloodWaitError as exc:
            logger.warning("VC_DEBUG: FloodWait %ss during group join for '%s'", exc.seconds, chat_title)
            return False
        except Exception as exc:
            logger.warning(
                "VC_DEBUG: Assistant could not auto-join group '%s' (%s): %s",
                chat_title,
                chat_id,
                exc,
            )
            return False
            async def _poll_active_calls_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(6)
                if not self.client or not self.client.is_connected() or not self.calls:
                    continue
                await self._sync_dialogs_and_calls(auto_join=True)
            except asyncio.CancelledError:
                break
            except FloodWaitError as exc:
                logger.warning("VC_DEBUG: Poll loop FloodWait %ss", exc.seconds)
                await asyncio.sleep(min(int(exc.seconds) + 1, 30))
            except Exception as exc:
                logger.debug("VC_DEBUG: Poll loop tick error: %s", exc)

    async def _sync_dialogs_and_calls(self, auto_join: bool = False) -> None:
        if not self.client or not self.client.is_connected():
            return
        try:
            async for dialog in self.client.iter_dialogs(limit=150):
                ent = dialog.entity
                title = getattr(ent, "title", None) or str(dialog.id)
                chat_id: Optional[int] = None
                call = None

                if isinstance(ent, Channel) and (getattr(ent, "megagroup", False) or getattr(ent, "gigagroup", False)):
                    chat_id = normalize_channel_chat_id(ent.id)
                    try:
                        full = await self.client(GetFullChannelRequest(ent))
                        call = getattr(full.full_chat, "call", None)
                    except FloodWaitError:
                        await asyncio.sleep(2)
                    except Exception:
                        pass
                elif isinstance(ent, Chat):
                    chat_id = -abs(int(ent.id))
                    try:
                        full = await self.client(GetFullChatRequest(ent.id))
                        call = getattr(full.full_chat, "call", None)
                    except FloodWaitError:
                        await asyncio.sleep(2)
                    except Exception:
                        pass

                if chat_id is None:
                    continue

                self._chat_titles[chat_id] = title
                if call is not None and getattr(call, "id", None):
                    cid = int(call.id)
                    self._known_calls[cid] = chat_id
                    if auto_join and chat_id not in self._active_joins:
                        logger.info("VC_DEBUG: Found active VC '%s' (chat_id=%s, call_id=%s)", title, chat_id, cid)
                        await self.join_vc(chat_id, title, cid, 0)
                        await asyncio.sleep(0.5)
                elif chat_id in self._active_joins:
                    await self.leave_vc(chat_id, title, reason="VC_ENDED_BY_ADMIN", force=True)
        except Exception as exc:
            logger.warning("VC_DEBUG: Dialog sync error: %s", exc)

    async def _evaluate_participant_obj(self, chat_id: int, chat_title: str, p: Any) -> None:
        uid = getattr(p, "user_id", None)
        if not uid or (self.assistant_id and int(uid) == self.assistant_id):
            return
        user_id = int(uid)
        self._seen_group_users.setdefault(chat_id, set()).add(user_id)

        if getattr(p, "left", False):
            return

        if await self.moderation.is_user_admin_or_creator(chat_id, user_id):
            return

        video_active = bool(getattr(p, "is_video_on", False) or getattr(p, "video", False))
        video_paused = bool(getattr(p, "is_video_paused", False) or getattr(p, "video_paused", False))
        video_stopped = bool(getattr(p, "is_video_stopped", False) or getattr(p, "video_stopped", False))

        if video_active and not video_paused and not video_stopped:
            logger.info(
                "VC_DEBUG: Non-admin user=%s turned camera ON in VC '%s' (chat_id=%s) -> removing from group.",
                user_id,
                chat_title,
                chat_id,
            )
            await self.moderation.ensure_bot_is_admin(chat_id, chat_title)
            await self.moderation.remove_user_from_group(
                chat_id=chat_id,
                chat_title=chat_title,
                user_id=user_id,
                reason="Camera turned on in VC -> removed from group",
            )

    async def join_vc(self, chat_id: int, chat_title: str, call_id: int, access_hash: int = 0) -> bool:
        if not self.client or not self.client.is_connected() or not self.calls:
            logger.warning("VC_DEBUG: Assistant/PyTgCalls not ready; cannot join VC in %s", chat_id)
            return False

        settings = await self.db.get_group_settings(chat_id)
        if not settings.get("auto_vc_join", 1):
            logger.info("VC_DEBUG: auto_vc_join disabled in '%s' (chat_id=%s).", chat_title, chat_id)
            return False

        lock = self._join_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            if self._active_joins.get(chat_id) == call_id:
                return True

            self._known_calls[call_id] = chat_id
            self._chat_titles[chat_id] = chat_title
            last_err = ""

            for attempt in range(1, self.max_retries + 1):
                try:
                    await self.calls.play(
                        chat_id,
                        MediaStream(
                            media_path=None,
                            audio_parameters=None,
                            video_parameters=None,
                        ),
                    )
                    self._active_joins[chat_id] = call_id
                    logger.info(
                        "VC_DEBUG: Assistant PERMANENTLY JOINED VC '%s' (chat_id=%s, call_id=%s) on attempt %d.",
                        chat_title,
                        chat_id,
                        call_id,
                        attempt,
                    )
                    return True
                except FloodWaitError as exc:
                    last_err = f"FloodWait {exc.seconds}s"
                    wait_s = min(int(exc.seconds) + 1, 20)
                    logger.warning("VC_DEBUG: VC join FloodWait %ss in '%s'", exc.seconds, chat_title)
                    if attempt < self.max_retries:
                        await asyncio.sleep(wait_s)
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

    async def leave_vc(
        self,
        chat_id: int,
        chat_title: str,
        call_id: Optional[int] = None,
        reason: str = "VC_ENDED",
        force: bool = False,
    ) -> None:
        active_id = self._active_joins.get(chat_id)
        if not active_id and not force:
            return
        if not force and call_id is not None and active_id and int(call_id) != int(active_id):
            return
        try:
            if self.calls and reason != "VC_ENDED_BY_ADMIN":
                try:
                    await self.calls.leave_call(chat_id)
                except Exception:
                    pass
            logger.info(
                "VC_DEBUG: Assistant LEFT VC in '%s' (chat_id=%s, call_id=%s) | reason=%s",
                chat_title,
                chat_id,
                active_id,
                reason,
            )
        finally:
            self._active_joins.pop(chat_id, None)
            if active_id:
                self._known_calls.pop(active_id, None)

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
        for chat_id in list(self._active_joins.keys()):
            title = self._chat_titles.get(chat_id, str(chat_id))
            await self.leave_vc(chat_id, title, reason="BOT_SHUTDOWN", force=True)
        if self.client and self.client.is_connected():
            await self.client.disconnect()
                
