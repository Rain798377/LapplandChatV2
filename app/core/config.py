import os
import secrets

from dotenv import load_dotenv

# This deployment runs independently of the Discord bot (separate NAS
# container, no shared process), so it reads its own env file instead of the
# Discord bot's .env -- relative to cwd (app/), like WEBUI_DIR below.
load_dotenv("../.env-webui")

# Model config
MODEL                 = "qwen/qwen3.6-27b"
MOODS                 = ["chill", "playful", "sarcastic", "tired", "hyper", "annoyed"]
GREETINGS             = {"hello", "hi", "hey", "sup", "yo", "hiya", "heya", "howdy", "morning", "evening", "wsp"}

# Tokens
DISCORD_TOKEN         = os.environ.get("DISCORD_TOKEN")
GROQ_API_KEY          = os.environ.get("GROQ_API_KEY")
GEMINI_API_KEY        = os.environ.get("GEMINI_API_KEY")
# Extended LLM fallback chain (see core/llm.py) -- tried in order after
# Groq/Gemini. A provider with a missing key is skipped for the whole
# process rather than erroring per-request.
CLOUDFLARE_API_TOKEN  = os.environ.get("CLOUDFLARE_API_TOKEN")
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
MISTRAL_API_KEY       = os.environ.get("MISTRAL_API_KEY")
OPENROUTER_API_KEY    = os.environ.get("OPENROUTER_API_KEY")
SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")

# Voice call config
STT_SERVER_URL         = os.environ.get("STT_SERVER_URL", "http://127.0.0.1:8801")
TTS_SERVER_URL         = os.environ.get("TTS_SERVER_URL", "http://192.9.148.110:8000/tts")
TTS_SERVER_TOKEN       = os.environ.get("TTS_SERVER_TOKEN")
VOICE_MIN_UTTERANCE_MS = 300     # discard buffered speech shorter than this (noise/blips)
VOICE_IN_SAMPLE_RATE   = 48000   # Discord voice PCM rate (protocol constant)
VOICE_IN_CHANNELS      = 2       # Discord voice PCM is stereo
VOICE_STT_SAMPLE_RATE  = 16000   # rate expected by the whisper.cpp sidecar

# File handling
FILE_SERVER_PATH      = "/srv/downloads"
FILE_SERVER_BASE_URL  = "http://154.27.185.186:9091"
FILE_EXPIRY_SECONDS   = 43200

# Bot config
BOT_NAME              = "Lappland"
# Nicknames people use instead of the full name -- matched as whole words in
# is_name_dropped() (LapplandV2.py), not substrings, so e.g. "lap" doesn't
# also fire on unrelated words like "laptop" or "overlap".
BOT_NICKNAMES         = {"lappland", "lapp", "lappy", "lap"}
BOT_OWNER_ID          = 955604666689921086
REPLY_TO_ALL          = True
ALLOWED_CHANNELS      = [1483716134250217572]  # seed default; managed at runtime via /listen-here, persisted in ALLOWED_CHANNELS_FILE
ALLOWED_CHANNELS_FILE = "data/allowed_channels.json"
MIN_CHARS             = 5
# Reply-chance for messages that don't directly signal they're for the bot
# (no mention/reply/greeting/name-drop/image) -- see on_message in
# LapplandV2.py. Purely local heuristics, no extra LLM calls:
# - REPLY_CHANCE_MOMENTUM: message arrives soon after the bot's own last
#   reply in that channel (REPLY_MOMENTUM_WINDOW_SECONDS) -- treated as an
#   ongoing back-and-forth a real person wouldn't re-ping every line of.
# - REPLY_CHANCE_SOLO: nobody but this one person has spoken in the last
#   SOLO_SPEAKER_WINDOW messages -- if there's no one else in the channel to
#   be talking to, they're almost certainly talking to the bot even without
#   ever naming it (e.g. a channel that's basically a 1:1 with it).
# - REPLY_CHANCE_AMBIENT: neither of the above, nothing points at the bot --
#   genuine multi-person banter it's not part of; rare, occasional
#   interjection rather than replying to nearly everything.
# A message clearly directed at someone else (native reply to, or @mentions,
# a different user) gets none of these -- see on_message.
REPLY_CHANCE_MOMENTUM        = 0.75
REPLY_CHANCE_SOLO            = 0.75
REPLY_CHANCE_AMBIENT         = 0.12
REPLY_MOMENTUM_WINDOW_SECONDS = 3 * 60
SOLO_SPEAKER_WINDOW          = 5  # how many recent human messages to check for "just this one person"

