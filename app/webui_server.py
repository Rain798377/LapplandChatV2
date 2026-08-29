"""
webui_server.py -- local HTTP server for the WebUI/ chat client.

This branch's interface to the bot is this page instead of Discord. It
serves the browser frontend (WebUI/index.html + chat.js, WebUI/login.html +
login.js -- plain vanilla JS/CSS, no build step or framework) and exposes a
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
    GET  /              -- the chat client (WebUI/index.html) if the gate
                             cookie is valid or the request's Host matches
                             WEBUI_GATE_BYPASS_HOST, else the "Arkendpoint"
                             theme page (WebUI/cover.html) with its own
                             hidden "~" terminal -- see core/config.py's
                             WEBUI_GATE_* note and core/gate.py
    GET  /support.js, /arkendpoint.config.js, /_ds/*, /assets/*
                          -- cover.html's own static assets (copied in from
                             a separate design-canvas project -- see
                             cover.html's own comment for why they live here
                             now instead of a separate static-site container)
    POST /gate           -- {"input"} -> 200 + Set-Cookie on a match against
                             WEBUI_GATE_PASSPHRASE (typed into cover.html's
                             hidden "~" terminal), 401 otherwise
    POST /gate/exit       -- clears the gate cookie -- "return to homepage"
                             from inside the chat, back to cover.html
    GET  /chat.css       -- index.html's styles
    GET  /chat.js        -- index.html's behavior
    GET  /login.html     -- sign-in page (WebUI/login.html, Nocturne design
                             system -- see WebUI/nocturne.css)
    GET  /login.css      -- login.html's styles
    GET  /login.js       -- login.html's behavior
    GET  /nocturne.css   -- shared design-system stylesheet
    GET  /cropper.js     -- shared drag/zoom avatar-crop dialog (index.html's
                             own photo, admin.html's bot avatar)
    GET  /admin.html     -- admin panel (WebUI/admin.html) -- admin-only
                             actions are still gated per-request; this just
                             serves the static shell (see admin.js)
    GET  /admin.css      -- admin.html's styles
    GET  /admin.js       -- admin.html's behavior
    GET  /images/{file}  -- a generated image (core.imagegen.IMAGES_DIR) -- requires a session
    GET  /api/info       -- bot name, model, mood, provider chain, last
                             provider that actually served a reply, bot
                             avatar/banner/bio/about -- requires a session
    GET  /api/users      -- every registered account + online status (see
                             core/user_presence.py) -- requires a session
    GET  /api/messages/{channel}?since=<id> -- that channel's messages (all,
                             or only those after `since` for polling) -- requires a session
    POST /api/messages/{id}/edit -- {"body"} -> {"message"} -- the message's own
                             author, or the admin account, may edit; 403 otherwise
    POST /api/messages/{id}/delete -- same author-or-admin gating; 403 otherwise
    POST /api/chat        -- {"message", "channel"} -> {"message", "reply", "mood",
                             "provider", "isCommand"} -- requires a session; username/
                             user_id always come from the session, any client-supplied
                             value in the body is ignored
    POST /api/reset       -- {"channel"} -> clears that channel's persisted
                             messages (and, for the bot channel, its LLM
                             history+re-seeds a greeting) -- admin only (403
                             otherwise; see core/auth.py's is_admin())
    POST /api/register    -- {"identifier"?, "username"?, "email"?, "password", "token"?} ->
                             {"pending": true, "user_id", "isAdmin"} -- isAdmin
                             is true iff this matched the configured
                             ADMIN_USERNAME/ADMIN_EMAIL/ADMIN_PASSWORD in
                             core/config.py, which also assigns ADMIN_USER_ID.
                             "token" must match WEBUI_REGISTRATION_TOKEN (403
                             otherwise) unless that's left unset
                             instead of a random one
    POST /api/login        -- same body shape -> {"user_id", "username"}; sets an
                             HttpOnly `session` cookie (see core/auth.py)
    POST /api/logout       -- clears the current session
    GET  /api/me          -- the logged-in user + their profile (avatar/
                             banner/hue/description) (401 if no/invalid
                             session) -- index.html's own JS calls this on
                             load and redirects to /login.html on 401
    POST /api/profile     -- {"avatar"?, "banner"?, "hue"?, "description"?}
                             -> partial-updates the caller's own profile
                             (see core/auth.py's update_profile) -- these
                             used to be per-browser localStorage, now they
                             follow the account across devices

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
import secrets
import time

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core import ai, auth, chat_store
from core.ai import get_ai_response, histories
from core.bot_profile import load_bot_profile, save_bot_profile
from core.config import (
    ADMIN_USER_ID, AUTH_MIN_PASSWORD_LENGTH, AUTH_SESSION_MAX_AGE_SECONDS,
    BOT_NAME, MODEL, WEBUI_BASE_PATH, WEBUI_BOT_CHANNEL, WEBUI_CHANNEL_ID, WEBUI_CHANNELS,
    WEBUI_COOKIE_SECURE, WEBUI_DIR, WEBUI_HOST, WEBUI_PORT, WEBUI_NOTIFY_CHANNELS,
    WEBUI_GATE_BYPASS_HOST, WEBUI_GATE_COOKIE, WEBUI_GATE_PASSPHRASE, WEBUI_PUBLIC_HOMEPAGE,
    WEBUI_REGISTRATION_TOKEN,
)
from core.gate import is_valid as gate_is_valid, issue_token as gate_issue_token
from core.colors import *
from core.discord_notify import notify as notify_discord
from core.imagegen import IMAGES_DIR, enqueue_generate_image
from core.llm import DEFAULT_PROVIDER_CHAIN, describe_error
import core.llm as llm
from core.memory import load_memory, save_memory, update_memory_from_conversation
from core.user_presence import is_online, touch as touch_presence
from webui_commands import COMMANDS, parse_imagine_args

app = FastAPI()
auth.init_db()
chat_store.init_db()


def p(path: str) -> str:
    """Prefixes a route path with WEBUI_BASE_PATH -- every @app.get/post
    below is registered through this instead of a literal string, so the
    whole app can live under e.g. "/lapplandchat" behind a reverse proxy
    that forwards its entire domain here unfiltered, without a location
    block carved out per-path on the proxy side. A no-op (serves at "/")
    when WEBUI_BASE_PATH is unset, the default. `path` must start with "/"."""
    return WEBUI_BASE_PATH + path


if WEBUI_BASE_PATH:
    # "/lapplandchat" (no trailing slash) -> "/lapplandchat/" -- matters
    # because every frontend fetch()/href below is a *relative* reference
    # (no leading "/"), which the browser resolves against the current
    # page's URL; that only lands under the prefix if the page's own URL
    # ends in "/" (browsers treat a URL with no trailing slash as ending in
    # a "file", and relative references replace that file, dropping
    # whatever came before it -- see core/config.py's WEBUI_BASE_PATH note).
    @app.get(WEBUI_BASE_PATH)
    def base_path_redirect():
        return RedirectResponse(url=p("/"))
    # No redirect from bare "/" here -- unlike WEBUI_BASE_PATH itself, this
    # app doesn't necessarily own the whole domain; the reverse proxy may
    # route "/" to something else entirely (see NPM's Custom Locations),
    # and this app claiming "/" for itself would fight that.


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


class EditMessageRequest(BaseModel):
    body: str


class AdminMoodRequest(BaseModel):
    mood: str


class AdminProviderRequest(BaseModel):
    provider: str  # one of DEFAULT_PROVIDER_CHAIN, or "auto" to unpin


class AdminMemoryWipeRequest(BaseModel):
    user_id: str


class AdminDeleteUserRequest(BaseModel):
    user_id: str


class AdminBotProfileRequest(BaseModel):
    # All optional and independently settable -- None means "leave this
    # field alone" (e.g. changing just the bio shouldn't touch the avatar);
    # an empty string explicitly clears that field (see admin_set_bot_profile).
    avatar: str | None = None
    banner: str | None = None
    bio: str | None = None
    about: str | None = None


class AuthRequest(BaseModel):
    # login.html sends all three every time (whichever of username/email
    # apply to what was typed in its one "identifier" field) -- identifier is
    # what /api/login looks a user up by; username/email are only meaningful
    # to /api/register, which needs to know which one you actually gave it.
    identifier: str
    username: str | None = None
    email: str | None = None
    password: str
    # Invite code, register mode only -- checked against
    # WEBUI_REGISTRATION_TOKEN in /api/register. Unused by /api/login.
    token: str | None = None


async def require_user(request: Request):
    token = request.cookies.get("session")
    user = await asyncio.to_thread(auth.get_user_by_session, token) if token else None
    if not user:
        raise HTTPException(status_code=401, detail="not signed in")
    # Every authenticated request refreshes this account's "last seen" --
    # piggybacks on traffic that already exists (chat.js's polling) rather
    # than needing a dedicated heartbeat endpoint. See core/user_presence.py.
    touch_presence(user["user_id"])
    return user


async def require_admin(user=Depends(require_user)):
    if not auth.is_admin(user):
        raise HTTPException(status_code=403, detail="admin only")
    return user


@app.get(p("/"))
def index(request: Request):
    # See core/config.py's WEBUI_GATE_* note -- serves the real chat shell
    # only with a valid gate cookie or on the configured bypass hostname,
    # a benign placeholder (WebUI/cover.html) otherwise. Obscurity, not
    # auth -- the real login (core/auth.py) still gates chat data either way.
    #
    # no-store is required here, not optional -- this response's content
    # depends on the request's Cookie header, but FileResponse sends no
    # cache-control/Vary of its own, so a browser (or an intermediary) that
    # cached an earlier "/" would keep serving that cached copy after the
    # gate cookie is set OR cleared, since nothing told it this URL's
    # response isn't static. That's exactly what made "Return to homepage"
    # (POST /gate/exit then navigate here) look broken -- the POST worked
    # and the cookie really was cleared, but the browser reused its cached
    # index.html for the very next "/" instead of asking again.
    no_store = {"Cache-Control": "no-store"}
    host = (request.headers.get("host") or "").split(":", 1)[0].lower()
    if (WEBUI_GATE_BYPASS_HOST and host == WEBUI_GATE_BYPASS_HOST) or gate_is_valid(request.cookies.get(WEBUI_GATE_COOKIE)):
        return FileResponse(f"{WEBUI_DIR}/index.html", headers=no_store)
    return FileResponse(f"{WEBUI_DIR}/cover.html", headers=no_store)


class GateRequest(BaseModel):
    input: str


@app.post(p("/gate"))
async def gate(req: GateRequest, response: Response):
    if req.input.strip() != WEBUI_GATE_PASSPHRASE:
        raise HTTPException(status_code=401, detail="denied")
    response.set_cookie(
        WEBUI_GATE_COOKIE, gate_issue_token(),
        httponly=True, samesite="lax", secure=WEBUI_COOKIE_SECURE,
        max_age=10 * 365 * 24 * 60 * 60,  # "persistent" -- 10 years
    )
    return {"status": "ok"}


@app.post(p("/gate/exit"))
async def gate_exit(response: Response):
    """Clears the gate cookie so GET / goes back to showing cover.html --
    the in-chat "return to homepage" action (see index.html's header).
    The gate cookie is HttpOnly, so this has to be a server round-trip;
    frontend JS can't just delete it directly."""
    # httponly/secure explicitly matched to the original set_cookie() call
    # in gate() above -- Response.delete_cookie() defaults both to False,
    # which isn't actually why this didn't work (browsers match cookies for
    # deletion by name/domain/path, not these flags) but there's no reason
    # to leave a pointless mismatch between the two once it's been noticed.
    response.delete_cookie(WEBUI_GATE_COOKIE, httponly=True, samesite="lax", secure=WEBUI_COOKIE_SECURE)
    return {"status": "ok"}


@app.get(p("/login.html"))
def login():
    return FileResponse(f"{WEBUI_DIR}/login.html")


@app.get(p("/admin.html"))
def admin_page():
    # Static file only -- like index()/login() below, this serves the shell
    # unauthenticated; admin.js redirects non-admins via /api/me, and every
    # /api/admin/* route it calls is separately gated by require_admin.
    return FileResponse(f"{WEBUI_DIR}/admin.html")


@app.get(p("/admin.css"))
def admin_css():
    return FileResponse(f"{WEBUI_DIR}/admin.css", media_type="text/css")


@app.get(p("/admin.js"))
def admin_js():
    return FileResponse(f"{WEBUI_DIR}/admin.js", media_type="application/javascript")


@app.get(p("/cropper.js"))
def cropper_js():
    return FileResponse(f"{WEBUI_DIR}/cropper.js", media_type="application/javascript")


# ── cover.html's own static assets (the "Arkendpoint" theme page) --
# copied in from a separate project (see cover.html's own comment) so this
# server can decide, itself, whether a request gets the theme page or the
# real chat shell (see index() above) -- that decision has to happen on one
# backend for the same-domain cookie swap to work at all, so these files
# moved in here rather than staying served by a separate static-site
# container. Reachable from any host this process answers for (not just
# arkendpoint.dev), same as every other static route below. ──────────────
@app.get(p("/support.js"))
def support_js():
    return FileResponse(f"{WEBUI_DIR}/support.js", media_type="application/javascript")


@app.get(p("/arkendpoint.config.js"))
def arkendpoint_config_js():
    return FileResponse(f"{WEBUI_DIR}/arkendpoint.config.js", media_type="application/javascript")


app.mount(p("/_ds"), StaticFiles(directory=f"{WEBUI_DIR}/_ds"), name="ark_ds")
app.mount(p("/assets"), StaticFiles(directory=f"{WEBUI_DIR}/assets"), name="ark_assets")


@app.get(p("/nocturne.css"))
def nocturne_css():
    return FileResponse(f"{WEBUI_DIR}/nocturne.css", media_type="text/css")


@app.get(p("/chat.css"))
def chat_css():
    return FileResponse(f"{WEBUI_DIR}/chat.css", media_type="text/css")


@app.get(p("/chat.js"))
def chat_js():
    return FileResponse(f"{WEBUI_DIR}/chat.js", media_type="application/javascript")


@app.get(p("/login.css"))
def login_css():
    return FileResponse(f"{WEBUI_DIR}/login.css", media_type="text/css")


@app.get(p("/login.js"))
def login_js():
    return FileResponse(f"{WEBUI_DIR}/login.js", media_type="application/javascript")


@app.get(p("/images/{filename}"))
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


@app.get(p("/api/info"))
def info(user=Depends(require_user)):
    bot_profile = load_bot_profile()
    return {
        "botName": BOT_NAME,
        "model": MODEL,
        "mood": ai.current_mood,
        "providers": list(DEFAULT_PROVIDER_CHAIN),
        "lastProvider": llm.last_provider_used,
        "botAvatar": bot_profile["avatar"],
        "botBanner": bot_profile["banner"],
        "botBio": bot_profile["bio"],
        "botAbout": bot_profile["about"],
        "publicHomepage": WEBUI_PUBLIC_HOMEPAGE,
    }


@app.get(p("/api/users"))
async def list_users_route(user=Depends(require_user)):
    """Every registered account + whether they're currently online (see
    core/user_presence.py) -- for the chat's right-panel member list. Not
    admin-gated: username + online status is fine for anyone signed in to
    see, matching this app's small-group scale (see core/chat_store.py's
    module docstring). Also carries each account's profile fields (avatar/
    banner/hue/description) -- chat.js needs these to render anyone else's
    avatar in the member list and message log, not just your own."""
    rows = await asyncio.to_thread(auth.list_users)
    users = [
        {
            "userId": r["user_id"], "username": r["username"],
            "isAdmin": auth.is_admin(r), "online": is_online(r["user_id"]),
            "avatar": r["avatar"], "banner": r["banner"], "hue": r["hue"], "description": r["description"],
        }
        for r in rows
    ]
    users.sort(key=lambda u: (not u["online"], u["username"].lower()))
    return {"users": users}


@app.get(p("/api/messages/{channel}"))
async def get_messages(channel: str, since: int = 0, user=Depends(require_user)):
    if channel not in WEBUI_CHANNELS:
        raise HTTPException(status_code=404, detail="unknown channel")
    rows = await asyncio.to_thread(chat_store.get_messages, channel, since)
    return {"messages": [chat_store.serialize(r) for r in rows]}


def _can_modify(user, row) -> bool:
    """A message's own author may edit/delete it; the admin account may
    edit/delete anyone's (including the bot's -- e.g. to clean up a bad
    reply)."""
    return auth.is_admin(user) or (not row["is_bot"] and row["user_id"] == user["user_id"])


@app.post(p("/api/messages/{message_id}/edit"))
async def edit_message(message_id: int, req: EditMessageRequest, user=Depends(require_user)):
    row = await asyncio.to_thread(chat_store.get_message, message_id)
    if not row:
        raise HTTPException(status_code=404, detail="message not found")
    if not _can_modify(user, row):
        raise HTTPException(status_code=403, detail="you can't edit this message")
    body = req.body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="message can't be empty")
    updated = await asyncio.to_thread(chat_store.edit_message, message_id, body)
    return {"message": chat_store.serialize(updated)}


