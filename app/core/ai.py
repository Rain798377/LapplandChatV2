import re
import base64
import random
import httpx
from core.config import (
    SYSTEM_PROMPT, MOODS, MAX_HISTORY, MODEL, BOT_OWNER_ID,
    REPLY_MAX_TOKENS, REPLY_TEMPERATURE,
    VISION_MAX_TOKENS, VISION_TEMPERATURE,
    IDLE_MAX_TOKENS, IDLE_TEMPERATURE,
)
from core.llm import chat_completion, VISION_PROVIDER_CHAIN
from core.memory import get_user_memory_string

histories: dict = {}

current_mood = "chill"
mood_message_counter = 0
MOOD_SHIFT_EVERY = random.randint(15, 30)

# Per-reply length variety -- picked fresh each message rather than left to
# the model's own judgment, since "vary your length randomly" as a bare
# instruction tends to just get ignored and collapse back to one register.
# Weighted toward medium/long since short-only was the complaint.
_LENGTH_WEIGHTS = {"short": 0.3, "medium": 0.4, "long": 0.3}


def _pick_length_hint() -> str:
    return random.choices(
        list(_LENGTH_WEIGHTS.keys()), weights=list(_LENGTH_WEIGHTS.values())
    )[0]

# Vision model — Groq-hosted, supports image input
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

# MODEL is a reasoning model -- if it burns its whole max_tokens budget on
# hidden reasoning before writing a visible answer, chat_completion() returns
# "" and Discord's API 400s on message.reply("") (error 50006). Substitute
# this instead of ever returning an empty reply.
_EMPTY_REPLY_FALLBACK = "..."


def _speaker_tag(username: str, user_id: int) -> str:
    """Prefix used for a speaker's lines in the history sent to the LLM.
    Display names are just Discord nicknames -- anyone can rename themselves
    to impersonate the owner. Only the owner's messages get an explicit
    "(id:...)" tag so SYSTEM_PROMPT has something unspoofable to match on;
    everyone else is just their display name, as before."""
    if user_id == BOT_OWNER_ID:
        return f"{username} (id:{BOT_OWNER_ID})"
    return username


def maybe_shift_mood() -> bool:
    global current_mood, mood_message_counter, MOOD_SHIFT_EVERY
    mood_message_counter += 1
    if mood_message_counter >= MOOD_SHIFT_EVERY:
        mood_message_counter = 0
        MOOD_SHIFT_EVERY = random.randint(15, 30)
        if random.random() < 0.4:
            new_mood = random.choice([m for m in MOODS if m != current_mood])
            print(f"[mood] shifted: {current_mood} → {new_mood}", flush=True)
            current_mood = new_mood
            return True
    return False


def _fetch_image_as_base64(url: str) -> tuple[str, str]:
    """Download a Discord attachment and return (base64_data, media_type)."""
    resp = httpx.get(url, timeout=15)
    resp.raise_for_status()
    media_type = resp.headers.get("content-type", "image/png").split(";")[0]
    return base64.standard_b64encode(resp.content).decode("utf-8"), media_type


