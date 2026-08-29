import asyncio
import random
import discord
import os
import time
from discord import app_commands
from discord.ext import tasks

from core import ai, presence
from core.config import (
    DISCORD_TOKEN, MIN_CHARS, REPLY_TO_ALL, GREETINGS, BOT_NICKNAMES,
    REPLY_CHANCE_MOMENTUM, REPLY_CHANCE_SOLO, REPLY_CHANCE_AMBIENT,
    REPLY_CHANCE_CLASSIFIED_YES, REPLY_CHANCE_CLASSIFIED_NO,
    REPLY_MOMENTUM_WINDOW_SECONDS, SOLO_SPEAKER_WINDOW,
    IDLE_THRESHOLD_SECONDS, IDLE_CHECK_INTERVAL_SECONDS, IDLE_CHANCE,
)
from core.channels import allowed_channels, idle_channels
from core.memory import load_memory, update_memory_from_conversation
from core.ai import (histories, get_ai_response, get_idle_message, add_to_history, maybe_shift_mood)
from core.llm import describe_error
from core.intent import classify_intent
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


def is_name_dropped(text: str) -> bool:
    words = text.lower().split()
    return any(word.strip("!?,.'\"") in BOT_NICKNAMES for word in words)


# channel_id -> epoch time of the bot's own last reply there. Distinct from
# last_activity (idle-chatter, below) -- this is only used to detect "still
# mid-conversation with the bot" momentum in on_message's reply-chance logic.
last_bot_reply_time: dict[int, float] = {}

# channel_id -> the last SOLO_SPEAKER_WINDOW human author IDs to have spoken
# there. If they're all the same person, there's no one else in the channel
# for that person to be talking to -- see the reply-chance logic below.
recent_speakers: dict[int, list[int]] = {}


def get_image_attachments(message: discord.Message) -> list[str]:
    """Return CDN URLs for any image attachments on the message."""
    image_types = {"image/png", "image/jpeg", "image/gif", "image/webp"}
    return [
        a.url for a in message.attachments
        if a.content_type and a.content_type.split(";")[0] in image_types
    ]

# ── Idle chatter ──────────────────────────────────────────────────────────────
# channel_id -> epoch time of the last message seen there (any author,
# including the bot itself). Used to tell when the bot's main channel(s)
# (allowed_channels -- where it actually chats) have gone quiet; that's the
# trigger, not the target -- the "I'm bored" line gets posted into a
# *different* channel, the one set via /listen-idle.
last_activity: dict[int, float] = {}
# idle-target channel_id -> epoch time a bored line was last posted there,
# so a still-quiet main channel doesn't get a fresh "bored" line every tick.
idle_last_posted: dict[int, float] = {}


@tasks.loop(seconds=IDLE_CHECK_INTERVAL_SECONDS)
async def idle_chatter():
    if not allowed_channels or not idle_channels:
        return

    now = time.time()
    # "quiet" = nothing said in ANY of the bot's main channels recently
    most_recent_main_activity = max((last_activity.get(cid, now) for cid in allowed_channels), default=now)
    if now - most_recent_main_activity < IDLE_THRESHOLD_SECONDS:
        return

    for channel_id in list(idle_channels):
        # re-uses IDLE_THRESHOLD_SECONDS as the minimum gap between bored
        # lines in the same idle channel, so it doesn't repeat every tick
        # while the main channel stays quiet
        if now - idle_last_posted.get(channel_id, 0.0) < IDLE_THRESHOLD_SECONDS:
            continue
        if random.random() > IDLE_CHANCE:
            continue

        channel = bot.get_channel(channel_id)
        if channel is None:
            continue

        try:
            memory = load_memory()
            line = await asyncio.to_thread(get_idle_message, channel_id, memory)
            await channel.send(line)
        except Exception as e:
            print(f"{RED}[idle] failed in channel {channel_id}: {e}{RESET}", flush=True)
        # reset either way -- a failed attempt shouldn't retry every tick
        idle_last_posted[channel_id] = time.time()

