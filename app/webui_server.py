"""
webui_server.py -- local HTTP server for the WebUI/ chat client.

This branch's interface to the bot is this page instead of Discord. It
serves the browser frontend (WebUI/index.html + support.js) and exposes a
small JSON API that drives it through the exact same LLM fallback chain and
memory system the Discord bot uses (core/ai.py, core/memory.py) -- reused,
not duplicated, so persona/mood/memory behave identically either way.

Endpoints:
    GET  /              -- the chat client (WebUI/index.html)
    GET  /support.js     -- the client's runtime (WebUI/support.js)
    GET  /api/info       -- bot name, model, mood, provider chain, last
                             provider that actually served a reply
    POST /api/chat        -- {"message", "username", "user_id"} -> {"reply", "mood", "provider"}
    POST /api/reset       -- clears the local conversation history

Run with `python webui_server.py` from the app/ directory (same as
LapplandV2.py). Config: WEBUI_HOST / WEBUI_PORT / WEBUI_DIR in core/config.py.
"""

import asyncio
import random

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core import ai
from core.ai import get_ai_response, histories
from core.config import BOT_NAME, MODEL, WEBUI_CHANNEL_ID, WEBUI_DIR, WEBUI_HOST, WEBUI_PORT
from core.colors import *
from core.llm import DEFAULT_PROVIDER_CHAIN, describe_error
import core.llm as llm
from core.memory import load_memory, update_memory_from_conversation

app = FastAPI()


class ChatRequest(BaseModel):
    message: str
    username: str = "guest"
    user_id: str = "webui-guest"


@app.get("/")
def index():
    return FileResponse(f"{WEBUI_DIR}/index.html")


@app.get("/support.js")
def support_js():
    return FileResponse(f"{WEBUI_DIR}/support.js", media_type="application/javascript")


@app.get("/api/info")
def info():
    return {
        "botName": BOT_NAME,
        "model": MODEL,
        "mood": ai.current_mood,
        "providers": list(DEFAULT_PROVIDER_CHAIN),
        "lastProvider": llm.last_provider_used,
    }


@app.post("/api/chat")
async def chat(req: ChatRequest):
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    memory = load_memory()
    try:
        # get_ai_response() is a plain blocking function (synchronous
        # Groq/Gemini/etc. HTTP calls) -- run it off-thread same as
        # LapplandV2.py's on_message does, so a slow generation doesn't stall
        # the server for other requests.
        reply = await asyncio.to_thread(
            get_ai_response, WEBUI_CHANNEL_ID, message, req.username, memory
        )
    except Exception as e:
        print(f"{RED}[webui] {e}{RESET}", flush=True)
        raise HTTPException(status_code=502, detail=describe_error(e))

    if len(message.split()) > 5 and random.random() < 0.75:
        await asyncio.to_thread(
            update_memory_from_conversation,
            WEBUI_CHANNEL_ID, req.user_id, req.username, memory, histories,
        )

    return {"reply": reply, "mood": ai.current_mood, "provider": llm.last_provider_used}


@app.post("/api/reset")
def reset():
    histories.pop(WEBUI_CHANNEL_ID, None)
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    print(f"{LIGHT_GREEN}[webui] serving {WEBUI_DIR} on http://{WEBUI_HOST}:{WEBUI_PORT}{RESET}", flush=True)
    uvicorn.run(app, host=WEBUI_HOST, port=WEBUI_PORT)