def get_ai_response(
    channel_id: int,
    user_message: str,
    username: str,
    user_id: int,
    memory: dict,
    image_urls: list[str] | None = None,
) -> str:
    if channel_id not in histories:
        histories[channel_id] = []

    filled_prompt = SYSTEM_PROMPT.format(
        mood=current_mood,
        user_memories=get_user_memory_string(memory),
        length_hint=_pick_length_hint(),
    )

    tag = _speaker_tag(username, user_id)

    # ── Vision path (message has images) ──────────────────────────────────────
    if image_urls:
        content_blocks = []
        for url in image_urls:
            try:
                b64, mime = _fetch_image_as_base64(url)
                content_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                })
            except Exception as e:
                print(f"[vision] failed to fetch image: {e}", flush=True)

        text_part = f"{tag}: {user_message}" if user_message else f"{tag} sent an image"
        content_blocks.append({"type": "text", "text": text_part})

        vision_messages = [
            {"role": "system", "content": filled_prompt},
            *histories[channel_id],
            {"role": "user", "content": content_blocks},
        ]

        reply = chat_completion(
            messages=vision_messages,
            model=VISION_MODEL,
            max_tokens=VISION_MAX_TOKENS,
            temperature=VISION_TEMPERATURE,
            providers=VISION_PROVIDER_CHAIN,
        )
        reply = re.sub(r'^[^:]{1,50}:\s*', '', reply).strip() or _EMPTY_REPLY_FALLBACK

        # Add to history as plain text so future turns stay compatible
        histories[channel_id].append({"role": "user", "content": text_part})
        histories[channel_id].append({"role": "assistant", "content": reply})
        if len(histories[channel_id]) > MAX_HISTORY:
            histories[channel_id] = histories[channel_id][-MAX_HISTORY:]
        return reply

    # ── Normal text path ───────────────────────────────────────────────────────
    histories[channel_id].append({"role": "user", "content": f"{tag}: {user_message}"})
    if len(histories[channel_id]) > MAX_HISTORY:
        histories[channel_id] = histories[channel_id][-MAX_HISTORY:]

    reply = chat_completion(
        messages=[{"role": "system", "content": filled_prompt}] + histories[channel_id],
        model=MODEL,
        # core/llm.py sends reasoning_effort="none" for MODEL, so no hidden
        # reasoning tokens eat into this budget -- back to a plain reply size.
        max_tokens=REPLY_MAX_TOKENS,
        temperature=REPLY_TEMPERATURE,
    )
    reply = re.sub(r'^[^:]{1,50}:\s*', '', reply).strip() or _EMPTY_REPLY_FALLBACK
    histories[channel_id].append({"role": "assistant", "content": reply})
    return reply


# Standalone instruction for get_idle_message() -- deliberately not folded
# into SYSTEM_PROMPT (that's tuned, see CLAUDE.md) or given the channel's own
# recent history: this fires in a channel that's quiet in its own right (see
# idle_chatter in LapplandV2.py, which posts here because the bot's *main*
# channel went quiet, not this one), so it's meant to read as an unprompted,
# in-the-moment aside rather than a reply to anything.
_IDLE_INSTRUCTION = (
    "Nobody's been talking to you in a while and you're a little bored. Say "
    "one short, natural line about that -- how you're feeling, what's on "
    "your mind, whatever. Don't greet anyone, don't ask a question, don't "
    "say you're posting this unprompted."
)


def get_idle_message(channel_id: int, memory: dict) -> str:
    """Generate a single unprompted "I'm bored" line for idle_chatter()
    (LapplandV2.py) to post -- reflects current_mood the same way a normal
    reply would."""
    if channel_id not in histories:
        histories[channel_id] = []

    # length_hint pinned to "short" here -- _IDLE_INSTRUCTION already asks for
    # one short unprompted line, so this isn't part of the random reply-length
    # variety used for actual replies.
    filled_prompt = SYSTEM_PROMPT.format(
        mood=current_mood,
        user_memories=get_user_memory_string(memory),
        length_hint="short",
    )

    reply = chat_completion(
        messages=[
            {"role": "system", "content": filled_prompt},
            {"role": "user", "content": _IDLE_INSTRUCTION},
        ],
        model=MODEL,
        max_tokens=IDLE_MAX_TOKENS,
        temperature=IDLE_TEMPERATURE,
    )
    reply = re.sub(r'^[^:]{1,50}:\s*', '', reply).strip() or _EMPTY_REPLY_FALLBACK
    histories[channel_id].append({"role": "assistant", "content": reply})
    if len(histories[channel_id]) > MAX_HISTORY:
        histories[channel_id] = histories[channel_id][-MAX_HISTORY:]
    return reply


def add_to_history(channel_id: int, username: str, user_id: int, content: str):
    if channel_id not in histories:
        histories[channel_id] = []
    tag = _speaker_tag(username, user_id)
    histories[channel_id].append({"role": "user", "content": f"{tag}: {content}"})
    if len(histories[channel_id]) > MAX_HISTORY:
        histories[channel_id] = histories[channel_id][-MAX_HISTORY:]