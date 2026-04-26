import io
import random
import asyncio
import os
import urllib.request
import aiohttp
import discord
import ai
import tempfile
from mutagen.mp3 import MP3
from discord import app_commands
from PIL import Image, ImageDraw, ImageFont
from .download import _cancel_autoplay, play_next, resolve_spotify_to_query, search_and_download_audio, voice_states, build_now_playing_embed, FFMPEG_OPTIONS, get_ffmpeg_options, play_local_file

OWNER_ID = 955604666689921086


def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None and interaction.user.guild_permissions.administrator


def setup(tree: app_commands.CommandTree, bot: discord.Client):

    # ── Fun / Utility ─────────────────────────────────────────────────────────

    @tree.command(name="ship", description="Ship two users and get a compatibility rating")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(user1="First user to ship", user2="Second user to ship")
    async def ship_users(interaction: discord.Interaction, user1: discord.User, user2: discord.User):
        if user1.id == user2.id:
            await interaction.response.send_message("💔 You can't ship someone with themselves!", ephemeral=True)
            return

        seed = min(user1.id, user2.id) * max(user1.id, user2.id)
        compatibility = seed % 101
        n1, n2 = user1.display_name, user2.display_name
        ship_name = n1[:len(n1) // 2] + n2[len(n2) // 2:]

        if compatibility >= 80:   label, color = "Soulmates 💞", 0xFF69B4
        elif compatibility >= 60: label, color = "Great match 💕", 0xFF8C00
        elif compatibility >= 40: label, color = "Could work 🤔", 0xFFD700
        elif compatibility >= 20: label, color = "Rough waters 😬", 0x808080
        else:                     label, color = "Disaster 💀", 0x8B0000

        filled = round(compatibility / 10)
        bar = "█" * filled + "░" * (10 - filled)

        embed = discord.Embed(
            title=f"{user1.display_name} x {user2.display_name}",
            description=f"**{label}**\n`{bar}` **{compatibility}%**\nShip name: **{ship_name}**",
            color=color
        )
        embed.set_thumbnail(url=user2.display_avatar.url)
        embed.set_author(name=user1.display_name, icon_url=user1.display_avatar.url)
        embed.set_footer(text=f"{user1.display_name} x {user2.display_name}", icon_url=user1.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @tree.command(name="mood", description="Check the bot's current mood")
    async def check_mood(interaction: discord.Interaction):
        await interaction.response.send_message(f"I'm currently feeling {ai.current_mood}!")

    @tree.command(name="change_mood", description="Change the bot's mood (admin only)")
    async def change_mood(interaction: discord.Interaction, mood: str):
        if not is_admin(interaction):
            await interaction.response.send_message("You're not an administrator.", ephemeral=True)
            return
        ai.current_mood = mood
        await interaction.response.send_message(f"Mood changed to {mood}!")

    @tree.command(name="ping", description="Check the bot's latency")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def ping(interaction: discord.Interaction):
        await interaction.response.send_message(f"Pong! Latency: {round(bot.latency * 1000)}ms")

    @tree.command(name="echo", description="Echo back your message (admin only)")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def echo(interaction: discord.Interaction, message: str):
        if not is_admin(interaction):
            await interaction.response.send_message("You're not an administrator.", ephemeral=True)
            return
        await interaction.response.send_message(message)

    @tree.command(name="curl", description="Make the bot perform a GET request to a URL (admin only)")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def curl(interaction: discord.Interaction, url: str):
        if not is_admin(interaction):
            await interaction.response.send_message("You're not an administrator.", ephemeral=True)
            return
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                content = await resp.text()
        if len(content) <= 1900:
            await interaction.response.send_message(f"Content from {url}:\n```{content}```")
        else:
            await interaction.response.send_message(file=discord.File(io.BytesIO(content.encode()), filename="response.txt"))

    @tree.command(name="ip", description="Get the bot's public IP address (admin only)")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def get_ip(interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("You're not an administrator.", ephemeral=True)
            return
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.ipify.org") as resp:
                ip = await resp.text()
        await interaction.response.send_message(f"Bot's public IP: {ip}")

    @tree.command(name="terminal", description="Run a shell command and get the output (owner only)")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def terminal(interaction: discord.Interaction, command: str):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("You're not the owner.", ephemeral=True)
            return
        await interaction.response.defer()
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            await interaction.followup.send("Command timed out.", ephemeral=True)
            return
        output = stdout.decode() + stderr.decode()
        if len(output) > 1900:
            output = output[:1900] + "\n...[output truncated]"
        await interaction.followup.send(f"Output of `{command}`:\n```{output}```")

    @tree.command(name="time", description="Get the current server time")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def get_time(interaction: discord.Interaction):
        from datetime import datetime
        await interaction.response.send_message(f"Current server time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    @tree.command(name="8ball", description="Ask the magic 8-ball a yes/no question")
    @app_commands.describe(question="Your question for the 8-ball")
    async def magic_8ball(interaction: discord.Interaction, question: str):
        responses = [
            "It is certain.", "It is decidedly so.", "Without a doubt.",
            "Yes - definitely.", "You may rely on it.", "As I see it, yes.",
            "Most likely.", "Outlook good.", "Yes.", "Signs point to yes.",
            "Reply hazy, try again.", "Ask again later.", "Better not tell you now.",
            "Cannot predict now.", "Concentrate and ask again.",
            "Don't count on it.", "My reply is no.", "My sources say no.",
            "Outlook not so good.", "Very doubtful."
        ]
        await interaction.response.send_message(f"Asked: {question}\n{random.choice(responses)}")

    @tree.command(name="quote", description="Turn a message into a quote image")
    @app_commands.describe(message="The message to quote", author="The author's name", user="Tag a user to use their avatar (optional)")
    async def quote(interaction: discord.Interaction, message: str, author: str, user: discord.User = None):
        await interaction.response.defer()
        W, H = 1080, 600
        img = Image.new("RGB", (W, H), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        try:
            font_main = ImageFont.truetype("arial.ttf", 52)
            font_sub  = ImageFont.truetype("arial.ttf", 28)
        except Exception:
            font_main = ImageFont.load_default()
            font_sub  = ImageFont.load_default()
        if user:
            async with aiohttp.ClientSession() as session:
                async with session.get(user.display_avatar.url) as resp:
                    avatar_data = await resp.read()
            avatar = Image.open(io.BytesIO(avatar_data)).convert("RGBA").resize((H, H))
            fade = Image.new("L", (H, H))
            for x in range(H):
                alpha = max(0, 255 - int((x / H) * 255))
                for y in range(H):
                    fade.putpixel((x, y), alpha)
            avatar.putalpha(fade)
            img.paste(avatar, (0, 0), avatar)
        words = message.split()
        lines, current = [], ""
        for word in words:
            if len(current) + len(word) + 1 <= 30:
                current = (current + " " + word).strip()
            else:
                if current: lines.append(current)
                current = word
        if current: lines.append(current)
        text_x = W // 2 + 50
        draw.text((text_x, H // 2 - 60), "\n".join(lines), font=font_main, fill=(255, 255, 255), anchor="lm")
        draw.text((text_x, H // 2 + 20), f"- {author}", font=font_sub, fill=(180, 180, 180), anchor="lm")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        await interaction.followup.send(file=discord.File(buffer, filename="quote.png"))

    @tree.context_menu(name="Make Quote")
    async def make_quote(interaction: discord.Interaction, message: discord.Message):
        await interaction.response.defer()
        W, H = 1200, 400
        img = Image.new("RGB", (W, H), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        font_path = "/tmp/Lato-Regular.ttf"
        try:
            urllib.request.urlretrieve("https://github.com/google/fonts/raw/main/ofl/lato/Lato-Regular.ttf", font_path)
            font_main = ImageFont.truetype(font_path, 56)
            font_sub  = ImageFont.truetype(font_path, 32)
        except Exception:
            font_main = ImageFont.load_default()
            font_sub  = ImageFont.load_default()
        async with aiohttp.ClientSession() as session:
            async with session.get(message.author.display_avatar.with_size(512).url) as resp:
                avatar_data = await resp.read()
        avatar = Image.open(io.BytesIO(avatar_data)).convert("RGBA").resize((H, H))
        fade = Image.new("L", (H, H))
        for x in range(H):
            alpha = max(0, 220 - int((x / H) ** 1.5 * 220))
            for y in range(H):
                fade.putpixel((x, y), alpha)
        avatar.putalpha(fade)
        img.paste(avatar, (0, 0), avatar)
        text_x = W // 2 + 80
        quote_text = message.content or "[no text]"
        author_text = f"- {message.author.display_name}"
        bbox_main = font_main.getbbox(quote_text)
        bbox_sub  = font_sub.getbbox(author_text)
        main_h = bbox_main[3] - bbox_main[1]
        sub_h  = bbox_sub[3]  - bbox_sub[1]
        gap = 20
        start_y = H // 2 - (main_h + gap + sub_h) // 2
        draw.text((text_x, start_y),                quote_text,  font=font_main, fill=(255, 255, 255))
        draw.text((text_x, start_y + main_h + gap), author_text, font=font_sub,  fill=(180, 180, 180))
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        await interaction.followup.send(file=discord.File(buffer, filename="quote.png"))

    # ── Music ─────────────────────────────────────────────────────────────────

    def format_duration(seconds: int | None) -> str:
        if not seconds:
            return "Unknown"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


    async def build_now_playing_embed(meta: dict, queued_count: int = 0) -> tuple[discord.Embed, discord.File | None]:
        """Builds the Now Playing embed. Returns (embed, file_or_none)."""
        title     = meta.get("title")    or "Unknown Title"
        artist    = meta.get("artist")   or "Unknown Artist"
        album     = meta.get("album")
        duration  = meta.get("duration")
        thumbnail = meta.get("thumbnail")

        embed = discord.Embed(title=title, color=0x1DB954)
        embed.add_field(name="Artist",   value=artist,                  inline=True)
        embed.add_field(name="Duration", value=format_duration(duration), inline=True)
        if album:
            embed.add_field(name="Album", value=album, inline=True)
        if queued_count:
            embed.add_field(name="Up next", value=f"{queued_count} song{'s' if queued_count != 1 else ''}", inline=True)
        embed.set_footer(text="Now Playing")

        file = None

        if isinstance(thumbnail, bytes):
            file = discord.File(io.BytesIO(thumbnail), filename="cover.png")
            embed.set_image(url="attachment://cover.png")
        elif isinstance(thumbnail, str) and thumbnail.startswith("http"):
            embed.set_image(url=thumbnail)

        return embed, file


    @tree.command(name="play", description="Play a song in your voice channel")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.describe(
        query="Spotify URL, YouTube URL, or song name",
        normalize="Normalize volume levels (EBU R128 loudnorm) — good for music with inconsistent loudness",
    )
    async def play(interaction: discord.Interaction, query: str, normalize: bool = False):
        await interaction.response.defer(thinking=True)

        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.followup.send("You need to be in a voice channel first.")
            return

        channel   = interaction.user.voice.channel
        guild_id  = interaction.guild_id

        vc = interaction.guild.voice_client
        if vc is None:
            vc = await channel.connect()
        elif vc.channel != channel:
            await vc.move_to(channel)

        if guild_id not in voice_states:
            voice_states[guild_id] = {
                "vc": vc, "queue": [],
                "current_file": None, "current_label": None, "current_meta": None,
                "last_title": None, "autoplay_task": None,
                "text_channel_id": interaction.channel_id,
            }
        else:
            voice_states[guild_id]["text_channel_id"] = interaction.channel_id

        _cancel_autoplay(guild_id)

        # Snapshot whether something is already playing BEFORE the download so that a
        # song ending naturally during the (slow) download doesn't change our intent.
        should_queue = vc.is_playing() or vc.is_paused()

        status = await interaction.followup.send("Searching...", wait=True)

        if "spotify.com" in query:
            search_query, label = await resolve_spotify_to_query(query)
            if not search_query:
                await status.edit(content="Couldn't resolve that Spotify link.")
                return
            await status.edit(content=f"Found **{label}** on Spotify, downloading...")
        elif "youtube.com" in query or "youtu.be" in query:
            search_query, label = query, query
            await status.edit(content="Downloading from YouTube...")
        else:
            search_query = f"ytsearch1:{query}"
            label = query
            await status.edit(content=f"Searching YouTube for **{query}**...")

        filepath, meta = await search_and_download_audio(search_query)
        if not filepath:
            await status.edit(content="Couldn't download that track.")
            return

        # Store normalize per-track in meta so it doesn't bleed across the session.
        meta["normalize"] = normalize

        display = meta.get("title") or label
        state   = voice_states[guild_id]

        if should_queue:
            state["queue"].append((filepath, display, meta))
            embed, file = await build_now_playing_embed(meta, queued_count=len(state["queue"]))
            embed.set_footer(text=f"Added to queue • #{len(state['queue'])}")
            if file:
                await status.edit(content=None, embed=embed, attachments=[file])
            else:
                await status.edit(content=None, embed=embed)
        else:
            state["queue"].append((filepath, display, meta))
            play_next(guild_id, vc, bot, silent=True)

            embed, file = await build_now_playing_embed(meta, queued_count=len(state["queue"]))
            if file:
                await status.edit(content=None, embed=embed, attachments=[file])
            else:
                await status.edit(content=None, embed=embed)


    @tree.command(name="skip", description="Skip the current song")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def skip(interaction: discord.Interaction):
        guild_id = interaction.guild_id
        state = voice_states.get(guild_id)

        _cancel_autoplay(guild_id)
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            # If autoplaying, flag to skip the delay so next autoplay fires immediately
            if state and state.get("autoplaying"):
                state["skip_autoplay_delay"] = True
            vc.stop()
            await interaction.response.send_message("Skipped.")
        else:
            await interaction.response.send_message("Nothing is playing.")


    @tree.command(name="stop", description="Stop playback and clear the queue")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def stop(interaction: discord.Interaction):
        guild_id = interaction.guild_id
        _cancel_autoplay(guild_id)
        state = voice_states.pop(guild_id, None)
        if state:
            for filepath, _, __ in state.get("queue", []):
                try: os.remove(filepath)
                except Exception: pass
            if state.get("current_file"):
                try: os.remove(state["current_file"])
                except Exception: pass
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
            await vc.disconnect()
        await interaction.response.send_message("Stopped and disconnected.")


    @tree.command(name="queue", description="Show the current queue")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def queue_cmd(interaction: discord.Interaction):
        state = voice_states.get(interaction.guild_id)
        if not state or (not state["current_label"] and not state["queue"]):
            await interaction.response.send_message("Queue is empty.")
            return
        lines = []
        if state["current_label"]:
            lines.append(f"**{state['current_label']}**")
        for i, (_, label, __) in enumerate(state["queue"], 1):
            lines.append(f"{i}. {label}")
        await interaction.response.send_message("\n".join(lines))


    @tree.command(name="clearqueue", description="Clear all queued songs (keeps the current song playing)")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def clearqueue(interaction: discord.Interaction):
        guild_id = interaction.guild_id
        state = voice_states.get(guild_id)

        if not state or not state["queue"]:
            await interaction.response.send_message("The queue is already empty.")
            return

        count = len(state["queue"])
        for filepath, _, __ in state["queue"]:
            try:
                os.remove(filepath)
            except Exception:
                pass
        state["queue"].clear()

        await interaction.response.send_message(
            f"Cleared **{count}** song{'s' if count != 1 else ''} from the queue."
        )

    @tree.command(name="pause", description="Pause or resume playback")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def pause(interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message("Not in a voice channel.")
            return
        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message("Paused.")
        elif vc.is_paused():
            vc.resume()
            await interaction.response.send_message("Resumed.")
        else:
            await interaction.response.send_message("Nothing is playing.")


    @tree.command(name="volume", description="Set playback volume (0–200)")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.describe(level="Volume level from 0 to 200 (default is 100)")
    async def volume(interaction: discord.Interaction, level: int):
        if not 0 <= level <= 200:
            await interaction.response.send_message("Volume must be between 0 and 200.", ephemeral=True)
            return

        vc = interaction.guild.voice_client
        if not vc or not vc.source:
            await interaction.response.send_message("Nothing is playing.")
            return

        # Wrap in PCMVolumeTransformer if not already
        if not isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source = discord.PCMVolumeTransformer(vc.source, volume=level / 100)
        else:
            vc.source.volume = level / 100

        state = voice_states.get(interaction.guild_id)
        if state:
            state["volume"] = level / 100  # persist so play_next can reapply it

        await interaction.response.send_message(f"Volume set to **{level}%**.")
        

    @tree.command(name="testplay", description="Test ffmpeg playback")
    async def testplay(interaction: discord.Interaction):
        if not interaction.user.voice:
            await interaction.response.send_message("Join a VC first")
            return
    
        await interaction.response.defer()
    
        # Download a very short test audio directly
        import subprocess
        test_file = os.path.join(tempfile.gettempdir(), "test_audio.mp3")
    
    # Generate a 5 second sine wave with ffmpeg directly — no yt-dlp involved
        result = subprocess.run([
            "ffmpeg",
            "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
            test_file
        ], capture_output=True)
    
        print(f"[testplay] ffmpeg generate returncode: {result.returncode}")
        print(f"[testplay] stderr: {result.stderr.decode()}")
        print(f"[testplay] file exists: {os.path.exists(test_file)}, size: {os.path.getsize(test_file) if os.path.exists(test_file) else 'MISSING'}")
    
        vc = await interaction.user.voice.channel.connect()
    
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(test_file, **FFMPEG_OPTIONS),
            volume=1.0
        )
    
        def after(error):
            print(f"[testplay] after error: {error}")
            try: os.remove(test_file)
            except: pass
    
        vc.play(source, after=after)
        await interaction.followup.send("Playing test tone — if you hear a beep ffmpeg works, if not the issue is ffmpeg itself.")

    @tree.command(name="playfile", description="Play an uploaded mp3 file in your voice channel")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.describe(file="The mp3 file to play")
    async def playfile(interaction: discord.Interaction, file: discord.Attachment):
        await interaction.response.defer(thinking=True)

        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.followup.send("You need to be in a voice channel first.")
            return

        if not file.filename.lower().endswith(".mp3"):
            await interaction.followup.send("Only mp3 files are supported.")
            return

        guild_id = interaction.guild_id
        channel  = interaction.user.voice.channel

        vc = interaction.guild.voice_client
        if vc is None:
            vc = await channel.connect()
        elif vc.channel != channel:
            await vc.move_to(channel)

        if guild_id not in voice_states:
            voice_states[guild_id] = {
                "vc": vc, "queue": [],
                "current_file": None, "current_label": None, "current_meta": None,
                "last_title": None, "autoplay_task": None,
                "text_channel_id": interaction.channel_id,
            }
        else:
            voice_states[guild_id]["text_channel_id"] = interaction.channel_id

        _cancel_autoplay(guild_id)

        status = await interaction.followup.send(f"Downloading `{file.filename}`...", wait=True)

        # Save attachment to a temp file
        dest = os.path.join(tempfile.gettempdir(), file.filename)
        async with aiohttp.ClientSession() as session:
            async with session.get(file.url) as resp:
                with open(dest, "wb") as f:
                    f.write(await resp.read())

        title  = os.path.splitext(file.filename)[0]
        meta   = {"title": title, "artist": None, "album": None, "duration": None, "thumbnail": None}

    # Try to read ID3 tags if present
        try:
            from mutagen.id3 import ID3
            from mutagen.mp3 import MP3
            tags = ID3(dest)
            mp3  = MP3(dest)
            meta["title"]    = str(tags.get("TIT2", "")) or title
            meta["artist"]   = str(tags.get("TPE1", "")) or None
            meta["album"]    = str(tags.get("TALB", "")) or None
            meta["duration"] = int(mp3.info.length)
            apic = tags.get("APIC:") or tags.get("APIC")
            if apic:
                meta["thumbnail"] = apic.data
        except Exception:
            pass

        state = voice_states[guild_id]

        if vc.is_playing() or vc.is_paused():
            state["queue"].append((dest, meta["title"], meta))
            await status.edit(content=f"Added to queue (#{len(state['queue'])}): **{meta['title']}**")
        else:
            started = await play_local_file(dest, meta, guild_id, vc, bot)
            if not started:
                await status.edit(content="Couldn't play that file.")
                return
            embed, art = await build_now_playing_embed(meta)
            if art:
                await status.edit(content=None, embed=embed, attachments=[art])
            else:
                await status.edit(content=None, embed=embed)