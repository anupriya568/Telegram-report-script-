import asyncio
import json
import os

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters, idle
from pyrogram.errors import FloodWait, RPCError
from pyrogram.raw import functions, types
from pyrogram.types import (
    CallbackQuery,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import (
    API_HASH,
    API_ID,
    BOT_TOKEN,
    DATA_FILE,
    OWNER_ID,
    SESSION_STRING,
    VC_POLL_INTERVAL,
)


def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        data = {"chats": [], "bad_words": {}, "locked_mutes": {}}
        save_data(data)
        return data
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("chats", [])
    data.setdefault("bad_words", {})
    data.setdefault("locked_mutes", {})
    return data


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def register_chat(chat_id: int) -> None:
    data = load_data()
    if chat_id not in data["chats"]:
        data["chats"].append(chat_id)
        save_data(data)


def add_bad_word(chat_id: int, word: str) -> bool:
    data = load_data()
    key = str(chat_id)
    word = word.strip().lower()
    if not word:
        return False
    words = data["bad_words"].setdefault(key, [])
    if word not in words:
        words.append(word)
        save_data(data)
        return True
    return False


def remove_bad_word(chat_id: int, word: str) -> bool:
    data = load_data()
    key = str(chat_id)
    word = word.strip().lower()
    words = data["bad_words"].get(key, [])
    if word in words:
        words.remove(word)
        save_data(data)
        return True
    return False


def get_bad_words(chat_id: int) -> list[str]:
    data = load_data()
    return data["bad_words"].get(str(chat_id), [])


def set_locked_mute(chat_id: int, user_id: int, reason: str) -> None:
    data = load_data()
    c_key, u_key = str(chat_id), str(user_id)
    data["locked_mutes"].setdefault(c_key, {})[u_key] = reason
    save_data(data)


def clear_locked_mute(chat_id: int, user_id: int) -> None:
    data = load_data()
    c_key, u_key = str(chat_id), str(user_id)
    if c_key in data["locked_mutes"] and u_key in data["locked_mutes"][c_key]:
        del data["locked_mutes"][c_key][u_key]
        save_data(data)


def get_locked_mute_reason(chat_id: int, user_id: int) -> str | None:
    data = load_data()
    return data["locked_mutes"].get(str(chat_id), {}).get(str(user_id))


def main_menu_keyboard(is_owner: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("📌 कमांड्स (Help)", callback_data="btn_help"),
            InlineKeyboardButton("🚫 शब्द सूची", callback_data="btn_words"),
        ]
    ]
    if is_owner:
        rows.append(
            [
                InlineKeyboardButton("💾 DB बैकअप", callback_data="btn_backup"),
                InlineKeyboardButton("📊 स्टैट्स", callback_data="btn_stats"),
            ]
        )
    return InlineKeyboardMarkup(rows)


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 वापस मेनू", callback_data="btn_home")]]
    )


def get_stats_text() -> str:
    data = load_data()
    total_chats = len(data.get("chats", []))
    total_locked = sum(len(v) for v in data.get("locked_mutes", {}).values())
    return (
        f"📊 **बॉट स्टैट्स**\n"
        f"• जुड़े हुए ग्रुप्स/चैट्स: **{total_chats}**\n"
        f"• एडमिन-लॉक्ड म्यूट यूज़र्स: **{total_locked}**"
    )


def get_help_text() -> str:
    return (
        "📌 **उपलब्ध कमांड्स:**\n"
        "• `/start` या `/panel` — बटन मेनू खोलें\n"
        "• `/addword <शब्द>` — प्रतिबंधित शब्द जोड़ें (Admin)\n"
        "• `/delword <शब्द>` — प्रतिबंधित शब्द हटाएं (Admin)\n"
        "• `/words` — प्रतिबंधित शब्दों की सूची देखें (Admin)\n"
        "• `/unmute` (रिप्लाई या `/unmute <user_id>`) — सिर्फ़ Admin अनम्यूट कर सकते हैं\n"
        "• `/stats` — स्टैट्स देखें (Owner)\n"
        "• `/backup` — पूरी DB फ़ाइल बैकअप लें (Owner)\n"
        "• `/broadcast <मैसेज>` — सभी जुड़े ग्रुप्स/चैट्स में ब्रॉडकास्ट करें (Owner)"
    )


