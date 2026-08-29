import io
import random
import asyncio
import aiohttp
import discord
from core import ai, llm
from datetime import datetime
from discord import app_commands
from core.config import BOT_OWNER_ID, MOODS
from core.llm import DEFAULT_PROVIDER_CHAIN
from core.permissions import is_admin
from core.quote_image import render_quote


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

    @tree.command(name="change_mood", description="Change the bot's mood")
    @app_commands.describe(
        mood="Pick a preset mood",
        custom_mood="Set a custom mood not in the preset list (owner only)",
    )
    @app_commands.choices(mood=[
        app_commands.Choice(name=m.capitalize(), value=m) for m in MOODS
    ])
    async def change_mood(
        interaction: discord.Interaction,
        mood: app_commands.Choice[str] = None,
        custom_mood: str = None,
    ):
        if custom_mood:
            if interaction.user.id != BOT_OWNER_ID:
                await interaction.response.send_message("Only the owner can set a custom mood.", ephemeral=True)
                return
            new_mood = custom_mood
        elif mood is not None:
            new_mood = mood.value
        else:
            await interaction.response.send_message("Pick a mood or provide a custom_mood.", ephemeral=True)
            return
        ai.current_mood = new_mood
        await interaction.response.send_message(f"Mood changed to {new_mood}!")

    @tree.command(name="ai-provider", description="Force the AI onto one provider, or back to auto fallback (admin only)")
    @app_commands.describe(provider="Which provider to force, or auto for normal fallback behavior")
    @app_commands.choices(provider=[
        app_commands.Choice(name="Auto (normal fallback chain)", value="auto"),
        *[app_commands.Choice(name=name.capitalize(), value=name) for name in DEFAULT_PROVIDER_CHAIN],
    ])
    async def ai_provider(interaction: discord.Interaction, provider: app_commands.Choice[str]):
        if not is_admin(interaction):
            await interaction.response.send_message("You're not an administrator.", ephemeral=True)
            return

        if provider.value == "auto":
            llm.set_forced_provider(None)
            await interaction.response.send_message("Back to normal fallback routing (auto).", ephemeral=True)
            return

        if llm.is_provider_disabled(provider.value):
            await interaction.response.send_message(
                f"Can't force {provider.name} -- it's disabled (no API key configured, or its key already failed auth).",
                ephemeral=True,
            )
            return

        llm.set_forced_provider(provider.value)
        await interaction.response.send_message(f"AI provider forced to **{provider.name}**.", ephemeral=True)

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
        if interaction.user.id != BOT_OWNER_ID:
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
        prefix, suffix, truncated_note = f"Output of `{command}`:\n```", "```", "\n...[output truncated]"
        budget = 2000 - len(prefix) - len(suffix)  # Discord's hard 2000-char message cap
        if len(output) > budget:
            output = output[:budget - len(truncated_note)] + truncated_note
        await interaction.followup.send(f"{prefix}{output}{suffix}")

    @tree.command(name="time", description="Get the current server time")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def get_time(interaction: discord.Interaction):
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

        avatar_data = None
        if user:
            async with aiohttp.ClientSession() as session:
                async with session.get(user.display_avatar.with_size(512).url) as resp:
                    avatar_data = await resp.read()

        buffer = render_quote(message, user.display_name, user.name, recorded_at, avatar_data)
        await interaction.followup.send(file=discord.File(buffer, filename="quote.png"))

    @tree.context_menu(name="Make Quote")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def make_quote(interaction: discord.Interaction, message: discord.Message):
        await interaction.response.defer()

        recorded_at = message.created_at.strftime("%B %d, %Y at %I:%M %p")
        quote_text = message.content or "[no text content]"

        async with aiohttp.ClientSession() as session:
            async with session.get(message.author.display_avatar.with_size(512).url) as resp:
                avatar_data = await resp.read()

        buffer = render_quote(quote_text, message.author.display_name, message.author.name, recorded_at, avatar_data)
        await interaction.followup.send(file=discord.File(buffer, filename="quote.png"))