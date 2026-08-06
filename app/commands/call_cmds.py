import discord
from discord import app_commands
from discord.ext import voice_recv
from services.voice_call.pipeline import start_session, end_session, sessions


def setup(tree: app_commands.CommandTree, bot: discord.Client):

    @tree.command(name="call", description="Join your voice channel and start a live voice conversation")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def call(interaction: discord.Interaction):
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("You need to be in a voice channel first.", ephemeral=True)
            return

        channel = interaction.user.voice.channel
        guild_id = interaction.guild_id

        vc = interaction.guild.voice_client
        if vc is None:
            vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
        elif vc.channel != channel:
            await vc.move_to(channel)

        if guild_id in sessions:
            end_session(guild_id)

        start_session(guild_id, vc, interaction.channel_id)
        await interaction.response.send_message(f"Joined **{channel.name}** — listening.")

    @tree.command(name="endcall", description="Leave the voice channel and end the conversation")
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def endcall(interaction: discord.Interaction):
        guild_id = interaction.guild_id
        if guild_id not in sessions:
            await interaction.response.send_message("Not in a call.", ephemeral=True)
            return

        end_session(guild_id)
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
            await vc.disconnect()
        await interaction.response.send_message("Left the call.")
