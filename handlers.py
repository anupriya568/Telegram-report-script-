import asyncio
import html
import os
import time
from typing import List, Optional, Set

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
)

from broadcast import BroadcastManager
from database import Database
from moderation import ModerationEngine
from vc_controller import AssistantVCController


def format_uptime(start_mono: float) -> str:
    sec = max(0, int(time.monotonic() - start_mono))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return f"{d}d {h}h {m}m {s}s"


def build_start_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    clean_user = (bot_username or "").lstrip("@").strip()
    add_url = f"https://t.me/{clean_user}?startgroup=true" if clean_user else "https://t.me/"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Add to Your Group", url=add_url)],
            [
                InlineKeyboardButton("🛡️ Features", callback_data="mg_features"),
                InlineKeyboardButton("📖 How to Use", callback_data="mg_how_to_use"),
            ],
        ]
    )


def build_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back", callback_data="mg_back_start")]]
    )


def _extract_caption_input(message: Optional[Message], args: Optional[List[str]]) -> Optional[str]:
    if args:
        return " ".join(args).strip()
    if message and message.reply_to_message:
        rep = message.reply_to_message
        raw = rep.text or rep.caption or ""
        if raw.strip():
            if rep.text_html:
                return rep.text_html.strip()
            if rep.caption_html:
                return rep.caption_html.strip()
            return html.escape(raw.strip())
    return None


