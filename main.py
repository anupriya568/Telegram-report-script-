import asyncio
import json
import os

# Python 3.14+ में Pyrogram इम्पोर्ट करने से पहले event loop सेट करें
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters
from pyrogram.errors import FloodWait, RPCError
from pyrogram.raw import functions, types
from pyrogram.types import ChatPermissions, Message

from config import (
    API_HASH,
    API_ID,
    BOT_TOKEN,
    DATA_FILE,
    OWNER_ID,
    SESSION_STRING,
    VC_POLL_INTERVAL,
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


# ================= DATA / DATABASE =================
def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        data = {
            "chats": [],
            "bad_words": {},
            "locked_mutes": {},
        }
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
    c_key = str(chat_id)
    u_key = str(user_id)
    data["locked_mutes"].setdefault(c_key, {})[u_key] = reason
    save_data(data)


def clear_locked_mute(chat_id: int, user_id: int) -> None:
    data = load_data()
    c_key = str(chat_id)
    u_key = str(user_id)
    if c_key in data["locked_mutes"] and u_key in data["locked_mutes"][c_key]:
        del data["locked_mutes"][c_key][u_key]
        save_data(data)


def get_locked_mute_reason(chat_id: int, user_id: int) -> str | None:
    data = load_data()
    return data["locked_mutes"].get(str(chat_id), {}).get(str(user_id))


# ================= MODERATION HELPERS =================
async def is_chat_admin(client: Client, chat_id: int, user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status.name in ("ADMINISTRATOR", "OWNER")
    except RPCError:
        return False


async def is_group_member(client: Client, chat_id: int, user_id: int) -> bool:
    try:
        member = await client.get_chat_member(chat_id, user_id)
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


# ================= VC MONITORING (ASSISTANT) =================
def _extract_peer_user_id(peer) -> int | None:
    if isinstance(peer, types.PeerUser):
        return peer.user_id
    return None


async def monitor_video_chats():
    while True:
        try:
            data = load_data()
            chats = list(data.get("chats", []))

            for chat_id in chats:
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
                            call=call,
                            ids=[],
                            sources=[],
                            offset="",
                            limit=100,
                        )
                    )

                    for p in participants_resp.participants:
                        uid = _extract_peer_user_id(p.peer)
                        if not uid:
                            continue

                        if await is_chat_admin(bot, chat_id, uid):
                            continue

                        camera_on = bool(getattr(p, "video", None))
                        in_group = await is_group_member(bot, chat_id, uid)

                        if camera_on:
                            if get_locked_mute_reason(chat_id, uid) != "camera_on":
                                await mute_user_locked(
                                    chat_id,
                                    uid,
                                    reason="camera_on",
                                    announce_text=(
                                        f"📷 VC में कैमरा ऑन करने पर यूज़र `{uid}` को म्यूट कर दिया गया है। "
                                        f"*(केवल एडमिन अनम्यूट कर सकते हैं।)*"
                                    ),
                                )
                            continue

                        if not in_group:
                            if get_locked_mute_reason(chat_id, uid) != "vc_without_group":
                                await mute_user_locked(
                                    chat_id,
                                    uid,
                                    reason="vc_without_group",
                                    announce_text=(
                                        f"🔇 ग्रुप जॉइन किए बिना VC में आने पर यूज़र `{uid}` को म्यूट कर दिया गया है। "
                                        f"*(केवल एडमिन अनम्यूट कर सकते हैं।)*"
                                    ),
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


# ================= BOT COMMANDS & TEXT FILTER =================
@bot.on_message(filters.new_chat_members)
async def on_new_members(client: Client, message: Message):
    register_chat(message.chat.id)
    me = await client.get_me()
    for member in message.new_chat_members:
        if member.id == me.id:
            await message.reply_text(
                "👋 नमस्ते! बॉट सक्रिय हो गया है।\n"
                "कृपया बॉट और असिस्टेंट दोनों को **Restrict Members** / **Manage Video Chats** परमिशन दें।\n"
                "कमांड्स के लिए `/help` लिखें।"
            )


@bot.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Client, message: Message):
    register_chat(message.chat.id)
    await message.reply_text(
        "🛡️ **VC & Group Guard Bot** तैयार है!\n"
        "किसी भी ग्रुप में बॉट और असिस्टेंट को जोड़कर एडमिन बनाएं—यह अपने आप काम शुरू कर देगा।"
    )


