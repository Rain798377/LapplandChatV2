"""
llm.py — shared chat-completion wrapper with Groq -> Gemini fallback.

All Groq chat calls in the bot (main reply, vision, memory extraction) should
go through chat_completion() instead of hitting groq_client directly, so a
single daily-quota trip switches every call site over to Gemini at once.

Env vars: GROQ_API_KEY, GEMINI_API_KEY (see core/config.py)
"""

import base64
import re
import time
from groq import Groq, RateLimitError
from google import genai
from google.genai import types
from core.config import GROQ_API_KEY, GEMINI_API_KEY, MODEL
from core.colors import *

groq_client = Groq(api_key=GROQ_API_KEY)
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

GEMINI_MODEL = "gemini-3.5-flash-lite"

# retry-after values above this are treated as a daily/quota exhaustion
# rather than a short per-minute limit
DAILY_LIMIT_RETRY_AFTER_SECONDS = 120
# fallback cooldown when Groq doesn't give us a usable retry-after
GROQ_COOLDOWN_SECONDS = 6 * 60 * 60

_groq_daily_limited_until = 0.0

# Reasoning models (Qwen3, DeepSeek-R1-distill, gpt-oss, etc.) think out loud
# before answering. Groq's own fix for that is the reasoning_format param --
# "hidden" drops the reasoning tokens server-side and leaves only the final
# answer in message.content. Non-reasoning models (e.g. VISION_MODEL) just
# ignore the param, so this is safe to send on every call.
_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _retry_after_seconds(err: RateLimitError) -> float:
    try:
        return float(err.response.headers.get("retry-after", 0))
    except (TypeError, ValueError):
        return 0.0


def _is_daily_limit(err: RateLimitError) -> bool:
    body_msg = ""
    if isinstance(err.body, dict):
        body_msg = str(err.body.get("error", {}).get("message", "")).lower()
    if "per day" in body_msg or "tpd" in body_msg or "rpd" in body_msg:
        return True
    return _retry_after_seconds(err) > DAILY_LIMIT_RETRY_AFTER_SECONDS


def _messages_to_gemini(messages: list[dict]) -> tuple[str | None, list]:
    """Split OpenAI/Groq-style chat messages into (system_instruction, gemini contents)."""
    system_instruction = None
    contents = []

    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if isinstance(content, list):  # vision-style content blocks
            parts = []
            for block in content:
                if block.get("type") == "text":
                    parts.append(types.Part.from_text(text=block["text"]))
                elif block.get("type") == "image_url":
                    url = block["image_url"]["url"]
                    header, b64data = url.split(",", 1)
                    mime = header.split(":")[1].split(";")[0]
                    parts.append(types.Part.from_bytes(data=base64.b64decode(b64data), mime_type=mime))
        else:
            parts = [types.Part.from_text(text=content)]

        if role == "system":
            system_instruction = content
            continue

        contents.append(types.Content(role="model" if role == "assistant" else "user", parts=parts))

    return system_instruction, contents


def _call_gemini(messages: list[dict], max_tokens: int, temperature: float) -> str:
    system_instruction, contents = _messages_to_gemini(messages)
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            max_output_tokens=max_tokens,
            temperature=temperature,
        ),
    )
    return (response.text or "").strip()


def chat_completion(messages: list[dict], model: str = MODEL, max_tokens: int = 300, temperature: float = 0.9) -> str:
    global _groq_daily_limited_until

    if time.time() < _groq_daily_limited_until:
        reply = _call_gemini(messages, max_tokens, temperature)
        print(f"{LIGHT_BLUE}[llm] served by gemini ({GEMINI_MODEL}, groq cooling down){RESET}", flush=True)
        return reply

    for attempt in range(2):
        try:
            response = groq_client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_format="hidden",
            )
            print(f"{LIGHT_GREEN}[llm] served by groq ({model}){RESET}", flush=True)
            content = response.choices[0].message.content.strip()
            return _THINK_TAG_RE.sub("", content).strip()
        except RateLimitError as e:
            if _is_daily_limit(e):
                cooldown = _retry_after_seconds(e) or GROQ_COOLDOWN_SECONDS
                _groq_daily_limited_until = time.time() + cooldown
                print(f"{RED}[llm] Groq daily limit hit, falling back to Gemini for {cooldown:.0f}s{RESET}", flush=True)
                reply = _call_gemini(messages, max_tokens, temperature)
                print(f"{LIGHT_BLUE}[llm] served by gemini ({GEMINI_MODEL}){RESET}", flush=True)
                return reply

            wait = min(_retry_after_seconds(e), 10) or 2
            print(f"{YELLOW}[llm] Groq rate limited, retrying in {wait:.1f}s{RESET}", flush=True)
            time.sleep(wait)

    print(f"{RED}[llm] Groq still rate limited after retry, falling back to Gemini{RESET}", flush=True)
    reply = _call_gemini(messages, max_tokens, temperature)
    print(f"{LIGHT_BLUE}[llm] served by gemini ({GEMINI_MODEL}){RESET}", flush=True)
    return reply
