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
            InlineKeyboardButton("📌 Help", callback_data="btn_help"),
            InlineKeyboardButton("🚫 Words", callback_data="btn_words"),
        ]
    ]
    if is_owner:
        rows.append(
            [
                InlineKeyboardButton("💾 Backup", callback_data="btn_backup"),
                InlineKeyboardButton("📊 Stats", callback_data="btn_stats"),
            ]
        )
    return InlineKeyboardMarkup(rows)


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 Back", callback_data="btn_home")]]
    )


def get_stats_text() -> str:
    data = load_data()
    total_chats = len(data.get("chats", []))
    total_locked = sum(len(v) for v in data.get("locked_mutes", {}).values())
    return (
        f"📊 **Stats**\n"
        f"• Chats: **{total_chats}**\n"
        f"• Locked Mutes: **{total_locked}**"
    )


def get_help_text() -> str:
    return (
        "📌 **Commands**\n"
        "• `/start` या `/panel` — बटन मेनू\n"
        "• `/addword <word>` — शब्द जोड़ें (Group Admin)\n"
        "• `/delword <word>` — शब्द हटाएं (Group Admin)\n"
        "• `/words` — शब्द सूची देखें\n"
        "• `/unmute` (रिप्लाई या `/unmute <id>`) — अनम्यूट (Group Admin)\n"
        "• `/stats` — स्टैट्स (Owner)\n"
        "• `/backup` — DB फ़ाइल भेजें (Owner)\n"
        "• `/broadcast <msg>` — ब्रॉडकास्ट (Owner)"
    )


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
        print(f"[MUTED] chat={chat_id} user={user_id} reason={reason}")
        if announce_text:
            await bot.send_message(chat_id, announce_text)
        return True
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return False
    except RPCError as e:
        print(f"[MUTE ERROR] chat={chat_id} user={user_id} err={e}")
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
        print(f"[UNMUTED] chat={chat_id} user={user_id}")
        return True
    except RPCError as e:
        print(f"[UNMUTE ERROR] chat={chat_id} user={user_id} err={e}")
        return False


async def send_backup_to_owner(reply_target: Message | CallbackQuery) -> None:
    load_data()
    try:
        await bot.send_document(
            chat_id=OWNER_ID,
            document=DATA_FILE,
            caption="💾 **DB Backup**",
        )
        msg = "✅ Backup फ़ाइल आपकी प्राइवेट चैट में भेज दी गई है।"
        if isinstance(reply_target, CallbackQuery):
            await reply_target.answer("✅ Backup भेज दिया!", show_alert=False)
            if reply_target.message and reply_target.message.chat.id != OWNER_ID:
                await reply_target.message.reply_text(msg)
        else:
            if reply_target.chat.id != OWNER_ID:
                await reply_target.reply_text(msg)
    except RPCError as e:
        err_text = f"❌ Backup एरर: `{e}`\nपहले बॉट के PM में `/start` भेजें।"
        if isinstance(reply_target, CallbackQuery):
            await reply_target.answer("❌ Backup नहीं भेजा जा सका", show_alert=True)
            if reply_target.message:
                await reply_target.message.reply_text(err_text)
        else:
            await reply_target.reply_text(err_text)


@bot.on_message(group=-1)
async def debug_msg(client: Client, message: Message):
    uid = message.from_user.id if message.from_user else None
    cid = message.chat.id if message.chat else None
    print(f"[MSG] chat={cid} user={uid} text={message.text!r}")


@bot.on_callback_query(group=-1)
async def debug_btn(client: Client, callback_query: CallbackQuery):
    uid = callback_query.from_user.id if callback_query.from_user else None
    print(f"[BTN] user={uid} data={callback_query.data!r}")


@bot.on_message(filters.new_chat_members)
async def on_new_members(client: Client, message: Message):
    register_chat(message.chat.id)
    me = await bot.get_me()
    for member in message.new_chat_members:
        if member.id == me.id:
            await message.reply_text(
                "👋 बॉट जुड़ गया! कृपया इसे **Restrict Members** और **Manage Video Chats** परमिशन दें।"
            )


