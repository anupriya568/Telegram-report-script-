import asyncio
import logging
import time
from typing import Dict, Iterable, Optional, Set, Tuple

from telegram import Bot
from telegram.error import BadRequest, Forbidden, TelegramError

from database import Database

logger = logging.getLogger("modguard.moderation")


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

    async def is_user_admin_or_creator(self, chat_id: int, user_id: int) -> bool:
        try:
            member = await self.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            return member.status in ("administrator", "creator")
        except (Forbidden, BadRequest, TelegramError):
            return False

    async def ensure_bot_is_admin(self, chat_id: int, chat_title: str) -> bool:
        try:
            me = await self.bot.get_me()
            member = await self.bot.get_chat_member(chat_id=chat_id, user_id=me.id)
            is_admin = member.status in ("administrator", "creator")
        except Forbidden:
            await self.db.mark_group_inactive(chat_id)
            return False
        except BadRequest:
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

    async def remove_user_from_group(
        self,
        chat_id: int,
        chat_title: str,
        user_id: int,
        reason: str,
    ) -> bool:
        """Remove non-admin user from group (kick without permanent ban)."""
        bucket = str(int(time.time() // 15))
        if await self._is_duplicate(chat_id, user_id, "GROUP_REMOVE_CAMERA", bucket, ttl=15.0):
            return False
        try:
            if await self.is_user_admin_or_creator(chat_id, user_id):
                logger.info("User %s is admin/creator in %s; skipping group remove.", user_id, chat_id)
                return False

            await self.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await self.bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
            await self.db.add_moderation_log(
                chat_id=chat_id,
                chat_title=chat_title,
                user_id=user_id,
                username=None,
                action_type="GROUP_REMOVE_CAMERA",
                reason=reason,
            )
            logger.info("GROUP REMOVED user %s from '%s' (%s) | reason: %s", user_id, chat_title, chat_id, reason)
            return True
        except (Forbidden, BadRequest, TelegramError) as exc:
            logger.warning("Group remove failed for %s in %s: %s", user_id, chat_id, exc)
            return False

    async def mass_ban_known_users(self, chat_id: int, chat_title: str, user_ids: Iterable[int]) -> Tuple[int, int]:
        """Ban non-admin users from the given user_ids in a target group."""
        me = await self.bot.get_me()
        banned = 0
        skipped = 0
        for uid in dict.fromkeys(int(u) for u in user_ids if u):
            if uid == me.id or await self.is_user_admin_or_creator(chat_id, uid):
                skipped += 1
                continue
            try:
                await self.bot.ban_chat_member(chat_id=chat_id, user_id=uid)
                banned += 1
            except (Forbidden, BadRequest, TelegramError):
                skipped += 1
            await asyncio.sleep(0.08)

        await self.db.add_moderation_log(
            chat_id=chat_id,
            chat_title=chat_title,
            user_id=None,
            username=None,
            action_type="MASS_WIPE_MEMBERS",
            reason=f"Owner wiped non-admin members: banned={banned}, skipped={skipped}",
        )
        return banned, skipped
          
