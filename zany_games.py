"""Minijogos estilo Zany: apostar, slots, crash, mines + drops automáticos + moedas por atividade.

Depende do cog EconomyShop (partilha o mesmo ficheiro de dados e saldo).
"""
from __future__ import annotations

import asyncio
import random
import time
import traceback
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from extensions.economy_shop import (
    COLOR_ERR, COLOR_GOLD, COLOR_MAIN, COLOR_OK, BaseView, EconomyShop, add_cog_safe,
    error_embed, handle_command_error, make_embed, ok_embed,
)

# ------------------------------- CONFIG ---------------------------------
MAX_BET = 50_000
DROP_MIN, DROP_MAX = 50, 300
ACTIVITY_MIN, ACTIVITY_MAX, ACTIVITY_COOLDOWN = 1, 3, 60
SLOT_SYMBOLS = {"🍒": 5, "🍋": 5, "🔔": 8, "⭐": 10, "7️⃣": 15, "💎": 25}  # prémio do trio (× aposta)
MINES_ROWS, MINES_COLS = 4, 5
MINES_EDGE = 0.97
# ------------------------------------------------------------------------


class ZanyGames(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active: set[tuple[int, int]] = set()
        self.last_msg: dict[tuple[int, int], float] = {}
        self.next_drop: dict[str, float] = {}
        self.dirty = False

    @property
    def eco(self) -> Optional[EconomyShop]:
        return self.bot.get_cog("EconomyShop")

    async def cog_load(self):
        self.drop_loop.start()
        self.flush_loop.start()

    async def cog_unload(self):
        self.drop_loop.cancel()
        self.flush_loop.cancel()

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        await handle_command_error(ctx, error)

    # ---------------------------- saldo / apostas ----------------------------
    def money(self, gid: int, n: int) -> str:
        return self.eco.money(gid, n)

    async def take(self, member: discord.Member, amount: int) -> Optional[discord.Embed]:
        """Retira a aposta. Devolve um embed de erro se não for possível."""
        eco = self.eco
        if eco is None:
            return error_embed("O módulo de economia não está carregado.")
        gid = member.guild.id
        if amount > MAX_BET:
            return error_embed(f"A aposta máxima é {self.money(gid, MAX_BET)}.")
        async with eco.store.lock:
            u = eco.store.user(gid, member.id)
            if u["balance"] < amount:
                return error_embed(f"Saldo insuficiente. Tens {self.money(gid, u['balance'])}.")
            u["balance"] -= amount
            await eco.store.save()
        return None

    async def give(self, member: discord.Member, amount: int) -> int:
        eco = self.eco
        async with eco.store.lock:
            u = eco.store.user(member.guild.id, member.id)
            u["balance"] += amount
            await eco.store.save()
            return u["balance"]

    def busy(self, member: discord.Member) -> bool:
        return (member.guild.id, member.id) in self.active

    BUSY_MSG = "Já tens um jogo a decorrer! Termina-o primeiro."

    # ------------------------------- apostar -------------------------------
    @commands.hybrid_command(name="apostar", aliases=["bet", "cf"], description="Cara ou coroa: 50/50, o dobro se acertares.")
    @commands.guild_only()
    @app_commands.describe(quantia="Quanto queres apostar", lado="Cara ou coroa")
    async def apostar(self, ctx: commands.Context, quantia: commands.Range[int, 1, 1_000_000_000],
                      lado: Literal["cara", "coroa"] = "cara"):
        if err := await self.take(ctx.author, quantia):
            return await ctx.send(embed=err, ephemeral=True)
        result = random.choice(["cara", "coroa"])
        won = result == lado
        bal = await self.give(ctx.author, quantia * 2 if won else 0)
        gid = ctx.guild.id
        if won:
            e = make_embed("🪙 Saiu " + result + "!", f"Acertaste! Ganhaste {self.money(gid, quantia)} 🎉", COLOR_OK, guild=ctx.guild)
        else:
            e = make_embed("🪙 Saiu " + result + "…", f"Escolheste **{lado}**. Perdeste {self.money(gid, quantia)}.", COLOR_ERR, guild=ctx.guild)
        e.add_field(name="Saldo atual", value=self.money(gid, bal))
        e.set_thumbnail(url=ctx.author.display_avatar.url)
        await ctx.send(embed=e)

    # -------------------------------- slots --------------------------------
    @commands.hybrid_command(name="slots", aliases=["slot", "cassino"], description="Slot machine: 3 iguais = prémio grande!")
    @commands.guild_only()
    @app_commands.describe(quantia="Quanto queres apostar")
    async def slots(self, ctx: commands.Context, quantia: commands.Range[int, 1, 1_000_000_000]):
        if err := await self.take(ctx.author, quantia):
            return await ctx.send(embed=err, ephemeral=True)
        gid = ctx.guild.id
        msg = await ctx.send(embed=make_embed("🎰 Slots", "**❓ | ❓ | ❓**\n*A girar…*", COLOR_MAIN, guild=ctx.guild))
        await asyncio.sleep(1.3)
        reels = [random.choice(list(SLOT_SYMBOLS)) for _ in range(3)]
        distinct = len(set(reels))
        mult = SLOT_SYMBOLS[reels[0]] if distinct == 1 else (1.5 if distinct == 2 else 0)
        payout = int(quantia * mult)
        bal = await self.give(ctx.author, payout)
        line = " | ".join(reels)
        if distinct == 1:
            e = make_embed("🎰 JACKPOT!", f"**{line}**\nGanhaste {self.money(gid, payout - quantia)} (×{mult})!", COLOR_OK, guild=ctx.guild)
        elif distinct == 2:
            e = make_embed("🎰 Quase!", f"**{line}**\nPar! Recebeste {self.money(gid, payout)} (×1.5).", COLOR_GOLD, guild=ctx.guild)
        else:
            e = make_embed("🎰 Sem sorte…", f"**{line}**\nPerdeste {self.money(gid, quantia)}.", COLOR_ERR, guild=ctx.guild)
        e.add_field(name="Saldo atual", value=self.money(gid, bal))
        try:
            await msg.edit(embed=e)
        except discord.HTTPException:
            await ctx.send(embed=e)

    # -------------------------------- crash --------------------------------
    @commands.hybrid_command(name="crash", description="O multiplicador sobe… retira antes de explodir! 📈")
    @commands.guild_only()
    @app_commands.describe(quantia="Quanto queres apostar")
    async def crash(self, ctx: commands.Context, quantia: commands.Range[int, 1, 1_000_000_000]):
        if self.busy(ctx.author):
            return await ctx.send(embed=error_embed(self.BUSY_MSG), ephemeral=True)
        if err := await self.take(ctx.author, quantia):
            return await ctx.send(embed=err, ephemeral=True)
        self.active.add((ctx.guild.id, ctx.author.id))
        view = CrashView(self, ctx.author, quantia)
        view.origin = await ctx.send(embed=view.embed(), view=view)
        asyncio.create_task(view.run())

    # -------------------------------- mines --------------------------------
    @commands.hybrid_command(name="mines", aliases=["minas"], description="Encontra diamantes e evita as bombas! 💣")
    @commands.guild_only()
    @app_commands.describe(quantia="Quanto queres apostar", minas="Número de bombas (1–10)")
    async def mines(self, ctx: commands.Context, quantia: commands.Range[int, 1, 1_000_000_000],
                    minas: commands.Range[int, 1, 10] = 3):
        if self.busy(ctx.author):
            return await ctx.send(embed=error_embed(self.BUSY_MSG), ephemeral=True)
        if err := await self.take(ctx.author, quantia):
            return await ctx.send(embed=err, ephemeral=True)
        self.active.add((ctx.guild.id, ctx.author.id))
        view = MinesView(self, ctx.author, quantia, minas)
        view.origin = await ctx.send(embed=view.embed(), view=view)

    # ------------------------------ drops/admin ------------------------------
    @commands.hybrid_command(name="drops", description="Configura os drops automáticos (sem canal = desativa).")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(canal="Canal onde caem os drops", minutos="Intervalo médio entre drops (5–1440)")
    async def drops(self, ctx: commands.Context, canal: Optional[discord.TextChannel] = None,
                    minutos: commands.Range[int, 5, 1440] = 30):
        eco = self.eco
        async with eco.store.lock:
            c = eco.cfg(ctx.guild.id)
            c["drop_enabled"] = canal is not None
            if canal:
                c["drop_channel"], c["drop_interval"] = canal.id, minutos
            await eco.store.save()
        self.next_drop.pop(str(ctx.guild.id), None)
        if canal:
            msg = f"Drops ativos em {canal.mention}, a cada ~**{minutos} min**."
        else:
            msg = "Drops desativados."
        await ctx.send(embed=ok_embed("🎁 Drops", msg), ephemeral=True)

    @commands.hybrid_command(name="dropagora", description="Solta um drop de moedas neste canal agora.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    async def dropagora(self, ctx: commands.Context):
        await self.send_drop(ctx.channel)
        await ctx.send(embed=ok_embed("🎁 Drop enviado", "Boa sorte aos apanhadores!"), ephemeral=True)

    @commands.hybrid_command(name="atividade", description="Liga/desliga moedas por conversar no servidor.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    async def atividade(self, ctx: commands.Context, ativo: bool):
        eco = self.eco
        async with eco.store.lock:
            eco.cfg(ctx.guild.id)["activity_enabled"] = ativo
            await eco.store.save()
        await ctx.send(embed=ok_embed("💬 Atividade", "Ativada ✅" if ativo else "Desativada ❌"), ephemeral=True)

    async def send_drop(self, channel: discord.abc.Messageable):
        amount = random.randint(DROP_MIN, DROP_MAX)
        view = DropView(self, amount)
        e = make_embed("🎁 Drop de moedas!",
                       f"Apareceu um drop de {self.money(channel.guild.id, amount)}!\n"
                       "Sê o **primeiro** a clicar no botão para o apanhares!", COLOR_GOLD, guild=channel.guild)
        view.origin = await channel.send(embed=e, view=view)

    @tasks.loop(seconds=45)
    async def drop_loop(self):
        eco = self.eco
        if eco is None:
            return
        now = time.time()
        for gid_s in list(eco.store.data["guilds"]):
            c = eco.store.guild(int(gid_s))["settings"]
            if not (c.get("drop_enabled") and c.get("drop_channel")):
                continue
            interval = max(5, int(c.get("drop_interval", 30))) * 60
            due = self.next_drop.get(gid_s)
            if due is None:
                self.next_drop[gid_s] = now + random.uniform(0.3, 1.0) * interval
                continue
            if now < due:
                continue
            self.next_drop[gid_s] = now + random.uniform(0.7, 1.3) * interval
            guild = self.bot.get_guild(int(gid_s))
            channel = guild.get_channel(c["drop_channel"]) if guild else None
            if channel:
                try:
                    await self.send_drop(channel)
                except (discord.Forbidden, discord.HTTPException):
                    pass

    @drop_loop.before_loop
    async def _before_drop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=30)
    async def flush_loop(self):
        eco = self.eco
        if eco and self.dirty:
            self.dirty = False
            async with eco.store.lock:
                await eco.store.save()

    # ------------------------------- atividade -------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or self.eco is None:
            return
        if not self.eco.cfg(message.guild.id).get("activity_enabled"):
            return
        key = (message.guild.id, message.author.id)
        now = time.time()
        if now - self.last_msg.get(key, 0) < ACTIVITY_COOLDOWN:
            return
        if len(self.last_msg) > 50_000:
            self.last_msg.clear()
        self.last_msg[key] = now
        self.eco.store.user(*key)["balance"] += random.randint(ACTIVITY_MIN, ACTIVITY_MAX)
        self.dirty = True


# ================================== DROP ==================================
class DropView(BaseView):
    def __init__(self, cog: ZanyGames, amount: int):
        super().__init__(None, timeout=180)
        self.cog, self.amount, self.claimed = cog, amount, False

    @discord.ui.button(label="Apanhar!", emoji="🎁", style=discord.ButtonStyle.success)
    async def grab(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed:
            return await interaction.response.send_message(embed=error_embed("Já foi apanhado por outra pessoa! 😢"), ephemeral=True)
        self.claimed = True
        self.stop()
        await self.cog.give(interaction.user, self.amount)
        e = make_embed("🎉 Drop apanhado!", f"{interaction.user.mention} foi o mais rápido e ganhou "
                       f"{self.cog.money(interaction.guild.id, self.amount)}!", COLOR_OK, guild=interaction.guild)
        await interaction.response.edit_message(embed=e, view=None)

    async def on_timeout(self):
        if self.claimed:
            return
        try:
            await self.origin.edit(embed=make_embed("🎁 Drop expirou", "Ninguém apanhou a tempo… 💨", COLOR_ERR), view=None)
        except discord.HTTPException:
            pass


# ================================== CRASH ==================================
class CrashView(BaseView):
    def __init__(self, cog: ZanyGames, member: discord.Member, bet: int):
        super().__init__(member.id, timeout=None)
        self.cog, self.member, self.bet = cog, member, bet
        self.key = (member.guild.id, member.id)
        r = random.random()
        self.crash_point = min(100.0, max(1.0, round(0.96 / (1 - r), 2)))
        self.n, self.mult, self.done = 0, 1.0, False
        self.lock = asyncio.Lock()

    def embed(self, title="📈 Crash", color=COLOR_MAIN, extra: str = "") -> discord.Embed:
        gid = self.member.guild.id
        e = make_embed(title, f"## {self.mult:.2f}x\n{extra}", color, guild=self.member.guild)
        e.add_field(name="Aposta", value=self.cog.money(gid, self.bet))
        e.add_field(name="Retiras agora", value=self.cog.money(gid, int(self.bet * self.mult)))
        e.set_thumbnail(url=self.member.display_avatar.url)
        return e

    def _end(self):
        self.done = True
        self.stop()
        self.cog.active.discard(self.key)

    async def run(self):
        try:
            while True:
                await asyncio.sleep(1.4)
                async with self.lock:
                    if self.done:
                        return
                    self.n += 1
                    m = round(1 + 0.1 * self.n + 0.012 * self.n ** 2, 2)
                    if m >= self.crash_point:
                        self.mult = self.crash_point
                        self._end()
                        bal = await self.cog.give(self.member, 0)
                        e = self.embed("💥 EXPLODIU!", COLOR_ERR, "Não retiraste a tempo…")
                        e.set_field_at(1, name="Perdeste", value=self.cog.money(self.member.guild.id, self.bet))
                        e.add_field(name="Saldo atual", value=self.cog.money(self.member.guild.id, bal), inline=False)
                        await self.origin.edit(embed=e, view=None)
                        return
                    self.mult = m
                    await self.origin.edit(embed=self.embed("📈 A subir…", COLOR_MAIN, "Carrega em **Retirar** antes que exploda!"))
        except discord.HTTPException:
            if not self.done:
                self._end()  # mensagem apagada: aposta perdida
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            if not self.done:
                self._end()

    @discord.ui.button(label="Retirar", emoji="💸", style=discord.ButtonStyle.success)
    async def cash(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            if self.done:
                return await interaction.response.defer()
            self._end()
            win = int(self.bet * self.mult)
            bal = await self.cog.give(self.member, win)
            e = self.embed("✅ Retiraste a tempo!", COLOR_OK, f"Lucro: {self.cog.money(self.member.guild.id, win - self.bet)}")
            e.add_field(name="Saldo atual", value=self.cog.money(self.member.guild.id, bal), inline=False)
            await interaction.response.edit_message(embed=e, view=None)


# ================================== MINES ==================================
class TileButton(discord.ui.Button):
    def __init__(self, index: int):
        super().__init__(emoji="❔", style=discord.ButtonStyle.secondary, row=index // MINES_COLS)
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        await self.view.reveal(interaction, self)


class MinesView(BaseView):
    def __init__(self, cog: ZanyGames, member: discord.Member, bet: int, n_mines: int):
        super().__init__(member.id, timeout=120)
        self.cog, self.member, self.bet, self.n_mines = cog, member, bet, n_mines
        self.key = (member.guild.id, member.id)
        self.total = MINES_ROWS * MINES_COLS
        self.mines = set(random.sample(range(self.total), n_mines))
        self.revealed: set[int] = set()
        self.done = False
        self.tiles = [TileButton(i) for i in range(self.total)]
        for t in self.tiles:
            self.add_item(t)
        self.cash.disabled = True

    def mult_for(self, k: int) -> float:
        m = 1.0
        for i in range(k):
            m *= (self.total - i) / (self.total - self.n_mines - i)
        return round(m * MINES_EDGE, 2) if k else 1.0

    @property
    def mult(self) -> float:
        return self.mult_for(len(self.revealed))

    def embed(self, title="💣 Mines", color=COLOR_MAIN, extra="") -> discord.Embed:
        gid = self.member.guild.id
        k = len(self.revealed)
        e = make_embed(title, extra or "Revela os quadrados 💎 e evita as bombas 💣.\nCarrega em **Retirar** quando quiseres.",
                       color, guild=self.member.guild)
        e.add_field(name="Aposta", value=self.cog.money(gid, self.bet))
        e.add_field(name="Bombas", value=f"💣 {self.n_mines}")
        e.add_field(name="Diamantes", value=f"💎 {k}")
        e.add_field(name="Multiplicador", value=f"**{self.mult:.2f}x**")
        e.add_field(name="Retiras agora", value=self.cog.money(gid, int(self.bet * self.mult)))
        e.set_thumbnail(url=self.member.display_avatar.url)
        return e

    def _reveal_all(self, hit: Optional[int] = None):
        for t in self.tiles:
            t.disabled = True
            if t.index in self.mines:
                t.emoji = "💣"
                t.style = discord.ButtonStyle.danger if t.index == hit else discord.ButtonStyle.secondary
            elif t.index not in self.revealed:
                t.emoji = "💎"
        self.cash.disabled = True

    async def _finish(self, interaction: Optional[discord.Interaction], *, payout: int, hit: Optional[int] = None,
                      title: str, color: int, extra: str):
        self.done = True
        self.stop()
        self.cog.active.discard(self.key)
        self._reveal_all(hit)
        bal = await self.cog.give(self.member, payout)
        e = self.embed(title, color, extra)
        e.add_field(name="Saldo atual", value=self.cog.money(self.member.guild.id, bal), inline=False)
        if interaction:
            await interaction.response.edit_message(embed=e, view=self)
        else:
            try:
                await self.origin.edit(embed=e, view=self)
            except discord.HTTPException:
                pass

    async def reveal(self, interaction: discord.Interaction, tile: TileButton):
        if self.done or tile.index in self.revealed:
            return await interaction.response.defer()
        gid = self.member.guild.id
        if tile.index in self.mines:
            return await self._finish(interaction, payout=0, hit=tile.index, title="💥 BOOM!", color=COLOR_ERR,
                                      extra=f"Pisaste uma bomba! Perdeste {self.cog.money(gid, self.bet)}.")
        self.revealed.add(tile.index)
        tile.emoji, tile.style, tile.disabled = "💎", discord.ButtonStyle.success, True
        self.cash.disabled = False
        if len(self.revealed) == self.total - self.n_mines:
            win = int(self.bet * self.mult)
            return await self._finish(interaction, payout=win, title="🏆 Tabuleiro limpo!", color=COLOR_OK,
                                      extra=f"Encontraste todos os diamantes! Ganhaste {self.cog.money(gid, win)}.")
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Retirar", emoji="💸", style=discord.ButtonStyle.success, row=4)
    async def cash(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done or not self.revealed:
            return await interaction.response.defer()
        win = int(self.bet * self.mult)
        await self._finish(interaction, payout=win, title="✅ Retiraste!", color=COLOR_OK,
                           extra=f"Lucro: {self.cog.money(self.member.guild.id, win - self.bet)}")

    async def on_timeout(self):
        if self.done:
            return
        win = int(self.bet * self.mult) if self.revealed else 0
        await self._finish(None, payout=win, title="⏰ Tempo esgotado",
                           color=COLOR_OK if win else COLOR_ERR,
                           extra="Retirada automática." if win else "Perdeste a aposta por inatividade.")


async def setup(bot: commands.Bot):
    if bot.get_cog("EconomyShop") is None:
        await add_cog_safe(bot, EconomyShop(bot))
    if bot.get_cog("ZanyGames") is None:
        await add_cog_safe(bot, ZanyGames(bot))