@app.post(p("/api/messages/{message_id}/delete"))
async def delete_message(message_id: int, user=Depends(require_user)):
    row = await asyncio.to_thread(chat_store.get_message, message_id)
    if not row:
        raise HTTPException(status_code=404, detail="message not found")
    if not _can_modify(user, row):
        raise HTTPException(status_code=403, detail="you can't delete this message")
    await asyncio.to_thread(chat_store.delete_message, message_id)
    return {"status": "ok"}


# ── /imagine, /imagine_anime -- handled outside webui_commands.COMMANDS (see
# that module's docstring) because, unlike every other text command, these
# need to keep editing their own chat_store row as generation progresses --
# a live text progress bar, polled into view the same way every other
# message update already reaches the browser (WebUI/chat.js's pollActive),
# no new transport needed. ──────────────────────────────────────────────────

IMAGINE_MODELS = {"imagine": "flux", "imagine_anime": "anima"}


def _render_progress_bar(step: int, total: int, length: int = 12) -> str:
    if not total:
        return "▱" * length
    filled = min(length, round(length * step / total))
    return "▰" * filled + "▱" * (length - filled)


async def _run_imagine_command(channel: str, command: str, raw_args: str):
    """Runs /imagine or /imagine_anime end to end: parse args, create a
    placeholder bot message immediately, then edit that same message in
    place as core.imagegen reports progress, finishing with the image (or an
    error). Returns the final message row. Blocks the caller's own request
    for the whole generation (same as before -- there's still only one
    request/response here), but because it edits chat_store as it goes,
    *every* viewer polling this channel -- including the requester's own
    other poll tick, independent of their in-flight POST -- sees the
    progress bar update live, not just the final result."""
    model = IMAGINE_MODELS[command]
    prompt, kwargs, error = parse_imagine_args(raw_args)
    if error:
        return await asyncio.to_thread(chat_store.add_message, channel, True, BOT_NAME, error)
    if not prompt:
        usage = (
            f"usage: /{command} <prompt> [--width N] [--height N] [--steps N] "
            f"[--cfg N] [--seed N] [--negative \"text\"]"
        )
        return await asyncio.to_thread(chat_store.add_message, channel, True, BOT_NAME, usage)

    # Discord messages cap out at 2000 chars -- truncate just the displayed
    # copy so an overly long prompt can't break rendering; generate_image()
    # still gets the full prompt untouched.
    display_prompt = prompt if len(prompt) <= 1000 else prompt[:1000] + "..."
    bot_msg = await asyncio.to_thread(
        chat_store.add_message, channel, True, BOT_NAME, display_prompt, None, None, "queued..."
    )

    update_queue, position = enqueue_generate_image(prompt, model=model, **kwargs)
    if position > 0:
        await asyncio.to_thread(
            chat_store.edit_message, bot_msg["id"], display_prompt, None, f"queued -- #{position + 1} in line", False,
        )

    start = time.monotonic()
    last_edit = 0.0
    filepath = None
    gen_error = None
    while True:
        update = await asyncio.to_thread(update_queue.get)
        if update is None:
            break
        if update["type"] == "progress":
            step, total = update["step"], update["total"]
            now = time.monotonic()
            # Throttle edits -- chat_store.edit_message is a write on every
            # call, and pollActive only checks every 3s anyway, so anything
            # tighter than that just burns writes nobody will see sooner.
            if now - last_edit < 1.5 and step < total:
                continue
            last_edit = now
            bar = _render_progress_bar(step, total)
            percent = int(100 * step / total) if total else 0
            await asyncio.to_thread(
                chat_store.edit_message, bot_msg["id"], display_prompt, None, f"{bar} {percent}%", False,
            )
        elif update["type"] == "done":
            filepath = update["path"]
        elif update["type"] == "error":
            gen_error = update["message"]

    elapsed = time.monotonic() - start
    if filepath and os.path.exists(filepath):
        # Unlike Discord's /imagine (uploads to Discord's CDN then deletes
        # its local copy), this server IS the host the browser loads the
        # image from, so the file has to stay on disk -- never cleaned up
        # here. data/images/ grows unbounded; not addressed, wasn't asked for.
        # No leading "/" -- stored as-is in chat_store and handed straight
        # to <img src> by chat.js, so it needs to resolve relative to
        # whatever page renders it (see WEBUI_BASE_PATH's note above), not
        # the domain root.
        image_url = f"images/{os.path.basename(filepath)}"
        return await asyncio.to_thread(
            chat_store.edit_message, bot_msg["id"], display_prompt, image_url, f"{elapsed:.1f}s · command", False,
        )
    body = f"couldn't generate that image, sorry.{f' ({gen_error})' if gen_error else ''}"
    return await asyncio.to_thread(chat_store.edit_message, bot_msg["id"], body, None, None, False)


