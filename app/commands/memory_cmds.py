import json
import tempfile
import discord
from discord import app_commands
from memory import load_memory, save_memory


class EditMemoryModal(discord.ui.Modal, title="Edit Your Memory"):
    def __init__(self, user_id: str, current_notes: str, memory: dict):
        super().__init__()
        self.user_id = user_id
        self.memory = memory
        self.notes = discord.ui.TextInput(
            label="Your notes",
            style=discord.TextStyle.paragraph,
            default=current_notes,
            max_length=500,
        )
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction):
        self.memory[self.user_id]["notes"] = self.notes.value
        save_memory(self.memory)
        await interaction.response.send_message("memory updated", ephemeral=True)


def setup(tree: app_commands.CommandTree):
    memory_group = app_commands.Group(name="memory", description="Memory related commands")

    @memory_group.command(name="wipe-all", description="Wipe all memory the bot has (admin only)")
    async def wipe_memory(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You're not an administrator.", ephemeral=True)
            return
        save_memory({})
        await interaction.response.send_message("All memory wiped.", ephemeral=True)

    @memory_group.command(name="wipe", description="Wipe your memory from the bot")
    async def wipe_my_memory(interaction: discord.Interaction):
        memory = load_memory()
        user_id = str(interaction.user.id)
        if user_id in memory:
            del memory[user_id]
            save_memory(memory)
            await interaction.response.send_message("Your memory has been wiped.", ephemeral=True)
        else:
            await interaction.response.send_message("I don't have anything on you.", ephemeral=True)

    @memory_group.command(name="edit", description="Edit what the bot remembers about you")
    async def change_my_memory(interaction: discord.Interaction):
        memory = load_memory()
        user_id = str(interaction.user.id)
        if user_id not in memory:
            await interaction.response.send_message("I don't have any memory of you yet.", ephemeral=True)
            return
        await interaction.response.send_modal(EditMemoryModal(user_id, memory[user_id]["notes"], memory))

    @memory_group.command(name="view", description="Get what the bot remembers about you")
    @app_commands.describe(format="File format to return (json or txt)")
    @app_commands.choices(format=[
        app_commands.Choice(name="json", value="json"),
        app_commands.Choice(name="txt",  value="txt"),
    ])
    async def my_memory(interaction: discord.Interaction, format: str = "txt"):
        memory = load_memory()
        user_id = str(interaction.user.id)
        entry = memory.get(user_id)
        if not entry:
            await interaction.response.send_message("I don't have anything on you yet", ephemeral=True)
            return
        display_name = entry["display_name"]
        notes = entry["notes"]
        with tempfile.TemporaryDirectory() as tmpdir:
            import os
            if format == "json":
                filepath = os.path.join(tmpdir, f"{display_name}_memory.json")
                with open(filepath, "w") as f:
                    json.dump({"user_id": user_id, "display_name": display_name, "notes": notes}, f, indent=2)
            else:
                filepath = os.path.join(tmpdir, f"{display_name}_memory.txt")
                with open(filepath, "w") as f:
                    f.write(f"memory log for {display_name}\n\n{notes}")
            await interaction.response.send_message(
                file=discord.File(filepath, os.path.basename(filepath)),
                ephemeral=True
            )

    tree.add_command(memory_group)
