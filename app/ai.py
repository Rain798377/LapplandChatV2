import re
import random
from groq import Groq
from config import GROQ_API_KEY, SYSTEM_PROMPT, MOODS, MAX_HISTORY, MODEL
from memory import get_user_memory_string

groq_client = Groq(api_key=GROQ_API_KEY)

histories: dict = {}

current_mood = "chill"
mood_message_counter = 0
MOOD_SHIFT_EVERY = random.randint(15, 30)


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
