import io
import re
import asyncio
import io as _io
import aiohttp
import discord
from discord.ui import View, Button
from PIL import Image, ImageFilter, ImageEnhance
from .spotify_api import fetch_spotify_track_meta


class NowPlayingView(View):
    """
    Persistent playback controls attached to the now-playing embed.
    Imported lazily from spotify_player to avoid circular imports.
    """

    def __init__(self, guild_id: int, bot: discord.Client):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.bot      = bot
        self._sync_loop_button()

    def _sync_loop_button(self):
        """Set the loop button label/style to match the current state["loop"] value."""
        from .spotify_player import voice_states
        state = voice_states.get(self.guild_id)
        loop  = state.get("loop", "off") if state else "off"
        labels = {"off": "🔁 Off", "track": "🔂 Track", "queue": "🔁 Queue"}
        for item in self.children:
            if getattr(item, "custom_id", None) == "np_loop":
                item.label = labels.get(loop, "🔁 Off")
                item.style = discord.ButtonStyle.primary if loop != "off" else discord.ButtonStyle.secondary
                break

    # ── helpers ───────────────────────────────────────────────────────────────

    def _vc(self, interaction: discord.Interaction):
        return interaction.guild.voice_client if interaction.guild else None

    def _state(self):
        from .spotify_player import voice_states
        return voice_states.get(self.guild_id)

    async def _refresh_pause_button(self, interaction: discord.Interaction):
        """Flip the pause/resume button label to match current vc state."""
        vc = self._vc(interaction)
        for item in self.children:
            if getattr(item, "custom_id", None) == "np_pause":
                item.label = "▶" if (vc and vc.is_paused()) else "⏸"
                break
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

    async def _require_same_channel(self, interaction: discord.Interaction) -> bool:
        vc = self._vc(interaction)
        if not vc:
            await interaction.response.send_message("Not in a voice channel.", ephemeral=True)
            return False
        if not interaction.user.voice or interaction.user.voice.channel != vc.channel:
            await interaction.response.send_message("Join my voice channel first.", ephemeral=True)
            return False
        return True

    # ── buttons ───────────────────────────────────────────────────────────────

    @discord.ui.button(label="⏮", style=discord.ButtonStyle.secondary, custom_id="np_back")
    async def back(self, interaction: discord.Interaction, button: Button):
        if not await self._require_same_channel(interaction):
            return
        from .spotify_player import voice_states, play_next
        state = self._state()
        vc    = self._vc(interaction)
        if not state or not vc:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        # Re-insert current track at front so it plays again (one-shot back)
        cur_file  = state.get("current_file")
        cur_label = state.get("current_label")
        cur_meta  = state.get("current_meta")
        if cur_file and cur_label and cur_meta:
            state["queue"].insert(0, (cur_file, cur_label, cur_meta))
            state["current_file"] = None  # prevent deletion in play_next

        vc.stop()  # triggers play_next via after-callback
        await interaction.response.defer()

    @discord.ui.button(label="⏸", style=discord.ButtonStyle.primary, custom_id="np_pause")
    async def pause(self, interaction: discord.Interaction, button: Button):
        if not await self._require_same_channel(interaction):
            return
        vc = self._vc(interaction)
        if not vc:
            await interaction.response.send_message("Not in a voice channel.", ephemeral=True)
            return
        if vc.is_playing():
            vc.pause()
        elif vc.is_paused():
            vc.resume()
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        await interaction.response.defer()
        await self._refresh_pause_button(interaction)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary, custom_id="np_skip")
    async def skip(self, interaction: discord.Interaction, button: Button):
        if not await self._require_same_channel(interaction):
            return
        from .spotify_player import voice_states, _cancel_autoplay
        state = self._state()
        vc    = self._vc(interaction)
        if not vc or not vc.is_playing():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        _cancel_autoplay(self.guild_id)
        if state and state.get("autoplaying"):
            state["skip_autoplay_delay"] = True
        vc.stop()
        await interaction.response.defer()

    @discord.ui.button(label="🔁 Off", style=discord.ButtonStyle.secondary, custom_id="np_loop")
    async def loop(self, interaction: discord.Interaction, button: Button):
        if not await self._require_same_channel(interaction):
            return
        state = self._state()
        if not state:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        # Cycle: off → track → queue → off
        cycle = {"off": "track", "track": "queue", "queue": "off"}
        labels = {"off": "🔁 Off", "track": "🔂 Track", "queue": "🔁 Queue"}
        new_mode     = cycle.get(state.get("loop", "off"), "off")
        state["loop"] = new_mode
        button.label  = labels[new_mode]
        button.style  = discord.ButtonStyle.primary if new_mode != "off" else discord.ButtonStyle.secondary
        await interaction.response.defer()
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

    @discord.ui.button(label="≡ Queue", style=discord.ButtonStyle.secondary, custom_id="np_queue")
    async def queue(self, interaction: discord.Interaction, button: Button):
        state = self._state()
        if not state or (not state.get("current_label") and not state.get("queue")):
            await interaction.response.send_message("Queue is empty.", ephemeral=True)
            return
        lines = []
        if state.get("current_label"):
            lines.append(f"**Now playing:** {state['current_label']}")
        for i, (_, label, __) in enumerate(state["queue"], 1):
            lines.append(f"{i}. {label}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def build_now_playing_embed(
    meta: dict,
    queued_count: int = 0,
    spotify_url: str | None = None,
    guild_id: int | None = None,
    bot: discord.Client | None = None,
) -> tuple[discord.Embed, discord.File | None, View | None]:

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

    # ── Build view ────────────────────────────────────────────────────────────
    view = NowPlayingView(guild_id, bot) if guild_id and bot else None

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

    return embed, file, view