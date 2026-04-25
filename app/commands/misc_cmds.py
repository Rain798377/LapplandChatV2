import io
import random
import secrets
import urllib.request
import aiohttp
import discord
import asyncio
import ai
from discord import app_commands
from PIL import Image, ImageDraw, ImageFont
from ai import current_mood

OWNER_ID = 955604666689921086

def setup(tree: app_commands.CommandTree, bot):

    @tree.command(name="ship", description="Ship two users and get a compatibility rating")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(user1="First user to ship", user2="Second user to ship")
    async def ship_users(interaction: discord.Interaction, user1: discord.User, user2: discord.User):
        if user1.id == user2.id:
            await interaction.response.send_message(
                "💔 You can't ship someone with themselves!", ephemeral=True
            )
            return

        seed = min(user1.id, user2.id) * max(user1.id, user2.id)
        compatibility = seed % 101

        n1, n2 = user1.display_name, user2.display_name
        ship_name = n1[:len(n1) // 2] + n2[len(n2) // 2:]

        if compatibility >= 80:
            label, color = "Soulmates 💞", 0xFF69B4
        elif compatibility >= 60:
            label, color = "Great match 💕", 0xFF8C00
        elif compatibility >= 40:
            label, color = "Could work 🤔", 0xFFD700
        elif compatibility >= 20:
            label, color = "Rough waters 😬", 0x808080
        else:
            label, color = "Disaster 💀", 0x8B0000

        filled = round(compatibility / 10)
        bar = "█" * filled + "░" * (10 - filled)

        pfp1 = user1.display_avatar.url
        pfp2 = user2.display_avatar.url

        embed = discord.Embed(
            title=f"{user1.display_name} x {user2.display_name}",
            description=f"**{label}**\n`{bar}` **{compatibility}%**\nShip name: **{ship_name}**",
            color=color
        )
        embed.set_thumbnail(url=pfp2)
        embed.set_author(name=user1.display_name, icon_url=pfp1)
        embed.set_footer(text=f"{user1.display_name} x {user2.display_name}", icon_url=pfp1)

        await interaction.response.send_message(embed=embed)

    @tree.command(name="mood", description="Check the bot's current mood")
    async def check_mood(interaction: discord.Interaction):
        await interaction.response.send_message(f"I'm currently feeling {ai.current_mood}!")

    @tree.command(name="change_mood", description="Change the bot's mood (admin only)")
    async def change_mood(interaction: discord.Interaction, mood: str):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You're not an administrator.", ephemeral=True)
            return
        ai.current_mood = mood
        await interaction.response.send_message(f"Mood changed to {mood}!")

    @tree.command(name="ping", description="Check the bot's latency")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def ping(interaction: discord.Interaction):
        latency = round(bot.latency * 1000)
        await interaction.response.send_message(f"Pong! Latency: {latency}ms")

    @tree.command(name="echo", description="Echo back your message (admin only)")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def echo(interaction: discord.Interaction, message: str):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You're not an administrator.", ephemeral=True)
            return
        await interaction.response.send_message(message)

    @tree.command(name="curl", description="Make the bot perform a GET request to a URL and return the response (admin only)")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def curl(interaction: discord.Interaction, url: str):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You're not an administrator.", ephemeral=True)
            return
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                content = await resp.text()
        await interaction.response.send_message(rf"Content from {url}:\n```{content}```")

    @tree.command(name="ip", description="Get the bot's public IP address (admin only)")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def get_ip(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
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
        if not interaction.user.id == OWNER_ID:
            await interaction.response.send_message("You're not the owner.", ephemeral=True)
            return
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode() + stderr.decode()
        if len(output) > 1900:
            output = output[:1900] + "\n...[output truncated]"
        await interaction.response.send_message(f"Output of `{command}`:\n```{output}```")

        try: # in case the command hangs, we don't want to leave a pending response forever
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            await interaction.response.send_message("Command timed out.", ephemeral=True)
            return
        
    @tree.command(name="time", description="Get the current server time")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def get_time(interaction: discord.Interaction):
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await interaction.response.send_message(f"Current server time: {now}")

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
    @app_commands.describe(
        message="The message to quote",
        author="The author's name",
        user="Tag a user to use their avatar (optional)"
    )
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
            avatar_url = user.display_avatar.url
            async with aiohttp.ClientSession() as session:
                async with session.get(avatar_url) as resp:
                    avatar_data = await resp.read()

            avatar = Image.open(io.BytesIO(avatar_data)).convert("RGBA").resize((H, H))
            fade = Image.new("L", (H, H))
            for x in range(H):
                alpha = max(0, 255 - int((x / H) * 255))
                for y in range(H):
                    fade.putpixel((x, y), alpha)
            avatar.putalpha(fade)
            img.paste(avatar, (0, 0), avatar)

        text_x = W // 2 + 50
        draw.text((text_x, H // 2 - 60), message, font=font_main, fill=(255, 255, 255), anchor="lm")
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

        font_url  = "https://github.com/google/fonts/raw/main/ofl/lato/Lato-Regular.ttf"
        font_path = "/tmp/Lato-Regular.ttf"
        try:
            urllib.request.urlretrieve(font_url, font_path)
            font_main = ImageFont.truetype(font_path, 56)
            font_sub  = ImageFont.truetype(font_path, 32)
        except Exception:
            font_main = ImageFont.load_default()
            font_sub  = ImageFont.load_default()

        avatar_url = message.author.display_avatar.with_size(512).url
        async with aiohttp.ClientSession() as session:
            async with session.get(avatar_url) as resp:
                avatar_data = await resp.read()

        avatar = Image.open(io.BytesIO(avatar_data)).convert("RGBA").resize((H, H))
        fade = Image.new("L", (H, H))
        for x in range(H):
            alpha = max(0, 220 - int((x / H) ** 1.5 * 220))
            for y in range(H):
                fade.putpixel((x, y), alpha)
        avatar.putalpha(fade)
        img.paste(avatar, (0, 0), avatar)

        text_x      = W // 2 + 80
        text_y_center = H // 2
        quote_text  = message.content or "[no text]"
        author_text = f"- {message.author.display_name}"

        bbox_main = font_main.getbbox(quote_text)
        bbox_sub  = font_sub.getbbox(author_text)
        main_h    = bbox_main[3] - bbox_main[1]
        sub_h     = bbox_sub[3]  - bbox_sub[1]
        gap       = 20
        start_y   = text_y_center - (main_h + gap + sub_h) // 2

        draw.text((text_x, start_y),               quote_text,  font=font_main, fill=(255, 255, 255))
        draw.text((text_x, start_y + main_h + gap), author_text, font=font_sub,  fill=(180, 180, 180))

        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        await interaction.followup.send(file=discord.File(buffer, filename="quote.png"))
