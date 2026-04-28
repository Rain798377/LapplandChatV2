import io
import re
import asyncio
import io as _io
import aiohttp
import discord
from PIL import Image, ImageFilter, ImageEnhance
from .spotify_api import fetch_spotify_track_meta


async def build_now_playing_embed(
    meta: dict,
    queued_count: int = 0,
    spotify_url: str | None = None,
) -> tuple[discord.Embed, discord.File | None]:

    def fmt_duration(seconds):
        if not seconds:
            return "Unknown"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"

    def _normalize_str(s: str) -> str:
        s = s.lower()
        s = re.sub(r"\(.*?\)|\[.*?\]", "", s)
        s = re.sub(r"[^a-z0-9\s]", "", s)
        return s.strip()

    def _matches(sp_meta: dict, dl_meta: dict) -> bool:
        sp_title  = _normalize_str(sp_meta.get("title", ""))
        sp_artist = _normalize_str(sp_meta.get("artist", ""))
        dl_title  = _normalize_str(dl_meta.get("title", ""))
        dl_artist = _normalize_str(dl_meta.get("artist", ""))
        title_ok  = sp_title and (sp_title in dl_title or dl_title in sp_title)
        artist_ok = sp_artist and any(
            a.strip() in dl_artist or dl_artist in a.strip()
            for a in sp_artist.split(",")
        )
        return title_ok or artist_ok

    def _build_composite(img_bytes: bytes) -> bytes:
        """
        Composite: blurred + desaturated background, sharp centered foreground.
        Returns PNG bytes.
        """
        W, H = 3840, 2160  # 16:9 canvas 4K

        src = Image.open(_io.BytesIO(img_bytes)).convert("RGB")

        # ── Background: scale to fill, blur, desaturate ───────────────────────
        bg_scale = max(W / src.width, H / src.height)
        bg = src.resize(
            (int(src.width * bg_scale), int(src.height * bg_scale)),
            Image.LANCZOS,
        )
        bx = (bg.width  - W) // 2
        by = (bg.height - H) // 2
        bg = bg.crop((bx, by, bx + W, by + H))
        bg = bg.filter(ImageFilter.GaussianBlur(radius=10))
        bg = ImageEnhance.Color(bg).enhance(0.35)
        bg = ImageEnhance.Brightness(bg).enhance(0.6)

        # ── Foreground: fit inside center box, sharp ──────────────────────────
        box_h = int(H * 0.82)
        scale = box_h / src.height
        fg_w  = int(src.width * scale)
        fg_h  = box_h
        fg = src.resize((fg_w, fg_h), Image.LANCZOS)

        px = (W - fg_w) // 2
        py = (H - fg_h) // 2
        bg.paste(fg, (px, py))

        out = _io.BytesIO()
        bg.save(out, format="PNG")
        return out.getvalue()

    async def _get_image_bytes(thumbnail) -> bytes | None:
        """Resolve thumbnail to raw bytes — handles both bytes and URL string."""
        if isinstance(thumbnail, bytes):
            return thumbnail
        if isinstance(thumbnail, str) and thumbnail.startswith("http"):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(thumbnail, timeout=aiohttp.ClientTimeout(total=8)) as r:
                        if r.status == 200:
                            return await r.read()
            except Exception as e:
                print(f"[embed] thumbnail fetch failed: {e}")
        return None

    # ── Try to enrich with Spotify metadata ──────────────────────────────────
    spotify_meta = None
    if spotify_url:
        spotify_meta = await fetch_spotify_track_meta(spotify_url)
        if spotify_meta:
            if _matches(spotify_meta, meta):
                print(f"[embed] Spotify metadata matched — using Spotify cover art")
                meta = {**meta, **spotify_meta}
            else:
                print(f"[embed] Spotify metadata did NOT match — using YT metadata")
                spotify_meta = None

    title     = meta.get("title")    or "Unknown Title"
    artist    = meta.get("artist")   or "Unknown Artist"
    album     = meta.get("album")
    duration  = meta.get("duration")
    thumbnail = meta.get("thumbnail")

    embed = discord.Embed(color=0x1DB954)
    embed.title = title
    embed.add_field(name="Artist",   value=artist,                 inline=True)
    embed.add_field(name="Duration", value=fmt_duration(duration), inline=True)
    if album:
        embed.add_field(name="Album", value=album, inline=True)
    if queued_count:
        embed.add_field(
            name="Up next",
            value=f"{queued_count} song{'s' if queued_count != 1 else ''}",
            inline=True,
        )
    embed.set_footer(text="Now Playing")

    # ── Build composite image ─────────────────────────────────────────────────
    file = None
    img_bytes = await _get_image_bytes(thumbnail)
    if img_bytes:
        try:
            composite = await asyncio.get_event_loop().run_in_executor(
                None, _build_composite, img_bytes
            )
            file = discord.File(io.BytesIO(composite), filename=f"{artist} - {title} cover.png")
            embed.set_image(url=f"attachment://{artist} - {title} cover.png")
        except Exception as e:
            print(f"[embed] composite failed, falling back to raw thumbnail: {e}")
            if isinstance(thumbnail, bytes):
                file = discord.File(io.BytesIO(thumbnail), filename=f"{artist} - {title} cover.png")
                embed.set_image(url=f"attachment://{artist} - {title} cover.png")
            elif isinstance(thumbnail, str) and thumbnail.startswith("http"):
                embed.set_image(url=thumbnail)

    return embed, file