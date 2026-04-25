import random
import discord
import app.ai as ai
from discord import app_commands

from config import (DISCORD_TOKEN, ALLOWED_CHANNELS, MIN_CHARS, REPLY_TO_ALL, REPLY_CHANCE, GREETINGS)
from memory import load_memory, update_memory_from_conversation
from app.ai import (groq_client, histories, get_ai_response, add_to_history, maybe_shift_mood)
from commands import download, random_cmds, memory_cmds, misc_cmds

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot  = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ── Register commands ─────────────────────────────────────────────────────────
download.setup(tree)
random_cmds.setup(tree)
memory_cmds.setup(tree)
misc_cmds.setup(tree)

# ── Helpers ───────────────────────────────────────────────────────────────────
def is_greeting(text: str) -> bool:
    words = text.lower().split()
    return any(word.strip("!?,. ") in GREETINGS for word in words)

# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    guild = discord.Object(id=1434279163346423963)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    await tree.sync()
    print(f"logged in as {bot.user} ✓", flush=True)

    print(f"mood: {ai.current_mood}", flush=True)

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
            update_memory_from_conversation(
                message.channel.id, str(message.author.id),
                message.author.display_name, memory, histories, groq_client
            )
            return

    async with message.channel.typing():
        try:
            reply = get_ai_response(message.channel.id, content, message.author.display_name, memory)
            update_memory_from_conversation(
                message.channel.id, str(message.author.id),
                message.author.display_name, memory, histories, groq_client
            )
            await message.reply(reply, mention_author=False)
        except Exception as e:
            print(f"[error] {e}", flush=True)
            await message.reply("An error has occured, try again later.", mention_author=False)


bot.run(DISCORD_TOKEN)
