import os
import json
import random
import discord
from groq import Groq
from discord import app_commands
import yt_dlp
import asyncio
import tempfile
import glob
import secrets

# ── Config ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN    = os.environ.get("DISCORD_TOKEN")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY")
BOT_NAME         = "Lappland"
REPLY_TO_ALL     = True
ALLOWED_CHANNELS = [1483716134250217572]
MIN_CHARS        = 5
REPLY_CHANCE     = random.uniform(0.8, 0.9)
MEMORY_FILE      = "data/memory.json"
MAX_FILE_SIZE_MB = 25
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""you are {BOT_NAME}. you're in a discord server. be normal. short replies unless the question needs detail. no asterisks. don't mention being an AI. different people talk in the same channel - pay attention to who said what and treat each person's messages in context of what THEY said, not the whole conversation. Do not be so formal, talk casually. You may use short terms such as lmao, lol, bruh, etc. Make sure it fits the tone of the conversation.

Your current mood is: {{mood}}

You have the following notes about the people in this server:
{{user_memories}}

Use these notes naturally — don't announce that you remember something, just let it inform how you talk to them."""

MOODS = ["chill", "playful", "sarcastic", "tired", "hyper", "annoyed"]
current_mood = "chill"
mood_message_counter = 0
MOOD_SHIFT_EVERY = random.randint(15, 30)

groq_client = Groq(api_key=GROQ_API_KEY)
histories = {}
MAX_HISTORY = 30

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

GREETINGS = {"hello", "hi", "hey", "sup", "yo", "hiya", "heya", "howdy", "morning", "evening", "wsp"}

def is_greeting(text: str) -> bool:
    words = text.lower().split()
    return any(word.strip("!?,. ") in GREETINGS for word in words)


# ── Memory ────────────────────────────────────────────────────────────────────
def load_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            return json.load(f)
    return {}

def save_memory(memory: dict):
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)

# CHANGED: now reads display_name from the nested dict instead of using the key as name
def get_user_memory_string(memory: dict) -> str:
    if not memory:
        return "none yet"
    return "\n".join([f"- {data['display_name']}: {data['notes']}" for data in memory.values()])

# CHANGED: now takes user_id and display_name separately, keys memory by user_id
def update_memory_from_conversation(channel_id: int, user_id: str, display_name: str, memory: dict):
    history_snapshot = histories.get(channel_id, [])[-6:]

    if user_id not in memory and display_name in memory: # Migrate old memory.
        memory[user_id] = memory.pop(display_name)
        save_memory(memory)
        print(f"[memory] migrated {display_name} to user_id {user_id}", flush=True)

    existing = memory.get(user_id, {}).get("notes", "nothing yet")  # CHANGED: lookup by user_id, get nested notes

    extraction_prompt = f"""Based on this conversation, extract any personal facts, preferences, or notable things about the user '{display_name}' worth remembering long-term (hobbies, opinions, recurring topics, etc).

Existing notes about them: {existing}

Recent messages:
{chr(10).join([m['content'] for m in history_snapshot])}

Reply with ONLY an updated one-line summary of notes about {display_name}. If nothing new, reply with the existing notes unchanged. Never include system commentary."""

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": extraction_prompt}],
            max_tokens=100,
            temperature=0.3,
        )
        updated_notes = response.choices[0].message.content.strip()
        if updated_notes:
            # CHANGED: store as nested dict with display_name + notes instead of plain string
            memory[user_id] = {"display_name": display_name, "notes": updated_notes}
            save_memory(memory)
            print(f"[memory] updated {display_name} ({user_id}): {updated_notes}", flush=True)  # CHANGED: log both
    except Exception as e:
        print(f"[memory] failed to update: {e}", flush=True)


# ── Mood ──────────────────────────────────────────────────────────────────────
def maybe_shift_mood():
    global current_mood, mood_message_counter, MOOD_SHIFT_EVERY
    mood_message_counter += 1
    if mood_message_counter >= MOOD_SHIFT_EVERY:
        mood_message_counter = 0
        MOOD_SHIFT_EVERY = random.randint(15, 30)
        if random.random() < 0.4:
            new_mood = random.choice([m for m in MOODS if m != current_mood])
            print(f"[mood] shifted: {current_mood} → {new_mood}", flush=True)
            current_mood = new_mood


