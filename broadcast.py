import asyncio
import logging
from typing import List, Set

from telegram import Bot, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import Database

logger = logging.getLogger("modguard.broadcast")


class BroadcastManager:
    def __init__(self, bot: Bot, db: Database, owner_id: int) -> None:
        self.bot = bot
        self.db = db
        self.owner_id = int(owner_id)
        self._tasks: Set[asyncio.Task] = set()

    async def _worker(
        self,
        category: str,
        text: str,
        scope: str,
        unique_targets: List[int],
        report_chat_id: int,
    ) -> None:
        sent = failed = 0
        try:
            for tid in unique_targets:
                try:
                    await self.bot.send_message(chat_id=tid, text=text, parse_mode="HTML")
                    sent += 1
                except TelegramError:
                    failed += 1
                await asyncio.sleep(0.06)

            await self.db.record_broadcast(category, text, scope, sent, failed)
            try:
                await self.bot.send_message(
                    chat_id=report_chat_id,
                    text=f"✅ Broadcast ({category}) complete: sent={sent}, failed={failed}",
                )
            except TelegramError:
                pass
        except asyncio.CancelledError:
            logger.info("Background broadcast (%s) cancelled on shutdown.", category)
        except Exception as exc:
            logger.exception("Background broadcast error (%s): %s", category, exc)

    async def dispatch(self, update: Update, context: ContextTypes.DEFAULT_TYPE, category: str) -> None:
        if not update.effective_user or int(update.effective_user.id) != self.owner_id or not update.message:
            return
        args = context.args or []
        cmd_label = "updatecast" if category == "update_broadcast" else "broadcast"
        if not args:
            await update.message.reply_text(
                f"Usage: <code>/{cmd_label} [users|groups|all] message</code>",
                parse_mode="HTML",
            )
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
            targets.extend(await self.db.get_all_user_ids())
        if scope in ("groups", "all"):
            targets.extend(await self.db.get_all_group_ids())
        unique_targets = list(dict.fromkeys(targets))

        await update.message.reply_text(
            f"🚀 Broadcast ({category}, scope={scope}) background mein shuru ho gaya hai ({len(unique_targets)} targets)."
        )
        task = asyncio.create_task(
            self._worker(
                category=category,
                text=text,
                scope=scope,
                unique_targets=unique_targets,
                report_chat_id=update.effective_chat.id if update.effective_chat else self.owner_id,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def cancel_all(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
          