@app.post(p("/api/chat"))
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
        if channel in WEBUI_NOTIFY_CHANNELS:
            await notify_discord(channel, req.username, message)
        return {
            "message": chat_store.serialize(user_msg), "reply": None,
            "mood": ai.current_mood, "provider": None, "isCommand": False,
        }

    started_at = time.monotonic()

    if message.startswith("/"):
        name, _, raw_args = message[1:].partition(" ")
        name_lower = name.lower()

        if name_lower in IMAGINE_MODELS:
            bot_msg = await _run_imagine_command(channel, name_lower, raw_args)
            return {
                "message": chat_store.serialize(user_msg), "reply": chat_store.serialize(bot_msg),
                "mood": ai.current_mood, "provider": None, "isCommand": True,
            }

        handler = COMMANDS.get(name_lower)
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


@app.post(p("/api/reset"))
async def reset(req: ResetRequest, user=Depends(require_user)):
    if not auth.is_admin(user):
        raise HTTPException(status_code=403, detail="Only the admin account can reset a channel.")

    channel = req.channel if req.channel in WEBUI_CHANNELS else WEBUI_BOT_CHANNEL
    await asyncio.to_thread(chat_store.clear_channel, channel)

    if channel != WEBUI_BOT_CHANNEL:
        return {"status": "ok", "greeting": None}

    histories.pop(WEBUI_CHANNEL_ID, None)
    greeting = await asyncio.to_thread(chat_store.add_message, channel, True, BOT_NAME, "Hey. What's up?")
    return {"status": "ok", "greeting": chat_store.serialize(greeting)}


