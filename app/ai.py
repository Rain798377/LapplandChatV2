import re
import base64
import random
import httpx
from groq import Groq
from config import GROQ_API_KEY, SYSTEM_PROMPT, MOODS, MAX_HISTORY, MODEL
from memory import get_user_memory_string

groq_client = Groq(api_key=GROQ_API_KEY)

histories: dict = {}

current_mood = "chill"
mood_message_counter = 0
MOOD_SHIFT_EVERY = random.randint(15, 30)

# Vision model — Groq-hosted, supports image input
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"


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
    memory: dict,
    image_urls: list[str] | None = None,
) -> str:
    if channel_id not in histories:
        histories[channel_id] = []

    filled_prompt = SYSTEM_PROMPT.format(
        mood=current_mood,
        user_memories=get_user_memory_string(memory)
    )

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

        text_part = f"{username}: {user_message}" if user_message else f"{username} sent an image"
        content_blocks.append({"type": "text", "text": text_part})

        vision_messages = [
            {"role": "system", "content": filled_prompt},
            *histories[channel_id],
            {"role": "user", "content": content_blocks},
        ]

        response = groq_client.chat.completions.create(
            model=VISION_MODEL,
            messages=vision_messages,
            max_tokens=400,
            temperature=0.9,
        )
        reply = response.choices[0].message.content.strip()
        reply = re.sub(r'^[^:]{1,50}:\s*', '', reply).strip()

        # Add to history as plain text so future turns stay compatible
        histories[channel_id].append({"role": "user", "content": text_part})
        histories[channel_id].append({"role": "assistant", "content": reply})
        if len(histories[channel_id]) > MAX_HISTORY:
            histories[channel_id] = histories[channel_id][-MAX_HISTORY:]
        return reply

    # ── Normal text path ───────────────────────────────────────────────────────
    histories[channel_id].append({"role": "user", "content": f"{username}: {user_message}"})
    if len(histories[channel_id]) > MAX_HISTORY:
        histories[channel_id] = histories[channel_id][-MAX_HISTORY:]

    response = groq_client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": filled_prompt}] + histories[channel_id],
        max_tokens=300,
        temperature=0.9,
    )

    reply = response.choices[0].message.content.strip()
    reply = re.sub(r'^[^:]{1,50}:\s*', '', reply).strip()
    histories[channel_id].append({"role": "assistant", "content": reply})
    return reply


def add_to_history(channel_id: int, username: str, content: str):
    if channel_id not in histories:
        histories[channel_id] = []
    histories[channel_id].append({"role": "user", "content": f"{username}: {content}"})
    if len(histories[channel_id]) > MAX_HISTORY:
        histories[channel_id] = histories[channel_id][-MAX_HISTORY:]