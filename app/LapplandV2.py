import os
import discord
from groq import Groq

# ── Config ──────────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GROQ_API_KEY  = os.environ.get("GROQ_API_KEY")
BOT_NAME      = "Lappland"          # change to whatever you want
REPLY_TO_ALL  = True           # True = responds to every message in allowed channels
ALLOWED_CHANNELS = [1483716134250217572]          # list of channel IDs to respond in, e.g. [123456, 789012]
                               # leave empty [] to respond in ALL channels
# ────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""you are {BOT_NAME}. you're in a discord server. be normal. short replies unless the question needs detail. no asterisks. don't mention being an AI. different people talk in the same channel - pay attention to who said what and treat each person's messages in context of what THEY said, not the whole conversation."""

groq_client = Groq(api_key=GROQ_API_KEY)

# stores conversation history per channel: {channel_id: [messages]}
histories = {}
MAX_HISTORY = 20  # messages to remember per channel

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)


def get_ai_response(channel_id: int, user_message: str, username: str) -> str:
    if channel_id not in histories:
        histories[channel_id] = []

    histories[channel_id].append({
        "role": "user",
        "content": f"{username}: {user_message}"
    })

    # trim history if too long
    if len(histories[channel_id]) > MAX_HISTORY:
        histories[channel_id] = histories[channel_id][-MAX_HISTORY:]

    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "system", "content": SYSTEM_PROMPT}] + histories[channel_id],
        max_tokens=300,
        temperature=0.9,
    )

    reply = response.choices[0].message.content.strip()

    histories[channel_id].append({
        "role": "assistant",
        "content": reply
    })

    return reply


@bot.event
async def on_ready():
    print(f"logged in as {bot.user} ✓")


@bot.event
async def on_message(message: discord.Message):
    # ignore own messages
    if message.author == bot.user:
        return

    # check channel restrictions
    if ALLOWED_CHANNELS and message.channel.id not in ALLOWED_CHANNELS:
        return

    # respond if: mentioned, replied to, or REPLY_TO_ALL is on
    mentioned   = bot.user in message.mentions
    replied_to  = (
        message.reference and
        message.reference.resolved and
        getattr(message.reference.resolved, "author", None) == bot.user
    )

    if not (REPLY_TO_ALL or mentioned or replied_to):
        return

    # strip the bot mention from the message if present
    content = message.content
    for mention in message.mentions:
        content = content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
    content = content.strip()

    if not content:
        return

    async with message.channel.typing():
        try:
            reply = get_ai_response(message.channel.id, content, message.author.display_name)
            await message.reply(reply, mention_author=False)
        except Exception as e:
            print(f"error: {e}")
            await message.reply("lol something broke on my end, try again")


bot.run(DISCORD_TOKEN)