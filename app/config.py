import os
import random

# Model config
MODEL                 = "llama-3.3-70b-versatile"
MOODS                 = ["chill", "playful", "sarcastic", "tired", "hyper", "annoyed"]
GREETINGS             = {"hello", "hi", "hey", "sup", "yo", "hiya", "heya", "howdy", "morning", "evening", "wsp"}

# Tokens
DISCORD_TOKEN         = os.environ.get("DISCORD_TOKEN")
GROQ_API_KEY          = os.environ.get("GROQ_API_KEY")
HF_TOKEN              = os.environ.get("HF_TOKEN")
SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")

# File handling
FILE_SERVER_PATH      = "/srv/downloads"
FILE_SERVER_BASE_URL  = "http://154.27.185.186:9090"
FILE_EXPIRY_SECONDS   = 43200

# Bot config
BOT_NAME              = "Lappland"
REPLY_TO_ALL          = True
ALLOWED_CHANNELS      = [1483716134250217572]
MIN_CHARS             = 5
REPLY_CHANCE          = random.uniform(0.8, 0.9)

# Music config
AUTOPLAY_DELAY        = 5
DEFAULT_VOLUME        = 0.15 # Default volume level is 15% (0.0 to 2.0)

# Memory config
MEMORY_FILE           = "data/memory.json"
MAX_HISTORY           = 30

# Downloader config
MAX_FILE_SIZE_MB      = 25
NORMALIZE_AUDIO       = False


SYSTEM_PROMPT = f"""you are {BOT_NAME}. you're in a discord server. be normal. short replies unless the question needs detail. no asterisks. don't mention being an AI. different people talk in the same channel - pay attention to who said what and treat each person's messages in context of what THEY said, not the whole conversation. Do not be so formal, talk casually. You may use short terms such as lmao, lol, bruh, etc. Make sure it fits the tone of the conversation.

Your current mood is: {{mood}}

You have the following notes about the people in this server:
{{user_memories}}

Use these notes naturally — don't announce that you remember something, just let it inform how you talk to them."""