# LapplandChatV2

A Discord AI chatbot persona powered by [Groq](https://groq.com/) and `llama-3.3-70b-versatile`. Lappland hangs out in your server, responds to messages, remembers your users, and shifts moods over time to keep things feeling natural.

---

## Features

- **Conversational AI** — Casual, low-formality replies using Llama 3.3 70B via the Groq API
- **Per-user memory** — Extracts and persists notes about each user across sessions (stored in `data/memory.json`)
- **Dynamic moods** — Randomly cycles through moods (`chill`, `playful`, `sarcastic`, `tired`, `hyper`, `annoyed`) every 15–30 messages
- **Selective replies** — Responds when mentioned, replied to, or greeted; otherwise joins in at a randomized chance (~80–90%) to feel less bot-like
- **Per-channel conversation history** — Maintains a rolling window of the last 30 messages per channel for context

---

## Requirements

- Python 3.10+
- A Discord bot token
- A Groq API key

---

## Setup

### Environment Variables

Set the following before running:

```
DISCORD_TOKEN=your_discord_bot_token
GROQ_API_KEY=your_groq_api_key
```

### Local

```bash
pip install discord.py groq
python LapplandV2.py
```

### Docker

```bash
docker build -t lappland .
docker run -e DISCORD_TOKEN=... -e GROQ_API_KEY=... lappland
```

### Docker Compose

```bash
docker-compose up -d
```

Make sure your `docker-compose.yml` passes in `DISCORD_TOKEN` and `GROQ_API_KEY` as environment variables.

---

## Configuration

At the top of `bot.py` you can tweak:

| Variable | Default | Description |
|---|---|---|
| `BOT_NAME` | `"Lappland"` | The bot's display name and persona |
| `REPLY_TO_ALL` | `True` | Whether the bot reads all messages in allowed channels |
| `ALLOWED_CHANNELS` | `[...]` | List of channel IDs the bot is active in |
| `MIN_CHARS` | `5` | Minimum message length to trigger a response |
| `REPLY_CHANCE` | `0.8–0.9` | Probability of replying to a random message |
| `MAX_HISTORY` | `30` | Rolling message history kept per channel |
| `MOOD_SHIFT_EVERY` | `15–30` | Messages between potential mood shifts |

---

## Memory

User notes are saved to `data/memory.json`. After each interaction, the bot uses a separate Groq call to extract any notable facts about the user (hobbies, opinions, recurring topics) and updates their entry. These notes are injected into the system prompt so Lappland naturally remembers people without announcing it.

---

## License

Do whatever you want with it.