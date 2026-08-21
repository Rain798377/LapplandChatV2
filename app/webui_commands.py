"""
webui_commands.py -- text-command equivalents of the bot's Discord slash
commands, for typing "/name args" into the WebUI chat box instead of
Discord's slash-command picker.

Only commands that are meaningful outside a Discord guild are here.
Deliberately NOT ported (see webui_server.py's dispatch for how these are
skipped rather than silently missing):
  - /call, /endcall, /spotify ...     -- need a Discord voice channel to
                                          play/stream into; no equivalent here.
  - /download, /frame_inflate          -- commands/downloader_cmds.py is a
                                          large module built around Discord
                                          message-edit progress updates and
                                          file uploads; porting it is a
                                          separate task, not a drop-in.
  - /listen-here, /stop-listening, /listen-idle, /stop-listening-idle
                                        -- Discord channel management; the
                                          web UI has exactly one conversation
                                          and is always listening.
  - /quote, /random meme, "Make Quote" -- the chat can render images now
                                          (see _run_imagine below), but these
                                          weren't asked for -- separate task.
  - /terminal                          -- arbitrary shell execution. Discord
                                          gates it to BOT_OWNER_ID; this
                                          server has no auth, and any web
                                          page open in the same browser could
                                          POST to localhost and trigger it
                                          (classic browser-to-localhost CSRF)
                                          -- too risky to expose unauthenticated.

/change_mood, /ai-provider, /memory wipe-all are effectively admin actions on
Discord (guild-admin gated) -- ported here ungated, since the only person who
can reach this server is whoever is running it on their own machine.

A command handler normally returns a plain str reply. cmd_imagine and
cmd_imagine_anime are the exception -- they return {"text": ...,
"image": "/images/<file>"} instead, so webui_server.py's dispatch accepts
either shape rather than every handler needing to conform to a richer
contract it doesn't use. Both share _run_imagine(), which just picks the
backend ("flux" or "anima") passed to core.imagegen.enqueue_generate_image().

/imagine and /imagine_anime each block the whole request until their image
is done (or fails) instead of streaming live progress edits the way
Discord's versions do -- there's no progress channel back to the browser
here, just one request/response. Given Modal's containers scale to zero
after 5s idle (see modal_imagegen/README.md and modal_flux/README.md), a
cold start can make this take 30-90s+; the chat's "thinking" indicator is
the only feedback for that whole span.
"""

import hashlib
import os
import random
import secrets

import httpx

from core import ai, llm
from core.imagegen import enqueue_generate_image
from core.llm import DEFAULT_PROVIDER_CHAIN
from core.memory import load_memory, save_memory

# ── misc / utility ──────────────────────────────────────────────────────────

def cmd_help(args: list[str], raw: str, req) -> str:
    lines = [
        "/help -- this list",
        "/ping -- check the server responds",
        "/time -- current server time",
        "/mood -- the bot's current mood",
        "/change_mood <mood> -- set the bot's mood",
        f"/ai-provider <auto|{'|'.join(DEFAULT_PROVIDER_CHAIN)}> -- pin or unpin the LLM provider",
        "/echo <text> -- echo it back",
        "/curl <url> -- GET a URL and show the response",
        "/ip -- the server's public IP",
        "/8ball <question> -- ask the magic 8-ball",
        "/ship <name1> <name2> -- compatibility rating",
        "/random number|coin|die|choice|word <...> -- random stuff",
        "/memory wipe|wipe-all|edit <notes>|view -- manage what the bot remembers about you",
        "/imagine <prompt> -- generate an image with FLUX.1-schnell (can take 30-90s+ on a cold start)",
        "/imagine_anime <prompt> -- generate an image with Filigree-Anima (same cold-start caveat)",
    ]
    return "\n".join(lines)


def cmd_ping(args: list[str], raw: str, req) -> str:
    return "pong"


def cmd_time(args: list[str], raw: str, req) -> str:
    from datetime import datetime
    return f"Current server time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"


def cmd_mood(args: list[str], raw: str, req) -> str:
    return f"I'm currently feeling {ai.current_mood}!"


def cmd_change_mood(args: list[str], raw: str, req) -> str:
    mood = raw.strip()
    if not mood:
        return "usage: /change_mood <mood>"
    ai.current_mood = mood
    return f"Mood changed to {mood}!"