# ── admin panel (WebUI/admin.html) -- every route below requires the admin
# account (see require_admin); admin.js is the only caller. ──────────────────

@app.get(p("/api/admin/overview"))
async def admin_overview(user=Depends(require_admin)):
    counts = await asyncio.to_thread(chat_store.message_counts)
    users = await asyncio.to_thread(auth.list_users)
    bot_profile = await asyncio.to_thread(load_bot_profile)
    return {
        "botName": BOT_NAME,
        "model": MODEL,
        "mood": ai.current_mood,
        "lastProvider": llm.last_provider_used,
        "forcedProvider": llm.get_forced_provider(),
        "providers": llm.get_provider_status(),
        "channels": [{"key": c, "count": counts.get(c, 0)} for c in WEBUI_CHANNELS],
        "userCount": len(users),
        "botAvatar": bot_profile["avatar"],
        "botBanner": bot_profile["banner"],
        "botBio": bot_profile["bio"],
        "botAbout": bot_profile["about"],
    }


@app.post(p("/api/admin/mood"))
async def admin_set_mood(req: AdminMoodRequest, user=Depends(require_admin)):
    mood = req.mood.strip()
    if not mood:
        raise HTTPException(status_code=400, detail="mood is required")
    ai.current_mood = mood
    return {"status": "ok", "mood": mood}


