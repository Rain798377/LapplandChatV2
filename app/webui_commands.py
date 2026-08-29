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
                                          (see /imagine, /imagine_anime in
                                          webui_server.py), but these weren't
                                          asked for -- separate task.
  - /terminal                          -- arbitrary shell execution. Discord
                                          gates it to BOT_OWNER_ID; this
                                          server has no auth, and any web
                                          page open in the same browser could
                                          POST to localhost and trigger it
                                          (classic browser-to-localhost CSRF)
                                          -- too risky to expose unauthenticated.

/change_mood, /ai-provider, /memory wipe-all are effectively admin actions on
Discord (guild-admin gated) -- gated the same way here (ADMIN_USER_ID only)
now that real multi-user accounts exist (core/auth.py); the original
"ungated, only person who can reach this server" reasoning no longer holds
once more than one person can sign in. See also WebUI/admin.html, which
surfaces these same actions as buttons instead of typed commands.

A command handler normally returns a plain str reply -- except /imagine and
/imagine_anime, which are NOT in COMMANDS below. Unlike every other command,
they need to keep editing their own chat_store row as generation progresses
(a live progress bar, mirroring Discord's message-edit version), which means
they need chat_store and event-loop access this module's handlers don't
have (a plain sync function returning one str/dict). So webui_server.py's
chat() intercepts those two command names directly, before the COMMANDS
dispatch below ever sees them -- see _run_imagine_command() there.
parse_imagine_args() below is the one piece of that still worth keeping as a
pure function here: it's just text parsing, no orchestration.
"""

import hashlib
import random
import re
import secrets

import httpx

from core import ai, llm
from core.config import ADMIN_USER_ID
from core.llm import DEFAULT_PROVIDER_CHAIN
from core.memory import load_memory, save_memory


def _require_admin(req) -> str | None:
    """Returns an error string if req isn't the admin account, else None --
    shared by the handlers below that are admin-only now that this server
    has real multi-user accounts."""
    if req.user_id != ADMIN_USER_ID:
        return "you're not an administrator."
    return None

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
        "/imagine <prompt> [--width N] [--height N] [--steps N] [--cfg N] [--seed N] [--negative \"text\"]"
        " -- generate an image with FLUX.1-schnell (can take 30-90s+ on a cold start)",
        "/imagine_anime <prompt> [--width N] [--height N] [--steps N] [--cfg N] [--seed N] [--negative \"text\"]"
        " -- generate an image with Filigree-Anima (same cold-start caveat)",
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
    if (err := _require_admin(req)) is not None:
        return err
    mood = raw.strip()
    if not mood:
        return "usage: /change_mood <mood>"
    ai.current_mood = mood
    return f"Mood changed to {mood}!"


def cmd_ai_provider(args: list[str], raw: str, req) -> str:
    if (err := _require_admin(req)) is not None:
        return err
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
        if (err := _require_admin(req)) is not None:
            return err
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


# ── /imagine (flux), /imagine_anime (anima) -- parsing only; the actual
# generation + progress streaming lives in webui_server.py's
# _run_imagine_command() (see the module docstring for why) ────────────────

# flag name -> (generate_image() kwarg, value type). Kept in one place so
# parse_imagine_args()'s error message and its casting loop can't drift out
# of sync with each other.
_IMAGINE_FLAGS = {
    "negative": ("negative_prompt", str),
    "width": ("width", int),
    "height": ("height", int),
    "steps": ("steps", int),
    "cfg": ("cfg", float),
    "seed": ("seed", int),
}
# --flag "quoted value with spaces"  |  --flag bareword
_IMAGINE_FLAG_RE = re.compile(r'--(\w+)\s+"([^"]*)"|--(\w+)\s+(\S+)')


def parse_imagine_args(raw: str) -> tuple[str, dict, str | None]:
    """Pulls "--flag value" pairs out of /imagine's raw argument text
    (value may be double-quoted to include spaces, e.g. --negative "extra
    limbs, blurry"), leaving whatever's left as the prompt. Returns
    (prompt, kwargs, error): kwargs is ready to pass straight to
    core.imagegen.generate_image()/enqueue_generate_image() as **kwargs, and
    is only meaningful when error is None -- an unrecognized flag or a value
    that won't cast to the right type is reported as error rather than
    silently dropped or left to crash generate_image() downstream."""
    raw_flags: dict[str, str] = {}

    def _collect(m: re.Match) -> str:
        name = m.group(1) or m.group(3)
        value = m.group(2) if m.group(1) is not None else m.group(4)
        raw_flags[name] = value
        return " "

    prompt = re.sub(r"\s+", " ", _IMAGINE_FLAG_RE.sub(_collect, raw)).strip()

    kwargs: dict = {}
    for name, value in raw_flags.items():
        if name not in _IMAGINE_FLAGS:
            return prompt, {}, f"unknown flag --{name}. try: {', '.join('--' + f for f in _IMAGINE_FLAGS)}"
        dest, cast = _IMAGINE_FLAGS[name]
        try:
            kwargs[dest] = cast(value)
        except ValueError:
            article = "an" if cast.__name__[0] in "aeiou" else "a"
            return prompt, {}, f"--{name} must be {article} {cast.__name__}, got '{value}'"
    return prompt, kwargs, None


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
    # "imagine"/"imagine_anime" are deliberately NOT here -- see the module
    # docstring; webui_server.py's chat() intercepts those two names before
    # this dict is ever consulted.
}
