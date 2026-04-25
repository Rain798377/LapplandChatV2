import secrets
import requests
import discord
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

    tree.add_command(random_group)
