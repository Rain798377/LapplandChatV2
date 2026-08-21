"""
webui_server.py -- local HTTP server for the WebUI/ chat client.

This branch's interface to the bot is this page instead of Discord. It
serves the browser frontend (WebUI/index.html + support.js) and exposes a
small JSON API that drives it through the exact same LLM fallback chain and
memory system the Discord bot uses (core/ai.py, core/memory.py) -- reused,
not duplicated, so persona/mood/memory behave identically either way.

Multiple real accounts use this now (core/auth.py), and every channel's
messages are persisted and shared across everyone viewing it (core/chat_store.py)
-- not just the bot-reply channel's short-term memory, which was already
shared server-side but never actually rendered to more than one browser.
Identity for chat/memory purposes always comes from the authenticated
session, never from the client's request body -- see chat() below.

Endpoints:
    GET  /              -- the chat client (WebUI/index.html)
    GET  /support.js     -- the client's runtime (WebUI/support.js)
    GET  /login.html     -- sign-in page (WebUI/login.html, Nocturne design
                             system -- see WebUI/nocturne.css)
    GET  /nocturne.css   -- login.html's/index.html's stylesheet
    GET  /images/{file}  -- a generated image (core.imagegen.IMAGES_DIR) -- requires a session
    GET  /api/info       -- bot name, model, mood, provider chain, last
                             provider that actually served a reply -- requires a session
    GET  /api/messages/{channel}?since=<id> -- that channel's messages (all,
                             or only those after `since` for polling) -- requires a session
    POST /api/chat        -- {"message", "channel"} -> {"message", "reply", "mood",
                             "provider", "isCommand"} -- requires a session; username/
                             user_id always come from the session, any client-supplied
                             value in the body is ignored
    POST /api/reset       -- {"channel"} -> clears that channel's persisted
                             messages (and, for the bot channel, its LLM
                             history+re-seeds a greeting) -- requires a session
    POST /api/register    -- {"identifier"?, "username"?, "email"?, "password"} -> {"pending": true, "user_id"}
    POST /api/login        -- same body shape -> {"user_id", "username"}; sets an
                             HttpOnly `session` cookie (see core/auth.py)
    POST /api/logout       -- clears the current session
    GET  /api/me          -- the logged-in user (401 if no/invalid session) --
                             index.html's own JS calls this on load and
                             redirects to /login.html on 401

A message starting with "/" is dispatched to webui_commands.COMMANDS (text
equivalents of the bot's Discord slash commands -- see that module's
docstring for what's ported and, more importantly, what's deliberately not)
instead of going through get_ai_response(); like Discord's slash commands,
it never touches conversation history or memory extraction. A handler
normally returns a plain str; /imagine returns {"text", "image"} instead
(see webui_commands.py).

Run with `python webui_server.py` from the app/ directory (same as
LapplandV2.py). Config: WEBUI_HOST / WEBUI_PORT / WEBUI_DIR / WEBUI_CHANNELS /
WEBUI_BOT_CHANNEL / WEBUI_COOKIE_SECURE / AUTH_* in core/config.py.
"""

import asyncio
import os
import random
import time

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core import ai, auth, chat_store
from core.ai import get_ai_response, histories
from core.config import (
    AUTH_MIN_PASSWORD_LENGTH, AUTH_SESSION_MAX_AGE_SECONDS,
    BOT_NAME, MODEL, WEBUI_BOT_CHANNEL, WEBUI_CHANNEL_ID, WEBUI_CHANNELS,
    WEBUI_COOKIE_SECURE, WEBUI_DIR, WEBUI_HOST, WEBUI_PORT,
)
from core.colors import *
from core.imagegen import IMAGES_DIR
from core.llm import DEFAULT_PROVIDER_CHAIN, describe_error
import core.llm as llm
from core.memory import load_memory, update_memory_from_conversation
from webui_commands import COMMANDS

app = FastAPI()
auth.init_db()
chat_store.init_db()


class ChatRequest(BaseModel):
    message: str
    channel: str = WEBUI_BOT_CHANNEL
    # Present so webui_commands.py's handlers (e.g. cmd_memory, which reads
    # req.user_id/req.username) don't need to change -- but a client-supplied
    # value here is never trusted. chat() overwrites both from the
    # authenticated session before req is used for anything.
    username: str = "guest"
    user_id: str = "webui-guest"