@app.post(p("/api/admin/provider"))
async def admin_set_provider(req: AdminProviderRequest, user=Depends(require_admin)):
    value = req.provider.strip().lower()
    if value == "auto":
        llm.set_forced_provider(None)
        return {"status": "ok", "forcedProvider": None}
    if value not in DEFAULT_PROVIDER_CHAIN:
        raise HTTPException(status_code=400, detail=f"unknown provider '{value}'")
    if llm.is_provider_disabled(value):
        raise HTTPException(status_code=400, detail=f"{value} is disabled (no/bad API key)")
    llm.set_forced_provider(value)
    return {"status": "ok", "forcedProvider": value}


@app.post(p("/api/admin/bot-profile"))
async def admin_set_bot_profile(req: AdminBotProfileRequest, user=Depends(require_admin)):
    profile = await asyncio.to_thread(load_bot_profile)
    if req.avatar is not None:
        profile["avatar"] = req.avatar
    if req.banner is not None:
        profile["banner"] = req.banner
    if req.bio is not None:
        profile["bio"] = req.bio.strip()
    if req.about is not None:
        profile["about"] = req.about.strip()
    await asyncio.to_thread(save_bot_profile, profile)
    return {
        "status": "ok", "avatar": profile["avatar"], "banner": profile["banner"],
        "bio": profile["bio"], "about": profile["about"],
    }


