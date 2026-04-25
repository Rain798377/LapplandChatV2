import io
import random
import secrets
import urllib.request
import aiohttp
import discord
from discord import app_commands
from PIL import Image, ImageDraw, ImageFont
from brain import current_mood


def setup(tree: app_commands.CommandTree):

    @tree.command(name="ship", description="Ship two users and get a compatibility rating")
    @app_commands.describe(user1="First user to ship", user2="Second user to ship")
    async def ship_users(interaction: discord.Interaction, user1: discord.User, user2: discord.User):
        compatibility = secrets.randbelow(101)
        await interaction.response.send_message(
            f"❤️ {user1.display_name} + {user2.display_name} = {compatibility}% compatible! ❤️"
        )

    @tree.command(name="mood", description="Check the bot's current mood")
    async def check_mood(interaction: discord.Interaction):
        import brain
        await interaction.response.send_message(f"I'm currently feeling {brain.current_mood}!")

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
        await interaction.response.send_message(f"{question}\n{random.choice(responses)}")

    @tree.command(name="quote", description="Turn a message into a quote image")
    @app_commands.describe(
        message="The message to quote",
        author="The author's name",
        user="Tag a user to use their avatar (optional)"
    )
    async def quote(interaction: discord.Interaction, message: str, author: str, user: discord.User = None):
        await interaction.response.defer()

        W, H = 1200, 400
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