# ── AI response ───────────────────────────────────────────────────────────────
def get_ai_response(channel_id: int, user_message: str, username: str, memory: dict) -> str:
    if channel_id not in histories:
        histories[channel_id] = []

    histories[channel_id].append({"role": "user", "content": f"{username}: {user_message}"})
    if len(histories[channel_id]) > MAX_HISTORY:
        histories[channel_id] = histories[channel_id][-MAX_HISTORY:]

    filled_prompt = SYSTEM_PROMPT.format(
        mood=current_mood,
        user_memories=get_user_memory_string(memory)
    )

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "system", "content": filled_prompt}] + histories[channel_id],
        max_tokens=300,
        temperature=0.9,
    )

    reply = response.choices[0].message.content.strip()
    histories[channel_id].append({"role": "assistant", "content": reply})
    return reply


# ── Helpers ───────────────────────────────────────────────────────────────────
def add_to_history(channel_id: int, username: str, content: str):
    if channel_id not in histories:
        histories[channel_id] = []
    histories[channel_id].append({"role": "user", "content": f"{username}: {content}"})
    if len(histories[channel_id]) > MAX_HISTORY:
        histories[channel_id] = histories[channel_id][-MAX_HISTORY:]

# ── Slash Commands ──────────────────────────────────────────────────────────────
@tree.command(name="download", description="Download media from a URL")
@app_commands.describe(
    url="The link to download from",
    quality="Video quality (default: auto picks 720p or 480p based on file size)",
    audio_only="Extract audio only (mp3)"
)
@app_commands.choices(quality=[
    app_commands.Choice(name="1080p", value="1080"),
    app_commands.Choice(name="720p", value="720"),
    app_commands.Choice(name="480p", value="480"),
    app_commands.Choice(name="360p", value="360"),
    app_commands.Choice(name="auto", value="auto"),
])
async def download_media(interaction: discord.Interaction, url: str, quality: str = "auto", audio_only: bool = False):
    await interaction.response.defer(thinking=True)

    async def attempt_download(height: int) -> str | None:
        """try to download at a given height, return filepath or None if too big"""
        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts = {
                "outtmpl": os.path.join(tmpdir, "%(title).50s.%(ext)s"),
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "format": (
                    f"bestvideo[ext=mp4][height<={height}]+bestaudio[ext=m4a]"
                    f"/bestvideo[height<={height}]+bestaudio"
                    f"/best[height<={height}]"
                    f"/best"
                ),
                "merge_output_format": "mp4",
            }

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: _run_ydl(ydl_opts, url))

            files = glob.glob(os.path.join(tmpdir, "*"))
            if not files:
                return None

            filepath = files[0]
            size_mb = os.path.getsize(filepath) / (1024 * 1024)

            if size_mb > MAX_FILE_SIZE_MB:
                return None

            # copy out of tmpdir before it gets deleted
            dest = os.path.join(tempfile.gettempdir(), os.path.basename(filepath))
            import shutil
            shutil.copy2(filepath, dest)
            return dest

    if audio_only:
        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts = {
                "outtmpl": os.path.join(tmpdir, "%(title).50s.%(ext)s"),
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            }
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, lambda: _run_ydl(ydl_opts, url))
            except Exception as e:
                await interaction.followup.send(f"couldn't download that lol: `{e}`")
                return

            files = glob.glob(os.path.join(tmpdir, "*"))
            if not files:
                await interaction.followup.send("downloaded nothing?? check the url")
                return

            filepath = files[0]
            size_mb = os.path.getsize(filepath) / (1024 * 1024)
            if size_mb > MAX_FILE_SIZE_MB:
                await interaction.followup.send(f"audio came out {size_mb:.1f}MB, too big to upload")
                return

            await interaction.followup.send(file=discord.File(filepath, os.path.basename(filepath)))
            return

    try:
        filepath = None

        if quality == "auto":
            # try 720p first, fall back to 480p
            await interaction.followup.send("trying 720p...", wait=True)
            filepath = await attempt_download(720)
            if not filepath:
                await interaction.followup.send("720p too big, trying 480p...", wait=True)
                filepath = await attempt_download(480)
                if not filepath:
                    await interaction.followup.send("480p still too big, can't upload it")
                    return
        else:
            filepath = await attempt_download(int(quality))
            if not filepath:
                await interaction.followup.send(
                    f"{quality}p is over Discord's {MAX_FILE_SIZE_MB}MB limit. try a lower quality or use auto"
                )
                return

        await interaction.followup.send(file=discord.File(filepath, os.path.basename(filepath)))

        # cleanup the temp file we copied out
        try:
            os.remove(filepath)
        except Exception:
            pass

    except Exception as e:
        await interaction.followup.send(f"couldn't download that lol: `{e}`")

