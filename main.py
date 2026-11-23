import asyncio

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client
from config import API_ID, API_HASH, BOT_TOKEN

app = Client("ping_test", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

@app.on_message()
async def got_msg(client, message):
    print("GOT UPDATE:", message.from_user.id if message.from_user else None, message.text)
    await message.reply_text("Pong! Update received.")

app.run()
