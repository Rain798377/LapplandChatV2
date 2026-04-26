import secrets
import requests
import discord
import aiohttp
from app.config import *
from discord import app_commands


def setup(tree: app_commands.CommandTree):
    random_group = app_commands.Group(name="random", description="Random commands")

    @random_group.command(name="number", description="Returns a random number below a number you choose.")
    async def random_number(interaction: discord.Interaction, max_number: int):
        await interaction.response.send_message(f"Your random number is: {secrets.randbelow(max_number) + 1}")

    @random_group.command(name="coin", description="Flips a coin for you, heads or tails.")
    async def coin_flip(interaction: discord.Interaction):
        result = secrets.choice(["heads", "tails"])
        await interaction.response.send_message(f"The coin landed on: {result}")

    @random_group.command(name="die", description="Rolls a die with a specified number of sides.")
    async def roll_die(interaction: discord.Interaction, sides: int):
        result = secrets.randbelow(sides) + 1
        await interaction.response.send_message(f"You rolled a {result} on a {sides}-sided die.")

    @random_group.command(name="choice", description="Selects a random item from a list you provide.")
    @app_commands.describe(items="A comma-separated list of items to choose from.")
    async def random_choice(interaction: discord.Interaction, items: str):
        item_list = [item.strip() for item in items.split(",")]
        result = secrets.choice(item_list)
        await interaction.response.send_message(f"I choose: {result}")

    @random_group.command(name="word", description="Tells you a random word.")
    async def random_word(interaction: discord.Interaction):
        words = [
            line.strip()
            for line in requests.get(
                "https://raw.githubusercontent.com/dwyl/english-words/master/words.txt"
            ).text.splitlines()
            if len(line.strip()) <= 12
        ]
        result = secrets.choice(words)
        await interaction.response.send_message(f"Your random word is: {result}")

    @random_group.command(name="meme", description="Get a random meme")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def meme(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        subreddits = ["memes", "dankmemes", "me_irl", "AdviceAnimals", "teenagers", "shitposting"]
        sub = random.choice(subreddits)

        try:
            async with aiohttp.ClientSession(headers={"User-Agent": "discord-bot/1.0"}) as session:
                async with session.get(
                    f"https://www.reddit.com/r/{sub}/hot.json?limit=50",
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status != 200:
                        await interaction.followup.send("Couldn't fetch a meme right now.")
                        return
                    data = await resp.json()

            posts = [
                p["data"] for p in data["data"]["children"]
                if not p["data"].get("stickied")
                and not p["data"].get("is_video")
                and p["data"].get("url", "").endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))
                and not p["data"].get("over_18")
            ]

            if not posts:
                await interaction.followup.send("Couldn't find a good meme, try again.")
                return

            post = random.choice(posts[:25])  # pick from top 25 to avoid deep obscure posts

            embed = discord.Embed(
                title=post["title"][:256],
                url=f"https://reddit.com{post['permalink']}",
                color=0xFF4500,  # reddit orange
            )
            embed.set_image(url=post["url"])
            embed.set_footer(text=f"r/{sub} • 👍 {post['score']:,} • 💬 {post['num_comments']:,}")

            await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"Something went wrong: `{e}`")

    tree.add_command(random_group)
