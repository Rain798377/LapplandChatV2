import os
import discord
from groq import Groq

# ── Config ──────────────────────────────────────────────────────────────────
DISCORD_TOKEN    = os.environ.get("DISCORD_TOKEN")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY")
BOT_NAME         = "Lappland"
REPLY_TO_ALL     = True
ALLOWED_CHANNELS = [1483716134250217572]
# ────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""you are {BOT_NAME}. you're in a discord server. be normal. short replies unless the question needs detail. no asterisks. don't mention being an AI. different people talk in the same channel - pay attention to who said what and treat each person's messages in context of what THEY said, not the whole conversation. Do not be so formal, talk casually. You may use short terms such as lmao, lol, bruh, etc. Make sure it fits the tone of the conversation."""

groq_client = Groq(api_key=GROQ_API_KEY)
histories   = {}
MAX_HISTORY = 20

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)


def get_ai_response(channel_id: int, user_message: str, username: str, image_url: str = None) -> str:
    if channel_id not in histories:
        histories[channel_id] = []

    if image_url:
        # llava handles the image — not added to history since it can't accept image content in history
        response = groq_client.chat.completions.create(
            model="llava-v1.5-7b-4096-preview",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": f"{username}: {user_message or 'what is this?'}"}
                ]}
            ],
            max_tokens=300,
            temperature=0.9,
        )
    else:
        histories[channel_id].append({"role": "user", "content": f"{username}: {user_message}"})
        if len(histories[channel_id]) > MAX_HISTORY:
            histories[channel_id] = histories[channel_id][-MAX_HISTORY:]

        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + histories[channel_id],
            max_tokens=300,
            temperature=0.9,
        )

    reply = response.choices[0].message.content.strip()
    histories[channel_id].append({"role": "assistant", "content": reply})
    return reply


@bot.event
async def on_ready():
    print(f"logged in as {bot.user} ✓")


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    if ALLOWED_CHANNELS and message.channel.id not in ALLOWED_CHANNELS:
        return

    mentioned  = bot.user in message.mentions
    replied_to = (
        message.reference and
        message.reference.resolved and
        getattr(message.reference.resolved, "author", None) == bot.user
    )
    if not (REPLY_TO_ALL or mentioned or replied_to):
        return

    content = message.content
    for mention in message.mentions:
        content = content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
    content = content.strip()

    # allow image-only messages through (no text is fine)
    if not content and not message.attachments:
        return

    async with message.channel.typing():
        try:
            image_url = message.attachments[0].url if message.attachments else None
            reply = get_ai_response(message.channel.id, content, message.author.display_name, image_url)
            await message.reply(reply, mention_author=False)
        except Exception as e:
            print(f"error: {e}")
            import traceback
            traceback.print_exc()
            await message.reply("lol something broke on my end, try again")


bot.run(DISCORD_TOKEN)