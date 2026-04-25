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
python LapplandV2.py
```

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

All config lives in `config.py`:

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
├── LapplandV2.py        # Entry point
├── config.py            # Constants and environment variables
├── ai.py                # AI responses, mood logic, Groq client
├── memory.py            # Load, save, and update user memory
└── commands/
    ├── download.py      # /download — video/audio + Spotify support
    ├── random_cmds.py   # /random — number, coin, die, choice, word
    ├── memory_cmds.py   # /memory — view, edit, wipe
    └── misc_cmds.py     # /ship, /mood, /8ball, /quote
```

---

## License

Do whatever you want with it.