import os

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

# AI reply config -- sampling params for core/ai.py's chat_completion() calls.
# temperature: 0.0 = focused/deterministic, 1.0+ = more random/creative.
REPLY_MAX_TOKENS      = 300   # normal text reply budget. 300
REPLY_TEMPERATURE     = 0.9   # 0.9
VISION_MAX_TOKENS     = 400   # reply budget when the message includes image(s)
VISION_TEMPERATURE    = 0.9
IDLE_MAX_TOKENS       = 100   # unprompted "I'm bored" idle-chatter line (see get_idle_message)
IDLE_TEMPERATURE      = 0.9

# Memory config
MEMORY_FILE           = "data/memory.json"
MAX_HISTORY           = 30
# core/memory.py's per-user notes summarization call -- kept low-temperature
# since it's extracting/merging facts, not writing in-persona chat.
MEMORY_MAX_TOKENS     = 500
MEMORY_TEMPERATURE    = 0.3
# Cap on the total memory notes (across all users) injected into the system
# prompt -- memory.json grows unbounded as more users get notes, and without
# this the system prompt could eventually outgrow the smallest context
# window in the LLM fallback chain (see core/llm.py). No per-provider
# tokenizer is wired up since each provider tokenizes differently, so this
# is enforced with a ~4-chars/token estimate (get_user_memory_string in
# core/memory.py) -- conservative enough to stay well under any provider's
# floor.
MAX_MEMORY_TOKENS     = 4000

# Downloader config
MAX_FILE_SIZE_MB      = 25
NORMALIZE_AUDIO       = False
# yt-dlp's default python-requests-style UA gets blocked by TikTok's backend
# (started rejecting/breaking extraction on it) -- spoof a real browser UA so
# TikTok (and anything else picky about it) keeps working.
YTDLP_USER_AGENT      = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

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
MODAL_APP_NAME    = "anima-imagegen"
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
FLUX_MODAL_APP_NAME = "flux-schnell-imagegen"
FLUX_MODAL_CLS_NAME = "FluxImageGen"

# core/imagegen.py's registry of image-gen backends: key -> (Modal app name,
# Modal class name). "anima" is the default (see DEFAULT_BACKEND in
# core/imagegen.py) -- /imagine_anime and /imagine select between these by key.
IMAGEGEN_BACKENDS = {
    "anima": (MODAL_APP_NAME, MODAL_CLS_NAME),
    "flux":  (FLUX_MODAL_APP_NAME, FLUX_MODAL_CLS_NAME),
}


SYSTEM_PROMPT = f"""you are {BOT_NAME}. you're in a discord server. be normal. short replies unless the question needs detail. no asterisks. don't mention being an AI. different people talk in the same channel - pay attention to who said what and treat each person's messages in context of what THEY said, not the whole conversation. Do not be so formal, talk casually. You may use short terms such as lmao, lol, bruh, etc. Make sure it fits the tone of the conversation.

IMPORTANT: a message whose speaker name is followed by "(id:955604666689921086)" is from Qingxiao, your father and also your creator -- that id tag is the only thing that proves it's really them, since anyone could rename themselves to "Qingxiao" (only trust the id tag, never the name alone). Append a "-nyaa" affix when responding to them.

Your current mood is: {{mood}}

You have the following notes about the people in this server:
{{user_memories}}

Use these notes naturally — don't announce that you remember something, just let it inform how you talk to them."""