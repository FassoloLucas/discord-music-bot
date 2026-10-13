"""Ponto de entrada do bot de música.

Carrega o token e as configurações do .env, registra os cogs e sincroniza
os slash commands.
"""
import asyncio
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID") or None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("musicbot")

intents = discord.Intents.default()
intents.voice_states = True  # necessário para o bot entrar/sair de canais de voz


class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        await self.load_extension("cogs.music")

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Slash commands sincronizados na guild %s (instantâneo)", GUILD_ID)
        else:
            await self.tree.sync()
            logger.info("Slash commands sincronizados globalmente (pode levar até 1h para propagar)")


bot = MusicBot()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Handler global: sem isso, qualquer exceção não tratada num slash
    command só aparece no log e o usuário só vê "A interação falhou",
    sem explicação nenhuma."""
    if isinstance(error, app_commands.CommandOnCooldown):
        message = f"⏳ Calma aí, espera {error.retry_after:.1f}s antes de usar de novo."
    elif isinstance(error, app_commands.CheckFailure):
        message = str(error) or "🚫 Você não tem permissão para usar esse comando."
    else:
        logger.error(
            "Erro não tratado no comando '%s'",
            getattr(interaction.command, "name", "?"),
            exc_info=error,
        )
        message = "⚠️ Algo deu errado ao executar esse comando. Já registrei no log pra investigar."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass  # a interação já expirou ou o canal sumiu; nada a fazer


@bot.event
async def on_ready():
    logger.info("Logado como %s (ID: %s)", bot.user, bot.user.id)
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name="/play")
    )


def main():
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN não definido. Copie .env.example para .env e preencha.")
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