# ── Slash commands: /imagine (FLUX.1-schnell), /imagine_anime (Filigree-Anima) ──
def render_progress_bar(step: int, total: int, length: int = 12) -> str:
    if not total:
        return "⬜" * length
    filled = min(length, round(length * step / total))
    return "⬜" * filled + "⬛" * (length - filled)


async def _run_imagine(
    interaction: discord.Interaction,
    model: str,
    prompt: str,
    negative_prompt: str,
    width: int,
    height: int,
    steps: int,
    cfg: float,
    seed: int,
):
    """Shared by imagine_cmd (flux) and imagine_anime_cmd (anima) -- only the
    backend differs, everything about defer/progress/attach/cleanup is the
    same regardless of which one is generating."""
    await interaction.response.defer()
    await presence.set_activity("drawing something")

    try:
        # Discord messages cap out at 2000 chars -- truncate just the displayed
        # copy so an overly long prompt can't break sending the progress/result
        # message. The full prompt still goes to generate_image() untouched.
        display_prompt = prompt if len(prompt) <= 1000 else prompt[:1000] + "..."

        # enqueue_generate_image() hands the job to model's own shared worker
        # thread rather than running it here directly -- Modal's remote_gen()
        # does blocking network I/O between yields even from an async caller,
        # so this still has to be polled off-thread, but queueing also means
        # concurrent /imagine calls to the SAME backend line up behind one
        # warm container instead of each spinning up their own (see
        # core/imagegen.py's job-queue note; a flux call and an anima call
        # don't queue behind each other, since they're independent containers).
        update_queue, position = enqueue_generate_image(
            prompt, negative_prompt=negative_prompt, width=width, height=height, steps=steps, cfg=cfg, seed=seed,
            model=model,
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
    finally:
        await presence.set_activity(None)


@tree.command(name="imagine", description="Generate an image from a prompt (FLUX.1-schnell)")
@app_commands.describe(
    prompt="What do you want Lappland to draw?",
    negative_prompt="What to avoid in the image (ignored -- FLUX.1-schnell is CFG-distilled and doesn't use one)",
    width="Image width (default: workflow's built-in resolution)",
    height="Image height (default: workflow's built-in resolution)",
    steps="Sampling steps (default: 4 -- schnell's own distillation regime, more doesn't help)",
    cfg="Classifier-free guidance scale (default: 1 -- schnell is distilled for this, raising it doesn't help)",
    seed="Sampler seed, for reproducible results (default: random each time)",
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
    seed: int = None,
):
    await _run_imagine(interaction, "flux", prompt, negative_prompt, width, height, steps, cfg, seed)


@tree.command(name="imagine_anime", description="Generate an anime-style image from a prompt (Filigree-Anima)")
@app_commands.describe(
    prompt="What do you want Lappland to draw?",
    negative_prompt="What to avoid in the image (default: workflow's built-in negative prompt)",
    width="Image width (default: workflow's built-in resolution)",
    height="Image height (default: workflow's built-in resolution)",
    steps="Sampling steps (default: workflow's built-in step count)",
    cfg="Classifier-free guidance scale (default: workflow's built-in cfg)",
    seed="Sampler seed, for reproducible results (default: random each time)",
)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def imagine_anime_cmd(
    interaction: discord.Interaction,
    prompt: str,
    negative_prompt: str = "",
    width: int = None,
    height: int = None,
    steps: int = None,
    cfg: float = None,
    seed: int = None,
):
    await _run_imagine(interaction, "anima", prompt, negative_prompt, width, height, steps, cfg, seed)

# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    guild = discord.Object(id=1529210886802247690)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    await tree.sync()
    print(f"{GREEN}Logged in as {bot.user} ✓{RESET}", flush=True)
    print(f"{LIGHT_BLUE}Mood: {ai.current_mood}{RESET}", flush=True)

    presence.bind(bot)
    await presence.set_mood(ai.current_mood)

    if not idle_chatter.is_running():
        idle_chatter.start()

    memory = load_memory()
    print(f"{LIGHT_GREEN}Loaded memory for {len(memory)} users{RESET}", flush=True)


@bot.event
async def on_message(message: discord.Message):
    # Resets the idle-chatter clock for any message in this channel,
    # including the bot's own (regular replies and idle lines both count as
    # "not quiet anymore") -- must run before the bot-author return below.
    last_activity[message.channel.id] = time.time()

    if message.author.bot:
        return
    if allowed_channels and message.channel.id not in allowed_channels:
        return

    speakers = recent_speakers.setdefault(message.channel.id, [])
    speakers.append(message.author.id)
    del speakers[:-SOLO_SPEAKER_WINDOW]

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
    # A native Discord reply to, or an @mention of, someone who ISN'T the
    # bot is a strong signal this message is meant for that person, not
    # Lappland -- used below to suppress the ambient/momentum chance rather
    # than rolling on a message that's clearly part of someone else's exchange.
    directed_at_other = (
        (message.reference and message.reference.resolved and
         getattr(message.reference.resolved, "author", None) not in (None, bot.user))
        or any(u != bot.user for u in message.mentions)
    )

    if maybe_shift_mood():
        await presence.set_mood(ai.current_mood)
    memory = load_memory()

    # Images always get a response (Lappland reacts to them naturally)
    should_respond = mentioned or replied_to or is_greeting(content) or is_name_dropped(content) or has_images
    if not should_respond:
        if not REPLY_TO_ALL:
            return

        if directed_at_other:
            chance = 0.0
        elif time.time() - last_bot_reply_time.get(message.channel.id, 0.0) < REPLY_MOMENTUM_WINDOW_SECONDS:
            # still mid-conversation with the bot -- a real person keeps
            # responding in a back-and-forth without re-pinging every line
            chance = REPLY_CHANCE_MOMENTUM
        elif len(set(speakers)) <= 1:
            # nobody but this one person has spoken recently -- no one else
            # in the channel for them to be talking to, so they're almost
            # certainly talking to the bot even without naming it
            chance = REPLY_CHANCE_SOLO
        else:
            # genuine multi-person banter with no other signal -- the one
            # case cheap heuristics can't tell apart well (implicit address
            # in a group chat). Ask the local intent sidecar (see
            # core/intent.py); if it's unset/unreachable/uncertain this comes
            # back None and we fall back to the old flat ambient chance.
            directed = await asyncio.to_thread(classify_intent, content)
            if directed is True:
                chance = REPLY_CHANCE_CLASSIFIED_YES
            elif directed is False:
                chance = REPLY_CHANCE_CLASSIFIED_NO
            else:
                chance = REPLY_CHANCE_AMBIENT

        if random.random() > chance:
            add_to_history(message.channel.id, message.author.display_name, message.author.id, content)
            if len(content.split()) > 5 and random.random() < 0.75:
                await asyncio.to_thread(
                    update_memory_from_conversation,
                    message.channel.id, str(message.author.id),
                    message.author.display_name, memory, histories
                )
            return

    async with message.channel.typing():
        try:
            # get_ai_response()/update_memory_from_conversation() are plain
            # blocking functions (synchronous Groq/Gemini HTTP calls) -- run
            # them off-thread so a slow reasoning-model generation doesn't
            # stall the bot's event loop and cause other interactions (slash
            # commands) to miss Discord's 3s ack window and 404.
            reply = await asyncio.to_thread(
                get_ai_response,
                message.channel.id,
                content,
                message.author.display_name,
                message.author.id,
                memory,
                image_urls=image_urls if has_images else None,
            )
            if len(content.split()) > 5 and random.random() < 0.75:
                await asyncio.to_thread(
                    update_memory_from_conversation,
                    message.channel.id, str(message.author.id),
                    message.author.display_name, memory, histories
                )
            await message.reply(reply, mention_author=False)
            last_bot_reply_time[message.channel.id] = time.time()
        except Exception as e:
            print(f"[error] {e}", flush=True)
            await message.reply(describe_error(e), mention_author=False)


bot.run(DISCORD_TOKEN)