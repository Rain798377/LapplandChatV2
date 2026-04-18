import os
import json
import random
import discord
from groq import Groq
from datetime import datetime

# ── Config ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN    = os.environ.get("DISCORD_TOKEN")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY")
BOT_NAME         = "Lappland"
REPLY_TO_ALL     = True
ALLOWED_CHANNELS = [1483716134250217572]
MIN_CHARS        = 5      # ignore messages shorter than this
REPLY_CHANCE     = 0.4    # 0.0 - 1.0, only used when REPLY_TO_ALL is True
MEMORY_FILE      = "data/memory.json"
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""you are {BOT_NAME}. you're in a discord server. be normal. short replies unless the question needs detail. no asterisks. don't mention being an AI. different people talk in the same channel - pay attention to who said what and treat each person's messages in context of what THEY said, not the whole conversation. Do not be so formal, talk casually. You may use short terms such as lmao, lol, bruh, etc. Make sure it fits the tone of the conversation.

Your current mood is: {{mood}}

You have the following notes about the people in this server:
{{user_memories}}

Use these notes naturally — don't announce that you remember something, just let it inform how you talk to them."""

# mood shifts based on conversation, starts neutral
MOODS = ["chill", "playful", "sarcastic", "tired", "hyper", "annoyed"]
current_mood = "chill"
mood_message_counter = 0
MOOD_SHIFT_EVERY = random.randint(15, 30)  # messages before mood can randomly shift

groq_client = Groq(api_key=GROQ_API_KEY)
histories = {}
MAX_HISTORY = 30

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)


# ── Memory ────────────────────────────────────────────────────────────────────
def load_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            return json.load(f)
    return {}

def save_memory(memory: dict):
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)

def get_user_memory_string(memory: dict) -> str:
    if not memory:
        return "none yet"
    lines = []
    for username, notes in memory.items():
        lines.append(f"- {username}: {notes}")
    return "\n".join(lines)

def update_memory_from_conversation(channel_id: int, username: str, message: str, memory: dict):
    """Ask the AI to extract anything worth remembering about the user."""
    history_snapshot = histories.get(channel_id, [])[-6:]  # last 6 messages for context
    existing = memory.get(username, "nothing yet")

    extraction_prompt = f"""Based on this conversation, extract any personal facts, preferences, or notable things about the user '{username}' worth remembering long-term (hobbies, opinions, recurring topics, name, etc).

Existing notes about them: {existing}

Recent messages:
{chr(10).join([m['content'] for m in history_snapshot])}

Reply with ONLY an updated one-line summary of notes about {username}. If nothing new, reply with the existing notes unchanged. Never include system commentary."""

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": extraction_prompt}],
            max_tokens=100,
            temperature=0.3,
        )
        updated_notes = response.choices[0].message.content.strip()
        if updated_notes:
            memory[username] = updated_notes
            save_memory(memory)
    except Exception as e:
        print(f"[memory] failed to update: {e}", flush=True)


# ── Mood ──────────────────────────────────────────────────────────────────────
def maybe_shift_mood():
    global current_mood, mood_message_counter
    mood_message_counter += 1
    if mood_message_counter >= MOOD_SHIFT_EVERY:
        mood_message_counter = 0
        if random.random() < 0.4:  # 40% chance to actually shift
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
        model="llama-3.1-8b-instant",
        messages=[{"role": "system", "content": filled_prompt}] + histories[channel_id],
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

    # ignore short messages
    if len(content) < MIN_CHARS:
        return

    mentioned  = bot.user in message.mentions
    replied_to = (
        message.reference and
        message.reference.resolved and
        getattr(message.reference.resolved, "author", None) == bot.user
    )

    # always reply if mentioned or replied to, otherwise use REPLY_CHANCE
    if not (mentioned or replied_to):
        if not REPLY_TO_ALL:
            return
        if random.random() > REPLY_CHANCE:
            # still track history and update memory even when not replying
            memory = load_memory()
            if message.channel.id not in histories:
                histories[message.channel.id] = []
            histories[message.channel.id].append({"role": "user", "content": f"{message.author.display_name}: {content}"})
            if len(histories[message.channel.id]) > MAX_HISTORY:
                histories[message.channel.id] = histories[message.channel.id][-MAX_HISTORY:]
            update_memory_from_conversation(message.channel.id, message.author.display_name, content, memory)
            maybe_shift_mood()
            return

    maybe_shift_mood()
    memory = load_memory()

    async with message.channel.typing():
        try:
            reply = get_ai_response(message.channel.id, content, message.author.display_name, memory)
            # update memory every time bot actually responds
            update_memory_from_conversation(message.channel.id, message.author.display_name, content, memory)
            await message.reply(reply, mention_author=False)
        except Exception as e:
            print(f"[error] {e}", flush=True)
            await message.reply("lol something broke on my end, try again")

bot.run(DISCORD_TOKEN)