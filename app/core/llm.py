"""
llm.py — shared chat-completion wrapper with a Groq -> Gemini -> Cloudflare ->
Mistral -> OpenRouter fallback chain.

All chat calls in the bot (main reply, vision, memory extraction) should go
through chat_completion() instead of hitting a provider client directly, so a
single provider outage switches every call site over to the next one at once.

Env vars: GROQ_API_KEY, GEMINI_API_KEY, CLOUDFLARE_API_TOKEN,
CLOUDFLARE_ACCOUNT_ID, MISTRAL_API_KEY, OPENROUTER_API_KEY (see core/config.py)
"""

import base64
import re
import threading
import time
import httpx
from groq import (
    Groq,
    RateLimitError,
    InternalServerError,
    APIConnectionError,
    AuthenticationError,
    PermissionDeniedError,
)
from google import genai
from google.genai import types
from google.genai import errors as genai_errors
from core.config import (
    GROQ_API_KEY,
    GEMINI_API_KEY,
    CLOUDFLARE_API_TOKEN,
    CLOUDFLARE_ACCOUNT_ID,
    MISTRAL_API_KEY,
    OPENROUTER_API_KEY,
    MODEL,
)
from core.colors import *

groq_client = Groq(api_key=GROQ_API_KEY)
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

GEMINI_MODEL = "gemini-3.5-flash-lite"

CLOUDFLARE_BASE_URL = "https://api.cloudflare.com/client/v4"
CLOUDFLARE_MODEL = "@cf/qwen/qwen3-30b-a3b-fp8"

MISTRAL_BASE_URL = "https://api.mistral.ai/v1"
MISTRAL_MODEL = "mistral-small-latest"
# Mistral's free tier is ~1 request/second -- requests are serialized through
# _mistral_lock and spaced at least this far apart rather than fired concurrently.
MISTRAL_MIN_INTERVAL_SECONDS = 1.05

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Free endpoints get delisted without notice, so the model id is normally
# resolved at runtime from /models (see _resolve_openrouter_model). This is
# only the last-known-good id used if resolution fails on the very first
# call of a fresh process (before anything has been cached) -- update it if
# it goes stale.
OPENROUTER_FALLBACK_MODEL = "qwen/qwen3-30b-a3b:free"
OPENROUTER_MODEL_CACHE_TTL = 6 * 60 * 60
# Model family preference (earlier = preferred) and target size for
# resolving an OpenRouter free model, per CLAUDE.md.
OPENROUTER_FAMILY_PREFERENCE = ("qwen", "llama", "gemma")
OPENROUTER_TARGET_PARAMS_B = 30
OPENROUTER_MIN_PARAMS_B = 20
OPENROUTER_MAX_PARAMS_B = 35
# cap on how many cached candidates _call_openrouter will cascade through on
# repeated 429s -- bounded well below the request's daily quota impact even
# if every attempt fails
OPENROUTER_MAX_CASCADE_ATTEMPTS = 3
# how long a single OpenRouter model is skipped in the cascade after 429ing
OPENROUTER_MODEL_COOLDOWN_SECONDS = 60

# retry-after values above this are treated as a daily/quota exhaustion
# rather than a short per-minute limit
DAILY_LIMIT_RETRY_AFTER_SECONDS = 120
# fallback cooldown when Groq doesn't give us a usable retry-after on a
# confirmed daily-quota trip
GROQ_DAILY_COOLDOWN_SECONDS = 6 * 60 * 60
# default cooldown for any other provider failure (5xx, connection error,
# empty reply, or a 429 with no usable Retry-After)
GENERIC_COOLDOWN_SECONDS = 60

DEFAULT_PROVIDER_CHAIN = ("groq", "gemini", "cloudflare", "mistral", "openrouter")
# vision requests only exist as Groq (image-capable) -> Gemini (adapts image
# blocks itself in _messages_to_gemini); the other three providers' vision
# support isn't specced here, so they're left out of this chain rather than
# guessed at.
VISION_PROVIDER_CHAIN = ("groq", "gemini")

# provider name -> epoch time until which it's skipped process-wide (429/5xx/
# connection errors -- a shared-quota resource, so this isn't per-channel)
_provider_cooldown_until: dict[str, float] = {name: 0.0 for name in DEFAULT_PROVIDER_CHAIN}
# provider name -> permanently skipped for this process (bad key / 401 / 403,
# or the key was never configured)
_provider_disabled: dict[str, bool] = {
    "groq": False,
    "gemini": gemini_client is None,
    "cloudflare": not (CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID),
    "mistral": not MISTRAL_API_KEY,
    "openrouter": not OPENROUTER_API_KEY,
}
# admin override set via the /ai-provider slash command: pins chat_completion()
# to a single provider instead of the normal DEFAULT_PROVIDER_CHAIN fallback.
# None = normal chain behavior. Only applies to calls that didn't already pass
# their own explicit providers tuple (see chat_completion), so it never forces
# the vision chain onto a non-vision-capable provider.
_forced_provider: str | None = None

