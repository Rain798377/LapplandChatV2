import discord
from discord import app_commands
from core.channels import add_channel, remove_channel
from core.permissions import is_admin


def setup(tree: app_commands.CommandTree):

    @tree.command(name="listen-here", description="Make the bot listen and reply in this channel (admin only)")
    async def listen_here(interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("You're not an administrator.", ephemeral=True)
            return
        if add_channel(interaction.channel_id):
            await interaction.response.send_message(f"Got it, I'll listen in {interaction.channel.mention} now.", ephemeral=True)
        else:
            await interaction.response.send_message(f"I'm already listening in {interaction.channel.mention}.", ephemeral=True)

    @tree.command(name="stop-listening", description="Stop the bot from listening in this channel (admin only)")
    async def stop_listening(interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("You're not an administrator.", ephemeral=True)
            return
        if remove_channel(interaction.channel_id):
            await interaction.response.send_message(f"Alright, I'll stop listening in {interaction.channel.mention}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"I wasn't listening in {interaction.channel.mention} anyway.", ephemeral=True)
