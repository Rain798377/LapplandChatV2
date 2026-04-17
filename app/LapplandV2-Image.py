import os
import discord
import io
import requests
from groq import Groq

# ── Config ──────────────────────────────────────────────────────────────────
DISCORD_TOKEN    = os.environ.get("DISCORD_TOKEN")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY")
HF_TOKEN         = os.environ.get("HF_TOKEN")
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


def generate_image(prompt: str) -> bytes:
    response = requests.post(
        "https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell",
        headers={"Authorization": f"Bearer {HF_TOKEN}"},
        json={"inputs": prompt},
        timeout=60,
    )
    response.raise_for_status()
    return response.content


def get_ai_response(channel_id: int, user_message: str, username: str) -> tuple:
    if channel_id not in histories:
        histories[channel_id] = []

    histories[channel_id].append({"role": "user", "content": f"{username}: {user_message}"})
    if len(histories[channel_id]) > MAX_HISTORY:
        histories[channel_id] = histories[channel_id][-MAX_HISTORY:]

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "system", "content": SYSTEM_PROMPT}] + histories[channel_id],
        max_tokens=300,
        temperature=0.9,
    )

    reply = response.choices[0].message.content.strip()
    print(f"DEBUG reply: {repr(reply)}")
    histories[channel_id].append({"role": "assistant", "content": reply})
    return ("text", reply)


def is_image_request(message: str) -> bool:
    keywords = ["draw", "generate", "create", "make", "paint", "sketch", "render", "!draw"]
    image_words = ["image", "picture", "pic", "photo", "art", "illustration"]
    msg = message.lower()
    # !draw shortcut
    if msg.startswith("!draw "):
        return True
    # keyword combo check
    return any(k in msg for k in keywords) and any(i in msg for i in image_words)


def extract_image_prompt(message: str) -> str:
    # for !draw just take everything after the command
    if message.lower().startswith("!draw "):
        return message[6:].strip()
    # otherwise ask llama to extract a clean image prompt
    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "system", "content": "Extract a clean, detailed image generation prompt from the user's message. Return ONLY the prompt, nothing else. No preamble, no explanation."},
            {"role": "user", "content": message}
        ],
        max_tokens=100,
        temperature=0.7,
    )
    return response.choices[0].message.content.strip()


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

    if not content and not message.attachments:
        return

    async with message.channel.typing():
        try:
            if is_image_request(content):
                prompt = extract_image_prompt(content)
                print(f"DEBUG image prompt: {repr(prompt)}")
                image_bytes = generate_image(prompt)
                await message.reply(file=discord.File(io.BytesIO(image_bytes), filename="image.png"), mention_author=False)
            else:
                result = get_ai_response(message.channel.id, content, message.author.display_name)
                await message.reply(result[1], mention_author=False)
        except Exception as e:
            print(f"error: {e}")
            import traceback
            traceback.print_exc()
            await message.reply("lol something broke on my end, try again")


bot.run(DISCORD_TOKEN)