# Optional local sidecar (see intent_server/) -- a small embedding-similarity
# classifier (all-MiniLM-L6-v2 via fastembed, no LLM calls, runs on its own
# machine/venv) that judges whether an otherwise-unaddressed message is
# actually talking to the bot. Consulted (see core/intent.py) only for
# messages that reach the ambient/momentum/solo fallback below -- when unset,
# unreachable, or itself uncertain, on_message falls back to those heuristic
# tiers unchanged, so this is additive rather than a hard dependency.
INTENT_SERVER_URL      = os.environ.get("INTENT_SERVER_URL", "")
INTENT_SERVER_API_KEY  = os.environ.get("INTENT_SERVER_API_KEY")
INTENT_CONNECT_TIMEOUT = 2    # seconds -- fail fast if the sidecar's down
INTENT_RETRY_COOLDOWN  = 60   # seconds to skip it after found unreachable
REPLY_CHANCE_CLASSIFIED_YES = 0.9   # sidecar is confident this IS directed at the bot
REPLY_CHANCE_CLASSIFIED_NO  = 0.02  # sidecar is confident this ISN'T

# Idle chatter config -- when the bot's main channel(s) (ALLOWED_CHANNELS)
# have been quiet for a while, it may unprompted post a single "I'm bored"
# line into a *different* channel: one set via /listen-idle and
# /stop-listening-idle, persisted in IDLE_CHANNELS_FILE. No default channels
# (opt-in only, unlike ALLOWED_CHANNELS).
IDLE_CHANNELS         = []
IDLE_CHANNELS_FILE    = "data/idle_channels.json"
IDLE_THRESHOLD_SECONDS      = 30 * 60  # how long the main channel(s) must be quiet before it's "bored"
IDLE_CHECK_INTERVAL_SECONDS = 5 * 60   # how often to check
IDLE_CHANCE                 = 0.35     # chance to actually speak up once bored, checked each interval

# Music config
AUTOPLAY_DELAY        = 5
DEFAULT_VOLUME        = 0.15 # Default volume level is 15% (0.0 to 2.0)
SPOTIFY_PLAYLIST_MAX_SONGS    = 25

# Memory config
MEMORY_FILE           = "data/memory.json"
MAX_HISTORY           = 30
# Cap on the total memory notes (across all users) injected into the system
# prompt -- memory.json grows unbounded as more users get notes, and without
# this the system prompt could eventually outgrow the smallest context
# window in the LLM fallback chain (see core/llm.py). No per-provider
# tokenizer is wired up since each provider tokenizes differently, so this
# is enforced with a ~4-chars/token estimate (get_user_memory_string in
# core/memory.py) -- conservative enough to stay well under any provider's
# floor.
MAX_MEMORY_TOKENS     = 4000

# Bot profile config -- avatar photo + a short bio line, admin-editable via
# the WebUI admin panel (see core/bot_profile.py, WebUI/admin.html). Shared
# by everyone (unlike a user's own avatar, which is per-browser
# localStorage) since it's the bot's presentation, not any one viewer's.
BOT_PROFILE_FILE      = "data/bot_profile.json"