_mistral_lock = threading.Lock()
_mistral_last_call = 0.0

_openrouter_model_cache: list[str] = []
_openrouter_model_cache_time = 0.0
# model id -> epoch time until which _call_openrouter skips it in the cascade
# (429'd upstream -- e.g. a saturated free-tier model). Without this, every
# message re-discovers the same saturated model is down and burns a request
# on it before falling through, and OpenRouter counts failed attempts against
# the daily request quota.
_openrouter_model_cooldown_until: dict[str, float] = {}

_PROVIDER_LOG_COLOR = {
    "groq": LIGHT_GREEN,
    "gemini": LIGHT_BLUE,
    "cloudflare": CYAN,
    "mistral": CYAN,
    "openrouter": CYAN,
}

# qwen/qwen3.6-27b (MODEL) is a reasoning model that thinks out loud before
# answering, which ate into max_tokens and could exhaust the whole budget on
# hidden reasoning before writing a visible reply. reasoning_effort="none" is
# a Qwen-3.6-27b-specific param that turns reasoning off entirely -- other
# Groq models (e.g. VISION_MODEL) don't support it, so it's only sent for
# this model rather than on every call.
_REASONING_EFFORT_MODELS = {"qwen/qwen3.6-27b"}


class _RetryableFailure(Exception):
    """Raised by a provider adapter for a failure that should advance the
    chain (429/5xx/connection error/timeout/empty reply). Adapters must NOT
    raise this for a 400/422 -- that's a bug in our request, and advancing
    would just burn the next provider's quota on the same broken payload;
    those should propagate as whatever exception the SDK/HTTP call raised."""

    def __init__(self, cooldown: float = 0.0, disable: bool = False):
        self.cooldown = cooldown
        self.disable = disable


class AllProvidersUnavailable(RuntimeError):
    """Raised by chat_completion() when every provider in the chain is
    disabled, cooling down, or just failed. Its own type (rather than a bare
    RuntimeError) so callers like describe_error() can recognize it without
    string-matching the message."""


def _clamp_temperature(temperature: float) -> float:
    return max(0.0, min(temperature, 1.0))


def _retry_after_seconds(err: RateLimitError) -> float:
    try:
        return float(err.response.headers.get("retry-after", 0))
    except (TypeError, ValueError):
        return 0.0


def _retry_after_header(resp: httpx.Response) -> float:
    try:
        return float(resp.headers.get("retry-after", 0))
    except (TypeError, ValueError):
        return 0.0


def _is_daily_limit(err: RateLimitError) -> bool:
    body_msg = ""
    if isinstance(err.body, dict):
        body_msg = str(err.body.get("error", {}).get("message", "")).lower()
    if "per day" in body_msg or "tpd" in body_msg or "rpd" in body_msg:
        return True
    return _retry_after_seconds(err) > DAILY_LIMIT_RETRY_AFTER_SECONDS


# ── Groq ─────────────────────────────────────────────────────────────────────

def _call_groq(messages: list[dict], max_tokens: int, temperature: float, model: str) -> str:
    extra_params = {"reasoning_effort": "none"} if model in _REASONING_EFFORT_MODELS else {}

    for attempt in range(2):
        try:
            response = groq_client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                **extra_params,
            )
            content = (response.choices[0].message.content or "").strip() if response.choices else ""
            if not content:
                raise _RetryableFailure()
            return content
        except RateLimitError as e:
            if _is_daily_limit(e):
                cooldown = _retry_after_seconds(e) or GROQ_DAILY_COOLDOWN_SECONDS
                raise _RetryableFailure(cooldown=cooldown)

            if attempt == 0:
                wait = min(_retry_after_seconds(e), 10) or 2
                print(f"{YELLOW}[llm] Groq rate limited, retrying in {wait:.1f}s{RESET}", flush=True)
                time.sleep(wait)
                continue
            raise _RetryableFailure(cooldown=_retry_after_seconds(e))
        except (InternalServerError, APIConnectionError):
            # 5xx / connection error / timeout (APITimeoutError subclasses
            # APIConnectionError) -- not worth retrying inline
            raise _RetryableFailure()
        except (AuthenticationError, PermissionDeniedError):
            raise _RetryableFailure(disable=True)

    raise _RetryableFailure()