@app.get(p("/api/admin/users"))
async def admin_users(user=Depends(require_admin)):
    rows = await asyncio.to_thread(auth.list_users)
    return {"users": [
        {
            "userId": r["user_id"], "username": r["username"], "email": r["email"],
            "createdAt": r["created_at"], "isAdmin": auth.is_admin(r),
        }
        for r in rows
    ]}


@app.post(p("/api/admin/users/delete"))
async def admin_delete_user(req: AdminDeleteUserRequest, user=Depends(require_admin)):
    if req.user_id == user["user_id"]:
        raise HTTPException(status_code=400, detail="You can't delete your own account.")
    if req.user_id == ADMIN_USER_ID:
        raise HTTPException(status_code=400, detail="The admin account can't be deleted.")
    deleted = await asyncio.to_thread(auth.delete_user, req.user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="no such user")
    return {"status": "ok"}


@app.get(p("/api/admin/memory"))
async def admin_memory(user=Depends(require_admin)):
    memory = await asyncio.to_thread(load_memory)
    return {"entries": [
        {"userId": uid, "displayName": data["display_name"], "notes": data["notes"]}
        for uid, data in memory.items()
    ]}


@app.post(p("/api/admin/memory/wipe"))
async def admin_memory_wipe(req: AdminMemoryWipeRequest, user=Depends(require_admin)):
    memory = await asyncio.to_thread(load_memory)
    if memory.pop(req.user_id, None) is None:
        raise HTTPException(status_code=404, detail="no memory for that user")
    await asyncio.to_thread(save_memory, memory)
    return {"status": "ok"}