# WebUI config -- this branch's interface to the bot is the local HTML/JS
# client in WebUI/ (served + driven by webui_server.py) instead of Discord.
# Multiple real accounts now use this (see WEBUI auth config below) -- the
# bot-reply channel still shares one history+memory across everyone in it
# (core/ai.py, core/memory.py unchanged), same as a real Discord channel
# does; core/chat_store.py is what makes the *messages themselves* actually
# shared across viewers, which core.ai.histories alone never did (that's
# just the bot's own short-term context, never rendered to anyone).
WEBUI_HOST      = os.environ.get("WEBUI_HOST", "127.0.0.1")
WEBUI_PORT      = int(os.environ.get("WEBUI_PORT", "9021"))
WEBUI_DIR       = "./WebUI"  # relative to cwd (app/), like the other *_FILE paths above
# Serves the whole app under this URL prefix (e.g. "/lapplandchat") instead
# of the domain root -- for a reverse proxy (see webui_server.py's p())
# that forwards a whole domain to this server rather than carving out a
# specific location block for it. Empty (the default) serves at "/", same
# as before this existed. Every route is registered through p(path), and
# every frontend fetch()/href/redirect uses a path *without* a leading "/"
# (e.g. "api/chat", not "/api/chat") so the browser resolves it relative to
# whatever page it's on instead of the domain root -- that's what actually
# makes the prefix work end to end, not just the route registration side.
# Must not have a trailing slash (WEBUI_BASE_PATH + "/x", not "//x").
WEBUI_BASE_PATH = os.environ.get("WEBUI_BASE_PATH", "").rstrip("/")

# Hidden "gate" in front of the WebUI's own root page (see webui_server.py's
# GET p("/") and POST p("/gate"), WebUI/cover.html). By default GET p("/")
# serves a benign static placeholder instead of the real chat shell; typing
# WEBUI_GATE_PASSPHRASE into the hidden "~"-triggered terminal on that page
# sets a signed cookie that flips root over to actually serving index.html.
#
# This is obscurity, not authentication -- core/auth.py's real login is
# still what actually gates chat data and every /api/* route either way;
# this only decides which static HTML a bare GET / happens to return, so
# the app's existence isn't obvious to a casual visitor or scanner hitting
# the domain. Don't treat WEBUI_GATE_SECRET or the passphrase as a real
# security boundary.
WEBUI_GATE_PASSPHRASE = os.environ.get("WEBUI_GATE_PASSPHRASE", "chat")
WEBUI_GATE_COOKIE     = "gate"
# Signs the gate cookie so it can't just be forged/guessed -- generated
# fresh each process start if WEBUI_GATE_SECRET isn't set via env, which is
# safe (no hardcoded default to leak) but means restarting the process logs
# everyone's gate cookie out. Set WEBUI_GATE_SECRET yourself for a stable
# secret that survives restarts.
WEBUI_GATE_SECRET = os.environ.get("WEBUI_GATE_SECRET") or secrets.token_hex(32)
# A hostname that skips the gate entirely and always serves the real chat
# shell -- e.g. "lappland.arkendpoint.dev", a subdomain nobody stumbles onto
# by accident, as a direct bypass alongside the "type the passphrase" route
# through whatever public/root domain also reaches this same server. Empty
# (the default) means every hostname is gated the same way.
WEBUI_GATE_BYPASS_HOST = os.environ.get("WEBUI_GATE_BYPASS_HOST", "").lower()
# Where "Return to homepage" (index.html's header) sends you -- an absolute
# URL, since a relative "./" only ever lands back on whatever host you're
# currently on. That's a no-op on WEBUI_GATE_BYPASS_HOST specifically: that
# host always serves the real chat shell regardless of the gate cookie (see
# is_valid()'s bypass check in webui_server.py's index()), so clearing the
# cookie there and reloading the same host just shows chat again -- there's
# no cover page to return to on that host by design. Point this at the
# actual gated domain instead, e.g. "https://arkendpoint.dev/". Empty (the
# default) falls back to relative "./", fine for a single-host deployment.
WEBUI_PUBLIC_HOMEPAGE = os.environ.get("WEBUI_PUBLIC_HOMEPAGE", "")