def register_handlers(bot: Client, assistant: Client) -> None:
    async def is_chat_admin(chat_id: int, user_id: int) -> bool:
        if user_id == OWNER_ID:
            return True
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            return member.status.name in ("ADMINISTRATOR", "OWNER")
        except RPCError:
            return False

    async def is_group_member(chat_id: int, user_id: int) -> bool:
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            return member.status.name not in ("LEFT", "BANNED")
        except RPCError:
            return False

    async def mute_user_locked(
        chat_id: int, user_id: int, reason: str, announce_text: str | None = None
    ) -> bool:
        try:
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(),
            )
            set_locked_mute(chat_id, user_id, reason)
            if announce_text:
                await bot.send_message(chat_id, announce_text)
            return True
        except FloodWait as e:
            await asyncio.sleep(e.value)
            return False
        except RPCError:
            return False

    async def unmute_user_admin(
        chat_id: int, user_id: int, force_clear_lock: bool = True
    ) -> bool:
        try:
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                ),
            )
            if force_clear_lock:
                clear_locked_mute(chat_id, user_id)
            return True
        except RPCError:
            return False

    async def send_backup_to_owner(reply_target: Message | CallbackQuery) -> None:
        load_data()
        try:
            await bot.send_document(
                chat_id=OWNER_ID,
                document=DATA_FILE,
                caption="💾 **बॉट DB बैकअप**\nयह आपकी मौजूदा डेटा फ़ाइल है—इसे सुरक्षित रखें।",
            )
            msg = "✅ DB बैकअप फ़ाइल आपके प्राइवेट चैट में भेज दी गई है।"
            if isinstance(reply_target, CallbackQuery):
                await reply_target.answer("✅ बैकअप भेज दिया गया!", show_alert=False)
                if reply_target.message and reply_target.message.chat.id != OWNER_ID:
                    await reply_target.message.reply_text(msg)
            else:
                if reply_target.chat.id != OWNER_ID:
                    await reply_target.reply_text(msg)
        except RPCError as e:
            err_text = (
                f"❌ बैकअप भेजने में समस्या आई: `{e}`\n"
                "कृपया पहले बॉट के प्राइवेट चैट में `/start` भेजें।"
            )
            if isinstance(reply_target, CallbackQuery):
                await reply_target.answer("❌ बैकअप नहीं भेजा जा सका", show_alert=True)
                if reply_target.message:
                    await reply_target.message.reply_text(err_text)
            else:
                await reply_target.reply_text(err_text)

    @bot.on_message(group=-1)
    async def debug_msg(client: Client, message: Message):
        uid = message.from_user.id if message.from_user else None
        cid = message.chat.id if message.chat else None
        print(f"[BOT MSG] chat={cid} user={uid} text={message.text!r}")

    @bot.on_callback_query(group=-1)
    async def debug_btn(client: Client, callback_query: CallbackQuery):
        uid = callback_query.from_user.id if callback_query.from_user else None
        print(f"[BOT BTN] user={uid} data={callback_query.data!r}")

    @bot.on_message(filters.new_chat_members)
    async def on_new_members(client: Client, message: Message):
        register_chat(message.chat.id)
        me = await bot.get_me()
        for member in message.new_chat_members:
            if member.id == me.id:
                await message.reply_text(
                    "👋 नमस्ते! बॉट सक्रिय हो गया है।\n"
                    "कृपया बॉट और असिस्टेंट दोनों को **Restrict Members** / **Manage Video Chats** परमिशन दें।\n"
                    "बटन और कमांड्स के लिए `/panel` या `/help` लिखें।"
                )

    @bot.on_message(filters.command(["start", "panel"]))
    async def start_or_panel_cmd(client: Client, message: Message):
        register_chat(message.chat.id)
        is_owner = bool(message.from_user and message.from_user.id == OWNER_ID)
        await message.reply_text(
            "🛡️ **VC & Group Guard Bot** तैयार है!\nनीचे दिए गए बटन दबाकर सीधे काम करें:",
            reply_markup=main_menu_keyboard(is_owner=is_owner),
        )

    @bot.on_message(filters.command("help"))
    async def help_cmd(client: Client, message: Message):
        is_owner = bool(message.from_user and message.from_user.id == OWNER_ID)
        await message.reply_text(
            get_help_text(),
            reply_markup=main_menu_keyboard(is_owner=is_owner),
        )

    @bot.on_message(filters.command("backup") & filters.user(OWNER_ID))
    async def backup_cmd(client: Client, message: Message):
        await send_backup_to_owner(message)

    @bot.on_message(filters.command("addword") & filters.group)
    async def addword_cmd(client: Client, message: Message):
        register_chat(message.chat.id)
        if not message.from_user or not await is_chat_admin(
            message.chat.id, message.from_user.id
        ):
            await message.reply_text("⛔ यह कमांड केवल एडमिन चला सकते हैं।")
            return
        if len(message.command) < 2:
            await message.reply_text("उपयोग: `/addword <शब्द>`")
            return
        word = " ".join(message.command[1:])
        if add_bad_word(message.chat.id, word):
            await message.reply_text(f"✅ शब्द `{word}` प्रतिबंधित सूची में जोड़ दिया गया है।")
        else:
            await message.reply_text("ℹ️ यह शब्द पहले से सूची में मौजूद है।")

    @bot.on_message(filters.command("delword") & filters.group)
    async def delword_cmd(client: Client, message: Message):
        register_chat(message.chat.id)
        if not message.from_user or not await is_chat_admin(
            message.chat.id, message.from_user.id
        ):
            await message.reply_text("⛔ यह कमांड केवल एडमिन चला सकते हैं।")
            return
        if len(message.command) < 2:
            await message.reply_text("उपयोग: `/delword <शब्द>`")
            return
        word = " ".join(message.command[1:])
        if remove_bad_word(message.chat.id, word):
            await message.reply_text(f"🗑️ शब्द `{word}` सूची से हटा दिया गया है।")
        else:
            await message.reply_text("ℹ️ यह शब्द सूची में नहीं मिला।")

    @bot.on_message(filters.command("words"))
    async def words_cmd(client: Client, message: Message):
        register_chat(message.chat.id)
        words = get_bad_words(message.chat.id)
        if not words:
            await message.reply_text("इस चैट में अभी कोई प्रतिबंधित शब्द नहीं जोड़ा गया है।")
        else:
            await message.reply_text("🚫 **प्रतिबंधित शब्द:**\n" + ", ".join(f"`{w}`" for w in words))

    @bot.on_message(filters.command("unmute") & filters.group)
    async def unmute_cmd(client: Client, message: Message):
        register_chat(message.chat.id)
        if not message.from_user or not await is_chat_admin(
            message.chat.id, message.from_user.id
        ):
            await message.reply_text("⛔ म्यूट किए गए यूज़र को केवल एडमिन ही अनम्यूट कर सकते हैं।")
            return
        target_user_id = None
        if message.reply_to_message and message.reply_to_message.from_user:
            target_user_id = message.reply_to_message.from_user.id
        elif len(message.command) >= 2 and message.command[1].isdigit():
            target_user_id = int(message.command[1])
        if not target_user_id:
            await message.reply_text(
                "कृपया जिस यूज़र को अनम्यूट करना है उसके मैसेज पर रिप्लाई करें या `/unmute <user_id>` लिखें।"
            )
            return
        ok = await unmute_user_admin(message.chat.id, target_user_id, force_clear_lock=True)
        if ok:
            await message.reply_text(f"🔊 यूज़र `{target_user_id}` को एडमिन द्वारा अनम्यूट कर दिया गया है।")
        else:
            await message.reply_text("❌ अनम्यूट करने में समस्या आई। कृपया बॉट की एडमिन परमिशन जांचें।")

    @bot.on_message(filters.command("stats") & filters.user(OWNER_ID))
    async def stats_cmd(client: Client, message: Message):
        await message.reply_text(get_stats_text())

    @bot.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
    async def broadcast_cmd(client: Client, message: Message):
        if len(message.command) < 2 and not message.reply_to_message:
            await message.reply_text(
                "उपयोग: `/broadcast <मैसेज>` या किसी मैसेज पर रिप्लाई करके `/broadcast` लिखें।"
            )
            return
        data = load_data()
        chats = data.get("chats", [])
        sent, failed = 0, 0
        for chat_id in chats:
            try:
                if message.reply_to_message:
                    await message.reply_to_message.copy(chat_id)
                else:
                    text = message.text.split(None, 1)[1]
                    await bot.send_message(chat_id, text)
                sent += 1
                await asyncio.sleep(0.15)
            except FloodWait as e:
                await asyncio.sleep(e.value)
                failed += 1
            except RPCError:
                failed += 1
        await message.reply_text(f"📢 **ब्रॉडकास्ट पूरा हुआ**\n• सफल: {sent}\n• असफल: {failed}")

    @bot.on_callback_query()
    async def button_callback_handler(client: Client, callback_query: CallbackQuery):
        data = callback_query.data
        uid = callback_query.from_user.id if callback_query.from_user else 0
        is_owner = uid == OWNER_ID
        chat_id = callback_query.message.chat.id if callback_query.message else 0

        if data == "btn_home":
            await callback_query.answer()
            if callback_query.message:
                await callback_query.message.edit_text(
                    "🛡️ **VC & Group Guard Bot** तैयार है!\nनीचे दिए गए बटन दबाकर सीधे काम करें:",
                    reply_markup=main_menu_keyboard(is_owner=is_owner),
                )
            return
        if data == "btn_help":
            await callback_query.answer()
            if callback_query.message:
                await callback_query.message.edit_text(get_help_text(), reply_markup=back_keyboard())
            return
        if data == "btn_words":
            await callback_query.answer()
            words = get_bad_words(chat_id) if chat_id else []
            text = (
                "इस चैट में अभी कोई प्रतिबंधित शब्द नहीं जोड़ा गया है।"
                if not words
                else "🚫 **प्रतिबंधित शब्द:**\n" + ", ".join(f"`{w}`" for w in words)
            )
            if callback_query.message:
                await callback_query.message.edit_text(text, reply_markup=back_keyboard())
            return
        if data == "btn_stats":
            if not is_owner:
                await callback_query.answer("⛔ यह बटन केवल Owner के लिए है।", show_alert=True)
                return
            await callback_query.answer()
            if callback_query.message:
                await callback_query.message.edit_text(get_stats_text(), reply_markup=back_keyboard())
            return
        if data == "btn_backup":
            if not is_owner:
                await callback_query.answer("⛔ यह बटन केवल Owner के लिए है।", show_alert=True)
                return
            await send_backup_to_owner(callback_query)
            return

    @bot.on_message(filters.group & filters.text & ~filters.service)
    async def filter_bad_words(client: Client, message: Message):
        register_chat(message.chat.id)
        if not message.from_user:
            return
        user_id = message.from_user.id
        if await is_chat_admin(message.chat.id, user_id):
            return
        text_lower = message.text.lower()
        for word in get_bad_words(message.chat.id):
            if word in text_lower:
                try:
                    await message.delete()
                except RPCError:
                    pass
                await mute_user_locked(
                    message.chat.id,
                    user_id,
                    reason=f"bad_word:{word}",
                    announce_text=(
                        f"🔇 प्रतिबंधित शब्द का उपयोग करने पर `{message.from_user.first_name}` को म्यूट कर दिया गया है।\n"
                        f"*(केवल एडमिन ही अनम्यूट कर सकते हैं।)*"
                    ),
                )
                break