# ── Gemini ───────────────────────────────────────────────────────────────────

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
    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                max_output_tokens=max_tokens,
                temperature=temperature,
            ),
        )
    except genai_errors.ClientError as e:
        if e.code in (400, 422):
            raise
        if e.code in (401, 403):
            raise _RetryableFailure(disable=True)
        if e.code == 429:
            raise _RetryableFailure(cooldown=GENERIC_COOLDOWN_SECONDS)
        raise _RetryableFailure()
    except genai_errors.ServerError:
        raise _RetryableFailure()

    content = (response.text or "").strip()
    if not content:
        raise _RetryableFailure()
    return content


# ── shared OpenAI-compatible REST response parsing (Cloudflare/Mistral/OpenRouter) ──

def _parse_openai_compatible(resp: httpx.Response) -> str:
    if resp.status_code in (400, 422):
        resp.raise_for_status()
    if resp.status_code in (401, 403):
        raise _RetryableFailure(disable=True)
    if resp.status_code == 429:
        raise _RetryableFailure(cooldown=_retry_after_header(resp) or GENERIC_COOLDOWN_SECONDS)
    if resp.status_code != 200:
        raise _RetryableFailure()

    try:
        data = resp.json()
        content = (data["choices"][0]["message"]["content"] or "").strip()
    except (ValueError, KeyError, IndexError, TypeError):
        raise _RetryableFailure()

    if not content:
        raise _RetryableFailure()
    return content


# ── Cloudflare Workers AI ───────────────────────────────────────────────────
# Free tier is ~10,000 Neurons/day (a few hundred requests on a 30B model)
# and failed requests still consume budget, so this makes exactly one
# attempt -- no retry loop.

# CLOUDFLARE_MODEL is a reasoning model like MODEL (see _REASONING_EFFORT_MODELS),
# but Cloudflare's REST API has no reasoning_effort/enable_thinking param to turn
# it off -- confirmed against the live endpoint's documented params (prompt, lora,
# response_format, raw, stream, max_tokens, temperature, top_p, top_k, seed,
# repetition_penalty, frequency_penalty, presence_penalty). Without it, the hidden
# reasoning trace can eat the whole max_tokens budget and return empty content.
# Qwen3's chat template instead honors an in-band "/no_think" directive; appending
# it to the system message (verified live) empties the reasoning trace and returns
# the full budget to the visible reply. This only touches the copy of the messages
# sent to Cloudflare, not the shared system prompt content itself.
def _disable_cloudflare_thinking(messages: list[dict]) -> list[dict]:
    out = list(messages)
    for i, msg in enumerate(out):
        if msg["role"] == "system":
            out[i] = {**msg, "content": f"{msg['content']} /no_think"}
            return out
    return [{"role": "system", "content": "/no_think"}] + out


def _call_cloudflare(messages: list[dict], max_tokens: int, temperature: float) -> str:
    url = f"{CLOUDFLARE_BASE_URL}/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/{CLOUDFLARE_MODEL}"
    try:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"},
            json={
                "messages": _disable_cloudflare_thinking(messages),
                "max_tokens": max_tokens,
                "temperature": _clamp_temperature(temperature),
            },
            timeout=30,
        )
    except httpx.RequestError:
        raise _RetryableFailure()

    if resp.status_code in (400, 422):
        resp.raise_for_status()
    if resp.status_code in (401, 403):
        raise _RetryableFailure(disable=True)
    if resp.status_code == 429:
        raise _RetryableFailure(cooldown=_retry_after_header(resp) or GENERIC_COOLDOWN_SECONDS)
    if resp.status_code != 200:
        raise _RetryableFailure()

    try:
        data = resp.json()
    except ValueError:
        raise _RetryableFailure()

    if not data.get("success", True):
        raise _RetryableFailure()

    content = ((data.get("result") or {}).get("response") or "").strip()
    if not content:
        raise _RetryableFailure()
    return content


# ── Mistral ──────────────────────────────────────────────────────────────────
# Free tier is ~1 request/second -- _mistral_lock is held for the full
# duration of the call so concurrent messages queue up serialized instead of
# bursting, and a minimum spacing is enforced between calls.

def _call_mistral(messages: list[dict], max_tokens: int, temperature: float) -> str:
    global _mistral_last_call

    with _mistral_lock:
        wait = MISTRAL_MIN_INTERVAL_SECONDS - (time.time() - _mistral_last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = httpx.post(
                f"{MISTRAL_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {MISTRAL_API_KEY}"},
                json={
                    "model": MISTRAL_MODEL,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": _clamp_temperature(temperature),
                },
                timeout=30,
            )
        except httpx.RequestError:
            _mistral_last_call = time.time()
            raise _RetryableFailure()
        _mistral_last_call = time.time()

    return _parse_openai_compatible(resp)


