"""Ponto de entrada do plugin Zany (Economia + Loja + Minijogos).

* Como plugin/extensão: expõe `setup(bot)`, que regista o cog EconomyShop.
* Standalone (python __main__.py): arranca um bot próprio. O token é lido de
  DISCORD_TOKEN / TOKEN / BOT_TOKEN (variável de ambiente) ou de config.json
  ({"token": "..."}) ao lado deste ficheiro.
"""
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import discord
from discord.ext import commands

from extensions.economy_shop import EconomyShop, add_cog_safe
from extensions.zany_games import ZanyGames


async def setup(bot: commands.Bot):
    if bot.get_cog("EconomyShop") is None:
        await add_cog_safe(bot, EconomyShop(bot))
    if bot.get_cog("ZanyGames") is None:
        await add_cog_safe(bot, ZanyGames(bot))


def _find_token():
    for name in ("DISCORD_TOKEN", "TOKEN", "BOT_TOKEN"):
        if os.environ.get(name):
            return os.environ[name].strip()
    cfg = ROOT / "config.json"
    if cfg.exists():
        try:
            return json.loads(cfg.read_text(encoding="utf-8")).get("token", "").strip()
        except (OSError, ValueError):
            pass
    return None


class _StandaloneBot(commands.Bot):
    def __init__(self, intents):
        super().__init__(command_prefix=commands.when_mentioned_or("z!", "Z!"), intents=intents,
                         case_insensitive=True, strip_after_prefix=True, help_command=None)

    async def setup_hook(self):
        await setup(self)

    async def on_ready(self):
        print(f"✅ Ligado como {self.user} ({len(self.guilds)} servidores)")


async def _run(token: str):
    for use_message_content in (True, False):
        intents = discord.Intents.default()
        intents.message_content = use_message_content
        bot = _StandaloneBot(intents)
        try:
            async with bot:
                await bot.start(token)
            return
        except discord.PrivilegedIntentsRequired:
            if not use_message_content:
                raise
            print("⚠️ 'Message Content Intent' desativado no portal: comandos z! "
                  "off, mas os slash commands (/) funcionam.")


if __name__ == "__main__":
    discord.utils.setup_logging(level=logging.INFO)
    token = _find_token()
    if not token:
        print("❌ Token não encontrado. Define DISCORD_TOKEN ou cria config.json com {\"token\": \"...\"}")
        sys.exit(1)
    asyncio.run(_run(token))
