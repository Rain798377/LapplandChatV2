import os
import json
from colors import *
from config import MEMORY_FILE


def load_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            return json.load(f)
    return {}


def save_memory(memory: dict):
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)


def get_user_memory_string(memory: dict) -> str:
    if not memory:
        return "none yet"
    return "\n".join([f"- {data['display_name']}: {data['notes']}" for data in memory.values()])


def update_memory_from_conversation(channel_id: int, user_id: str, display_name: str, memory: dict, histories: dict, groq_client):
    history_snapshot = histories.get(channel_id, [])[-6:]

    if user_id not in memory and display_name in memory:  # migrate old memory
        memory[user_id] = memory.pop(display_name)
        save_memory(memory)
        print(f"[memory] migrated {display_name} to user_id {user_id}", flush=True)

    existing = memory.get(user_id, {}).get("notes", "nothing yet")

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
            memory[user_id] = {"display_name": display_name, "notes": updated_notes}
            save_memory(memory)
            print(f"{LIGHT_BLUE}[memory] Updated {display_name} ({user_id}): {updated_notes}{RESET}", flush=True)
    except Exception as e:
        print(f"{RED}[memory] failed to update: {e}{RESET}", flush=True)
