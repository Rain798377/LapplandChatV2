import asyncio
import random
import discord
import os
import time
from discord import app_commands

from core import ai
from core.config import (DISCORD_TOKEN, MIN_CHARS, REPLY_TO_ALL, REPLY_CHANCE, GREETINGS)
from core.channels import allowed_channels
from core.memory import load_memory, update_memory_from_conversation
from core.ai import (histories, get_ai_response, add_to_history, maybe_shift_mood)
from core.imagegen import enqueue_generate_image
from core.checksum import checksum
from core.colors import *
from commands import random_cmds, memory_cmds, misc_cmds, spotify_cmds, downloader_cmds, call_cmds, channel_cmds

#checksum()

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot  = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ── Register commands ─────────────────────────────────────────────────────────
downloader_cmds.setup(tree)
random_cmds.setup(tree)
memory_cmds.setup(tree)
misc_cmds.setup(tree, bot)
spotify_cmds.setup(tree, bot)
call_cmds.setup(tree, bot)
channel_cmds.setup(tree)

# ── Helpers ───────────────────────────────────────────────────────────────────
def is_greeting(text: str) -> bool:
    words = text.lower().split()
    return any(word.strip("!?,. ") in GREETINGS for word in words)


def get_image_attachments(message: discord.Message) -> list[str]:
    """Return CDN URLs for any image attachments on the message."""
    image_types = {"image/png", "image/jpeg", "image/gif", "image/webp"}
    return [
        a.url for a in message.attachments
        if a.content_type and a.content_type.split(";")[0] in image_types
    ]

# ── Slash command: /imagine ───────────────────────────────────────────────────
def render_progress_bar(step: int, total: int, length: int = 12) -> str:
    if not total:
        return "⬜" * length
    filled = min(length, round(length * step / total))
    return "⬜" * filled + "⬛" * (length - filled)


@tree.command(name="imagine", description="Generate an image from a prompt")
@app_commands.describe(
    prompt="What do you want Lappland to draw?",
    negative_prompt="What to avoid in the image (default: workflow's built-in negative prompt)",
    width="Image width (default: workflow's built-in resolution)",
    height="Image height (default: workflow's built-in resolution)",
    steps="Sampling steps (default: workflow's built-in step count)",
    cfg="Classifier-free guidance scale (default: workflow's built-in cfg)",
)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def imagine_cmd(
    interaction: discord.Interaction,
    prompt: str,
    negative_prompt: str = "",
    width: int = None,
    height: int = None,
    steps: int = None,
    cfg: float = None,
):
    await interaction.response.defer()

    # Discord messages cap out at 2000 chars -- truncate just the displayed
    # copy so an overly long prompt can't break sending the progress/result
    # message. The full prompt still goes to generate_image() untouched.
    display_prompt = prompt if len(prompt) <= 1000 else prompt[:1000] + "..."

    # enqueue_generate_image() hands the job to a shared worker thread rather
    # than running it here directly -- Modal's remote_gen() does blocking
    # network I/O between yields even from an async caller, so this still
    # has to be polled off-thread, but queueing also means concurrent
    # /imagine calls line up behind one warm container instead of each
    # spinning up their own (see core/imagegen.py's job-queue note).
    update_queue, position = enqueue_generate_image(
        prompt, negative_prompt=negative_prompt, width=width, height=height, steps=steps, cfg=cfg
    )

    if position > 0:
        await interaction.edit_original_response(
            content=f"*{display_prompt}*\nqueued -- #{position + 1} in line..."
        )

    filepath = None
    error = None
    start = time.monotonic()
    last_edit = 0.0

    while True:
        update = await asyncio.to_thread(update_queue.get)
        if update is None:
            break
        if update["type"] == "progress":
            step, total = update["step"], update["total"]
            now = time.monotonic()
            # Throttle edits to every ~2s (Discord rate-limits message edits),
            # but always show the final step's update immediately.
            if now - last_edit < 2 and step < total:
                continue
            last_edit = now

            bar = render_progress_bar(step, total)
            percent = int(100 * step / total) if total else 0
            elapsed = now - start
            eta = (elapsed / step) * (total - step) if step else None
            eta_text = f"~{eta:.0f}s left" if eta and eta > 1 else "almost done"
            await interaction.edit_original_response(
                content=f"*{display_prompt}*\n{bar} {percent}% ({eta_text})"
            )
        elif update["type"] == "done":
            filepath = update["path"]
        elif update["type"] == "error":
            error = update["message"]

    if filepath and os.path.exists(filepath):
        await interaction.edit_original_response(
            content=f"*{display_prompt}*",
            attachments=[discord.File(filepath)],
        )
        # Clean up the temp file after sending
        try:
            os.remove(filepath)
        except Exception:
            pass
    else:
        await interaction.edit_original_response(
            content=f"couldn't generate that image, sorry.{f' ({error})' if error else ''}",
        )

# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    guild = discord.Object(id=1529210886802247690)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    await tree.sync()
    print(f"{GREEN}Logged in as {bot.user} ✓{RESET}", flush=True)
    print(f"{LIGHT_BLUE}Mood: {ai.current_mood}{RESET}", flush=True)

    memory = load_memory()
    print(f"{LIGHT_GREEN}Loaded memory for {len(memory)} users{RESET}", flush=True)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if allowed_channels and message.channel.id not in allowed_channels:
        return

    content = message.content
    for mention in message.mentions:
        content = content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
    content = content.strip()

    image_urls = get_image_attachments(message)
    has_images = len(image_urls) > 0

    # Allow image-only messages through even if text is short
    if not has_images and len(content) < MIN_CHARS:
        return

    mentioned  = bot.user in message.mentions
    replied_to = (
        message.reference and
        message.reference.resolved and
        getattr(message.reference.resolved, "author", None) == bot.user
    )

    maybe_shift_mood()
    memory = load_memory()

    # Images always get a response (Lappland reacts to them naturally)
    should_respond = mentioned or replied_to or is_greeting(content) or has_images
    if not should_respond:
        if not REPLY_TO_ALL:
            return
        if random.random() > REPLY_CHANCE:
            add_to_history(message.channel.id, message.author.display_name, content)
            if len(content.split()) > 5 and random.random() < 0.75:
                update_memory_from_conversation(
                    message.channel.id, str(message.author.id),
                    message.author.display_name, memory, histories
                )
            return

    async with message.channel.typing():
        try:
            reply = get_ai_response(
                message.channel.id,
                content,
                message.author.display_name,
                memory,
                image_urls=image_urls if has_images else None,
            )
            if len(content.split()) > 5 and random.random() < 0.75:
                update_memory_from_conversation(
                    message.channel.id, str(message.author.id),
                    message.author.display_name, memory, histories
                )
            await message.reply(reply, mention_author=False)
        except Exception as e:
            print(f"[error] {e}", flush=True)
            await message.reply("An error has occured, try again later.", mention_author=False)


bot.run(DISCORD_TOKEN)