# Negative sentinel so it can never collide with a real Discord channel
# snowflake (always positive) -- every web session shares one conversation,
# keyed into the same core.ai.histories dict Discord uses.
WEBUI_CHANNEL_ID = -1
# The web UI's channels (see WebUI/index.html's own CHANNELS list, which
# must stay in sync with this -- there's no single source of truth shared
# between the JS and Python sides here, same pragmatic tradeoff as the rest
# of this file's *_FILE path conventions). Only WEBUI_BOT_CHANNEL gets bot
# replies; every channel's messages are shared/persisted the same way.
WEBUI_CHANNELS     = ("general", "bug-reports", "feature-suggestions", "off-topic")
WEBUI_BOT_CHANNEL  = "general"

# Discord webhook notification -- when someone posts in one of these WebUI
# channels, the message is relayed into Discord via a channel webhook (see
# core/discord_notify.py) so BOT_OWNER_ID sees it without watching the WebUI.
# A webhook is used instead of going through the actual Discord bot because
# this deployment runs independently of the Discord bot process (see the
# load_dotenv comment above) -- a webhook is just an HTTP POST, no bot
# process required on this end. Add DISCORD_NOTIFY_WEBHOOK_URL to
# .env-webui yourself (a webhook URL from the target Discord channel's
# Integrations settings); leaving it unset just disables notifications.
DISCORD_NOTIFY_WEBHOOK_URL = os.environ.get("DISCORD_NOTIFY_WEBHOOK_URL")
WEBUI_NOTIFY_CHANNELS      = ("bug-reports", "feature-suggestions")

# WebUI auth config -- SQLite-backed accounts (see core/auth.py) behind
# WebUI/login.html (Nocturne design), and shared chat storage (see
# core/chat_store.py) in the same file. WEBUI_COOKIE_SECURE defaults to
# unset/false for local dev (plain http on WEBUI_HOST=127.0.0.1, where a
# Secure cookie would make the browser refuse to send it back at all) --
# set WEBUI_COOKIE_SECURE=true when this sits behind a reverse proxy that
# terminates HTTPS (the browser only ever sees https:// in that case, so
# Secure is correct and expected there).
WEBUI_DB_FILE                 = "data/webui.db"
WEBUI_COOKIE_SECURE           = os.environ.get("WEBUI_COOKIE_SECURE", "false").lower() == "true"
AUTH_SESSION_MAX_AGE_SECONDS  = 30 * 24 * 60 * 60  # 30 days
AUTH_MIN_PASSWORD_LENGTH      = 8

# The admin account -- registering with this exact username-or-email plus
# this exact password (see core/auth.py's create_user()) gets ADMIN_USER_ID
# instead of a random uuid4, marking that one account as admin
# (core/auth.py's is_admin()). Real credentials, so these
# are read from the environment like every other secret in this file, never
# hardcoded here -- add ADMIN_USERNAME / ADMIN_EMAIL / ADMIN_PASSWORD to
# .env yourself to provision one; leaving any of the three unset disables
# admin signup entirely (no account can ever match). Admin-gated actions
# (channel wipe/reset -- see webui_server.py's /api/reset) 403 for anyone
# whose user_id isn't this.
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME")
ADMIN_EMAIL    = os.environ.get("ADMIN_EMAIL")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
ADMIN_USER_ID  = "1337"

# Static invite token required to create a new account (checked in
# webui_server.py's /api/register against whatever login.html's "Invite
# code" field submits) -- a simple gate against public/bot signups, on top
# of password auth. Empty (unset) disables the check entirely, same
# "leaving it unset disables the feature" convention as ADMIN_* above.
WEBUI_REGISTRATION_TOKEN = os.environ.get("WEBUI_REGISTRATION_TOKEN", "")

# Downloader config
MAX_FILE_SIZE_MB      = 25
NORMALIZE_AUDIO       = False

