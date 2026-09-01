# ytembed_server

An fxtwitter/vxtwitter-style link-rewrite proxy, but for YouTube. Standalone
website -- no Discord bot integration, no `discord.Embed` code, nothing in
`app/` touched. It only does one thing: given a rewritten link, decide
whether the requester is a link-preview crawler or a real browser, and serve
each one something different.

Paste `https://www.youtube.com/watch?v=VIDEO_ID` into a chat and you get
YouTube's own (often mediocre) link preview. Replace `youtube.com` with your
subdomain -- `https://yt.example.dev/watch?v=VIDEO_ID` -- and:

- A **real browser** opening that link gets an instant 302 redirect straight
  to the real YouTube URL. Nothing changes for a human.
- A **link-preview crawler** (Discord, Telegram, Slack, etc. -- detected by
  User-Agent, see `CRAWLER_UA_SUBSTRINGS` in `server.py`) instead gets back
  an HTML page with OpenGraph/Twitter-card `video` meta tags pointing at a
  locally cached mp4 of that video. The crawler's own platform is what
  renders the inline embed -- this service never talks to Discord's API.

## How it decides what to serve

1. Extract the video ID from `/watch?v=`, `/shorts/{id}`, or a bare
   `/{id}` (youtu.be-style).
2. Not a known crawler UA -> `302` to the real YouTube URL. Done.
3. Known crawler -> check the on-disk cache (`VIDEO_EMBED_CACHE_DIR`,
   `{id}.mp4` + `{id}.json` sidecar). If missing, download it with yt-dlp
   (capped at 720p / `VIDEO_EMBED_MAX_MB`, default 50MB -- concurrent
   requests for the same fresh ID are serialized with an `asyncio.Lock` so
   two crawlers hitting it at once don't trigger two downloads).
4. Download failed, or came out over the size cap -> `302` to the real
   YouTube URL (crawler still gets *a* preview, just YouTube's own).
5. Otherwise -> HTML page with OG/Twitter meta tags pointing at
   `/media/{id}.mp4` (the cached file, range-request friendly via
   Starlette's `FileResponse`) and `/player/{id}` (a bare `<video>` page for
   platforms that iframe a Twitter-style player card).

Cached files are deleted after `VIDEO_EMBED_CACHE_TTL_SECONDS` (default 6h)
by a background loop that runs every 30 minutes.

## Deploy

This needs to be reachable from the public internet at whatever subdomain
you pick -- unlike `intent_server`/`gpu_worker`, it publishes a host port.
You still need your own reverse proxy (nginx/Caddy/etc.) and DNS record
pointing that subdomain at this container's port with TLS termination --
that's external infra, not included here (same as how `cdn.arkendpoint.dev`
in `app/core/config.py`'s `FILE_SERVER_BASE_URL` is fronted by something
outside this repo).

1. Set `VIDEO_EMBED_DOMAIN` in the `ytembed` service's `environment:` block
   in `docker-compose.yml` (repo root) to your actual subdomain.
   `app/core/config.py` has a `VIDEO_EMBED_DOMAIN` constant too, kept in
   sync by hand -- it documents the value for the future step of wiring a
   chat bot to actually rewrite links, but nothing in `app/` reads it yet.
2. Point DNS + your reverse proxy at this container's published port
   (`8804` by default).
3. `docker compose up -d --build`

## Config (env vars)

| Var | Default | Meaning |
|---|---|---|
| `VIDEO_EMBED_DOMAIN` | *(required)* | Public domain this is reachable at |
| `VIDEO_EMBED_PORT` | `8804` | Listen port |
| `VIDEO_EMBED_CACHE_DIR` | `./cache` | Where downloaded videos are cached |
| `VIDEO_EMBED_MAX_MB` | `50` | Size cap; over this, falls back to redirect |
| `VIDEO_EMBED_CACHE_TTL_SECONDS` | `21600` (6h) | How long a cached video is kept |

## Not built (on purpose)

Nothing here rewrites links automatically in Discord messages, and nothing
here calls the Discord API. If/when that's wanted, it's a small addition to
the bot in `app/` (a message listener that replaces `youtube.com`/`youtu.be`
links with `VIDEO_EMBED_DOMAIN` ones before Discord's own unfurler sees
them) -- deliberately out of scope for this folder.