# ── OpenRouter ───────────────────────────────────────────────────────────────
# Failed attempts count against the daily request quota, so this makes one
# attempt -- no retry loop. The model id is resolved at runtime since free
# endpoints get delisted without notice.

def _param_billions(model_id: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)b", model_id.lower())
    return float(match.group(1)) if match else None


def _resolve_openrouter_model() -> str:
    global _openrouter_model_cache, _openrouter_model_cache_time

    if _openrouter_model_cache and time.time() - _openrouter_model_cache_time < OPENROUTER_MODEL_CACHE_TTL:
        return _openrouter_model_cache[0]

    try:
        resp = httpx.get(
            f"{OPENROUTER_BASE_URL}/models",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            timeout=10,
        )
        resp.raise_for_status()
        models = resp.json().get("data", [])
    except Exception as e:
        print(f"{YELLOW}[llm] OpenRouter model resolution failed ({e}), using last known-good{RESET}", flush=True)
        return _openrouter_model_cache[0] if _openrouter_model_cache else OPENROUTER_FALLBACK_MODEL

    candidates = []
    for m in models:
        model_id = m.get("id", "")
        if not model_id.endswith(":free"):
            continue
        supported = m.get("supported_parameters") or []
        if "tools" not in supported:
            continue
        params_b = _param_billions(model_id)
        if params_b is None or not (OPENROUTER_MIN_PARAMS_B <= params_b <= OPENROUTER_MAX_PARAMS_B):
            continue
        family_rank = next(
            (i for i, fam in enumerate(OPENROUTER_FAMILY_PREFERENCE) if fam in model_id.lower()),
            len(OPENROUTER_FAMILY_PREFERENCE),
        )
        candidates.append((family_rank, abs(params_b - OPENROUTER_TARGET_PARAMS_B), model_id))

    if not candidates:
        print(f"{YELLOW}[llm] no matching OpenRouter free models found, using last known-good{RESET}", flush=True)
        return _openrouter_model_cache[0] if _openrouter_model_cache else OPENROUTER_FALLBACK_MODEL

    candidates.sort()
    _openrouter_model_cache = [model_id for _, _, model_id in candidates[:5]]
    _openrouter_model_cache_time = time.time()
    return _openrouter_model_cache[0]


def _call_openrouter(messages: list[dict], max_tokens: int, temperature: float) -> str:
    # A resolved model is occasionally rate-limited on OpenRouter's shared free
    # upstream (seen live: two different free Gemma variants, routed through
    # two different backing providers, both 429 with "temporarily rate-limited
    # upstream") even though the model itself is fine and other cached
    # candidates work. On 429, cascade through the rest of the cached
    # candidates (already sorted qwen > llama > gemma per CLAUDE.md's family
    # preference, then closeness to target size) instead of giving up after
    # one -- if a qwen/llama free model is ever available again it'll already
    # be first in this list, no hardcoded id needed (free endpoints get
    # delisted without notice, and there are currently zero free qwen/llama
    # models on OpenRouter at all -- confirmed live). Capped at
    # OPENROUTER_MAX_CASCADE_ATTEMPTS so this stays bounded rather than
    # unbounded retries.
    #
    # A model that just 429'd is skipped for OPENROUTER_MODEL_COOLDOWN_SECONDS
    # rather than re-tried on the very next message -- otherwise every message
    # burns a request rediscovering the same saturated model is down, and
    # OpenRouter counts failed attempts against the daily request quota.
    now = time.time()
    primary = _resolve_openrouter_model()
    ordered = [primary] + [m for m in _openrouter_model_cache if m != primary]
    candidates = [m for m in ordered if now >= _openrouter_model_cooldown_until.get(m, 0)][:OPENROUTER_MAX_CASCADE_ATTEMPTS]

    if not candidates:
        # every cached candidate is cooling down -- don't burn a request
        # rediscovering that
        raise _RetryableFailure(cooldown=GENERIC_COOLDOWN_SECONDS)

    for i, model in enumerate(candidates):
        try:
            resp = httpx.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": _clamp_temperature(temperature),
                },
                timeout=30,
            )
        except httpx.RequestError:
            raise _RetryableFailure()

        if resp.status_code == 429:
            _openrouter_model_cooldown_until[model] = time.time() + OPENROUTER_MODEL_COOLDOWN_SECONDS
            if i < len(candidates) - 1:
                print(f"{YELLOW}[llm] OpenRouter model {model} rate-limited upstream, cooling down {OPENROUTER_MODEL_COOLDOWN_SECONDS:.0f}s, trying {candidates[i + 1]}{RESET}", flush=True)
                continue

        return _parse_openai_compatible(resp)