class ResetRequest(BaseModel):
    channel: str = WEBUI_BOT_CHANNEL


class AuthRequest(BaseModel):
    # login.html sends all three every time (whichever of username/email
    # apply to what was typed in its one "identifier" field) -- identifier is
    # what /api/login looks a user up by; username/email are only meaningful
    # to /api/register, which needs to know which one you actually gave it.
    identifier: str
    username: str | None = None
    email: str | None = None
    password: str


async def require_user(request: Request):
    token = request.cookies.get("session")
    user = await asyncio.to_thread(auth.get_user_by_session, token) if token else None
    if not user:
        raise HTTPException(status_code=401, detail="not signed in")
    return user


@app.get("/")
def index():
    return FileResponse(f"{WEBUI_DIR}/index.html")


@app.get("/support.js")
def support_js():
    return FileResponse(f"{WEBUI_DIR}/support.js", media_type="application/javascript")


@app.get("/login.html")
def login():
    return FileResponse(f"{WEBUI_DIR}/login.html")


@app.get("/nocturne.css")
def nocturne_css():
    return FileResponse(f"{WEBUI_DIR}/nocturne.css", media_type="text/css")


@app.get("/images/{filename}")
def serve_image(filename: str, user=Depends(require_user)):
    # os.path.basename strips any directory component a caller tries to
    # smuggle in (e.g. "../../.env") -- reject outright instead of silently
    # "correcting" it, so a path-traversal attempt 404s rather than maybe
    # resolving somewhere unexpected.
    if filename != os.path.basename(filename):
        raise HTTPException(status_code=400, detail="invalid filename")
    path = os.path.join(IMAGES_DIR, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="image/png")


@app.get("/api/info")
def info(user=Depends(require_user)):
    return {
        "botName": BOT_NAME,
        "model": MODEL,
        "mood": ai.current_mood,
        "providers": list(DEFAULT_PROVIDER_CHAIN),
        "lastProvider": llm.last_provider_used,
    }


@app.get("/api/messages/{channel}")
async def get_messages(channel: str, since: int = 0, user=Depends(require_user)):
    if channel not in WEBUI_CHANNELS:
        raise HTTPException(status_code=404, detail="unknown channel")
    rows = await asyncio.to_thread(chat_store.get_messages, channel, since)
    return {"messages": [chat_store.serialize(r) for r in rows]}


