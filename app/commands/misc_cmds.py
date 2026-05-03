import io
import random
import asyncio
import os
import urllib.request
import aiohttp
import discord
import ai
import tempfile
import datetime
from fontTools.ttLib import TTFont
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
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(message="The message to quote", user="The user to quote")
    async def quote(interaction: discord.Interaction, message: str, user: discord.User):
        await interaction.response.defer()

        recorded_at = datetime.now().strftime("%B %d, %Y at %I:%M %p")

        BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
        FONTS_DIR      = os.path.join(BASE_DIR, "fonts")
        font_reg_path  = os.path.join(FONTS_DIR, "DejaVuSans.ttf")
        font_bold_path = os.path.join(FONTS_DIR, "DejaVuSans-Bold.ttf")
        noto_cjk_path  = os.path.join(FONTS_DIR, "NotoSansCJK-Regular.ttc")

        AV = 350
        W  = AV + 500
        H  = AV

        PAD_Y     = 40
        TEXT_X    = AV + 40
        ACCENT_X  = TEXT_X - 16
        max_width = W - TEXT_X - 30

        def is_cjk_char(cp):
            return (
                0x1100  <= cp <= 0x11FF  or
                0x2E80  <= cp <= 0x2EFF  or
                0x2F00  <= cp <= 0x2FDF  or
                0x3000  <= cp <= 0x9FFF  or
                0xA000  <= cp <= 0xA4CF  or
                0xAC00  <= cp <= 0xD7AF  or
                0xF900  <= cp <= 0xFAFF  or
                0xFE10  <= cp <= 0xFE1F  or
                0xFE30  <= cp <= 0xFE4F  or
                0xFF00  <= cp <= 0xFFEF  or
                0x20000 <= cp <= 0x2FA1F
            )

        def build_cmap(path, index=0):
            try:
                tt = TTFont(path, fontNumber=index)
                cmap = tt.getBestCmap()
                if not cmap:
                    return set()
                glyph_order = tt.getGlyphOrder()
                notdef = glyph_order[0] if glyph_order else '.notdef'
                return set(cp for cp, name in cmap.items() if name != notdef)
            except Exception as e:
                print(f"[quote] build_cmap failed for {path} index={index}: {e}")
                return set()

        def make_fonts(size_quote, size_name, size_handle):
            fq  = ImageFont.truetype(font_reg_path,  size_quote)
            fn  = ImageFont.truetype(font_bold_path, size_name)
            fh  = ImageFont.truetype(font_reg_path,  size_handle)
            fc  = ImageFont.truetype(noto_cjk_path,  size_quote,  index=0)
            fcn = ImageFont.truetype(noto_cjk_path,  size_name,   index=0)
            fch = ImageFont.truetype(noto_cjk_path,  size_handle, index=0)
            return fq, fn, fh, fc, fcn, fch

        def build_fallback_list(font_quote, font_cjk, size_quote):
            fl = [(font_quote, build_cmap(font_reg_path))]
            for fname in ["DejaVuSerif.ttf", "DejaVuSansMono.ttf"]:
                p = os.path.join(FONTS_DIR, fname)
                if os.path.exists(p):
                    fl.append((ImageFont.truetype(p, size_quote), build_cmap(p)))
            fl.append((font_cjk, build_cmap(noto_cjk_path, index=0)))
            return fl

        def best_font_for(ch, font_quote, font_cjk, fallback_list):
            cp = ord(ch)
            if is_cjk_char(cp):
                return font_cjk
            for pil_font, cmap_set in fallback_list[:-1]:
                if cp in cmap_set:
                    return pil_font
            return font_cjk

        def line_width(text, font_quote, font_cjk, fallback_list):
            w = 0
            for ch in text:
                f = best_font_for(ch, font_quote, font_cjk, fallback_list)
                bbox = f.getbbox(ch)
                w += bbox[2] - bbox[0]
            return w

        def wrap_text(text, font_quote, font_cjk, fallback_list, max_w, max_lines=4):
            words = text.split()
            lines, cur = [], ""
            for word in words:
                test = (cur + " " + word).strip()
                if line_width(test, font_quote, font_cjk, fallback_list) <= max_w:
                    cur = test
                else:
                    if cur:
                        lines.append(cur)
                    cur = word
                    if len(lines) == max_lines:
                        break
            if cur and len(lines) < max_lines:
                lines.append(cur)
            if len(lines) == max_lines and line_width(lines[-1], font_quote, font_cjk, fallback_list) > max_w:
                while line_width(lines[-1] + "…", font_quote, font_cjk, fallback_list) > max_w:
                    lines[-1] = lines[-1][:-1]
                lines[-1] += "…"
            return lines

        def get_baseline_offset(font_a, font_b):
            return font_a.getbbox("A")[1] - font_b.getbbox("A")[1]

        # ── Dynamic font sizing ───────────────────────────────────────────────
        size_quote  = 36
        size_name   = 22
        size_handle = 17
        MIN_SIZE    = 18

        while size_quote >= MIN_SIZE:
            font_quote, font_name, font_handle, font_cjk, font_cjk_name, font_cjk_handle = make_fonts(size_quote, size_name, size_handle)
            fallback_list = build_fallback_list(font_quote, font_cjk, size_quote)
            wrapped_lines = wrap_text(message, font_quote, font_cjk, fallback_list, max_width)
            if all(line_width(l, font_quote, font_cjk, fallback_list) <= max_width for l in wrapped_lines):
                break
            size_quote  -= 2
            size_name   = max(12, size_name  - 1)
            size_handle = max(10, size_handle - 1)

        # ── Fetch avatar if user provided ──────────────────────────────────────
        avatar_data = None
        if user:
            async with aiohttp.ClientSession() as session:
                async with session.get(user.display_avatar.with_size(512).url) as resp:
                    avatar_data = await resp.read()

        # ── Canvas ────────────────────────────────────────────────────────────
        img  = Image.new("RGB", (W, H), (10, 10, 15))
        draw = ImageDraw.Draw(img)

        if avatar_data:
            avatar = Image.open(io.BytesIO(avatar_data)).convert("RGBA").resize((AV, AV))
            fade = Image.new("L", (AV, AV), 0)
            fd = ImageDraw.Draw(fade)
            for x in range(AV):
                t = x / AV
                alpha = int(255 * max(0.0, 1.0 - t ** 1.4))
                fd.line([(x, 0), (x, AV - 1)], fill=alpha)
            avatar.putalpha(fade)
            img.paste(avatar, (0, 0), avatar)

        draw.rectangle([ACCENT_X, PAD_Y, ACCENT_X + 3, H - PAD_Y],
                       fill=(255, 255, 255))

        # ── Vertically center text block ──────────────────────────────────────
        line_h   = font_quote.getbbox("Ag")[3] + 10
        name_h   = font_name.getbbox("Ag")[3] + 4
        recorded_h = font_handle.getbbox("Ag")[3] + 6
        handle_h = font_handle.getbbox("Ag")[3]
        gap      = 14
        block_h  = line_h * len(wrapped_lines) + gap + name_h + handle_h + recorded_h
        text_y   = (H - block_h) // 2

        cjk_offset_quote  = get_baseline_offset(font_quote,  font_cjk)
        cjk_offset_name   = get_baseline_offset(font_name,   font_cjk_name)
        cjk_offset_handle = get_baseline_offset(font_handle, font_cjk_handle)

        # ── Draw functions ────────────────────────────────────────────────────
        def draw_mixed_line(draw, x, y, text, fill):
            cx = x
            for ch in text:
                f = best_font_for(ch, font_quote, font_cjk, fallback_list)
                offset = cjk_offset_quote if f is font_cjk else 0
                draw.text((cx, y + offset), ch, font=f, fill=fill)
                bbox = f.getbbox(ch)
                cx += bbox[2] - bbox[0]

        def draw_mixed_line_sized(draw, x, y, text, fill, font_default, cjk_font, cjk_offset):
            cx = x
            for ch in text:
                cp = ord(ch)
                if is_cjk_char(cp):
                    chosen = cjk_font
                    offset = cjk_offset
                else:
                    chosen = font_default
                    offset = 0
                    for pil_font, cmap_set in fallback_list[:-1]:
                        if cp in cmap_set:
                            break
                    else:
                        chosen = cjk_font
                        offset = cjk_offset
                draw.text((cx, y + offset), ch, font=chosen, fill=fill)
                bbox = chosen.getbbox(ch)
                cx += bbox[2] - bbox[0]

        for i, line in enumerate(wrapped_lines):
            draw_mixed_line(draw, TEXT_X, text_y + i * line_h, line, (255, 255, 255))

        author_y = text_y + line_h * len(wrapped_lines) + gap
        draw_mixed_line_sized(draw, TEXT_X, author_y,
                              f"— {user.display_name}",
                              (210, 210, 215), font_name, font_cjk_name, cjk_offset_name)
        draw_mixed_line_sized(draw, TEXT_X, author_y + name_h,
                              f"@{user.name}",
                              (130, 130, 140), font_handle, font_cjk_handle, cjk_offset_handle)
        draw_mixed_line_sized(draw, TEXT_X, author_y + name_h + handle_h + 4,
                              recorded_at,
                              (90, 90, 100), font_handle, font_cjk_handle, cjk_offset_handle)
        # ── Send ──────────────────────────────────────────────────────────────
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        await interaction.followup.send(file=discord.File(buffer, filename="quote.png"))

    @tree.context_menu(name="Make Quote")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def make_quote(interaction: discord.Interaction, message: discord.Message):
        await interaction.response.defer()

        recorded_at = message.created_at.strftime("%B %d, %Y at %I:%M %p")

        BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
        FONTS_DIR      = os.path.join(BASE_DIR, "fonts")
        font_reg_path  = os.path.join(FONTS_DIR, "DejaVuSans.ttf")
        font_bold_path = os.path.join(FONTS_DIR, "DejaVuSans-Bold.ttf")
        noto_cjk_path  = os.path.join(FONTS_DIR, "NotoSansCJK-Regular.ttc")

        AV = 350
        W  = AV + 500
        H  = AV

        PAD_Y     = 40
        TEXT_X    = AV + 40
        ACCENT_X  = TEXT_X - 16
        max_width = W - TEXT_X - 30

        def is_cjk_char(cp):
            return (
                0x1100  <= cp <= 0x11FF  or
                0x2E80  <= cp <= 0x2EFF  or
                0x2F00  <= cp <= 0x2FDF  or
                0x3000  <= cp <= 0x9FFF  or
                0xA000  <= cp <= 0xA4CF  or
                0xAC00  <= cp <= 0xD7AF  or
                0xF900  <= cp <= 0xFAFF  or
                0xFE10  <= cp <= 0xFE1F  or
                0xFE30  <= cp <= 0xFE4F  or
                0xFF00  <= cp <= 0xFFEF  or
                0x20000 <= cp <= 0x2FA1F
            )

        def build_cmap(path, index=0):
            try:
                tt = TTFont(path, fontNumber=index)
                cmap = tt.getBestCmap()
                if not cmap:
                    return set()
                glyph_order = tt.getGlyphOrder()
                notdef = glyph_order[0] if glyph_order else '.notdef'
                return set(cp for cp, name in cmap.items() if name != notdef)
            except Exception as e:
                print(f"[quote] build_cmap failed for {path} index={index}: {e}")
                return set()

        def make_fonts(size_quote, size_name, size_handle):
            fq  = ImageFont.truetype(font_reg_path,  size_quote)
            fn  = ImageFont.truetype(font_bold_path, size_name)
            fh  = ImageFont.truetype(font_reg_path,  size_handle)
            fc  = ImageFont.truetype(noto_cjk_path,  size_quote,  index=0)
            fcn = ImageFont.truetype(noto_cjk_path,  size_name,   index=0)
            fch = ImageFont.truetype(noto_cjk_path,  size_handle, index=0)
            return fq, fn, fh, fc, fcn, fch

        def build_fallback_list(font_quote, font_cjk, size_quote):
            fl = [(font_quote, build_cmap(font_reg_path))]
            for fname in ["DejaVuSerif.ttf", "DejaVuSansMono.ttf"]:
                p = os.path.join(FONTS_DIR, fname)
                if os.path.exists(p):
                    fl.append((ImageFont.truetype(p, size_quote), build_cmap(p)))
            fl.append((font_cjk, build_cmap(noto_cjk_path, index=0)))
            return fl

        def best_font_for(ch, font_quote, font_cjk, fallback_list):
            cp = ord(ch)
            if is_cjk_char(cp):
                return font_cjk
            for pil_font, cmap_set in fallback_list[:-1]:
                if cp in cmap_set:
                    return pil_font
            return font_cjk

        def line_width(text, font_quote, font_cjk, fallback_list):
            w = 0
            for ch in text:
                f = best_font_for(ch, font_quote, font_cjk, fallback_list)
                bbox = f.getbbox(ch)
                w += bbox[2] - bbox[0]
            return w

        def wrap_text(text, font_quote, font_cjk, fallback_list, max_w, max_lines=4):
            words = text.split()
            lines, cur = [], ""
            for word in words:
                test = (cur + " " + word).strip()
                if line_width(test, font_quote, font_cjk, fallback_list) <= max_w:
                    cur = test
                else:
                    if cur:
                        lines.append(cur)
                    cur = word
                    if len(lines) == max_lines:
                        break
            if cur and len(lines) < max_lines:
                lines.append(cur)
            if len(lines) == max_lines and line_width(lines[-1], font_quote, font_cjk, fallback_list) > max_w:
                while line_width(lines[-1] + "…", font_quote, font_cjk, fallback_list) > max_w:
                    lines[-1] = lines[-1][:-1]
                lines[-1] += "…"
            return lines

        def get_baseline_offset(font_a, font_b):
            return font_a.getbbox("A")[1] - font_b.getbbox("A")[1]

        # ── Dynamic font sizing ───────────────────────────────────────────────
        # Start at max size, shrink until text fits in 4 lines or size floor hit
        quote_text = message.content or "[no text content]"
        size_quote = 36
        size_name  = 22
        size_handle= 17
        MIN_SIZE   = 18

        while size_quote >= MIN_SIZE:
            font_quote, font_name, font_handle, font_cjk, font_cjk_name, font_cjk_handle = make_fonts(size_quote, size_name, size_handle)
            fallback_list = build_fallback_list(font_quote, font_cjk, size_quote)
            wrapped_lines = wrap_text(quote_text, font_quote, font_cjk, fallback_list, max_width)
            # check if longest line fits and we have 4 or fewer lines
            if all(line_width(l, font_quote, font_cjk, fallback_list) <= max_width for l in wrapped_lines):
                break
            size_quote  -= 2
            size_name   = max(12, size_name  - 1)
            size_handle = max(10, size_handle - 1)

        # ── Fetch avatar ───────────────────────────────────────────────────────
        async with aiohttp.ClientSession() as session:
            async with session.get(message.author.display_avatar.with_size(512).url) as resp:
                avatar_data = await resp.read()

        # ── Canvas ────────────────────────────────────────────────────────────
        img  = Image.new("RGB", (W, H), (10, 10, 15))
        draw = ImageDraw.Draw(img)

        avatar = Image.open(io.BytesIO(avatar_data)).convert("RGBA").resize((AV, AV))
        fade = Image.new("L", (AV, AV), 0)
        fd = ImageDraw.Draw(fade)
        for x in range(AV):
            t = x / AV
            alpha = int(255 * max(0.0, 1.0 - t ** 1.4))
            fd.line([(x, 0), (x, AV - 1)], fill=alpha)
        avatar.putalpha(fade)
        img.paste(avatar, (0, 0), avatar)

        draw.rectangle([ACCENT_X, PAD_Y, ACCENT_X + 3, H - PAD_Y],
                       fill=(255, 255, 255))

        # ── Vertically center text block ──────────────────────────────────────
        line_h   = font_quote.getbbox("Ag")[3] + 10
        name_h   = font_name.getbbox("Ag")[3] + 4
        recorded_h = font_handle.getbbox("Ag")[3] + 6
        handle_h = font_handle.getbbox("Ag")[3]
        gap      = 14
        block_h  = line_h * len(wrapped_lines) + gap + name_h + handle_h + recorded_h
        text_y   = (H - block_h) // 2

        cjk_offset_quote  = get_baseline_offset(font_quote,  font_cjk)
        cjk_offset_name   = get_baseline_offset(font_name,   font_cjk_name)
        cjk_offset_handle = get_baseline_offset(font_handle, font_cjk_handle)

        # ── Draw functions ────────────────────────────────────────────────────
        def draw_mixed_line(draw, x, y, text, fill):
            cx = x
            for ch in text:
                f = best_font_for(ch, font_quote, font_cjk, fallback_list)
                offset = cjk_offset_quote if f is font_cjk else 0
                draw.text((cx, y + offset), ch, font=f, fill=fill)
                bbox = f.getbbox(ch)
                cx += bbox[2] - bbox[0]

        def draw_mixed_line_sized(draw, x, y, text, fill, font_default, cjk_font, cjk_offset):
            cx = x
            for ch in text:
                cp = ord(ch)
                if is_cjk_char(cp):
                    chosen = cjk_font
                    offset = cjk_offset
                else:
                    chosen = font_default
                    offset = 0
                    for pil_font, cmap_set in fallback_list[:-1]:
                        if cp in cmap_set:
                            break
                    else:
                        chosen = cjk_font
                        offset = cjk_offset
                draw.text((cx, y + offset), ch, font=chosen, fill=fill)
                bbox = chosen.getbbox(ch)
                cx += bbox[2] - bbox[0]

        for i, line in enumerate(wrapped_lines):
            draw_mixed_line(draw, TEXT_X, text_y + i * line_h, line, (255, 255, 255))

        author_y = text_y + line_h * len(wrapped_lines) + gap
        draw_mixed_line_sized(draw, TEXT_X, author_y,
                              f"— {message.author.display_name}",
                              (210, 210, 215), font_name, font_cjk_name, cjk_offset_name)
        draw_mixed_line_sized(draw, TEXT_X, author_y + name_h,
                              f"@{message.author.name}",
                              (130, 130, 140), font_handle, font_cjk_handle, cjk_offset_handle)
        draw_mixed_line_sized(draw, TEXT_X, author_y + name_h + handle_h + 4,
                              recorded_at,
                              (90, 90, 100), font_handle, font_cjk_handle, cjk_offset_handle)

        # ── Send ──────────────────────────────────────────────────────────────
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        await interaction.followup.send(file=discord.File(buffer, filename="quote.png"))