# GPU worker config -- offloads ffmpeg encodes to a NVENC-capable machine
# (e.g. a laptop) over HTTP; falls back to local CPU encoding when unset,
# unreachable, or the job fails. See gpu_worker/README.md for the server side.
GPU_WORKER_URL           = os.environ.get("GPU_WORKER_URL", "")
GPU_WORKER_API_KEY       = os.environ.get("GPU_WORKER_API_KEY")
GPU_WORKER_CONNECT_TIMEOUT = 3    # seconds -- fail fast when the laptop is off
GPU_WORKER_RETRY_COOLDOWN  = 120  # seconds to skip the worker after it's found unreachable

# Image generation config -- self-hosted "Filigree-Anima" checkpoint (a
# Cosmos-Predict2-2B finetune with a swapped-in Qwen3 text encoder), run via
# an actual ComfyUI instance inside a Modal container (see
# modal_imagegen/app.py + README.md). All three are local files (personal
# downloads, not a vendor model), so there's no API token, just paths --
# modal_imagegen/app.py reads them (relative to app/) to know what to bake
# into the deployed container. MODAL_APP_NAME/MODAL_CLS_NAME are shared with
# core/imagegen.py so the caller and the deployed app agree on what they're
# both named.
#
# Sampler/scheduler/resolution/cfg aren't duplicated here -- they live in
# modal_imagegen/workflow_template.json (the actual exported ComfyUI
# workflow), which is the real source of truth for those and would drift out
# of sync with a second hardcoded copy.
ANIMA_MODEL_PATH = os.environ.get("ANIMA_MODEL_PATH", "models/anima/Filigree-Anima-v4.0.safetensors")
# "web-ui-" prefixed on this branch so its deployed Modal app is a separate
# container from the Discord bot's "anima-imagegen" (main branch) -- deploying
# from here never overwrites/collides with that one, and `modal app list`
# makes it obvious which is which.
MODAL_APP_NAME    = "web-ui-anima-imagegen"
MODAL_CLS_NAME    = "AnimaImageGen"

QWEN_TEXT_ENCODER_PATH = os.environ.get("QWEN_TEXT_ENCODER_PATH", "models/qwen_image/qwen_3_06b_base.safetensors")
QWEN_VAE_PATH           = os.environ.get("QWEN_VAE_PATH", "models/qwen_image/qwen_image_vae.safetensors")

# Second image-gen backend, alongside Anima -- black-forest-labs/FLUX.1-schnell,
# also run via a real ComfyUI instance inside a Modal container (see
# modal_flux/app.py + README.md). Unlike Anima (a personal checkpoint that
# lives on local disk and gets baked into the image at deploy time), FLUX's
# weights are a public HF Hub download fetched straight into a modal.Volume
# (see modal_flux/app.py's download_weights()) -- there's no local file path
# to configure here, just the app/class names core/imagegen.py needs to find
# the deployed container by, same as MODAL_APP_NAME/MODAL_CLS_NAME above.
FLUX_MODAL_APP_NAME = "web-ui-flux-schnell-imagegen"
FLUX_MODAL_CLS_NAME = "FluxImageGen"

# core/imagegen.py's registry of image-gen backends: key -> (Modal app name,
# Modal class name). "anima" is the default (see DEFAULT_BACKEND in
# core/imagegen.py) -- /imagine_anime and /imagine on Discord, and
# /imagine_anime and /imagine in the web UI, select between these by key.
IMAGEGEN_BACKENDS = {
    "anima": (MODAL_APP_NAME, MODAL_CLS_NAME),
    "flux":  (FLUX_MODAL_APP_NAME, FLUX_MODAL_CLS_NAME),
}


SYSTEM_PROMPT = f"""you are {BOT_NAME}. you're chatting with someone in a chat app. be normal. short replies unless the question needs detail. no asterisks. don't mention being an AI. different people talk in the same conversation - pay attention to who said what and treat each person's messages in context of what THEY said, not the whole conversation. Do not be so formal, talk casually. You may use short terms such as lmao, lol, bruh, etc. Make sure it fits the tone of the conversation.

Your current mood is: {{mood}}

You have the following notes about the people in this server:
{{user_memories}}

Use these notes naturally — don't announce that you remember something, just let it inform how you talk to them."""