async def monitor_video_chats(bot: Client, assistant: Client) -> None:
    await asyncio.sleep(3)
    while True:
        try:
            data = load_data()
            for chat_id in list(data.get("chats", [])):
                try:
                    chat = await assistant.get_chat(chat_id)
                    if chat.type.name not in ("GROUP", "SUPERGROUP"):
                        continue
                    full_chat = await assistant.invoke(
                        functions.channels.GetFullChannel(
                            channel=await assistant.resolve_peer(chat_id)
                        )
                    )
                    call = getattr(full_chat.full_chat, "call", None)
                    if not call:
                        continue
                    participants_resp = await assistant.invoke(
                        functions.phone.GetGroupParticipants(
                            call=call, ids=[], sources=[], offset="", limit=100
                        )
                    )
                    for p in participants_resp.participants:
                        uid = p.peer.user_id if isinstance(p.peer, types.PeerUser) else None
                        if not uid:
                            continue
                        if await is_chat_admin(bot, chat_id, uid):
                            continue
                        camera_on = bool(getattr(p, "video", None))
                        in_group = await is_group_member(bot, chat_id, uid)
                        if camera_on and get_locked_mute_reason(chat_id, uid) != "camera_on":
                            await bot.restrict_chat_member(chat_id, uid, ChatPermissions())
                            set_locked_mute(chat_id, uid, "camera_on")
                            await bot.send_message(
                                chat_id,
                                f"📷 VC में कैमरा ऑन करने पर यूज़र `{uid}` को म्यूट कर दिया गया है। *(केवल एडमिन अनम्यूट कर सकते हैं।)*",
                            )
                            continue
                        if not in_group and get_locked_mute_reason(chat_id, uid) != "vc_without_group":
                            await bot.restrict_chat_member(chat_id, uid, ChatPermissions())
                            set_locked_mute(chat_id, uid, "vc_without_group")
                            await bot.send_message(
                                chat_id,
                                f"🔇 ग्रुप जॉइन किए बिना VC में आने पर यूज़र `{uid}` को म्यूट कर दिया गया है। *(केवल एडमिन अनम्यूट कर सकते हैं।)*",
                            )
                            continue
                        if not get_locked_mute_reason(chat_id, uid):
                            try:
                                await bot.restrict_chat_member(
                                    chat_id,
                                    uid,
                                    ChatPermissions(
                                        can_send_messages=True,
                                        can_send_media_messages=True,
                                        can_send_other_messages=True,
                                        can_add_web_page_previews=True,
                                    ),
                                )
                            except RPCError:
                                pass
                except RPCError:
                    continue
                await asyncio.sleep(0.3)
        except Exception:
            pass
        await asyncio.sleep(VC_POLL_INTERVAL)


async def main() -> None:
    if not SESSION_STRING or SESSION_STRING == "YOUR_ASSISTANT_SESSION_STRING":
        print("कृपया config.py में SESSION_STRING की जगह अपनी असली String Session पेस्ट करें।")
        return

    bot = Client(
        "vc_guard_bot_client",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
    )
    assistant = Client(
        "vc_guard_assistant_client",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
    )

    register_handlers(bot, assistant)
    await bot.start()
    print("Bot started successfully!")
    await assistant.start()
    print("Assistant started successfully!")
    vc_task = asyncio.create_task(monitor_video_chats(bot, assistant))
    try:
        await idle()
    finally:
        vc_task.cancel()
        await assistant.stop()
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
