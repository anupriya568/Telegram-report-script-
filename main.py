import ast
import asyncio
import logging
import signal
import sys
import time

from telegram.ext import Application

import config as cfg
from broadcast import BroadcastManager
from database import Database
from handlers import register_handlers
from moderation import ModerationEngine
from vc_controller import AssistantVCController

logging.basicConfig(
    level=getattr(logging, getattr(cfg, "LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("modguard.main")


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
    broadcaster = BroadcastManager(
        bot=app.bot,
        db=db,
        owner_id=int(cfg.OWNER_ID),
    )

    register_handlers(
        app=app,
        db=db,
        moderation=moderation,
        vc_controller=vc_controller,
        broadcaster=broadcaster,
        owner_id=int(cfg.OWNER_ID),
        start_mono=start_mono,
        notify_owner_cb=notify_owner,
    )

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
        await broadcaster.cancel_all()
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
  
