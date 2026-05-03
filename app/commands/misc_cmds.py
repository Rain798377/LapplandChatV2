import io
import random
import asyncio
import os
import urllib.request
import aiohttp
import discord
import ai
import tempfile
from discord import app_commands
from PIL import Image, ImageDraw, ImageFont

OWNER_ID = 955604666689921086


def is_admin(interaction: discord.Interaction) -> bool:
    # Owner can always use admin commands (including in DMs)
    if interaction.user.id == OWNER_ID:
        return True
    # In DMs, no guild = no admin check possible
    if interaction.guild is None:
        return False
    return interaction.user.guild_permissions.administrator


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
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def make_quote(interaction: discord.Interaction, message: discord.Message):
        await interaction.response.defer()

        W, H = 1200, 400
        PADDING = 60
        ACCENT_W = 5

        # ── Fonts (Noto Sans = full Unicode/emoji support, no boxes) ──────────
        font_regular = "/tmp/NotoSans-Regular.ttf"
        font_bold    = "/tmp/NotoSans-Bold.ttf"
        try:
            if not os.path.exists(font_regular):
                urllib.request.urlretrieve(
                    "https://github.com/google/fonts/raw/main/ofl/notosans/NotoSans%5Bwdth%2Cwght%5D.ttf",
                    font_regular,
                )
            if not os.path.exists(font_bold):
                import shutil as _shutil
                _shutil.copy(font_regular, font_bold)
            font_quote  = ImageFont.truetype(font_regular, 48)
            font_name   = ImageFont.truetype(font_bold,    28)
            font_handle = ImageFont.truetype(font_regular, 22)
        except Exception:
            font_quote  = ImageFont.load_default()
            font_name   = ImageFont.load_default()
            font_handle = ImageFont.load_default()

        # ── Fetch avatar ───────────────────────────────────────────────────────
        async with aiohttp.ClientSession() as session:
            async with session.get(message.author.display_avatar.with_size(512).url) as resp:
                avatar_data = await resp.read()

        # ── Canvas ────────────────────────────────────────────────────────────
        img  = Image.new("RGB", (W, H), (10, 10, 15))
        draw = ImageDraw.Draw(img)

        # Avatar with smooth cubic fade left→right
        avatar = Image.open(io.BytesIO(avatar_data)).convert("RGBA").resize((H, H))
        fade = Image.new("L", (H, H), 0)
        fade_draw = ImageDraw.Draw(fade)
        for x in range(H):
            t = x / H
            alpha = int(255 * max(0.0, 1 - t ** 1.8))
            fade_draw.line([(x, 0), (x, H - 1)], fill=alpha)
        avatar.putalpha(fade)
        img.paste(avatar, (0, 0), avatar)

        # Vignette
        vignette = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        vd = ImageDraw.Draw(vignette)
        for i in range(80):
            vd.rectangle([i, i, W - i, H - i], outline=(0, 0, 0, int(i * 1.8)))
        img.paste(vignette, (0, 0), vignette)

        # Accent bar left of text
        text_start_x = H // 2 + 20
        bar_x = text_start_x - 20
        draw.rectangle([bar_x, PADDING, bar_x + ACCENT_W, H - PADDING], fill=(255, 255, 255))

        # ── Word-wrap (max 38 chars/line, 3 lines) ────────────────────────────
        quote_text = message.content or "[no text content]"
        max_chars  = 38
        words      = quote_text.split()
        lines, cur = [], ""
        for word in words:
            test = (cur + " " + word).strip()
            if len(test) <= max_chars:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = word
                if len(lines) >= 3:
                    break
        if cur and len(lines) < 4:
            lines.append(cur)
        if len(lines) > 3:
            lines[2] = lines[2][:max_chars - 3] + "..."
            lines = lines[:3]
        wrapped = "\n".join(lines)

        # ── Vertically centered text block ────────────────────────────────────
        line_h   = font_quote.getbbox("Ag")[3] + 8
        name_h   = font_name.getbbox("Ag")[3] + 4
        handle_h = font_handle.getbbox("Ag")[3]
        block_h  = line_h * len(lines) + 16 + name_h + handle_h
        text_y   = (H - block_h) // 2
        text_x   = text_start_x + PADDING // 2

        draw.multiline_text((text_x, text_y), wrapped, font=font_quote,
                            fill=(255, 255, 255), spacing=8)

        author_y = text_y + line_h * len(lines) + 16
        draw.text((text_x, author_y),              f"— {message.author.display_name}",
                  font=font_name,   fill=(220, 220, 220))
        draw.text((text_x, author_y + name_h + 2), f"@{message.author.name}",
                  font=font_handle, fill=(140, 140, 150))

        # ── Send ──────────────────────────────────────────────────────────────
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        await interaction.followup.send(file=discord.File(buffer, filename="quote.png"))