@app.post("/api/chat")
async def chat(req: ChatRequest, user=Depends(require_user)):
    channel = req.channel if req.channel in WEBUI_CHANNELS else WEBUI_BOT_CHANNEL
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    # Identity always comes from the session, never the request body -- with
    # a real multi-user deployment, trusting a client-supplied username/user_id
    # would let anyone impersonate anyone else in the bot's memory and in the
    # shared channel history.
    req.username = user["username"]
    req.user_id = user["user_id"]

    user_msg = await asyncio.to_thread(
        chat_store.add_message, channel, False, req.username, message, req.user_id
    )

    if channel != WEBUI_BOT_CHANNEL:
        # Shared note-taking channel -- persisted for everyone, no bot involved.
        return {
            "message": chat_store.serialize(user_msg), "reply": None,
            "mood": ai.current_mood, "provider": None, "isCommand": False,
        }

    started_at = time.monotonic()

    if message.startswith("/"):
        name, _, raw_args = message[1:].partition(" ")
        handler = COMMANDS.get(name.lower())
        image = None
        if handler is None:
            reply_text = f"unknown command /{name}. try /help."
        else:
            try:
                # Same reasoning as get_ai_response below -- some handlers
                # (curl, ip, random word, imagine) make a blocking call.
                result = await asyncio.to_thread(handler, raw_args.split(), raw_args, req)
            except Exception as e:
                print(f"{RED}[webui] command /{name} failed: {e}{RESET}", flush=True)
                result = f"that command hit an error: {e}"
            if isinstance(result, dict):
                reply_text, image = result.get("text", ""), result.get("image")
            else:
                reply_text = result

        stats = f"{(time.monotonic() - started_at):.1f}s · command"
        bot_msg = await asyncio.to_thread(
            chat_store.add_message, channel, True, BOT_NAME, reply_text, None, image, stats
        )
        return {
            "message": chat_store.serialize(user_msg), "reply": chat_store.serialize(bot_msg),
            "mood": ai.current_mood, "provider": None, "isCommand": True,
        }

    memory = load_memory()
    try:
        # get_ai_response() is a plain blocking function (synchronous
        # Groq/Gemini/etc. HTTP calls) -- run it off-thread same as
        # LapplandV2.py's on_message does, so a slow generation doesn't stall
        # the server for other requests.
        reply_text = await asyncio.to_thread(
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

    elapsed = time.monotonic() - started_at
    tok = round(len(reply_text) / 3.6)
    provider = llm.last_provider_used
    stats = f"{tok} tok · {elapsed:.1f}s" + (f" · {provider}" if provider else "")
    bot_msg = await asyncio.to_thread(
        chat_store.add_message, channel, True, BOT_NAME, reply_text, None, None, stats
    )

    return {
        "message": chat_store.serialize(user_msg), "reply": chat_store.serialize(bot_msg),
        "mood": ai.current_mood, "provider": provider, "isCommand": False,
    }


@app.post("/api/reset")
async def reset(req: ResetRequest, user=Depends(require_user)):
    channel = req.channel if req.channel in WEBUI_CHANNELS else WEBUI_BOT_CHANNEL
    await asyncio.to_thread(chat_store.clear_channel, channel)

    if channel != WEBUI_BOT_CHANNEL:
        return {"status": "ok", "greeting": None}

    histories.pop(WEBUI_CHANNEL_ID, None)
    greeting = await asyncio.to_thread(chat_store.add_message, channel, True, BOT_NAME, "Hey. What's up?")
    return {"status": "ok", "greeting": chat_store.serialize(greeting)}


@app.post("/api/register")
async def register(req: AuthRequest):
    if len(req.password) < AUTH_MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {AUTH_MIN_PASSWORD_LENGTH} characters.")
    try:
        user_id, username = await asyncio.to_thread(auth.create_user, req.username, req.password, req.email)
    except auth.UsernameTakenError:
        raise HTTPException(status_code=409, detail="That username is already taken.")
    except auth.EmailTakenError:
        raise HTTPException(status_code=409, detail="An account with that email already exists.")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    print(f"{LIGHT_GREEN}[auth] registered {username} ({user_id}){RESET}", flush=True)
    # {"pending": true} rather than logging them in immediately -- matches
    # login.html's own onSubmit, which on this response switches to login
    # mode with a "sign in now" notice instead of redirecting.
    return {"pending": True, "user_id": user_id, "username": username}


@app.post("/api/login")
async def login_route(req: AuthRequest, response: Response):
    identifier = req.identifier.strip()
    user = await asyncio.to_thread(auth.get_user_by_identifier, identifier)
    if not user or not await asyncio.to_thread(auth.verify_password, req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Wrong username/email or password.")

    token = await asyncio.to_thread(auth.create_session, user["user_id"])
    response.set_cookie(
        "session", token,
        httponly=True, samesite="lax", secure=WEBUI_COOKIE_SECURE,
        max_age=AUTH_SESSION_MAX_AGE_SECONDS,
    )
    return {"user_id": user["user_id"], "username": user["username"]}


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("session")
    if token:
        await asyncio.to_thread(auth.delete_session, token)
    response.delete_cookie("session")
    return {"status": "ok"}


@app.get("/api/me")
async def me(request: Request):
    token = request.cookies.get("session")
    user = await asyncio.to_thread(auth.get_user_by_session, token) if token else None
    if not user:
        raise HTTPException(status_code=401, detail="not signed in")
    return {"user_id": user["user_id"], "username": user["username"]}


if __name__ == "__main__":
    import uvicorn

    print(f"{LIGHT_GREEN}[webui] serving {WEBUI_DIR} on http://{WEBUI_HOST}:{WEBUI_PORT}{RESET}", flush=True)
    uvicorn.run(app, host=WEBUI_HOST, port=WEBUI_PORT)