def cmd_ai_provider(args: list[str], raw: str, req) -> str:
    choices = ("auto",) + DEFAULT_PROVIDER_CHAIN
    value = args[0].lower() if args else ""
    if not value:
        return f"usage: /ai-provider <{'|'.join(choices)}>"
    if value == "auto":
        llm.set_forced_provider(None)
        return "Back to normal fallback routing (auto)."
    if value not in DEFAULT_PROVIDER_CHAIN:
        return f"unknown provider '{value}'. choose one of: {', '.join(choices)}"
    if llm.is_provider_disabled(value):
        return f"Can't force {value} -- it's disabled (no API key configured, or its key already failed auth)."
    llm.set_forced_provider(value)
    return f"AI provider forced to {value}."


def cmd_echo(args: list[str], raw: str, req) -> str:
    return raw or "(nothing to echo)"


def cmd_curl(args: list[str], raw: str, req) -> str:
    url = raw.strip()
    if not url:
        return "usage: /curl <url>"
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        resp = httpx.get(url, timeout=10)
    except httpx.RequestError as e:
        return f"request failed: {e}"
    content = resp.text
    return content if len(content) <= 1900 else content[:1900] + "\n...[truncated]"


def cmd_ip(args: list[str], raw: str, req) -> str:
    try:
        resp = httpx.get("https://api.ipify.org", timeout=10)
    except httpx.RequestError as e:
        return f"couldn't fetch IP: {e}"
    return f"Public IP: {resp.text}"


def cmd_8ball(args: list[str], raw: str, req) -> str:
    question = raw.strip()
    if not question:
        return "usage: /8ball <question>"
    responses = [
        "It is certain.", "It is decidedly so.", "Without a doubt.",
        "Yes - definitely.", "You may rely on it.", "As I see it, yes.",
        "Most likely.", "Outlook good.", "Yes.", "Signs point to yes.",
        "Reply hazy, try again.", "Ask again later.", "Better not tell you now.",
        "Cannot predict now.", "Concentrate and ask again.",
        "Don't count on it.", "My reply is no.", "My sources say no.",
        "Outlook not so good.", "Very doubtful.",
    ]
    return f"Asked: {question}\n{random.choice(responses)}"


