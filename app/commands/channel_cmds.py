import discord
from discord import app_commands
from core.channels import add_channel, remove_channel, add_idle_channel, remove_idle_channel
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

    @tree.command(name="listen-idle", description="Post here when the bot's main channel(s) go quiet, saying it's bored (admin only)")
    async def listen_idle(interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("You're not an administrator.", ephemeral=True)
            return
        if add_idle_channel(interaction.channel_id):
            await interaction.response.send_message("Alright, if things go quiet where I usually chat, I might say something here.", ephemeral=True)
        else:
            await interaction.response.send_message(f"Already doing that in {interaction.channel.mention}.", ephemeral=True)

    @tree.command(name="stop-listening-idle", description="Stop posting bored/idle lines in this channel (admin only)")
    async def stop_listening_idle(interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("You're not an administrator.", ephemeral=True)
            return
        if remove_idle_channel(interaction.channel_id):
            await interaction.response.send_message(f"Got it, no more idle/bored lines in {interaction.channel.mention}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"Wasn't doing that in {interaction.channel.mention} anyway.", ephemeral=True)