@app.post(p("/api/admin/memory/wipe-all"))
async def admin_memory_wipe_all(user=Depends(require_admin)):
    await asyncio.to_thread(save_memory, {})
    return {"status": "ok"}


@app.post(p("/api/register"))
async def register(req: AuthRequest):
    # WEBUI_REGISTRATION_TOKEN unset disables this check entirely (see
    # core/config.py) -- constant-time compare so a wrong guess can't be
    # narrowed down via response timing, same reasoning as verify_password.
    if WEBUI_REGISTRATION_TOKEN and not secrets.compare_digest(req.token or "", WEBUI_REGISTRATION_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid invite code.")
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

    is_admin = auth.is_admin({"user_id": user_id})
    print(
        f"{LIGHT_GREEN}[auth] registered {username} ({user_id}){' -- admin' if is_admin else ''}{RESET}",
        flush=True,
    )
    # {"pending": true} rather than logging them in immediately -- matches
    # login.html's own onSubmit, which on this response switches to login
    # mode with a "sign in now" notice instead of redirecting.
    return {"pending": True, "user_id": user_id, "username": username, "isAdmin": is_admin}


@app.post(p("/api/login"))
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
    return {"user_id": user["user_id"], "username": user["username"], "isAdmin": auth.is_admin(user)}


@app.post(p("/api/logout"))
async def logout(request: Request, response: Response):
    token = request.cookies.get("session")
    if token:
        await asyncio.to_thread(auth.delete_session, token)
    response.delete_cookie("session")
    return {"status": "ok"}


@app.get(p("/api/me"))
async def me(request: Request):
    token = request.cookies.get("session")
    user = await asyncio.to_thread(auth.get_user_by_session, token) if token else None
    if not user:
        raise HTTPException(status_code=401, detail="not signed in")
    return {
        "user_id": user["user_id"], "username": user["username"], "isAdmin": auth.is_admin(user),
        # Profile fields ride along on the same request checkAuth() already
        # makes on page load -- avoids a second round-trip just for these.
        "avatar": user["avatar"], "banner": user["banner"], "hue": user["hue"], "description": user["description"],
    }


class ProfileRequest(BaseModel):
    # All optional; only fields actually present in the request body are
    # applied (see model_fields_set below) -- omitting a field leaves it
    # untouched, sending it as null/"" explicitly clears it. Each is its
    # own discrete UI action in chat.js (upload photo, pick a swatch, save
    # description, ...), so a request only ever carries the one field that
    # action changed.
    avatar: str | None = None
    banner: str | None = None
    hue: int | None = None
    description: str | None = None


@app.post(p("/api/profile"))
async def update_profile(req: ProfileRequest, user=Depends(require_user)):
    fields = {k: getattr(req, k) for k in req.model_fields_set}
    if fields:
        await asyncio.to_thread(auth.update_profile, user["user_id"], **fields)
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    print(f"{LIGHT_GREEN}[webui] serving {WEBUI_DIR} on http://{WEBUI_HOST}:{WEBUI_PORT}{RESET}", flush=True)
    uvicorn.run(app, host=WEBUI_HOST, port=WEBUI_PORT)
