import os
import json
import random
import discord
from groq import Groq
from discord import app_commands, File
import yt_dlp
import asyncio
import tempfile
import glob
import secrets
import requests
import shutil
import subprocess
import asyncio
import re
import io
import spotipy
import aiohttp
from spotipy.oauth2 import SpotifyClientCredentials

# ── Config ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN        = os.environ.get("DISCORD_TOKEN")
GROQ_API_KEY         = os.environ.get("GROQ_API_KEY")
SPOTIFY_CLIENT_ID    = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.environ.get("SPOTIFY_REDIRECT_URI")
BOT_NAME             = "Lappland"
REPLY_TO_ALL         = True
ALLOWED_CHANNELS     = [1483716134250217572]
MIN_CHARS            = 5
REPLY_CHANCE         = random.uniform(0.8, 0.9)
MEMORY_FILE          = "data/memory.json" # data folder outside this folder.
MAX_FILE_SIZE_MB     = 25
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

sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET
))

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

# ── Classes ───────────────────────────────────────────────────────────────────

class EditMemoryModal(discord.ui.Modal, title="Edit Your Memory"):
    def __init__(self, user_id: str, current_notes: str, memory: dict):
        super().__init__()
        self.user_id = user_id
        self.memory = memory
        self.notes = discord.ui.TextInput(
            label="Your notes",
            style=discord.TextStyle.paragraph,
            default=current_notes,  # pre-fills with existing memory
            max_length=500,
        )
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction):
        self.memory[self.user_id]["notes"] = self.notes.value
        save_memory(self.memory)
        await interaction.response.send_message("memory updated", ephemeral=True)

# ── Helpers ───────────────────────────────────────────────────────────────────
def add_to_history(channel_id: int, username: str, content: str):
    if channel_id not in histories:
        histories[channel_id] = []
    histories[channel_id].append({"role": "user", "content": f"{username}: {content}"})
    if len(histories[channel_id]) > MAX_HISTORY:
        histories[channel_id] = histories[channel_id][-MAX_HISTORY:]

# ── Slash Commands ──────────────────────────────────────────────────────────────
@tree.command(name="download", description="Download media from a URL")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
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
    
    # Check if URL is a Spotify link
    is_spotify = "spotify.com" in url or "open.spotify.com" in url
    
    if is_spotify:
        await download_spotify_track(interaction, url)
        return
    
    # Original code for non-Spotify URLs
    # Define your ydl_opts here so it can be reused
    def get_audio_opts(outtmpl):
        return {
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        }

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
            shutil.copy2(filepath, dest)
            return dest

    if audio_only:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Use your ydl_opts configuration here
            ydl_opts = get_audio_opts(os.path.join(tmpdir, "%(title).50s.%(ext)s"))
            ydl_opts["noplaylist"] = True  # Add this from your original code
            
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

    # Rest of your code remains the same
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

async def download_spotify_track(interaction: discord.Interaction, url: str):
    status = await interaction.followup.send("Detected Spotify link, fetching track info...", wait=True)

    try:
        async with aiohttp.ClientSession() as session:
            # Try with US country code first
            async with session.get(
                f"https://api.song.link/v1-alpha.1/links?url={url}&userCountry=US"
            ) as resp:
                data = await resp.json()

            entities = list(data.get("entitiesByUniqueId", {}).values())

            # Retry without country restriction if no results
            if not entities:
                async with session.get(
                    f"https://api.song.link/v1-alpha.1/links?url={url}"
                ) as resp:
                    data = await resp.json()
                entities = list(data.get("entitiesByUniqueId", {}).values())

        if not entities:
            # Last resort: extract track ID and search Spotify open graph for title
            await status.edit(content="song.link failed, trying to fetch title from Spotify page...")
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    html = await resp.text()
            og_title = re.search(r'<meta property="og:title" content="([^"]+)"', html)
            og_desc = re.search(r'<meta name="description" content="([^"]+)"', html)
            if og_title:
                track_name = og_title.group(1).split(" - ")[0].strip()
                artist = og_desc.group(1).split(" · ")[0].strip() if og_desc else ""
            else:
                await status.edit(content="Couldn't fetch track info from any source.")
                return
            yt_url = None
        else:
            entity = entities[0]
            track_name = entity.get("title")
            artist = entity.get("artistName", "")
            links_by_platform = data.get("linksByPlatform", {})
            yt_music = links_by_platform.get("youtubeMusic", {})
            youtube = links_by_platform.get("youtube", {})
            yt_url = yt_music.get("url") or youtube.get("url") or None

        if not track_name:
            await status.edit(content="Couldn't extract track name.")
            return

    except Exception as e:
        await status.edit(content=f"Couldn't fetch track info: `{e}`")
        return

    clean_artist = artist.split(",")[0].split("&")[0].strip()
    clean_title = re.sub(r"[\(\[].*?[\)\]]", "", track_name).strip()

    search_attempts = []
    if yt_url:
        search_attempts.append((yt_url, f"**{artist} - {track_name}** (exact match)"))
    search_attempts += [
        (f"ytsearch1:{artist} {track_name}", f"**{artist} - {track_name}**"),
        (f"ytsearch1:{clean_artist} {clean_title}", f"**{clean_artist} - {clean_title}** (simplified)"),
        (f"ytsearch1:{clean_title}", f"**{clean_title}** (title only)"),
    ]

    for search_query, label in search_attempts:
        await status.edit(content=f"Searching YouTube for: {label}...")
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                ydl_opts = {
                    "outtmpl": os.path.join(tmpdir, "%(title).50s.%(ext)s"),
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "playlist_items": "1",
                    "format": "bestaudio/best",
                    "postprocessors": [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }],
                }
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, lambda: _run_ydl(ydl_opts, search_query))

                files = glob.glob(os.path.join(tmpdir, "*.mp3"))
                if not files:
                    continue

                filepath = files[0]
                size_mb = os.path.getsize(filepath) / (1024 * 1024)

                if size_mb > MAX_FILE_SIZE_MB:
                    await status.edit(content=f"Track is {size_mb:.1f}MB, too big to upload.")
                    return

                dest = os.path.join(tempfile.gettempdir(), os.path.basename(filepath))
                shutil.copy2(filepath, dest)
                await status.edit(content=f"Found: **{clean_artist} - {clean_title}**")
                await interaction.followup.send(file=discord.File(dest, os.path.basename(dest)))

                try:
                    os.remove(dest)
                except Exception:
                    pass
                return

            except Exception:
                continue

    await status.edit(content="Couldn't find the track on YouTube after multiple attempts.")
    