def register_handlers(
    app: Application,
    db: Database,
    moderation: ModerationEngine,
    vc_controller: AssistantVCController,
    broadcaster: BroadcastManager,
    owner_id: int,
    start_mono: float,
    notify_owner_cb,
) -> None:
    active_tag_tasks: dict[int, asyncio.Task] = {}

    def is_owner(user_id: Optional[int]) -> bool:
        return user_id is not None and int(user_id) == int(owner_id)

    async def is_group_owner_or_admin(chat_id: int, user_id: Optional[int]) -> bool:
        if not user_id:
            return False
        if is_owner(user_id):
            return True
        if await moderation.is_user_admin_or_creator(chat_id, int(user_id)):
            return True
        try:
            member = await app.bot.get_chat_member(chat_id=chat_id, user_id=int(user_id))
            return getattr(member, "status", None) in ("creator", "administrator")
        except TelegramError:
            return False

    async def try_assistant_join_group(chat_id: int, chat_title: str) -> bool:
        if not vc_controller.client or not vc_controller.client.is_connected():
            return False
        try:
            invite = await app.bot.create_chat_invite_link(
                chat_id=chat_id,
                name="ModGuard Assistant Auto-Join",
                member_limit=1,
            )
            if invite and invite.invite_link:
                return await vc_controller.join_group_via_invite(invite.invite_link, chat_id, chat_title)
        except TelegramError:
            pass
        return False

    async def send_group_activation_notice(chat_id: int, chat_title: str) -> None:
        joined = await try_assistant_join_group(chat_id, chat_title)
        if joined:
            msg_text = "ModGuard is now activated in your group."
        else:
            msg_text = (
                "ModGuard is now activated in your group.\n"
                "Note: VC moderation ke liye assistant account ko bhi group mein add karein aur bot ko Invite Users admin permission dein."
            )
        try:
            await app.bot.send_message(chat_id=chat_id, text=msg_text)
        except TelegramError:
            pass

    async def _get_group_link(chat_id: int) -> str:
        try:
            chat_obj = await app.bot.get_chat(chat_id)
            if getattr(chat_obj, "username", None):
                return f"https://t.me/{chat_obj.username}"
            if getattr(chat_obj, "invite_link", None):
                return str(chat_obj.invite_link)
            inv = await app.bot.create_chat_invite_link(chat_id=chat_id, name="ModGuard Groups View")
            if inv and inv.invite_link:
                return str(inv.invite_link)
        except TelegramError:
            pass
        return "Link unavailable (bot needs Invite permission)"

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
            title = chat.title or str(chat.id)
            await moderation.ensure_bot_is_admin(chat.id, title)
            await send_group_activation_notice(chat.id, title)
            return

        intro_text = await db.get_intro_caption()
        me = await app.bot.get_me()
        keyboard = build_start_keyboard(me.username or "")
        if update.message:
            try:
                await update.message.reply_text(
                    intro_text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            except BadRequest:
                await update.message.reply_text(
                    html.escape(intro_text),
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )

    async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_owner(update.effective_user.id):
            return
        db_file_path = await db.flush_for_backup()
        if not os.path.exists(db_file_path):
            if update.message:
                await update.message.reply_text("❌ Database file nahi mili.")
            return

        file_size = os.path.getsize(db_file_path)
        caption = (
            f"💾 <b>ModGuardBot Database Backup</b>\n"
            f"File: <code>{html.escape(os.path.basename(db_file_path))}</code>\n"
            f"Size: <b>{file_size}</b> bytes"
        )
        try:
            with open(db_file_path, "rb") as fh:
                await app.bot.send_document(
                    chat_id=int(owner_id),
                    document=fh,
                    filename=os.path.basename(db_file_path),
                    caption=caption,
                    parse_mode="HTML",
                )
            if update.message and update.effective_chat and update.effective_chat.id != int(owner_id):
                await update.message.reply_text("✅ Database backup aapke DM mein bhej diya gaya hai.")
        except TelegramError as exc:
            if update.message:
                await update.message.reply_text(f"❌ Backup bhejne mein error aaya: <code>{exc}</code>", parse_mode="HTML")

    async def _handle_text_set(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        setter_coro,
        label: str,
        cmd_name: str,
    ) -> None:
        if not update.effective_user or not is_owner(update.effective_user.id):
            return
        new_text = _extract_caption_input(update.message, context.args)
        if not new_text:
            if update.message:
                await update.message.reply_text(
                    f"Usage: <code>/{cmd_name} Naya {label} text yahan likhein</code> ya kisi message ko reply karke <code>/{cmd_name}</code> likhein.",
                    parse_mode="HTML",
                )
            return
        await setter_coro(new_text)
        if update.message:
            await update.message.reply_text(
                f"✅ <b>{label.title()} update ho gaya hai!</b>\n\n<b>Preview:</b>\n" + new_text,
                parse_mode="HTML",
            )

    async def cmd_setintro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _handle_text_set(update, context, db.set_intro_caption, "intro caption", "setintro")

    async def cmd_resetintro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_owner(update.effective_user.id):
            return
        await db.reset_intro_caption()
        if update.message:
            await update.message.reply_text("🔄 Intro caption default par reset ho gaya hai.", parse_mode="HTML")

    async def cmd_setfeatures(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _handle_text_set(update, context, db.set_features_caption, "features text", "setfeatures")

    async def cmd_resetfeatures(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_owner(update.effective_user.id):
            return
        await db.reset_features_caption()
        if update.message:
            await update.message.reply_text("🔄 Features text default par reset ho gaya hai.", parse_mode="HTML")

    async def cmd_setusage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _handle_text_set(update, context, db.set_how_to_use_caption, "how to use guide", "setusage")

    async def cmd_resetusage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_owner(update.effective_user.id):
            return
        await db.reset_how_to_use_caption()
        if update.message:
            await update.message.reply_text("🔄 How to Use guide default par reset ho gaya hai.", parse_mode="HTML")

    async def cmd_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_owner(update.effective_user.id):
            return
        groups = await db.get_active_groups()
        if not groups:
            if update.message:
                await update.message.reply_text("Kisi active group mein bot add nahi hai.")
            return

        lines: List[str] = [f"📌 <b>Active Groups ({len(groups)}):</b>\n"]
        for idx, g in enumerate(groups, start=1):
            gid = int(g["chat_id"])
            title = html.escape(str(g["title"]))
            adm = "Admin ✅" if g.get("is_bot_admin") else "Not Admin ⚠️"
            link = html.escape(await _get_group_link(gid))
            lines.append(f"{idx}. <b>{title}</b> (<code>{gid}</code>) — {adm}\n   🔗 {link}")

        if update.message:
            await update.message.reply_text(
                "\n\n".join(lines),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

    async def cmd_wipegroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_owner(update.effective_user.id):
            return
        args = context.args or []
        if not args or args[-1].upper() != "CONFIRM":
            if update.message:
                await update.message.reply_text(
                    "⚠️ <b>Warning:</b> Yeh command target group ke non-admin members ko remove/ban karegi.\n"
                    "Confirm karne ke liye likhein:\n"
                    "<code>/wipegroup &lt;chat_id&gt; CONFIRM</code>",
                    parse_mode="HTML",
                )
            return

        try:
            target_chat_id = int(args[0])
        except ValueError:
            if update.message:
                await update.message.reply_text("Invalid <code>chat_id</code>.", parse_mode="HTML")
            return

        grp = await db.get_group(target_chat_id)
        target_title = grp["title"] if grp else str(target_chat_id)

        if not await moderation.ensure_bot_is_admin(target_chat_id, target_title):
            if update.message:
                await update.message.reply_text("❌ Bot is group mein admin nahi hai ya access nahi hai.")
            return

        candidate_ids: Set[int] = set(await db.get_all_user_ids())
        candidate_ids.update(await vc_controller.collect_group_participants(target_chat_id))

        banned, skipped = await moderation.mass_ban_known_users(target_chat_id, target_title, candidate_ids)
        if update.message:
            await update.message.reply_text(
                f"💥 <b>Group Wipe Complete</b>\n"
                f"Group: <b>{html.escape(target_title)}</b> (<code>{target_chat_id}</code>)\n"
                f"• Removed/Banned: <b>{banned}</b>\n"
                f"• Skipped (Admins/Other): <b>{skipped}</b>",
                parse_mode="HTML",
            )

    async def cmd_addword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if not chat or chat.type not in ("group", "supergroup") or not update.message:
            return
        if not await is_group_owner_or_admin(chat.id, user.id if user else None):
            await update.message.reply_text("❌ Sirf group owner ya admin word add kar sakte hain.")
            return
        raw_word = " ".join(context.args or []).strip()
        if not raw_word:
            await update.message.reply_text("Usage: <code>/addword &lt;word&gt;</code>", parse_mode="HTML")
            return
        await db.upsert_group(chat.id, chat.title or str(chat.id), await moderation.is_bot_admin(chat.id))
        added = await db.add_bad_word(chat.id, raw_word, added_by=user.id if user else None)
        if added:
            await update.message.reply_text(
                f"✅ Word added to group filter: <code>{html.escape(raw_word.lower())}</code>",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                f"ℹ️ Word pehle se list mein hai: <code>{html.escape(raw_word.lower())}</code>",
                parse_mode="HTML",
            )

    async def cmd_delword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if not chat or chat.type not in ("group", "supergroup") or not update.message:
            return
        if not await is_group_owner_or_admin(chat.id, user.id if user else None):
            await update.message.reply_text("❌ Sirf group owner ya admin word remove kar sakte hain.")
            return
        raw_word = " ".join(context.args or []).strip()
        if not raw_word:
            await update.message.reply_text("Usage: <code>/delword &lt;word&gt;</code>", parse_mode="HTML")
            return
        removed = await db.remove_bad_word(chat.id, raw_word)
        if removed:
            await update.message.reply_text(
                f"🗑️ Word removed: <code>{html.escape(raw_word.lower())}</code>",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text("❌ Yeh word is group ki list mein nahi mila.")

    async def cmd_words(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if not chat or chat.type not in ("group", "supergroup") or not update.message:
            return
        if not await is_group_owner_or_admin(chat.id, user.id if user else None):
            await update.message.reply_text("❌ Sirf group owner ya admin word list dekh sakte hain.")
            return
        words = await db.get_bad_words(chat.id)
        if not words:
            await update.message.reply_text("Is group mein koi custom word add nahi hai.")
            return
        formatted = ", ".join(f"<code>{html.escape(w)}</code>" for w in words)
        await update.message.reply_text(
            f"📝 <b>Group Custom Words ({len(words)}):</b>\n{formatted}",
            parse_mode="HTML",
        )

    async def cmd_vcsettings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if not chat or chat.type not in ("group", "supergroup") or not update.message:
            return
        if not await is_group_owner_or_admin(chat.id, user.id if user else None):
            await update.message.reply_text("❌ Sirf group owner ya admin VC settings badal sakte hain.")
            return

        key_map = {
            "camera": "mute_on_camera",
            "screen": "mute_on_screen_share",
            "nonmember": "mute_non_members",
            "abuse": "mute_on_abuse",
        }
        if context.args and len(context.args) >= 2:
            k_raw = context.args[0].lower()
            v_raw = context.args[1].lower()
            if k_raw in key_map and v_raw in ("on", "off"):
                await db.set_group_setting(chat.id, key_map[k_raw], v_raw == "on")

        s = await db.get_group_settings(chat.id)
        await update.message.reply_text(
            f"🛡️ <b>VC Moderation Settings — {html.escape(chat.title or str(chat.id))}</b>\n"
            f"• Camera Mute (<code>camera</code>): <b>{'ON' if s.get('mute_on_camera', 1) else 'OFF'}</b>\n"
            f"• Screen Share Mute (<code>screen</code>): <b>{'ON' if s.get('mute_on_screen_share', 1) else 'OFF'}</b>\n"
            f"• Non-Member VC Mute (<code>nonmember</code>): <b>{'ON' if s.get('mute_non_members', 1) else 'OFF'}</b>\n"
            f"• Abusive Audio Mute (<code>abuse</code>): <b>{'ON' if s.get('mute_on_abuse', 1) else 'OFF'}</b>\n\n"
            f"Toggle: <code>/vcsettings camera|screen|nonmember|abuse on|off</code>",
            parse_mode="HTML",
        )

    async def _run_tagall_background(chat_id: int, reason_text: str) -> None:
        try:
            member_ids: List[int] = []
            if vc_controller.client and vc_controller.client.is_connected():
                try:
                    entity = await vc_controller.client.get_entity(chat_id)
                    async for m in vc_controller.client.iter_participants(entity, limit=2000):
                        if getattr(m, "id", None) and not getattr(m, "bot", False):
                            member_ids.append(int(m.id))
                except Exception:
                    pass

            if not member_ids:
                member_ids = [uid for uid in await vc_controller.collect_group_participants(chat_id) if uid > 0]

            if not member_ids:
                try:
                    await app.bot.send_message(chat_id=chat_id, text="ℹ️ Tag karne ke liye members nahi mile.")
                except TelegramError:
                    pass
                return

            batch_size = 5
            prefix = f"📣 <b>{html.escape(reason_text)}</b>\n" if reason_text else "📣 "
            for i in range(0, len(member_ids), batch_size):
                chunk = member_ids[i : i + batch_size]
                mentions = ", ".join(f'<a href="tg://user?id={uid}">User</a>' for uid in chunk)
                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=f"{prefix}{mentions}",
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except TelegramError:
                    pass
                # Non-blocking yield so other groups, broadcasts, and commands never stall
                await asyncio.sleep(1.5)
        finally:
            active_tag_tasks.pop(chat_id, None)

    async def cmd_tagall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if not chat or chat.type not in ("group", "supergroup") or not update.message:
            return
        if not await is_group_owner_or_admin(chat.id, user.id if user else None):
            await update.message.reply_text("❌ Sirf group owner ya admin /tagall use kar sakte hain.")
            return

        existing = active_tag_tasks.get(chat.id)
        if existing and not existing.done():
            await update.message.reply_text("ℹ️ Is group mein tagging pehle se chal rahi hai.")
            return

        reason_text = _extract_caption_input(update.message, context.args) or ""
        await update.message.reply_text("🏷️ Tagging background mein start ho gayi hai (baaki kaam bina ruke chalenge).")
        active_tag_tasks[chat.id] = asyncio.create_task(_run_tagall_background(chat.id, reason_text))

    async def on_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data:
            return
        await query.answer()

        if query.data == "mg_features":
            features_text = await db.get_features_caption()
            try:
                await query.edit_message_text(
                    features_text,
                    parse_mode="HTML",
                    reply_markup=build_back_keyboard(),
                    disable_web_page_preview=True,
                )
            except BadRequest:
                pass
        elif query.data == "mg_how_to_use":
            usage_text = await db.get_how_to_use_caption()
            try:
                await query.edit_message_text(
                    usage_text,
                    parse_mode="HTML",
                    reply_markup=build_back_keyboard(),
                    disable_web_page_preview=True,
                )
            except BadRequest:
                pass
        elif query.data == "mg_back_start":
            intro_text = await db.get_intro_caption()
            me = await app.bot.get_me()
            keyboard = build_start_keyboard(me.username or "")
            try:
                await query.edit_message_text(
                    intro_text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            except BadRequest:
                await query.edit_message_text(
                    html.escape(intro_text),
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )

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
            f"/backup | /groups | /wipegroup &lt;chat_id&gt; CONFIRM\n"
            f"/setintro | /resetintro | /setfeatures | /resetfeatures | /setusage | /resetusage\n"
            f"/maintenance on|off | /broadcast | /updatecast | /logs [n] | /alerts",
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

    async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        asyncio.create_task(broadcaster.dispatch(update, context, "admin_broadcast"))

    async def cmd_updatecast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        asyncio.create_task(broadcaster.dispatch(update, context, "update_broadcast"))

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
        if not chat or chat.type not in ("group", "supergroup"):
            return
        title = chat.title or str(chat.id)
        new_status = update.my_chat_member.new_chat_member.status if update.my_chat_member else None
        old_status = update.my_chat_member.old_chat_member.status if update.my_chat_member else None
        if new_status in ("left", "kicked"):
            await db.mark_group_inactive(chat.id)
            return
        await moderation.ensure_bot_is_admin(chat.id, title)
        if old_status in ("left", "kicked") and new_status in ("member", "administrator"):
            await send_group_activation_notice(chat.id, title)

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        if notify_owner_cb:
            await notify_owner_cb("critical_error", f"🚨 <b>Critical Error</b>\n<code>{context.error}</code>")

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("setintro", cmd_setintro))
    app.add_handler(CommandHandler("resetintro", cmd_resetintro))
    app.add_handler(CommandHandler("setfeatures", cmd_setfeatures))
    app.add_handler(CommandHandler("resetfeatures", cmd_resetfeatures))
    app.add_handler(CommandHandler("setusage", cmd_setusage))
    app.add_handler(CommandHandler("resetusage", cmd_resetusage))
    app.add_handler(CommandHandler("groups", cmd_groups))
    app.add_handler(CommandHandler("wipegroup", cmd_wipegroup))
    app.add_handler(CommandHandler("addword", cmd_addword))
    app.add_handler(CommandHandler("delword", cmd_delword))
    app.add_handler(CommandHandler("words", cmd_words))
    app.add_handler(CommandHandler("vcsettings", cmd_vcsettings))
    app.add_handler(CommandHandler("tagall", cmd_tagall))
    app.add_handler(CallbackQueryHandler(on_start_callback, pattern=r"^mg_(features|how_to_use|back_start)$"))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("maintenance", cmd_maintenance))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("updatecast", cmd_updatecast))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(on_error)