@bot.on_message(filters.command(["start", "panel"]))
async def start_or_panel_cmd(client: Client, message: Message):
    register_chat(message.chat.id)
    is_owner = bool(message.from_user and message.from_user.id == OWNER_ID)
    await message.reply_text(
        "🛡️ **VC & Group Guard Bot** तैयार है:",
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
    if not message.from_user or not await is_chat_admin(message.chat.id, message.from_user.id):
        await message.reply_text("⛔ यह कमांड केवल Admin के लिए है।")
        return
    if len(message.command) < 2:
        await message.reply_text("उपयोग: `/addword <शब्द>`")
        return
    word = " ".join(message.command[1:])
    if add_bad_word(message.chat.id, word):
        await message.reply_text(f"✅ `{word}` जोड़ा गया।")
    else:
        await message.reply_text("ℹ️ पहले से मौजूद है।")


@bot.on_message(filters.command("delword") & filters.group)
async def delword_cmd(client: Client, message: Message):
    register_chat(message.chat.id)
    if not message.from_user or not await is_chat_admin(message.chat.id, message.from_user.id):
        await message.reply_text("⛔ यह कमांड केवल Admin के लिए है।")
        return
    if len(message.command) < 2:
        await message.reply_text("उपयोग: `/delword <शब्द>`")
        return
    word = " ".join(message.command[1:])
    if remove_bad_word(message.chat.id, word):
        await message.reply_text(f"🗑️ `{word}` हटाया गया।")
    else:
        await message.reply_text("ℹ️ सूची में नहीं मिला।")


@bot.on_message(filters.command("words"))
async def words_cmd(client: Client, message: Message):
    register_chat(message.chat.id)
    words = get_bad_words(message.chat.id)
    if not words:
        await message.reply_text("कोई प्रतिबंधित शब्द नहीं है।")
    else:
        await message.reply_text("🚫 **Words:** " + ", ".join(f"`{w}`" for w in words))


@bot.on_message(filters.command("unmute") & filters.group)
async def unmute_cmd(client: Client, message: Message):
    register_chat(message.chat.id)
    if not message.from_user or not await is_chat_admin(message.chat.id, message.from_user.id):
        await message.reply_text("⛔ केवल Admin अनम्यूट कर सकते हैं।")
        return
    target_user_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user_id = message.reply_to_message.from_user.id
    elif len(message.command) >= 2 and message.command[1].isdigit():
        target_user_id = int(message.command[1])
    if not target_user_id:
        await message.reply_text("मैसेज पर रिप्लाई करके `/unmute` या `/unmute <user_id>` लिखें।")
        return
    if await unmute_user_admin(message.chat.id, target_user_id, force_clear_lock=True):
        await message.reply_text(f"🔊 `{target_user_id}` अनम्यूट हो गया।")
    else:
        await message.reply_text("❌ अनम्यूट नहीं हो सका (बॉट परमिशन चेक करें)।")


@bot.on_message(filters.command("stats") & filters.user(OWNER_ID))
async def stats_cmd(client: Client, message: Message):
    await message.reply_text(get_stats_text())


@bot.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
async def broadcast_cmd(client: Client, message: Message):
    if len(message.command) < 2 and not message.reply_to_message:
        await message.reply_text("उपयोग: `/broadcast <मैसेज>` या रिप्लाई करके `/broadcast` लिखें।")
        return
    data = load_data()
    sent, failed = 0, 0
    for chat_id in data.get("chats", []):
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
    await message.reply_text(f"📢 Sent: {sent} | Failed: {failed}")


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
                "🛡️ **VC & Group Guard Bot** तैयार है:",
                reply_markup=main_menu_keyboard(is_owner=is_owner),
            )
    elif data == "btn_help":
        await callback_query.answer()
        if callback_query.message:
            await callback_query.message.edit_text(get_help_text(), reply_markup=back_keyboard())
    elif data == "btn_words":
        await callback_query.answer()
        words = get_bad_words(chat_id) if chat_id else []
        txt = "कोई शब्द नहीं है।" if not words else "🚫 **Words:** " + ", ".join(f"`{w}`" for w in words)
        if callback_query.message:
            await callback_query.message.edit_text(txt, reply_markup=back_keyboard())
    elif data == "btn_stats":
        if not is_owner:
            await callback_query.answer("⛔ सिर्फ़ Owner के लिए।", show_alert=True)
            return
        await callback_query.answer()
        if callback_query.message:
            await callback_query.message.edit_text(get_stats_text(), reply_markup=back_keyboard())
    elif data == "btn_backup":
        if not is_owner:
            await callback_query.answer("⛔ सिर्फ़ Owner के लिए।", show_alert=True)
            return
        await send_backup_to_owner(callback_query)


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
                announce_text=f"🔇 प्रतिबंधित शब्द लिखने पर `{message.from_user.first_name}` को म्यूट कर दिया गया है।",
            )
            break


async def monitor_video_chats() -> None:
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
                        if await is_chat_admin(chat_id, uid):
                            continue
                        camera_on = bool(getattr(p, "video", None))
                        in_group = await is_group_member(chat_id, uid)
                        if camera_on and get_locked_mute_reason(chat_id, uid) != "camera_on":
                            await mute_user_locked(
                                chat_id,
                                uid,
                                reason="camera_on",
                                announce_text=f"📷 VC में कैमरा ऑन करने पर `{uid}` को म्यूट कर दिया गया है।",
                            )
                            continue
                        if not in_group and get_locked_mute_reason(chat_id, uid) != "vc_without_group":
                            await mute_user_locked(
                                chat_id,
                                uid,
                                reason="vc_without_group",
                                announce_text=f"🔇 बिना ग्रुप जॉइन किए VC में आने पर `{uid}` को म्यूट कर दिया गया है।",
                            )
                            continue
                        if not get_locked_mute_reason(chat_id, uid):
                            await unmute_user_admin(chat_id, uid, force_clear_lock=False)
                except RPCError:
                    continue
                await asyncio.sleep(0.3)
        except Exception:
            pass
        await asyncio.sleep(VC_POLL_INTERVAL)


async def main() -> None:
    if not SESSION_STRING or SESSION_STRING == "YOUR_ASSISTANT_SESSION_STRING":
        print("कृपया config.py में SESSION_STRING डालें।")
        return

    vc_task = asyncio.create_task(monitor_video_chats())
    try:
        await bot.start()
        await assistant.start()
        me_b = await bot.get_me()
        me_a = await assistant.get_me()
        print(f"Bot ready: @{me_b.username} | Assistant ready: {me_a.id}")
        await idle()
    finally:
        vc_task.cancel()
        if assistant.is_connected:
            await assistant.stop()
        if bot.is_connected:
            await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
    
