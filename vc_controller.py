import asyncio
import logging
import os
import struct
import tempfile
import wave
from typing import Any, Dict, List, Optional, Set, Tuple

from pytgcalls import PyTgCalls
from pytgcalls.types import ChatUpdate, MediaStream
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError, UserAlreadyParticipantError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest, ImportChatInviteRequest
from telethon.tl.functions.phone import EditGroupCallParticipantRequest, GetGroupCallRequest
from telethon.tl.types import (
    Channel,
    Chat,
    GroupCall,
    GroupCallDiscarded,
    InputGroupCall,
    InputPeerChannel,
    InputPeerChat,
    InputPeerUser,
    UpdateGroupCall,
    UpdateGroupCallParticipants,
)

from database import Database, extract_invite_hash, normalize_channel_chat_id
from moderation import ModerationEngine

logger = logging.getLogger("modguard.vc")

SILENT_WAV_PATH = "silent_vc.wav"


def _ensure_silent_wav(path: str = SILENT_WAV_PATH, duration_sec: int = 1, sample_rate: int = 8000) -> str:
    if os.path.exists(path) and os.path.getsize(path) > 44:
        return path
    num_samples = sample_rate * duration_sec
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack("<" + "h" * num_samples, *([0] * num_samples)))
    return path


def _transcribe_wav_sync(wav_path: str) -> str:
    """Best-effort local speech-to-text if an STT package is available in the environment."""
    try:
        import speech_recognition as sr  # type: ignore

        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)
        return str(recognizer.recognize_google(audio_data) or "").lower()
    except Exception:
        return ""


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
        self._call_access_hashes: Dict[int, int] = {}
        self._seen_group_users: Dict[int, Set[int]] = {}
        self._group_members_cache: Dict[int, Set[int]] = {}
        self._muted_cooldown: Dict[Tuple[int, int], float] = {}
        self._join_locks: Dict[int, asyncio.Lock] = {}
        self._poll_sem = asyncio.Semaphore(5)
        self._poll_task: Optional[asyncio.Task] = None
        self._bg_tasks: Set[asyncio.Task] = set()
        self._running = False

    def _spawn_bg(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def collect_group_participants(self, chat_id: int) -> List[int]:
        collected: Set[int] = set(self._seen_group_users.get(chat_id, set()))
        collected.update(self._group_members_cache.get(chat_id, set()))
        if not self.client or not self.client.is_connected():
            return list(collected)
        try:
            entity = await self.client.get_entity(chat_id)
            async for member in self.client.iter_participants(entity, limit=2000):
                if getattr(member, "id", None):
                    collected.add(int(member.id))
            self._group_members_cache[chat_id] = set(collected)
        except Exception as exc:
            logger.debug("Participant list fetch for %s: %s", chat_id, exc)
        return list(collected)

    async def _is_group_member(self, chat_id: int, user_id: int) -> bool:
        cached = self._group_members_cache.get(chat_id)
        if cached and user_id in cached:
            return True
        if not self.client or not self.client.is_connected():
            return True
        try:
            entity = await self.client.get_entity(chat_id)
            members: Set[int] = set()
            async for m in self.client.iter_participants(entity, limit=2000):
                if getattr(m, "id", None):
                    members.add(int(m.id))
            self._group_members_cache[chat_id] = members
            return user_id in members
        except Exception:
            return True

    async def mute_participant_in_vc(self, chat_id: int, chat_title: str, user_id: int, reason: str) -> None:
        if not self.client or not self.client.is_connected():
            return
        call_id = self._known_calls.get(chat_id) or self._active_joins.get(chat_id)
        access_hash = self._call_access_hashes.get(call_id or 0, 0)
        if not call_id:
            return

        now_ts = asyncio.get_running_loop().time()
        key = (chat_id, user_id)
        if now_ts - self._muted_cooldown.get(key, 0.0) < 4.0:
            return
        self._muted_cooldown[key] = now_ts

        try:
            user_entity = await self.client.get_entity(user_id)
            input_user = InputPeerUser(user_id=user_id, access_hash=int(getattr(user_entity, "access_hash", 0) or 0))
            await self.client(
                EditGroupCallParticipantRequest(
                    call=InputGroupCall(id=int(call_id), access_hash=int(access_hash)),
                    participant=input_user,
                    muted=True,
                    video_stopped=True,
                )
            )
            logger.info("VC_DEBUG: Muted user=%s in VC '%s' (%s) | reason=%s", user_id, chat_title, chat_id, reason)
            await self.db.add_moderation_log(chat_id, chat_title, user_id, None, "VC_MUTE", reason)
        except Exception as exc:
            logger.debug("VC_DEBUG: Mute failed for user=%s in '%s': %s", user_id, chat_title, exc)

    async def check_spoken_text_for_abuse(self, chat_id: int, chat_title: str, user_id: int, spoken_text: str) -> None:
        if not spoken_text:
            return
        if await self.moderation.is_user_admin_or_creator(chat_id, user_id):
            return
        settings = await self.db.get_group_settings(chat_id)
        if not settings.get("mute_on_abuse", 1):
            return
        words = await self.db.get_bad_words(chat_id)
        if not words:
            return
        norm_text = " ".join(spoken_text.lower().split())
        for w in words:
            if w and w in norm_text:
                await self.mute_participant_in_vc(
                    chat_id=chat_id,
                    chat_title=chat_title,
                    user_id=user_id,
                    reason=f"Abusive word detected in VC: '{w}'",
                )
                break

    async def handle_audio_chunk_for_user(
        self, chat_id: int, user_id: int, pcm_bytes: bytes, sample_rate: int = 16000
    ) -> None:
        if not pcm_bytes or (self.assistant_id and int(user_id) == self.assistant_id):
            return
        title = self._chat_titles.get(chat_id, str(chat_id))
        if await self.moderation.is_user_admin_or_creator(chat_id, user_id):
            return
        settings = await self.db.get_group_settings(chat_id)
        if not settings.get("mute_on_abuse", 1):
            return
        words = await self.db.get_bad_words(chat_id)
        if not words:
            return

        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                tmp_path = tf.name
            with wave.open(tmp_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(pcm_bytes)

            loop = asyncio.get_running_loop()
            transcript = await loop.run_in_executor(None, _transcribe_wav_sync, tmp_path)
            if transcript:
                await self.check_spoken_text_for_abuse(chat_id, title, user_id, transcript)
        except Exception as exc:
            logger.debug("VC_DEBUG: Audio abuse check error: %s", exc)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    async def start(self) -> None:
        cleaned = (self.session_string or "").strip()
        if not cleaned:
            logger.warning("VC_DEBUG: SESSION_STRING empty; Assistant VC monitor disabled.")
            return
        try:
            _ensure_silent_wav(SILENT_WAV_PATH)
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

            self.client.add_event_handler(self._on_raw_update, events.Raw)

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
                raw_cid = int(getattr(update, "chat_id", 0) or 0)
                if raw_cid:
                    chat_id = normalize_channel_chat_id(raw_cid) if raw_cid > 0 else raw_cid
                    title = self._chat_titles.get(chat_id, str(chat_id))
                    status_str = str(getattr(update, "status", "")).upper()
                    if "CLOSED_VOICE_CHAT" in status_str or "DISCARDED" in status_str or "LEFT" in status_str:
                        self._spawn_bg(self.leave_vc(chat_id, title, reason=f"CHAT_UPDATE_{status_str}", force=True))
                    elif "CREATED_VOICE_CHAT" in status_str or "ACTIVE" in status_str:
                        self._spawn_bg(self._sync_dialogs_and_calls(auto_join=True))
                return

            participants = getattr(update, "participants", None)
            raw_cid = int(getattr(update, "chat_id", 0) or 0)
            if participants and raw_cid:
                chat_id = normalize_channel_chat_id(raw_cid) if raw_cid > 0 else raw_cid
                title = self._chat_titles.get(chat_id, str(chat_id))
                for p in participants:
                    self._spawn_bg(self._evaluate_participant(chat_id, title, p))

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

                active_items = list(self._active_joins.items())
                if not active_items:
                    continue

                async def _check_one(cid_chat: int, cid_call: int) -> None:
                    async with self._poll_sem:
                        ahash = self._call_access_hashes.get(cid_call, 0)
                        title = self._chat_titles.get(cid_chat, str(cid_chat))
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
                    self._known_calls[chat_id] = cid
                    if ahash:
                        self._call_access_hashes[cid] = ahash
                    if auto_join and chat_id not in self._active_joins:
                        logger.info("VC_DEBUG: Found active VC '%s' (chat_id=%s, call_id=%s)", title, chat_id, cid)
                        await self.join_vc(chat_id, title, cid, ahash)
                        await asyncio.sleep(0.5)
                elif chat_id in self._active_joins:
                    await self.leave_vc(chat_id, title, reason="VC_ENDED_BY_ADMIN", force=True)
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
            for p in participants:
                self._spawn_bg(self._evaluate_participant(chat_id, chat_title, p))
        except Exception as exc:
            logger.debug("VC_DEBUG: GetGroupCallRequest check in '%s': %s", chat_title, exc)

    async def _evaluate_participant(self, chat_id: int, chat_title: str, p: Any) -> None:
        peer = getattr(p, "peer", None)
        uid = (
            getattr(p, "user_id", None)
            or getattr(peer, "user_id", None)
            or getattr(peer, "chat_id", None)
            or getattr(peer, "channel_id", None)
        )
        if not uid or (self.assistant_id and int(uid) == self.assistant_id):
            return
        user_id = int(uid)
        self._seen_group_users.setdefault(chat_id, set()).add(user_id)

        if getattr(p, "left", False):
            return

        if await self.moderation.is_user_admin_or_creator(chat_id, user_id):
            return

        settings = await self.db.get_group_settings(chat_id)

        # 1. Non-group member in VC -> mute
        if settings.get("mute_non_members", 1):
            if not await self._is_group_member(chat_id, user_id):
                await self.mute_participant_in_vc(
                    chat_id=chat_id,
                    chat_title=chat_title,
                    user_id=user_id,
                    reason="Non-group member joined VC",
                )
                return

        video_attr = getattr(p, "video", None)
        camera_attr = getattr(p, "camera", None)
        is_video_on_attr = getattr(p, "is_video_on", None)
        screen_attr = getattr(p, "presentation", None) or getattr(p, "screen_sharing", None) or getattr(p, "is_screen_on", None)
        video_paused = bool(getattr(p, "video_paused", False) or getattr(p, "is_video_paused", False))
        video_stopped = bool(getattr(p, "video_stopped", False) or getattr(p, "is_video_stopped", False))

        has_video_obj = video_attr is not None and not isinstance(video_attr, bool)
        has_video_bool = video_attr is True or is_video_on_attr is True
        has_camera = bool(camera_attr)
        has_screen = bool(screen_attr)

        # 2. Screen sharing -> mute
        if settings.get("mute_on_screen_share", 1) and has_screen and not video_stopped:
            await self.mute_participant_in_vc(
                chat_id=chat_id,
                chat_title=chat_title,
                user_id=user_id,
                reason="Started screen sharing in VC",
            )
            return

        # 3. Camera ON -> mute
        is_video_active = (has_video_obj or has_video_bool or has_camera) and (not video_paused) and (not video_stopped)
        if settings.get("mute_on_camera", 1) and is_video_active:
            await self.mute_participant_in_vc(
                chat_id=chat_id,
                chat_title=chat_title,
                user_id=user_id,
                reason="Camera turned ON in VC",
            )

    async def _resolve_chat_from_update(self, update: Any) -> Tuple[Optional[int], str]:
        raw_chat_id = getattr(update, "chat_id", None)
        call_obj = getattr(update, "call", None)
        call_id = getattr(call_obj, "id", None)

        if raw_chat_id:
            raw_int = int(raw_chat_id)
            chat_id = normalize_channel_chat_id(raw_int) if raw_int > 0 else raw_int
            if call_id:
                self._known_calls[int(call_id)] = chat_id
                self._known_calls[chat_id] = int(call_id)
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
        if chat_id is None:
            return

        if isinstance(update, UpdateGroupCall):
            if isinstance(call_obj, GroupCallDiscarded):
                self._spawn_bg(self.leave_vc(chat_id, chat_title, call_id=cid, reason="VC_END_DISCARDED", force=True))
            elif isinstance(call_obj, GroupCall):
                ahash = int(getattr(call_obj, "access_hash", 0) or 0)
                if ahash:
                    self._call_access_hashes[cid] = ahash
                if cid:
                    self._known_calls[chat_id] = cid
                if chat_id not in self._active_joins:
                    self._spawn_bg(self.join_vc(chat_id, chat_title, cid, ahash))

        elif isinstance(update, UpdateGroupCallParticipants):
            for p in getattr(update, "participants", []) or []:
                self._spawn_bg(self._evaluate_participant(chat_id, chat_title, p))

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
            self._known_calls[chat_id] = call_id
            self._chat_titles[chat_id] = chat_title
            if access_hash:
                self._call_access_hashes[call_id] = access_hash
            wav_file = _ensure_silent_wav(SILENT_WAV_PATH)
            last_err = ""

            for attempt in range(1, self.max_retries + 1):
                try:
                    await self.calls.play(
                        chat_id,
                        MediaStream(wav_file),
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
            self._known_calls.pop(chat_id, None)
            if active_id:
                self._known_calls.pop(active_id, None)
                self._call_access_hashes.pop(active_id, None)

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
        for t in list(self._bg_tasks):
            t.cancel()
        for chat_id in list(self._active_joins.keys()):
            title = self._chat_titles.get(chat_id, str(chat_id))
            await self.leave_vc(chat_id, title, reason="BOT_SHUTDOWN", force=True)
        if self.client and self.client.is_connected():
            await self.client.disconnect()
