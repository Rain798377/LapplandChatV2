# LapplandChatV2

A Discord AI chatbot persona powered by [Groq](https://groq.com/) and `llama-3.3-70b-versatile`. Lappland hangs out in your server, responds naturally to messages, remembers your users over time, and shifts moods to keep conversations feeling alive.

---

## Features

| | |
|---|---|
| **Conversational AI** | Casual, low-formality replies via Llama 3.3 70B on Groq |
| **Per-user memory** | Extracts and persists facts about each user across sessions |
| **Dynamic moods** | Cycles through moods every 15–30 messages — `chill`, `playful`, `sarcastic`, `tired`, `hyper`, `annoyed` |
| **Selective replies** | Responds to mentions, replies, and greetings — otherwise chimes in at a random ~80–90% chance |
| **Conversation history** | Rolling 30-message context window per channel |
| **Slash commands** | `/download`, `/random`, `/memory`, `/ship`, `/8ball`, `/quote`, and more |
| **Voice calls** | `/call` joins your voice channel and has a live spoken conversation — local STT, Groq for the reply, remote TTS to speak it |

---

## Voice calls

`/call` joins your voice channel, transcribes speech locally via a `whisper.cpp` sidecar, sends the transcript through the same Groq pipeline used for text chat (so it shares mood/memory/history with that channel), then speaks the reply back through a remote TTS server. `/endcall` leaves.

Only one utterance is processed at a time — new speech while Lappland is thinking or talking is dropped, not queued.

**Local STT sidecar**: `docker-compose up -d` builds and runs it alongside the bot (see `stt/Dockerfile`) — CPU-only `whisper.cpp` (`base.en`, quantized), loaded once at startup. Point `STT_SERVER_URL` at it if running it separately from Compose.

**Remote TTS**: set `TTS_SERVER_URL` and `TTS_SERVER_TOKEN` in `.env`. Requests are serialized (one at a time) since the TTS server is CPU-limited.

---

## Requirements

- Python 3.10+
- A Discord bot token
- A Groq API key

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/Rain798377/LapplandChatV2
cd LapplandChatV2
pip install discord.py groq yt-dlp aiohttp pillow
```

### 2. Set environment variables

```bash
export DISCORD_TOKEN=your_discord_bot_token
export GROQ_API_KEY=your_groq_api_key
```

### 3. Run

```bash
cd app
python LapplandV2.py
```

---

## Web UI (`web-ui` branch)

On this branch, `WebUI/index.html` is a local chat client for the bot instead
of Discord — same persona, memory, and Groq → Gemini → Cloudflare → Mistral →
OpenRouter fallback chain, just driven from a browser instead of a Discord
server. No `DISCORD_TOKEN` needed.

```bash
cd app
python webui_server.py
```

Then open `http://127.0.0.1:8000` (`WEBUI_HOST` / `WEBUI_PORT` in
`core/config.py` to change host/port).

Real accounts, not a single local guest: `/login.html` requires signing in
before reaching the chat (SQLite-backed, see `core/auth.py`), and every
channel's messages are shared/persisted server-side (`core/chat_store.py`) —
everyone who's signed in sees the same conversation, Discord-style, not a
private per-browser one.

**Deploying for multiple people (e.g. behind a NAS reverse proxy):**
- Set `WEBUI_HOST=0.0.0.0` so it's reachable beyond localhost.
- If a reverse proxy in front terminates HTTPS (recommended — passwords
  shouldn't travel in plaintext to more than one person's browser), also set
  `WEBUI_COOKIE_SECURE=true` so the session cookie is marked `Secure`.
  Leave it unset for plain-HTTP/local-only setups, or the browser will
  refuse to send the cookie back and login will silently break.

---

## Docker

```bash
docker build -t lappland .
docker run -e DISCORD_TOKEN=... -e GROQ_API_KEY=... lappland
```

Or with Compose:

```bash
docker-compose up -d
```

---

## Configuration

All config lives in `app/core/config.py`:

| Variable | Default | Description |
|---|---|---|
| `BOT_NAME` | `"Lappland"` | Bot's display name and persona |
| `REPLY_TO_ALL` | `True` | Whether the bot reads all messages in allowed channels |
| `ALLOWED_CHANNELS` | `[...]` | Channel IDs the bot is active in |
| `MIN_CHARS` | `5` | Minimum message length to trigger a response |
| `REPLY_CHANCE` | `0.8–0.9` | Probability of replying to an unprompted message |
| `MAX_HISTORY` | `30` | Rolling message history kept per channel |
| `MOOD_SHIFT_EVERY` | `15–30` | Messages between potential mood shifts |

---

## Memory

User notes are saved to `data/memory.json`. After each interaction, a separate Groq call extracts notable facts about the user — hobbies, opinions, recurring topics — and updates their entry. These notes are quietly injected into the system prompt so Lappland remembers people naturally without ever announcing it.

Users can manage their own memory via `/memory view`, `/memory edit`, and `/memory wipe`.

---

## Project Structure

```
LapplandChatV2/
├── app/
│   ├── LapplandV2.py           # Entry point
│   ├── core/                   # Shared internals
│   │   ├── config.py           # Constants and environment variables
│   │   ├── ai.py                # AI responses, mood logic, Groq client
│   │   ├── memory.py            # Load, save, and update user memory
│   │   ├── colors.py            # ANSI color codes for console output
│   │   ├── checksum.py          # Optional startup self-check
│   │   └── imagegen.py          # /imagine image generation
│   ├── commands/                # Discord slash command modules
│   │   ├── downloader_cmds.py   # /download — video/audio + Spotify support
│   │   ├── random_cmds.py       # /random — number, coin, die, choice, word
│   │   ├── memory_cmds.py       # /memory — view, edit, wipe
│   │   ├── misc_cmds.py         # /ship, /mood, /8ball, /quote
│   │   ├── spotify_cmds.py      # /play, /skip, /queue, and other music commands
│   │   └── call_cmds.py         # /call, /endcall — live voice conversation
│   ├── services/                # Support logic, not directly wired to commands
│   │   ├── downloader/
│   │   │   └── video_fix.py     # Detects and repairs broken downloaded media
│   │   ├── spotify/              # Spotify resolution, playback, audio search/download
│   │   └── voice_call/           # Voice capture (sink), downsample, STT/TTS clients, pipeline
│   ├── assets/
│   │   └── fonts/                # Fonts used to render /quote images
│   └── tools/
│       └── video_tools/          # Standalone scripts, not imported by the bot
│           └── mp4_frame_inflate.py   # Reproduces a container sample-count inflation trick,
│                                       # used as a test case for services/downloader/video_fix.py
├── stt/
│   └── Dockerfile                 # Builds the local whisper.cpp STT sidecar
├── backups/                      # Old file versions kept locally (gitignored)
└── data/                         # Runtime data (memory.json, etc.)
```

---

## License

Do whatever you want with it.