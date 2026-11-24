import asyncio
import json
import logging
import random
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError, UserAlreadyParticipantError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest, ImportChatInviteRequest
from telethon.tl.functions.phone import (
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
    UpdateGroupCall,
    UpdateGroupCallParticipants,
)

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
        self.assistant_id: Optional[int] = None
        self._active_joins: Dict[int, int] = {}
        self._known_calls: Dict[int, int] = {}
        self._chat_titles: Dict[int, str] = {}
        self._call_access_hashes: Dict[int, int] = {}
        self._seen_group_users: Dict[int, Set[int]] = {}
        self._join_locks: Dict[int, asyncio.Lock] = {}
        self._rejoin_cooldown: Dict[int, float] = {}
        self._poll_sem = asyncio.Semaphore(5)
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
            self._running = True
            self.client.add_event_handler(self._on_raw_update, events.Raw)
            logger.info("VC_DEBUG: Assistant connected as id=%s username=@%s", me.id, me.username or "")
            await self._sync_dialogs_and_calls(auto_join=True)
            self._poll_task = asyncio.create_task(self._poll_active_calls_loop())
        except Exception as exc:
            logger.error("VC_DEBUG: Assistant start failed (check SESSION_STRING): %s", exc)
            self.client = None

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
                if not self.client or not self.client.is_connected():
                    logger.warning("VC_DEBUG: Assistant disconnected; attempting reconnect...")
                    await self.client.connect()
                    continue

                await self._sync_dialogs_and_calls(auto_join=True)

                active_items = list(self._active_joins.items())
                if not active_items:
                    continue

                async def _check_one(cid_chat: int, cid_call: int) -> None:
                    async with self._poll_sem:
                        ahash = self._call_access_hashes.get(cid_call, 0)
                        title = self._chat_titles.get(cid_chat, str(cid_chat))
                        settings = await self.db.get_group_settings(cid_chat)
                        if settings.get("auto_camera_mute", 1):
                            await self._inspect_call_participants(cid_chat, title, cid_call, ahash)
                        await asyncio.sleep(0.25)

                await asyncio.gather(*(_check_one(c_id, call_id) for c_id, call_id in active_items))
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
                    ahash = int(getattr(call, "access_hash", 0) or 0)
                    self._known_calls[cid] = chat_id
                    if ahash:
                        self._call_access_hashes[cid] = ahash
                    if auto_join and chat_id not in self._active_joins:
                        logger.info("VC_DEBUG: Found active VC '%s' (chat_id=%s, call_id=%s)", title, chat_id, cid)
                        await self.join_vc(chat_id, title, cid, ahash)
                        await asyncio.sleep(0.5)
        except Exception as exc:
            logger.warning("VC_DEBUG: Dialog sync error: %s", exc)

    async def _inspect_call_participants(self, chat_id: int, chat_title: str, call_id: int, access_hash: int) -> None:
        if not self.client or not self.client.is_connected():
            return
        try:
            call_resp = await self.client(
                GetGroupCallRequest(call=InputGroupCall(id=call_id, access_hash=access_hash), limit=100)
            )
            participants = getattr(call_resp, "participants", None) or []
            assistant_present = False
            for p in participants:
                peer = getattr(p, "peer", None)
                uid = (
                    getattr(peer, "user_id", None)
                    or getattr(peer, "chat_id", None)
                    or getattr(peer, "channel_id", None)
                )
                if uid and self.assistant_id and int(uid) == self.assistant_id and not getattr(p, "left", False):
                    assistant_present = True
                await self._evaluate_participant(chat_id, chat_title, p)

            # Silent media-less joins may not always appear in participant list;
            # only attempt a gentle rejoin after a longer cooldown (180s) if already marked joined.
            now = time.monotonic()
            last_rejoin = self._rejoin_cooldown.get(chat_id, 0.0)
            if not assistant_present and self._active_joins.get(chat_id) == call_id and (now - last_rejoin) > 180.0:
                self._rejoin_cooldown[chat_id] = now
                logger.debug(
                    "VC_DEBUG: Refreshing silent VC session in '%s' (%s) call_id=%s",
                    chat_title,
                    chat_id,
                    call_id,
                )
                self._active_joins.pop(chat_id, None)
                await self.join_vc(chat_id, chat_title, call_id, access_hash)
        except Exception as exc:
            logger.debug("VC_DEBUG: GetGroupCallRequest check in '%s': %s", chat_title, exc)

    async def _evaluate_participant(self, chat_id: int, chat_title: str, p: Any) -> None:
        peer = getattr(p, "peer", None)
        user_id = (
            getattr(peer, "user_id", None)
            or getattr(peer, "chat_id", None)
            or getattr(peer, "channel_id", None)
        )
        if not user_id or (self.assistant_id and int(user_id) == self.assistant_id):
            return

        uid = int(user_id)
        self._seen_group_users.setdefault(chat_id, set()).add(uid)

        if getattr(p, "left", False):
            return

        if await self.moderation.is_user_admin_or_creator(chat_id, uid):
            return

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
                "VC_DEBUG: Non-admin user=%s turned camera ON in VC '%s' (chat_id=%s) -> removing from group.",
                uid,
                chat_title,
                chat_id,
            )
            await self.moderation.ensure_bot_is_admin(chat_id, chat_title)
            await self.moderation.remove_user_from_group(
                chat_id=chat_id,
                chat_title=chat_title,
                user_id=uid,
                reason="Camera turned on in VC -> removed from group",
            )

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
        for chat_id in list(self._active_joins.keys()):
            title = self._chat_titles.get(chat_id, str(chat_id))
            await self.leave_vc(chat_id, title, reason="BOT_SHUTDOWN", force=True)
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
                    self._rejoin_cooldown[chat_id] = time.monotonic()
                    logger.info(
                        "VC_DEBUG: Assistant JOINED VC '%s' (chat_id=%s, call_id=%s) on attempt %d.",
                        chat_title,
                        chat_id,
                        call_id,
                        attempt,
                    )
                    if settings.get("auto_camera_mute", 1):
                        await self._inspect_call_participants(chat_id, chat_title, call_id, access_hash)
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
        if not active_id:
            return
        if not force and call_id is not None and int(call_id) != int(active_id):
            return
        ahash = self._call_access_hashes.get(active_id, 0)
        try:
            if self.client and self.client.is_connected() and reason != "VC_END_DISCARDED":
                await self.client(LeaveGroupCallRequest(call=InputGroupCall(id=active_id, access_hash=ahash), source=0))
            logger.warning(
                "VC_DEBUG: Assistant LEFT VC in '%s' (chat_id=%s, call_id=%s) | reason=%s",
                chat_title,
                chat_id,
                active_id,
                reason,
            )
        except RPCError as exc:
            logger.warning("VC_DEBUG: Leave VC error in %s (%s): %s", chat_id, reason, exc)
        finally:
            self._active_joins.pop(chat_id, None)
            self._known_calls.pop(active_id, None)
            self._call_access_hashes.pop(active_id, None)

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
            if isinstance(call_obj, GroupCallDiscarded):
                await self.leave_vc(chat_id, chat_title, call_id=cid, reason="VC_END_DISCARDED", force=True)
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
            for p in getattr(update, "participants", []) or []:
                await self._evaluate_participant(chat_id, chat_title, p)