# ── chain orchestration ─────────────────────────────────────────────────────

_ADAPTERS = {
    "groq": lambda messages, max_tokens, temperature, model: _call_groq(messages, max_tokens, temperature, model),
    "gemini": lambda messages, max_tokens, temperature, model: _call_gemini(messages, max_tokens, temperature),
    "cloudflare": lambda messages, max_tokens, temperature, model: _call_cloudflare(messages, max_tokens, temperature),
    "mistral": lambda messages, max_tokens, temperature, model: _call_mistral(messages, max_tokens, temperature),
    "openrouter": lambda messages, max_tokens, temperature, model: _call_openrouter(messages, max_tokens, temperature),
}


def get_forced_provider() -> str | None:
    return _forced_provider


def set_forced_provider(name: str | None) -> None:
    """Pin (or, with None, un-pin) chat_completion() to a single provider.
    Used by the /ai-provider admin command. Clears that provider's cooldown
    so the forced choice is usable right away instead of waiting one out."""
    global _forced_provider
    _forced_provider = name
    if name is not None:
        _provider_cooldown_until[name] = 0.0


def is_provider_disabled(name: str) -> bool:
    return _provider_disabled.get(name, True)


def chat_completion(
    messages: list[dict],
    model: str = MODEL,
    max_tokens: int = 300,
    temperature: float = 0.9,
    providers: tuple[str, ...] = DEFAULT_PROVIDER_CHAIN,
) -> str:
    if providers is DEFAULT_PROVIDER_CHAIN and _forced_provider is not None:
        providers = (_forced_provider,)

    last_error: Exception | None = None
    for name in providers:
        if _provider_disabled[name]:
            continue
        if time.time() < _provider_cooldown_until[name]:
            continue

        try:
            content = _ADAPTERS[name](messages, max_tokens, temperature, model)
        except _RetryableFailure as e:
            last_error = e
            if e.disable:
                _provider_disabled[name] = True
                print(f"{RED}[llm] {name} auth failed, disabling for this process{RESET}", flush=True)
            else:
                cooldown = e.cooldown or GENERIC_COOLDOWN_SECONDS
                _provider_cooldown_until[name] = time.time() + cooldown
                print(f"{RED}[llm] {name} failed, cooling down for {cooldown:.0f}s{RESET}", flush=True)
            continue

        color = _PROVIDER_LOG_COLOR.get(name, CYAN)
        print(f"{color}[llm] served by {name}{RESET}", flush=True)
        return content

    # every remaining provider in the chain is disabled/cooling down or
    # failed -- degrade the way the code already degraded before this chain
    # existed: let the failure propagate to the caller's own error handling
    # rather than inventing a new failure path here.
    if last_error is not None:
        raise AllProvidersUnavailable(f"All providers in the fallback chain are unavailable ({providers})") from last_error
    raise AllProvidersUnavailable(f"No providers available in the fallback chain ({providers}) -- all disabled or cooling down")


def describe_error(e: Exception) -> str:
    """Turn a chat_completion() failure into a short, category-specific
    message for the user (rate limit, auth, bad request, connection/timeout),
    followed by the raw exception on its own line as Discord subtext (`-# `).
    Anything that doesn't match a known category still gets a generic label
    up top, but the raw exception underneath keeps it visible either way."""
    if isinstance(e, AllProvidersUnavailable):
        cause = e.__cause__
        if isinstance(cause, _RetryableFailure) and cause.disable:
            reason = "one of my providers just rejected its API key -- that needs a human to fix, not a retry."
        else:
            reason = "everything I talk to is rate limited or down right now, try again in a bit."

    elif isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status == 429:
            reason = "getting rate limited right now, try again in a bit."
        elif status in (401, 403):
            reason = "auth issue with one of my providers -- bad or expired API key."
        elif status in (400, 422):
            reason = "sent a bad request somewhere -- that's a bug, not a fluke."
        else:
            reason = f"provider returned an unexpected {status}."

    elif isinstance(e, httpx.TimeoutException):
        reason = "that timed out waiting on a provider."

    elif isinstance(e, httpx.RequestError):
        reason = "can't reach one of my providers right now -- connection issue."

    else:
        reason = "hit an error that doesn't fit a known category."

    return f"{reason}\n-# {e}"
