import os
import discord
import httpx
import base64
import io
from discord import app_commands
from groq import Groq

# ── Config ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN    = os.environ.get("DISCORD_TOKEN")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY")
FAL_API_KEY      = os.environ.get("FAL_API_KEY")
BOT_NAME         = "Lappland"
REPLY_TO_ALL     = True
ALLOWED_CHANNELS = [1483716134250217572]
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""you are {BOT_NAME}. you're in a discord server. be normal. short replies unless the question needs detail. no asterisks. don't mention being an AI. different people talk in the same channel - pay attention to who said what and treat each person's messages in context of what THEY said, not the whole conversation. Do not be so formal, talk casually. You may use short terms such as lmao, lol, bruh, etc. Make sure it fits the tone of the conversation."""

groq_client = Groq(api_key=GROQ_API_KEY)
histories   = {}
MAX_HISTORY = 20

# ── Bot setup with command tree ───────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True

class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print("slash commands synced", flush=True)

bot = Bot()


# ── Image generation ──────────────────────────────────────────────────────────
async def generate_image(prompt: str) -> bytes | None:
    print(f"[img-gen] prompt: {prompt}", flush=True)
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://fal.run/fal-ai/flux/schnell",
            headers={
                "Authorization": f"Key {FAL_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"prompt": prompt, "image_size": "square_hd", "num_images": 1}
        )
        data = response.json()
        print(f"[img-gen] fal response: {data}", flush=True)
        if not data.get("images"):
            return None
        image_url = data["images"][0]["url"]
        img_response = await client.get(image_url)
        print(f"[img-gen] downloaded {len(img_response.content)} bytes", flush=True)
        return img_response.content


# ── Slash command ─────────────────────────────────────────────────────────────
@bot.tree.command(name="imagine", description="Generate an image")
@app_commands.describe(prompt="What do you want to generate?")
async def imagine(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer()
    image_bytes = await generate_image(prompt)
    if image_bytes:
        await interaction.followup.send(
            content=f'"{prompt}"',
            file=discord.File(fp=io.BytesIO(image_bytes), filename="generated.png")
        )
    else:
        await interaction.followup.send("generation failed, try again")


# ── AI response (text or vision) ──────────────────────────────────────────────
async def get_ai_response(channel_id: int, user_message: str, username: str, image_url: str = None) -> str:
    if channel_id not in histories:
        histories[channel_id] = []

    if image_url:
        print(f"[vision] fetching image: {image_url}", flush=True)
        async with httpx.AsyncClient() as client:
            img_response = await client.get(image_url)
            img_b64 = base64.b64encode(img_response.content).decode("utf-8")
            content_type = img_response.headers.get("content-type", "image/png").split(";")[0]
        print(f"[vision] got image, content-type: {content_type}, size: {len(img_response.content)} bytes", flush=True)

        response = groq_client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{img_b64}"}},
                    {"type": "text", "text": user_message if user_message else "what's in this image? be casual and brief."}
                ]}
            ],
            max_tokens=300,
        )
        reply = response.choices[0].message.content.strip()
        print(f"[vision] reply: {reply}", flush=True)
        return reply

    # normal text path
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


# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"logged in as {bot.user} ✓", flush=True)


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

    print(f"[msg] author={message.author.display_name} content={repr(content)}", flush=True)
    print(f"[msg] attachments={message.attachments}", flush=True)
    print(f"[msg] embeds={message.embeds}", flush=True)

    # check attachments first
    image_url = None
    if message.attachments:
        for a in message.attachments:
            print(f"[msg] attachment: url={a.url} content_type={a.content_type}", flush=True)
        image_attachments = [a for a in message.attachments if a.content_type and a.content_type.startswith("image/")]
        if image_attachments:
            image_url = image_attachments[0].url
            print(f"[msg] using attachment image: {image_url}", flush=True)

    # fallback: check embeds
    if not image_url and message.embeds:
        for embed in message.embeds:
            print(f"[msg] embed type={embed.type} image={embed.image} thumbnail={embed.thumbnail}", flush=True)
            if embed.image and embed.image.url:
                image_url = embed.image.url
                print(f"[msg] using embed image: {image_url}", flush=True)
                break
            if embed.thumbnail and embed.thumbnail.url:
                image_url = embed.thumbnail.url
                print(f"[msg] using embed thumbnail: {image_url}", flush=True)
                break

    if not image_url:
        print("[msg] no image found", flush=True)

    if not content and not image_url:
        return

    async with message.channel.typing():
        try:
            reply = await get_ai_response(message.channel.id, content, message.author.display_name, image_url)
            await message.reply(reply, mention_author=False)
        except Exception as e:
            print(f"[error] {e}", flush=True)
            await message.reply("lol something broke on my end, try again")

bot.run(DISCORD_TOKEN)