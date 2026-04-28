import os
import asyncio
import tempfile
import aiohttp
import discord
from mutagen.mp3 import MP3
from discord import app_commands
from spotify.spotify_player import (_cancel_autoplay, play_next, play_local_file, voice_states, FFMPEG_OPTIONS)
from spotify.audio import search_and_download_audio
from spotify.embed import build_now_playing_embed
from spotify.spotify_api import resolve_spotify_to_query
from spotify.resolver import _is_playlist_url, _is_spotify_url, _is_apple_music_url, _is_youtube_url, _is_soundcloud_url, resolve_apple_music_to_query, resolve_playlist_tracks


def setup(tree: app_commands.CommandTree, bot: discord.Client):
    spotify_cmds = app_commands.Group(name="spotify", description="Commands for Spotify playback")

    # ── Music ─────────────────────────────────────────────────────────────────

    def format_duration(seconds: int | None) -> str:
        if not seconds:
            return "Unknown"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"

    @spotify_cmds.command(name="play", description="Play a song or playlist in your voice channel")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.describe(
        query="Spotify, Apple Music, YouTube, SoundCloud URL — or just a song name",
        normalize="Normalize volume levels — good for music with inconsistent loudness",
    )
    async def play(interaction: discord.Interaction, query: str, normalize: bool = False):
        await interaction.response.defer(thinking=True)

        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.followup.send("You need to be in a voice channel first.")
            return

        channel  = interaction.user.voice.channel
        guild_id = interaction.guild_id

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
                "loop": "off",
            }
        else:
            voice_states[guild_id]["text_channel_id"] = interaction.channel_id

        _cancel_autoplay(guild_id)
        should_queue = vc.is_playing() or vc.is_paused()
        status = await interaction.followup.send("Searching...", wait=True)

        # ── Playlist / multi-track URLs ───────────────────────────────────────
        if _is_playlist_url(query):
            await status.edit(content="Fetching playlist...")
            tracks = await resolve_playlist_tracks(query)
            if not tracks:
                await status.edit(content="Couldn't resolve that playlist. Try a direct song URL or search.")
                return

            await status.edit(content=f"Found **{len(tracks)} tracks** — queuing...")

            queued = 0
            first_meta = None
            state = voice_states[guild_id]

            for i, (track_query, track_label) in enumerate(tracks):
                filepath, meta = await search_and_download_audio(track_query)
                if not filepath:
                    print(f"[playlist] skipping '{track_label}': download failed")
                    continue
                meta["normalize"] = normalize
                display = meta.get("title") or track_label
                state["queue"].append((filepath, display, meta))
                if first_meta is None:
                    first_meta = meta
                queued += 1
                if queued % 5 == 0 or i == len(tracks) - 1:
                    await status.edit(content=f"Queuing playlist… {queued}/{len(tracks)} tracks loaded")

            if queued == 0:
                await status.edit(content="Couldn't download any tracks from that playlist.")
                return

            if not should_queue and not vc.is_playing():
                play_next(guild_id, vc, bot, silent=True)

            source_label = "Spotify" if _is_spotify_url(query) else \
                           "Apple Music" if _is_apple_music_url(query) else \
                           "SoundCloud" if _is_soundcloud_url(query) else "YouTube"
            embed, art, view = await build_now_playing_embed(first_meta or {}, queued_count=len(state["queue"]), guild_id=guild_id, bot=bot)
            embed.set_footer(text=f"Queued {queued} tracks from {source_label} playlist")
            if art:
                await status.edit(content=None, embed=embed, attachments=[art], view=view)
            else:
                await status.edit(content=None, embed=embed, view=view)
            return

        # ── Single track ──────────────────────────────────────────────────────
        if _is_spotify_url(query):
            search_query, label = await resolve_spotify_to_query(query)
            if not search_query:
                await status.edit(content="Couldn't resolve that Spotify link.")
                return
            await status.edit(content=f"Found **{label}** on Spotify, downloading...")

        elif _is_apple_music_url(query):
            search_query, label = await resolve_apple_music_to_query(query)
            if not search_query:
                await status.edit(content="Couldn't resolve that Apple Music link.")
                return
            await status.edit(content=f"Found **{label}** on Apple Music, downloading...")

        elif _is_youtube_url(query):
            search_query, label = query, query
            await status.edit(content="Downloading from YouTube...")

        elif _is_soundcloud_url(query):
            search_query, label = query, query
            await status.edit(content="Downloading from SoundCloud...")

        else:
            search_query = f"ytsearch1:{query}"
            label = query
            await status.edit(content=f"Searching YouTube for **{query}**...")

        filepath, meta = await search_and_download_audio(search_query)
        if not filepath:
            await status.edit(content="Couldn't download that track.")
            return

        meta["normalize"] = normalize
        if _is_spotify_url(query):
            meta["spotify_url"] = query
        display = meta.get("title") or label
        state   = voice_states[guild_id]

        if should_queue:
            state["queue"].append((filepath, display, meta))
            embed, file, view = await build_now_playing_embed(meta, queued_count=len(state["queue"]), spotify_url=meta.get("spotify_url"), guild_id=guild_id, bot=bot)
            embed.set_footer(text=f"Added to queue • #{len(state['queue'])}")
            if file:
                await status.edit(content=None, embed=embed, attachments=[file], view=view)
            else:
                await status.edit(content=None, embed=embed, view=view)
        else:
            state["queue"].append((filepath, display, meta))
            play_next(guild_id, vc, bot, silent=True)
            embed, file, view = await build_now_playing_embed(meta, queued_count=len(state["queue"]), spotify_url=meta.get("spotify_url"), guild_id=guild_id, bot=bot)
            if file:
                await status.edit(content=None, embed=embed, attachments=[file], view=view)
            else:
                await status.edit(content=None, embed=embed, view=view)


    @spotify_cmds.command(name="skip", description="Skip the current song")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def skip(interaction: discord.Interaction):
        guild_id = interaction.guild_id
        state = voice_states.get(guild_id)

        _cancel_autoplay(guild_id)
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            if state and state.get("autoplaying"):
                state["skip_autoplay_delay"] = True
            vc.stop()
            await interaction.response.send_message("Skipped.")
        else:
            await interaction.response.send_message("Nothing is playing.")


    @spotify_cmds.command(name="stop", description="Stop playback and clear the queue")
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


    @spotify_cmds.command(name="queue", description="Show the current queue")
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


    @spotify_cmds.command(name="clearqueue", description="Clear all queued songs (keeps the current song playing)")
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


    @spotify_cmds.command(name="pause", description="Pause or resume playback")
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


    @spotify_cmds.command(name="volume", description="Set playback volume (0–200)")
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

        if not isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source = discord.PCMVolumeTransformer(vc.source, volume=level / 100)
        else:
            vc.source.volume = level / 100

        state = voice_states.get(interaction.guild_id)
        if state:
            state["volume"] = level / 100

        await interaction.response.send_message(f"Volume set to **{level}%**.")


    @spotify_cmds.command(name="loop", description="Set loop mode for playback")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.describe(mode="off = no loop, track = repeat current song, queue = repeat whole queue")
    @app_commands.choices(mode=[
        app_commands.Choice(name="off",   value="off"),
        app_commands.Choice(name="track", value="track"),
        app_commands.Choice(name="queue", value="queue"),
    ])
    async def loop(interaction: discord.Interaction, mode: str):
        guild_id = interaction.guild_id
        state = voice_states.get(guild_id)
        if not state:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return

        state["loop"] = mode
        labels = {"off": "🔁 Loop off", "track": "🔂 Looping current track", "queue": "🔁 Looping queue"}
        await interaction.response.send_message(labels[mode])


    @spotify_cmds.command(name="testplay", description="Test ffmpeg playback")
    async def testplay(interaction: discord.Interaction):
        if not interaction.user.voice:
            await interaction.response.send_message("Join a VC first")
            return

        await interaction.response.defer()

        import subprocess
        test_file = os.path.join(tempfile.gettempdir(), "test_audio.mp3")

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


    @spotify_cmds.command(name="playfile", description="Play an uploaded mp3 file in your voice channel")
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
                "loop": "off",
            }
        else:
            voice_states[guild_id]["text_channel_id"] = interaction.channel_id

        _cancel_autoplay(guild_id)

        status = await interaction.followup.send(f"Downloading `{file.filename}`...", wait=True)

        dest = os.path.join(tempfile.gettempdir(), file.filename)
        async with aiohttp.ClientSession() as session:
            async with session.get(file.url) as resp:
                with open(dest, "wb") as f:
                    f.write(await resp.read())

        title = os.path.splitext(file.filename)[0]
        meta  = {"title": title, "artist": None, "album": None, "duration": None, "thumbnail": None}

        try:
            from mutagen.id3 import ID3
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
            embed, art, view = await build_now_playing_embed(meta, guild_id=guild_id, bot=bot)
            if art:
                await status.edit(content=None, embed=embed, attachments=[art], view=view)
            else:
                await status.edit(content=None, embed=embed, view=view)
    tree.add_command(spotify_cmds)