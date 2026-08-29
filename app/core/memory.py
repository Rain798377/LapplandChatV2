import os
import json
from core.colors import *
from core.config import MEMORY_FILE, MAX_MEMORY_TOKENS, MEMORY_MAX_TOKENS, MEMORY_TEMPERATURE
from core.llm import chat_completion


def load_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            return json.load(f)
    return {}


def save_memory(memory: dict):
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)


def _approx_tokens(text: str) -> int:
    # no tokenizer shared across the fallback chain's providers, so this is
    # a conservative ~4-chars/token estimate rather than an exact count
    return len(text) // 4


def get_user_memory_string(memory: dict) -> str:
    if not memory:
        return "none yet"

    lines = []
    used_tokens = 0
    for data in memory.values():
        line = f"- {data['display_name']}: {data['notes']}"
        line_tokens = _approx_tokens(line)
        if used_tokens + line_tokens > MAX_MEMORY_TOKENS:
            break
        lines.append(line)
        used_tokens += line_tokens

    return "\n".join(lines) if lines else "none yet"


def update_memory_from_conversation(channel_id: int, user_id: str, display_name: str, memory: dict, histories: dict):
    history_snapshot = histories.get(channel_id, [])[-6:]

    if user_id not in memory and display_name in memory:  # migrate old memory
        memory[user_id] = memory.pop(display_name)
        save_memory(memory)
        print(f"[memory] migrated {display_name} to user_id {user_id}", flush=True)

    existing = memory.get(user_id, {}).get("notes", "nothing yet")

    extraction_prompt = f"""Based on this conversation, extract any NEW personal facts, preferences, or notable things about '{display_name}' worth remembering (hobbies, opinions, recurring topics, interests, etc).

Existing notes: {existing}

Recent messages:
{chr(10).join([m['content'] for m in history_snapshot])}

Reply with ONLY an updated summary merging old and new info about {display_name}. Keep all existing notes unless they are contradicted. Add any new details. Be concise but don't drop information. Never include system commentary."""

    try:
        updated_notes = chat_completion(
            messages=[{"role": "user", "content": extraction_prompt}],
            max_tokens=MEMORY_MAX_TOKENS,
            temperature=MEMORY_TEMPERATURE,
        )
        if updated_notes:
            memory[user_id] = {"display_name": display_name, "notes": updated_notes}
            save_memory(memory)
            print(f"{LIGHT_BLUE}[memory] Updated {display_name} ({user_id}): {updated_notes}{RESET}", flush=True)
    except Exception as e:
        print(f"{RED}[memory] failed to update: {e}{RESET}", flush=True)
