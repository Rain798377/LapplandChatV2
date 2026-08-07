import discord
from core.config import BOT_OWNER_ID


def is_admin(interaction: discord.Interaction) -> bool:
    # Owner can always use admin commands (including in DMs)
    if interaction.user.id == BOT_OWNER_ID:
        return True
    # In DMs, no guild = no admin check possible
    if interaction.guild is None:
        return False
    return interaction.user.guild_permissions.administrator