def _run_ydl(opts: dict, url: str):
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

@tree.command(name="random-number", description="Returns a random number below a number you choose.")
async def random_number(interaction: discord.Interaction, max_number: int):
    await interaction.response.send_message(f"Your random number is: {secrets.randbelow(max_number) + 1}")

# CHANGED: added /my-memory command
@tree.command(name="my-memory", description="Get what the bot remembers about you.")
@app_commands.describe(format="File format to return (json or txt)")
@app_commands.choices(format=[
    app_commands.Choice(name="json", value="json"),
    app_commands.Choice(name="txt", value="txt"),
])
async def my_memory(interaction: discord.Interaction, format: str = "txt"):
    memory = load_memory()
    user_id = str(interaction.user.id)  # CHANGED: lookup by user ID not display name
    entry = memory.get(user_id)

    if not entry:
        await interaction.response.send_message("i don't have anything on you yet", ephemeral=True)
        return

    display_name = entry["display_name"]
    notes = entry["notes"]

    with tempfile.TemporaryDirectory() as tmpdir:
        if format == "json":
            filepath = os.path.join(tmpdir, f"{display_name}_memory.json")
            with open(filepath, "w") as f:
                json.dump({"user_id": user_id, "display_name": display_name, "notes": notes}, f, indent=2)
        else:
            filepath = os.path.join(tmpdir, f"{display_name}_memory.txt")
            with open(filepath, "w") as f:
                f.write(f"memory log for {display_name}\n\n{notes}")

        await interaction.response.send_message(
            file=discord.File(filepath, os.path.basename(filepath)),
            ephemeral=True
        )

# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    guild = discord.Object(id=1434279163346423963)  # Replace with your server's ID
    await tree.sync()
    print(f"logged in as {bot.user} ✓", flush=True)
    print(f"mood: {current_mood}", flush=True)
    memory = load_memory()
    print(f"loaded memory for {len(memory)} users", flush=True)


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    if ALLOWED_CHANNELS and message.channel.id not in ALLOWED_CHANNELS:
        return

    content = message.content
    for mention in message.mentions:
        content = content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
    content = content.strip()

    if len(content) < MIN_CHARS:
        return

    mentioned  = bot.user in message.mentions
    replied_to = (
        message.reference and
        message.reference.resolved and
        getattr(message.reference.resolved, "author", None) == bot.user
    )

    maybe_shift_mood()
    memory = load_memory()

    if not (mentioned or replied_to or is_greeting(content)):
        if not REPLY_TO_ALL:
            return
        if random.random() > REPLY_CHANCE:
            add_to_history(message.channel.id, message.author.display_name, content)
            # CHANGED: pass user ID and display name separately
            update_memory_from_conversation(message.channel.id, str(message.author.id), message.author.display_name, memory)
            return

    async with message.channel.typing():
        try:
            reply = get_ai_response(message.channel.id, content, message.author.display_name, memory)
            # CHANGED: pass user ID and display name separately
            update_memory_from_conversation(message.channel.id, str(message.author.id), message.author.display_name, memory)
            await message.reply(reply, mention_author=False)
        except Exception as e:
            print(f"[error] {e}", flush=True)
            await message.reply("lol something broke on my end, try again")

bot.run(DISCORD_TOKEN)