def cmd_ship(args: list[str], raw: str, req) -> str:
    if len(args) < 2:
        return "usage: /ship <name1> <name2>"
    n1, n2 = args[0], args[1]
    if n1.lower() == n2.lower():
        return "You can't ship someone with themselves!"

    lo, hi = sorted((n1.lower(), n2.lower()))
    seed = int(hashlib.sha256(f"{lo}|{hi}".encode()).hexdigest(), 16)
    compatibility = seed % 101
    ship_name = n1[:len(n1) // 2] + n2[len(n2) // 2:]

    if compatibility >= 80:   label = "Soulmates"
    elif compatibility >= 60: label = "Great match"
    elif compatibility >= 40: label = "Could work"
    elif compatibility >= 20: label = "Rough waters"
    else:                     label = "Disaster"

    filled = round(compatibility / 10)
    bar = "█" * filled + "░" * (10 - filled)
    return f"{n1} x {n2}\n{label} -- {bar} {compatibility}%\nShip name: {ship_name}"


# ── /random ──────────────────────────────────────────────────────────────────

def cmd_random(args: list[str], raw: str, req) -> str:
    if not args:
        return "usage: /random <number|coin|die|choice|word> ..."
    sub, rest = args[0].lower(), args[1:]

    if sub == "number":
        if not rest or not rest[0].isdigit():
            return "usage: /random number <max>"
        max_n = int(rest[0])
        return f"Your random number is: {secrets.randbelow(max_n) + 1}"

    if sub == "coin":
        return f"The coin landed on: {secrets.choice(['heads', 'tails'])}"

    if sub == "die":
        if not rest or not rest[0].isdigit():
            return "usage: /random die <sides>"
        sides = int(rest[0])
        return f"You rolled a {secrets.randbelow(sides) + 1} on a {sides}-sided die."

    if sub == "choice":
        items_raw = raw.split(None, 1)[1] if len(raw.split(None, 1)) > 1 else ""
        items = [i.strip() for i in items_raw.split(",") if i.strip()]
        if not items:
            return "usage: /random choice <comma, separated, items>"
        return f"I choose: {secrets.choice(items)}"

    if sub == "word":
        try:
            resp = httpx.get(
                "https://raw.githubusercontent.com/dwyl/english-words/master/words.txt",
                timeout=15,
            )
            words = [w.strip() for w in resp.text.splitlines() if len(w.strip()) <= 12]
            return f"Your random word is: {secrets.choice(words)}"
        except httpx.RequestError as e:
            return f"couldn't fetch a word list: {e}"

    return f"unknown /random subcommand '{sub}' -- try /help"


# ── /memory ──────────────────────────────────────────────────────────────────

def cmd_memory(args: list[str], raw: str, req) -> str:
    if not args:
        return "usage: /memory <wipe|wipe-all|edit <notes>|view>"
    sub = args[0].lower()
    memory = load_memory()

    if sub == "wipe-all":
        save_memory({})
        return "All memory wiped."

    if sub == "wipe":
        if req.user_id in memory:
            del memory[req.user_id]
            save_memory(memory)
            return "Your memory has been wiped."
        return "I don't have anything on you."

    if sub == "edit":
        notes = raw.split(None, 1)[1].strip() if len(raw.split(None, 1)) > 1 else ""
        if not notes:
            return "usage: /memory edit <new notes text>"
        display_name = memory.get(req.user_id, {}).get("display_name", req.username)
        memory[req.user_id] = {"display_name": display_name, "notes": notes}
        save_memory(memory)
        return "memory updated"

    if sub == "view":
        entry = memory.get(req.user_id)
        if not entry:
            return "I don't have anything on you yet"
        return f"memory for {entry['display_name']}:\n{entry['notes']}"

    return f"unknown /memory subcommand '{sub}' -- try /help"


# ── /imagine (flux), /imagine_anime (anima) ────────────────────────────────

def _run_imagine(prompt: str, usage: str, model: str) -> str | dict:
    """Shared by cmd_imagine (flux) and cmd_imagine_anime (anima) -- only the
    backend differs; queueing/polling/response-shape is identical."""
    if not prompt:
        return usage

    # enqueue_generate_image() itself is non-blocking (hands the job to that
    # backend's own shared worker thread and returns immediately) -- the
    # blocking part is this while loop draining update_queue, which is fine
    # here since the caller (webui_server.py) already runs this whole
    # function via asyncio.to_thread, same as get_ai_response().
    update_queue, position = enqueue_generate_image(prompt, model=model)
    filepath = None
    error = None
    while True:
        update = update_queue.get()
        if update is None:
            break
        if update["type"] == "done":
            filepath = update["path"]
        elif update["type"] == "error":
            error = update["message"]
        # "progress" updates are dropped -- no channel back to the browser to
        # stream them through (see module docstring).

    if filepath and os.path.exists(filepath):
        # Unlike Discord's /imagine (services the file to Discord's CDN then
        # deletes its local copy), this server IS the host the browser loads
        # the image from, so the file has to stay on disk -- never cleaned up
        # here. data/images/ grows unbounded; not addressed, wasn't asked for.
        return {"text": prompt, "image": f"/images/{os.path.basename(filepath)}"}
    return f"couldn't generate that image, sorry.{f' ({error})' if error else ''}"


def cmd_imagine(args: list[str], raw: str, req) -> str | dict:
    return _run_imagine(raw.strip(), "usage: /imagine <prompt>", "flux")


def cmd_imagine_anime(args: list[str], raw: str, req) -> str | dict:
    return _run_imagine(raw.strip(), "usage: /imagine_anime <prompt>", "anima")


COMMANDS = {
    "help": cmd_help,
    "ping": cmd_ping,
    "time": cmd_time,
    "mood": cmd_mood,
    "change_mood": cmd_change_mood,
    "ai-provider": cmd_ai_provider,
    "echo": cmd_echo,
    "curl": cmd_curl,
    "ip": cmd_ip,
    "8ball": cmd_8ball,
    "ship": cmd_ship,
    "random": cmd_random,
    "memory": cmd_memory,
    "imagine": cmd_imagine,
    "imagine_anime": cmd_imagine_anime,
}