def _run_ydl(opts: dict, url: str):
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

random_group = app_commands.Group(name="random", description="Random commands")

@random_group.command(name="number", description="Returns a random number below a number you choose.")
async def random_number(interaction: discord.Interaction, max_number: int):
    await interaction.response.send_message(f"Your random number is: {secrets.randbelow(max_number) + 1}")

@random_group.command(name="coin", description="Flips a coin for you, heads or tails.")
async def coin_flip(interaction: discord.Interaction):
    result = secrets.choice(["heads", "tails"])
    await interaction.response.send_message(f"The coin landed on: {result}")

@random_group.command(name="die", description="Rolls a die with a specified number of sides.")
async def roll_die(interaction: discord.Interaction, sides: int):
    result = secrets.randbelow(sides) + 1
    await interaction.response.send_message(f"You rolled a {result} on a {sides}-sided die.")

@random_group.command(name="choice", description="Selects a random item from a list you provide.")
@app_commands.describe(items="A comma-separated list of items to choose from.")
async def random_choice(interaction: discord.Interaction, items: str):
    item_list = [item.strip() for item in items.split(",")]
    result = secrets.choice(item_list)
    await interaction.response.send_message(f"I choose: {result}")

@random_group.command(name="word", description="Tells you a random word.")
async def random_word(interaction: discord.Interaction):
    words = [line.strip() for line in requests.get("https://raw.githubusercontent.com/dwyl/english-words/master/words.txt").text.splitlines() if len(line.strip()) <= 12]
    result = secrets.choice(words)
    await interaction.response.send_message(f"Your random word is: {result}")


# CHANGED: added /my-memory command
memory_group = app_commands.Group(name="memory", description="Memory related commands")

@memory_group.command(name="wipe-all", description="Wipe all memory the bot has (admin only)")
async def wipe_memory(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("you're not an admin lol", ephemeral=True)
        return
    save_memory({})
    await interaction.response.send_message("all memory wiped", ephemeral=True)

@memory_group.command(name="wipe", description="Wipe your memory from the bot")
async def wipe_my_memory(interaction: discord.Interaction):
    memory = load_memory()
    user_id = str(interaction.user.id)
    if user_id in memory:
        del memory[user_id]
        save_memory(memory)
        await interaction.response.send_message("your memory has been wiped", ephemeral=True)
    else:
        await interaction.response.send_message("i don't have anything on you", ephemeral=True)

@memory_group.command(name="edit", description="Edit what the bot remembers about you")
async def change_my_memory(interaction: discord.Interaction):
    memory = load_memory()
    user_id = str(interaction.user.id)
    if user_id not in memory:
        await interaction.response.send_message("i don't have any memory of you yet", ephemeral=True)
        return
    await interaction.response.send_modal(EditMemoryModal(user_id, memory[user_id]["notes"], memory))

@memory_group.command(name="view", description="Get what the bot remembers about you")
@app_commands.describe(format="File format to return (json or txt)")
@app_commands.choices(format=[
    app_commands.Choice(name="json", value="json"),
    app_commands.Choice(name="txt", value="txt"),
])
async def my_memory(interaction: discord.Interaction, format: str = "txt"):
    memory = load_memory()
    user_id = str(interaction.user.id)
    entry = memory.get(user_id)
    if not entry:
        await interaction.response.send_message("I don't have anything on you yet", ephemeral=True)
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
@tree.command(name="ship", description="Ship two users and get a compatibility rating")
@app_commands.describe(user1="First user to ship", user2="Second user to ship")
async def ship_users(interaction: discord.Interaction, user1: discord.User, user2: discord.User):
    compatibility = secrets.randbelow(101)  # 0 to 100
    await interaction.response.send_message(f"❤️ {user1.display_name} + {user2.display_name} = {compatibility}% compatible! ❤️")

# ── register commands ────────────────────────────────────────────────────────────
tree.add_command(memory_group)
tree.add_command(random_group)

# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    guild = discord.Object(id=1434279163346423963)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)  # instant sync for your server
    await tree.sync()              # global sync for DMs (takes up to 1hr)
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