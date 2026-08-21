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
    GET  /login.html     -- sign-in page (WebUI/login.html, Nocturne design
                             system -- see WebUI/nocturne.css)
    GET  /nocturne.css   -- login.html's/index.html's stylesheet
    GET  /images/{file}  -- a generated image (core.imagegen.IMAGES_DIR)
    GET  /api/info       -- bot name, model, mood, provider chain, last
                             provider that actually served a reply
    POST /api/chat        -- {"message", "username", "user_id"} -> {"reply", "mood", "provider", "isCommand", "image"}
    POST /api/reset       -- clears the local conversation history
    POST /api/register    -- {"identifier"?, "username"?, "email"?, "password"} -> {"pending": true, "user_id"}
    POST /api/login        -- same body shape -> {"user_id", "username"}; sets an
                             HttpOnly `session` cookie (see core/auth.py)
    POST /api/logout       -- clears the current session
    GET  /api/me          -- the logged-in user (401 if no/invalid session) --
                             index.html's own JS calls this on load and
                             redirects to /login.html on 401; nothing here
                             enforces it server-side beyond that one check,
                             so hitting /api/chat directly without a session
                             still works (this is a local personal tool, not
                             a multi-tenant one -- see core/auth.py)

A message starting with "/" is dispatched to webui_commands.COMMANDS (text
equivalents of the bot's Discord slash commands -- see that module's
docstring for what's ported and, more importantly, what's deliberately not)
instead of going through get_ai_response(); like Discord's slash commands,
it never touches conversation history or memory extraction. A handler
normally returns a plain str; /imagine returns {"text", "image"} instead
(see webui_commands.py) -- "image" on the /api/chat response is that path,
relative ("/images/<file>"), or null.

Run with `python webui_server.py` from the app/ directory (same as
LapplandV2.py). Config: WEBUI_HOST / WEBUI_PORT / WEBUI_DIR / AUTH_* in
core/config.py.
"""

import asyncio
import os
import random

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core import ai, auth
from core.ai import get_ai_response, histories
from core.config import (
    AUTH_MIN_PASSWORD_LENGTH, AUTH_SESSION_MAX_AGE_SECONDS,
    BOT_NAME, MODEL, WEBUI_CHANNEL_ID, WEBUI_DIR, WEBUI_HOST, WEBUI_PORT,
)
from core.colors import *
from core.imagegen import IMAGES_DIR
from core.llm import DEFAULT_PROVIDER_CHAIN, describe_error
import core.llm as llm
from core.memory import load_memory, update_memory_from_conversation
from webui_commands import COMMANDS

app = FastAPI()
auth.init_db()


class ChatRequest(BaseModel):
    message: str
    username: str = "guest"
    user_id: str = "webui-guest"


class AuthRequest(BaseModel):
    # login.html sends all three every time (whichever of username/email
    # apply to what was typed in its one "identifier" field) -- identifier is
    # what /api/login looks a user up by; username/email are only meaningful
    # to /api/register, which needs to know which one you actually gave it.
    identifier: str
    username: str | None = None
    email: str | None = None
    password: str


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
def serve_image(filename: str):
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

    if message.startswith("/"):
        name, _, raw_args = message[1:].partition(" ")
        handler = COMMANDS.get(name.lower())
        image = None
        if handler is None:
            reply = f"unknown command /{name}. try /help."
        else:
            try:
                # Same reasoning as get_ai_response below -- some handlers
                # (curl, ip, random word, imagine) make a blocking call.
                result = await asyncio.to_thread(handler, raw_args.split(), raw_args, req)
            except Exception as e:
                print(f"{RED}[webui] command /{name} failed: {e}{RESET}", flush=True)
                result = f"that command hit an error: {e}"
            if isinstance(result, dict):
                reply, image = result.get("text", ""), result.get("image")
            else:
                reply = result
        return {"reply": reply, "mood": ai.current_mood, "provider": None, "isCommand": True, "image": image}

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

    return {"reply": reply, "mood": ai.current_mood, "provider": llm.last_provider_used, "isCommand": False, "image": None}


@app.post("/api/reset")
def reset():
    histories.pop(WEBUI_CHANNEL_ID, None)
    return {"status": "ok"}


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
        httponly=True, samesite="lax", secure=False,  # see core/config.py's AUTH_* comment for why secure=False
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