@bot.on_message(filters.command("help"))
async def help_cmd(client: Client, message: Message):
    await message.reply_text(
        "📌 **उपलब्ध कमांड्स:**\n"
        "• `/addword <शब्द>` — प्रतिबंधित शब्द जोड़ें (Admin)\n"
        "• `/delword <शब्द>` — प्रतिबंधित शब्द हटाएं (Admin)\n"
        "• `/words` — प्रतिबंधित शब्दों की सूची देखें (Admin)\n"
        "• `/unmute` (रिप्लाई करके या `/unmute <user_id>`) — सिर्फ़ Admin अनम्यूट कर सकते हैं\n"
        "• `/stats` — स्टैट्स देखें (Owner)\n"
        "• `/backup` — पूरी DB फ़ाइल बैकअप लें (Owner)\n"
        "• `/broadcast <मैसेज>` — सभी जुड़े ग्रुप्स/चैट्स में ब्रॉडकास्ट करें (Owner)"
    )


@bot.on_message(filters.command("backup") & filters.user(OWNER_ID))
async def backup_cmd(client: Client, message: Message):
    load_data()
    try:
        await client.send_document(
            chat_id=OWNER_ID,
            document=DATA_FILE,
            caption="💾 **बॉट DB बैकअप**\nयह आपकी मौजूदा डेटा फ़ाइल है—इसे सुरक्षित रखें।",
        )
        if message.chat.id != OWNER_ID:
            await message.reply_text("✅ DB बैकअप फ़ाइल आपके प्राइवेट चैट में भेज दी गई है।")
    except RPCError as e:
        await message.reply_text(
            f"❌ बैकअप भेजने में समस्या आई: `{e}`\n"
            "कृपया पहले बॉट के प्राइवेट चैट में `/start` भेजें।"
        )


@bot.on_message(filters.command("addword") & filters.group)
async def addword_cmd(client: Client, message: Message):
    register_chat(message.chat.id)
    if not message.from_user or not await is_chat_admin(
        client, message.chat.id, message.from_user.id
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
        client, message.chat.id, message.from_user.id
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


@bot.on_message(filters.command("words") & filters.group)
async def words_cmd(client: Client, message: Message):
    register_chat(message.chat.id)
    words = get_bad_words(message.chat.id)
    if not words:
        await message.reply_text("इस ग्रुप में अभी कोई प्रतिबंधित शब्द नहीं जोड़ा गया है।")
    else:
        await message.reply_text("🚫 **प्रतिबंधित शब्द:**\n" + ", ".join(f"`{w}`" for w in words))


@bot.on_message(filters.command("unmute") & filters.group)
async def unmute_cmd(client: Client, message: Message):
    register_chat(message.chat.id)
    if not message.from_user or not await is_chat_admin(
        client, message.chat.id, message.from_user.id
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
    data = load_data()
    total_chats = len(data.get("chats", []))
    total_locked = sum(len(v) for v in data.get("locked_mutes", {}).values())
    await message.reply_text(
        f"📊 **बॉट स्टैट्स**\n"
        f"• जुड़े हुए ग्रुप्स/चैट्स: **{total_chats}**\n"
        f"• एडमिन-लॉक्ड म्यूट यूज़र्स: **{total_locked}**"
    )


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
                await client.send_message(chat_id, text)
            sent += 1
            await asyncio.sleep(0.15)
        except FloodWait as e:
            await asyncio.sleep(e.value)
            failed += 1
        except RPCError:
            failed += 1

    await message.reply_text(f"📢 **ब्रॉडकास्ट पूरा हुआ**\n• सफल: {sent}\n• असफल: {failed}")


@bot.on_message(filters.group & filters.text & ~filters.service)
async def filter_bad_words(client: Client, message: Message):
    register_chat(message.chat.id)
    if not message.from_user:
        return

    user_id = message.from_user.id
    if await is_chat_admin(client, message.chat.id, user_id):
        return

    text_lower = message.text.lower()
    bad_words = get_bad_words(message.chat.id)

    for word in bad_words:
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


async def main():
    if not SESSION_STRING or SESSION_STRING == "YOUR_ASSISTANT_SESSION_STRING":
        print("कृपया config.py में SESSION_STRING की जगह अपनी असली String Session पेस्ट करें।")
        return

    await bot.start()
    await assistant.start()
    asyncio.create_task(monitor_video_chats())
    print("Bot and Assistant are running...")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
        
