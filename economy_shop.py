"""Economia + Loja com painéis interativos (discord.py 2.3+).

Carregar:  await bot.load_extension("extensions.economy_shop")
Dados:     data/economy.json (criado automaticamente)
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import time
import traceback
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

# ------------------------------- CONFIG ---------------------------------
DATA_DIR = Path(os.environ.get("YOURBOT_DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
DATA_FILE = DATA_DIR / "economy.json"
AUTO_SYNC = True          # sincroniza os slash commands sozinho ao arrancar
PAGE_SIZE = 5             # itens por página na loja
MAX_BET = 10_000

COLOR_MAIN, COLOR_GOLD, COLOR_OK, COLOR_ERR = 0x9B59B6, 0xF1C40F, 0x2ECC71, 0xE74C3C

DEFAULTS = {
    "emoji": "🪙", "name": "moedas", "daily": 100, "work_min": 20, "work_max": 60,
    "daily_cd": 24 * 3600, "work_cd": 3600, "log_channel": None,
    "drop_channel": None, "drop_interval": 30, "drop_enabled": False, "activity_enabled": True,
}
WORK_LINES = [
    "Entregaste encomendas pela cidade", "Ajudaste num café durante o turno da manhã",
    "Programaste um site para um cliente", "Fizeste um freelance de design",
    "Reparaste computadores na loja da esquina", "Deste explicações a um colega",
    "Organizaste o armazém de um vizinho", "Tocaste guitarra na rua e ganhaste gorjetas",
]
# ------------------------------------------------------------------------

CUSTOM_EMOJI_RE = re.compile(r"^<a?:\w{2,32}:\d{15,25}>$")


def clean_emoji(raw: Optional[str], default: str) -> str:
    raw = (raw or "").strip()
    if CUSTOM_EMOJI_RE.match(raw):
        return raw
    if raw and len(raw) <= 8 and not any(c.isascii() and c.isalnum() for c in raw):
        return raw
    return default


def fmt(n: int) -> str:
    return f"{int(n):,}".replace(",", ".")


def rel(ts: float) -> str:
    return f"<t:{int(ts)}:R>"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def make_embed(title: str, desc: Optional[str] = None, color: int = COLOR_MAIN, *,
               guild: Optional[discord.Guild] = None, thumb: Optional[str] = None) -> discord.Embed:
    e = discord.Embed(title=title, description=desc, color=color, timestamp=discord.utils.utcnow())
    if guild:
        e.set_footer(text=f"Zany • {guild.name}", icon_url=guild.icon.url if guild.icon else None)
    if thumb:
        e.set_thumbnail(url=thumb)
    return e


def error_embed(msg: str) -> discord.Embed:
    return make_embed("❌ Ups!", msg, COLOR_ERR)


def ok_embed(title: str, msg: str) -> discord.Embed:
    return make_embed(title, msg, COLOR_OK)


# =============================== ARMAZENAMENTO ===============================
class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.data: dict = {"guilds": {}}
        self._load()

    def _load(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except (json.JSONDecodeError, OSError):
            os.replace(self.path, self.path.with_suffix(f".corrupt-{int(time.time())}.json"))
            self.data = {"guilds": {}}

    def _save_sync(self):
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    async def save(self):
        await asyncio.to_thread(self._save_sync)

    def guild(self, gid: int) -> dict:
        g = self.data["guilds"].setdefault(str(gid), {})
        g.setdefault("users", {})
        g.setdefault("shop", {})
        s = g.setdefault("settings", {})
        for k, v in DEFAULTS.items():
            s.setdefault(k, v)
        return g

    def user(self, gid: int, uid: int) -> dict:
        u = self.guild(gid)["users"].setdefault(str(uid), {})
        u.setdefault("balance", 0)
        u.setdefault("inventory", {})
        u.setdefault("last_daily", 0)
        u.setdefault("last_work", 0)
        u.setdefault("streak", 0)
        return u


# ================================= VIEWS BASE =================================
class BaseView(discord.ui.View):
    def __init__(self, owner_id: Optional[int] = None, *, timeout: Optional[float] = 180, admin: bool = False):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.admin = admin
        self.origin: Union[discord.Interaction, discord.Message, None] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id and interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=error_embed("Este painel é de outra pessoa. Usa o teu próprio comando!"), ephemeral=True)
            return False
        if self.admin:
            perms = getattr(interaction.user, "guild_permissions", None)
            if not perms or not perms.manage_guild:
                await interaction.response.send_message(
                    embed=error_embed("Precisas da permissão **Gerir Servidor**."), ephemeral=True)
                return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            if isinstance(self.origin, discord.Interaction):
                await self.origin.edit_original_response(view=self)
            elif isinstance(self.origin, discord.Message):
                await self.origin.edit(view=self)
        except discord.HTTPException:
            pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        traceback.print_exception(type(error), error, error.__traceback__)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=error_embed("Ocorreu um erro inesperado."), ephemeral=True)
            else:
                await interaction.response.send_message(embed=error_embed("Ocorreu um erro inesperado."), ephemeral=True)
        except discord.HTTPException:
            pass

    async def swap(self, interaction: discord.Interaction, embed: discord.Embed, view: Optional["BaseView"] = None):
        """Substitui esta view por outra (ou por nada) na mesma mensagem."""
        if view is not None:
            view.origin = interaction
        await interaction.response.edit_message(embed=embed, view=view)
        self.stop()

    @staticmethod
    async def spawn(interaction: discord.Interaction, embed: discord.Embed, view: "BaseView"):
        """Abre uma nova mensagem efémera com uma view."""
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.origin = interaction


class BaseModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception):
        traceback.print_exception(type(error), error, error.__traceback__)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=error_embed("Ocorreu um erro inesperado."), ephemeral=True)
            else:
                await interaction.response.send_message(embed=error_embed("Ocorreu um erro inesperado."), ephemeral=True)
        except discord.HTTPException:
            pass


class ConfirmView(BaseView):
    def __init__(self, owner_id: int, on_confirm: Callable[[discord.Interaction], Awaitable[None]], *,
                 admin: bool = False):
        super().__init__(owner_id, admin=admin)
        self.on_confirm = on_confirm

    @discord.ui.button(label="Confirmar", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await self.on_confirm(interaction)

    @discord.ui.button(label="Cancelar", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.swap(interaction, make_embed("Cancelado", "Nada foi alterado.", COLOR_MAIN))


# ================================= COG =================================
class EconomyShop(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = Store(DATA_FILE)

    # ---------- ciclo de vida ----------
    async def cog_load(self):
        self.bot.add_view(MainPanelView(self))  # botões do painel sobrevivem a reinícios
        if AUTO_SYNC:
            asyncio.create_task(self._sync_when_ready())

    async def _sync_when_ready(self):
        await self.bot.wait_until_ready()
        try:
            synced = await self.bot.tree.sync()
            print(f"[economy_shop] {len(synced)} slash commands sincronizados.")
        except Exception as e:  # noqa: BLE001
            print(f"[economy_shop] falha ao sincronizar comandos: {e!r}")

    # ---------- atalhos ----------
    def cfg(self, gid: int) -> dict:
        return self.store.guild(gid)["settings"]

    def money(self, gid: int, n: int) -> str:
        return f"{self.cfg(gid)['emoji']} **{fmt(n)}**"

    def item_name(self, gid: int, iid: str) -> str:
        it = self.store.guild(gid)["shop"].get(iid)
        return f"{it['emoji']} {it['name']}" if it else iid

    async def log(self, guild: discord.Guild, embed: discord.Embed):
        ch_id = self.cfg(guild.id).get("log_channel")
        channel = guild.get_channel(ch_id) if ch_id else None
        if channel:
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden, discord.HTTPException):
                pass

    # ---------- construção de embeds ----------
    def balance_embed(self, member: discord.Member) -> discord.Embed:
        g = self.store.guild(member.guild.id)
        u = self.store.user(member.guild.id, member.id)
        ranking = sorted(g["users"].items(), key=lambda kv: kv[1].get("balance", 0), reverse=True)
        pos = next((i + 1 for i, (uid, _) in enumerate(ranking) if uid == str(member.id)), len(ranking))
        items = sum(u["inventory"].values())
        e = make_embed(f"💰 Carteira de {member.display_name}", color=COLOR_GOLD,
                       guild=member.guild, thumb=member.display_avatar.url)
        e.add_field(name="Saldo", value=self.money(member.guild.id, u["balance"]))
        e.add_field(name="Ranking", value=f"#{pos}")
        e.add_field(name="Sequência diária", value=f"🔥 {u['streak']} dia(s)")
        e.add_field(name="Itens", value=f"🎒 {items}")
        return e

    def rank_embed(self, guild: discord.Guild, viewer: Optional[discord.Member] = None) -> discord.Embed:
        users = self.store.guild(guild.id)["users"]
        top = sorted(((uid, u["balance"]) for uid, u in users.items() if u.get("balance", 0) > 0),
                     key=lambda kv: kv[1], reverse=True)
        e = make_embed("🏆 Ranking de riqueza", color=COLOR_GOLD, guild=guild)
        if not top:
            e.description = "Ainda ninguém tem moedas. Usa `/diario` para começar!"
            return e
        medals = ["🥇", "🥈", "🥉"]
        e.description = "\n".join(
            f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{uid}> — {self.money(guild.id, bal)}"
            for i, (uid, bal) in enumerate(top[:10]))
        if viewer:
            pos = next((i + 1 for i, (uid, _) in enumerate(top) if uid == str(viewer.id)), None)
            if pos:
                e.set_footer(text=f"A tua posição: #{pos} • {guild.name}")
        return e

    def inventory_embed(self, member: discord.Member) -> discord.Embed:
        u = self.store.user(member.guild.id, member.id)
        e = make_embed(f"🎒 Inventário de {member.display_name}", color=COLOR_MAIN,
                       guild=member.guild, thumb=member.display_avatar.url)
        rows = [f"• **{self.item_name(member.guild.id, iid)}** × {q}" for iid, q in u["inventory"].items() if q > 0]
        e.description = "\n".join(rows) if rows else "*Vazio. Visita a `/loja`!* 🛒"
        return e

    def shop_embed(self, guild: discord.Guild, page: int = 0) -> discord.Embed:
        items = list(self.store.guild(guild.id)["shop"].items())
        c = self.cfg(guild.id)
        pages = max(1, math.ceil(len(items) / PAGE_SIZE))
        page = max(0, min(page, pages - 1))
        e = make_embed("🛒 Loja do servidor", color=COLOR_GOLD, guild=guild)
        if not items:
            e.description = "A loja ainda está vazia. 😴\nUm admin pode adicionar itens com `/adminloja`."
            return e
        e.description = "Escolhe um item no menu em baixo para ver os detalhes e comprar."
        for iid, it in items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
            stock = "∞" if it["stock"] < 0 else ("❌ Esgotado" if it["stock"] == 0 else f"{it['stock']} restantes")
            role = f"\n🎭 Dá o cargo <@&{it['role_id']}>" if it.get("role_id") else ""
            e.add_field(
                name=f"{it['emoji']} {it['name']}",
                value=f"{it['description'] or '*Sem descrição.*'}\n💵 **{fmt(it['price'])}** {c['emoji']} • 📦 {stock}{role}",
                inline=False)
        e.set_footer(text=f"Página {page + 1}/{pages} • {len(items)} itens • {guild.name}")
        return e

    def item_embed(self, member: discord.Member, iid: str) -> Optional[discord.Embed]:
        it = self.store.guild(member.guild.id)["shop"].get(iid)
        if not it:
            return None
        c = self.cfg(member.guild.id)
        u = self.store.user(member.guild.id, member.id)
        e = make_embed(f"{it['emoji']} {it['name']}", it["description"] or "*Sem descrição.*",
                       COLOR_GOLD, guild=member.guild)
        e.add_field(name="Preço", value=f"{c['emoji']} **{fmt(it['price'])}**")
        e.add_field(name="Stock", value="∞" if it["stock"] < 0 else ("❌ Esgotado" if it["stock"] == 0 else str(it["stock"])))
        if it.get("role_id"):
            e.add_field(name="Cargo", value=f"<@&{it['role_id']}>")
        e.add_field(name="O teu saldo", value=self.money(member.guild.id, u["balance"]), inline=False)
        have = u["inventory"].get(iid, 0)
        if have:
            e.set_footer(text=f"Já tens {have}× deste item • {member.guild.name}")
        return e

    def admin_embed(self, guild: discord.Guild) -> discord.Embed:
        g = self.store.guild(guild.id)
        c = g["settings"]
        circulation = sum(u.get("balance", 0) for u in g["users"].values())
        log = f"<#{c['log_channel']}>" if c.get("log_channel") else "desativado"
        e = make_embed("🛠️ Painel de administração", "Gere a loja e a economia do servidor.",
                       COLOR_MAIN, guild=guild)
        e.add_field(name="Itens na loja", value=str(len(g["shop"])))
        e.add_field(name="Membros com saldo", value=str(sum(1 for u in g["users"].values() if u.get("balance", 0) > 0)))
        e.add_field(name="Em circulação", value=self.money(guild.id, circulation))
        e.add_field(name="Moeda", value=f"{c['emoji']} {c['name']}")
        e.add_field(name="Diário / Trabalho",
                    value=f"{fmt(c['daily'])} / {fmt(c['work_min'])}–{fmt(c['work_max'])}")
        e.add_field(name="Cooldowns", value=f"{c['daily_cd'] / 3600:g}h / {c['work_cd'] / 60:g}min")
        e.add_field(name="Canal de logs", value=log, inline=False)
        drops = (f"<#{c['drop_channel']}> a cada ~{c['drop_interval']} min"
                 if c.get("drop_enabled") and c.get("drop_channel") else "desativados")
        e.add_field(name="Drops", value=drops)
        e.add_field(name="Moedas por conversar", value="ativo" if c.get("activity_enabled") else "desativado")
        return e

    def help_pages(self, guild: discord.Guild, p: str) -> dict:
        def page(title, text):
            return make_embed(title, text, COLOR_MAIN, guild=guild)
        return {
            "home": page("📖 Zany — Ajuda", (
                f"Olá! Eu sou a **Zany**: economia, loja, apostas e drops.\n"
                f"Usa `{p}comando` ou `/comando`. Escolhe uma categoria no menu em baixo.\n\n"
                "💰 **Economia** • 🎰 **Apostas** • 🛒 **Loja** • 🎁 **Drops** • 🛠️ **Admin**")),
            "eco": page("💰 Economia", (
                f"`{p}saldo [@membro]` — a tua carteira\n`{p}diario` — recompensa diária com sequência 🔥\n"
                f"`{p}trabalhar` — ganha moedas\n`{p}pagar @membro <quantia>` — transfere moedas\n"
                f"`{p}ranking` — os mais ricos\n\n*Também ganhas moedas só por conversar no servidor!*")),
            "bet": page("🎰 Apostas", (
                f"`{p}apostar <quantia> [cara|coroa]` — cara ou coroa\n"
                f"`{p}slots <quantia>` — slot machine\n"
                f"`{p}crash <quantia>` — sobe o multiplicador, retira antes de explodir 📈\n"
                f"`{p}mines <quantia> [minas]` — encontra diamantes e evita as bombas 💣")),
            "shop": page("🛒 Loja", (
                f"`{p}loja` — abre a loja (menu + botões)\n`{p}comprar <item> [qtd]` — compra direta\n"
                f"`{p}inventario` — os teus itens\n`{p}usar <item>` — usa um item")),
            "drops": page("🎁 Drops & atividade", (
                "Drops aparecem sozinhos no canal configurado: o **primeiro** a clicar no botão leva as moedas!\n"
                "Além disso, enviar mensagens dá-te uns trocos (1 vez por minuto).")),
            "admin": page("🛠️ Admin (Gerir Servidor)", (
                f"`{p}painel` — publica o painel público com botões\n`{p}adminloja` — gestão da loja e economia\n"
                f"`{p}drops [#canal] [minutos]` — ativa drops (sem canal = desativa)\n"
                f"`{p}dropagora` — solta um drop agora\n`{p}atividade <on/off>` — moedas por conversar")),
        }

    # ---------- lógica ----------
    async def do_daily(self, member: discord.Member):
        gid = member.guild.id
        async with self.store.lock:
            c = self.cfg(gid)
            u = self.store.user(gid, member.id)
            now = time.time()
            ready = u["last_daily"] + c["daily_cd"]
            if now < ready:
                return False, error_embed(f"Já recolheste a tua recompensa.\nVolta {rel(ready)}.")
            streak = u["streak"] + 1 if now - u["last_daily"] <= c["daily_cd"] * 2 else 1
            bonus = min(streak - 1, 10) * 10
            total = c["daily"] + bonus
            u["balance"] += total
            u["streak"], u["last_daily"] = streak, now
            await self.store.save()
            bal = u["balance"]
        e = ok_embed("🎁 Recompensa diária", f"Recebeste {self.money(gid, total)}!")
        e.add_field(name="Base", value=self.money(gid, c["daily"]))
        e.add_field(name="Bónus 🔥", value=self.money(gid, bonus))
        e.add_field(name="Sequência", value=f"{streak} dia(s)")
        e.add_field(name="Saldo atual", value=self.money(gid, bal), inline=False)
        e.set_thumbnail(url=member.display_avatar.url)
        return True, e

    async def do_work(self, member: discord.Member):
        gid = member.guild.id
        async with self.store.lock:
            c = self.cfg(gid)
            u = self.store.user(gid, member.id)
            now = time.time()
            ready = u["last_work"] + c["work_cd"]
            if now < ready:
                return False, error_embed(f"Ainda estás cansado. 😮‍💨\nPodes trabalhar {rel(ready)}.")
            gain = random.randint(min(c["work_min"], c["work_max"]), max(c["work_min"], c["work_max"]))
            u["balance"] += gain
            u["last_work"] = now
            await self.store.save()
            bal = u["balance"]
        e = ok_embed("💼 Dia de trabalho", f"{random.choice(WORK_LINES)} e ganhaste {self.money(gid, gain)}.")
        e.add_field(name="Saldo atual", value=self.money(gid, bal))
        e.set_thumbnail(url=member.display_avatar.url)
        return True, e

    async def do_pay(self, sender: discord.Member, target: discord.Member, amount: int):
        gid = sender.guild.id
        if target.bot or target.id == sender.id:
            return False, error_embed("Não podes pagar a ti próprio nem a bots.")
        async with self.store.lock:
            s = self.store.user(gid, sender.id)
            if s["balance"] < amount:
                return False, error_embed(f"Saldo insuficiente. Tens {self.money(gid, s['balance'])}.")
            r = self.store.user(gid, target.id)
            s["balance"] -= amount
            r["balance"] += amount
            await self.store.save()
        e = ok_embed("💸 Transferência concluída", f"{sender.mention} enviou {self.money(gid, amount)} a {target.mention}.")
        e.set_footer(text=f"Zany • {sender.guild.name}")
        return True, e

    async def do_gamble(self, member: discord.Member, amount: int):
        gid = member.guild.id
        if amount > MAX_BET:
            return False, error_embed(f"A aposta máxima é {self.money(gid, MAX_BET)}.")
        async with self.store.lock:
            u = self.store.user(gid, member.id)
            if u["balance"] < amount:
                return False, error_embed(f"Saldo insuficiente. Tens {self.money(gid, u['balance'])}.")
            won = random.random() < 0.5
            u["balance"] += amount if won else -amount
            await self.store.save()
            bal = u["balance"]
        if won:
            e = make_embed("🎰 Ganhaste!", f"A sorte sorriu-te: +{self.money(gid, amount)}", COLOR_OK)
        else:
            e = make_embed("🎰 Perdeste…", f"Desta vez não deu: −{self.money(gid, amount)}", COLOR_ERR)
        e.add_field(name="Saldo atual", value=self.money(gid, bal))
        e.set_thumbnail(url=member.display_avatar.url)
        return won, e

    async def purchase(self, member: discord.Member, iid: str, qty: int):
        guild = member.guild
        gid = guild.id
        async with self.store.lock:
            shop = self.store.guild(gid)["shop"]
            it = shop.get(iid)
            if not it:
                return False, error_embed("Esse item já não existe.")
            if it["stock"] == 0:
                return False, error_embed("Este item está **esgotado**.")
            if 0 < it["stock"] < qty:
                return False, error_embed(f"Só restam **{it['stock']}** em stock.")
            role = None
            if it.get("role_id"):
                if qty != 1:
                    return False, error_embed("Itens com cargo só podem ser comprados 1 de cada vez.")
                role = guild.get_role(it["role_id"])
                if role is None:
                    return False, error_embed("O cargo deste item já não existe. Avisa um admin.")
                if role in member.roles:
                    return False, error_embed(f"Já tens o cargo {role.mention}.")
                if not role.is_assignable():
                    return False, error_embed("Não consigo atribuir esse cargo (o meu cargo tem de estar acima dele). "
                                              "Não foi cobrado nada.")
            total = it["price"] * qty
            u = self.store.user(gid, member.id)
            if u["balance"] < total:
                return False, error_embed(f"Precisas de {self.money(gid, total)} mas tens {self.money(gid, u['balance'])}.")
            if role:
                try:
                    await member.add_roles(role, reason=f"Compra na loja: {it['name']}")
                except (discord.Forbidden, discord.HTTPException):
                    return False, error_embed("Não consegui dar-te o cargo. Não foi cobrado nada.")
            u["balance"] -= total
            u["inventory"][iid] = u["inventory"].get(iid, 0) + qty
            if it["stock"] > 0:
                it["stock"] -= qty
            await self.store.save()
            bal = u["balance"]
        e = ok_embed("🛍️ Compra concluída", f"Compraste **{qty}× {it['emoji']} {it['name']}**.")
        e.add_field(name="Total pago", value=self.money(gid, total))
        e.add_field(name="Saldo atual", value=self.money(gid, bal))
        if role:
            e.add_field(name="Cargo recebido", value=role.mention, inline=False)
        e.set_thumbnail(url=member.display_avatar.url)
        log = make_embed("🧾 Nova compra", f"{member.mention} comprou **{qty}× {it['name']}** por {self.money(gid, total)}.",
                         COLOR_GOLD, guild=guild)
        await self.log(guild, log)
        return True, e

    async def use_item(self, member: discord.Member, iid: str):
        guild = member.guild
        gid = guild.id
        async with self.store.lock:
            u = self.store.user(gid, member.id)
            it = self.store.guild(gid)["shop"].get(iid)
            qty = u["inventory"].get(iid, 0)
            if qty <= 0:
                return False, error_embed("Não tens esse item.")
            if it and it.get("role_id"):
                return False, error_embed("Este item já aplicou o cargo na compra, não precisa de ser usado.")
            if qty == 1:
                del u["inventory"][iid]
            else:
                u["inventory"][iid] = qty - 1
            await self.store.save()
        name = f"{it['emoji']} {it['name']}" if it else iid
        has_log = bool(self.cfg(gid).get("log_channel"))
        await self.log(guild, make_embed("📦 Item usado",
                                         f"{member.mention} usou **{name}**. A staff deve entregar/aplicar o benefício.",
                                         COLOR_GOLD, guild=guild))
        extra = "A staff foi avisada." if has_log else "Contacta a staff para receberes o benefício."
        return True, ok_embed("✅ Item usado", f"Usaste **{name}**.\n{extra}")

    # ---------- autocomplete ----------
    async def shop_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        current = current.lower()
        return [app_commands.Choice(name=f"{it['name']} ({fmt(it['price'])})"[:100], value=iid)
                for iid, it in self.store.guild(interaction.guild.id)["shop"].items()
                if current in iid or current in it["name"].lower()][:25]

    async def inv_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        current = current.lower()
        inv = self.store.user(interaction.guild.id, interaction.user.id)["inventory"]
        shop = self.store.guild(interaction.guild.id)["shop"]
        return [app_commands.Choice(name=f"{shop[iid]['name']} (×{q})"[:100], value=iid)
                for iid, q in inv.items() if q > 0 and iid in shop and current in shop[iid]["name"].lower()][:25]

    # ---------- comandos ----------
    @commands.hybrid_command(name="ajuda", aliases=['comandos'], description="Mostra os comandos da economia e da loja.")
    @commands.guild_only()
    async def ajuda(self, ctx: commands.Context):
        pages = self.help_pages(ctx.guild, ctx.clean_prefix)
        view = HelpView(pages, ctx.author.id)
        view.origin = await ctx.send(embed=pages["home"], view=view)

    @commands.hybrid_command(name="saldo", aliases=['money', 'bal', 'carteira'], description="Vê a tua carteira (ou a de outro membro).")
    @commands.guild_only()
    @app_commands.describe(membro="Membro a consultar")
    async def saldo(self, ctx: commands.Context, membro: Optional[discord.Member] = None):
        await ctx.send(embed=self.balance_embed(membro or ctx.author))

    @commands.hybrid_command(name="diario", aliases=['daily'], description="Recolhe a tua recompensa diária.")
    @commands.guild_only()
    async def diario(self, ctx: commands.Context):
        ok, e = await self.do_daily(ctx.author)
        await ctx.send(embed=e, ephemeral=not ok)

    @commands.hybrid_command(name="trabalhar", aliases=['work'], description="Trabalha para ganhar moedas.")
    @commands.guild_only()
    async def trabalhar(self, ctx: commands.Context):
        ok, e = await self.do_work(ctx.author)
        await ctx.send(embed=e, ephemeral=not ok)

    @commands.hybrid_command(name="pagar", aliases=['pay'], description="Envia moedas a outro membro.")
    @commands.guild_only()
    @app_commands.describe(membro="Quem recebe", quantia="Quantas moedas")
    async def pagar(self, ctx: commands.Context, membro: discord.Member, quantia: commands.Range[int, 1, 1_000_000_000]):
        ok, e = await self.do_pay(ctx.author, membro, quantia)
        await ctx.send(embed=e, ephemeral=not ok, allowed_mentions=discord.AllowedMentions(users=[membro]))

    @commands.hybrid_command(name="ranking", aliases=['rank', 'top', 'riches'], description="Os membros mais ricos do servidor.")
    @commands.guild_only()
    async def ranking(self, ctx: commands.Context):
        await ctx.send(embed=self.rank_embed(ctx.guild, ctx.author), allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="loja", aliases=['shop'], description="Abre a loja do servidor.")
    @commands.guild_only()
    async def loja(self, ctx: commands.Context):
        view = ShopView(self, ctx.guild, ctx.author.id)
        view.origin = await ctx.send(embed=view.build_embed(), view=view)

    @commands.hybrid_command(name="comprar", aliases=['buy'], description="Compra um item diretamente.")
    @commands.guild_only()
    @app_commands.describe(item="Item a comprar", quantidade="Quantidade")
    async def comprar(self, ctx: commands.Context, item: str, quantidade: commands.Range[int, 1, 100] = 1):
        shop = self.store.guild(ctx.guild.id)["shop"]
        iid = item if item in shop else next(
            (k for k, v in shop.items() if v["name"].lower() == item.lower() or k == slugify(item)), None)
        if iid is None:
            return await ctx.send(embed=error_embed("Esse item não existe. Vê a `/loja`."), ephemeral=True)
        ok, e = await self.purchase(ctx.author, iid, quantidade)
        await ctx.send(embed=e, ephemeral=not ok)

    @comprar.autocomplete("item")
    async def comprar_ac(self, interaction: discord.Interaction, current: str):
        return await self.shop_autocomplete(interaction, current)

    @commands.hybrid_command(name="inventario", aliases=['inv', 'itens'], description="Vê o teu inventário (ou o de outro membro).")
    @commands.guild_only()
    @app_commands.describe(membro="Membro a consultar")
    async def inventario(self, ctx: commands.Context, membro: Optional[discord.Member] = None):
        target = membro or ctx.author
        view = InventoryView(self, target, ctx.author.id)
        msg = await ctx.send(embed=self.inventory_embed(target), view=view if view.children else discord.utils.MISSING)
        view.origin = msg

    @commands.hybrid_command(name="usar", aliases=['use'], description="Usa um item do teu inventário.")
    @commands.guild_only()
    @app_commands.describe(item="Item a usar")
    async def usar(self, ctx: commands.Context, item: str):
        ok, e = await self.use_item(ctx.author, item)
        await ctx.send(embed=e, ephemeral=not ok)

    @usar.autocomplete("item")
    async def usar_ac(self, interaction: discord.Interaction, current: str):
        return await self.inv_autocomplete(interaction, current)

    # ---------- admin ----------
    @commands.hybrid_command(name="painel", description="Publica o painel público de economia neste canal.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    async def painel(self, ctx: commands.Context):
        c = self.cfg(ctx.guild.id)
        e = make_embed(f"{c['emoji']} Centro Económico",
                       "Gere o teu dinheiro e compra na loja sem escrever comandos.\n\n"
                       "💰 **Saldo** — vê a tua carteira\n🎁 **Diário** — recompensa a cada 24h\n"
                       "💼 **Trabalhar** — ganha moedas\n🛒 **Loja** — compra itens e cargos\n"
                       "🎒 **Inventário** — os teus itens\n🏆 **Ranking** — os mais ricos",
                       COLOR_GOLD, guild=ctx.guild)
        if ctx.guild.icon:
            e.set_thumbnail(url=ctx.guild.icon.url)
        await ctx.channel.send(embed=e, view=MainPanelView(self))
        await ctx.send(embed=ok_embed("✅ Painel publicado", "Os botões continuam a funcionar mesmo depois de reiniciares o bot."),
                       ephemeral=True)

    @commands.hybrid_command(name="adminloja", description="Abre a gestão da loja e da economia.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    async def adminloja(self, ctx: commands.Context):
        view = AdminView(self, ctx.guild, ctx.author.id)
        view.origin = await ctx.send(embed=self.admin_embed(ctx.guild), view=view, ephemeral=True)

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        await handle_command_error(ctx, error)


# ============================ ERROS & AJUDA ============================
async def handle_command_error(ctx: commands.Context, error: Exception):
    for _ in range(3):
        error = getattr(error, "original", error)
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        msg = "Precisas da permissão **Gerir Servidor** para isto."
    elif isinstance(error, commands.NoPrivateMessage):
        msg = "Este comando só funciona dentro de um servidor."
    elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument, commands.RangeError)):
        msg = f"Argumentos inválidos: {error}"
    else:
        traceback.print_exception(type(error), error, error.__traceback__)
        msg = "Ocorreu um erro inesperado."
    try:
        await ctx.send(embed=error_embed(msg), ephemeral=True)
    except discord.HTTPException:
        pass


class HelpView(BaseView):
    def __init__(self, pages: dict, owner_id: int):
        super().__init__(owner_id, timeout=180)
        self.pages = pages
        opts = [("home", "🏠 Início"), ("eco", "💰 Economia"), ("bet", "🎰 Apostas"),
                ("shop", "🛒 Loja"), ("drops", "🎁 Drops & atividade"), ("admin", "🛠️ Admin")]
        select = discord.ui.Select(placeholder="📚 Escolhe uma categoria…",
                                   options=[discord.SelectOption(label=l, value=v) for v, l in opts])
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        self.origin = interaction
        await interaction.response.edit_message(embed=self.pages[interaction.data["values"][0]], view=self)


# ================================ PAINEL PÚBLICO ================================
class MainPanelView(BaseView):
    """Painel persistente (timeout=None). Os custom_id mantêm-se entre reinícios."""

    def __init__(self, cog: EconomyShop):
        super().__init__(None, timeout=None)
        self.cog = cog

    @discord.ui.button(label="Saldo", emoji="💰", style=discord.ButtonStyle.secondary, custom_id="eco:balance", row=0)
    async def b_balance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=self.cog.balance_embed(interaction.user), ephemeral=True)

    @discord.ui.button(label="Diário", emoji="🎁", style=discord.ButtonStyle.success, custom_id="eco:daily", row=0)
    async def b_daily(self, interaction: discord.Interaction, button: discord.ui.Button):
        _, e = await self.cog.do_daily(interaction.user)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="Trabalhar", emoji="💼", style=discord.ButtonStyle.success, custom_id="eco:work", row=0)
    async def b_work(self, interaction: discord.Interaction, button: discord.ui.Button):
        _, e = await self.cog.do_work(interaction.user)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="Loja", emoji="🛒", style=discord.ButtonStyle.primary, custom_id="eco:shop", row=1)
    async def b_shop(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = ShopView(self.cog, interaction.guild, interaction.user.id)
        await self.spawn(interaction, view.build_embed(), view)

    @discord.ui.button(label="Inventário", emoji="🎒", style=discord.ButtonStyle.secondary, custom_id="eco:inv", row=1)
    async def b_inv(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = InventoryView(self.cog, interaction.user, interaction.user.id)
        e = self.cog.inventory_embed(interaction.user)
        if view.children:
            await self.spawn(interaction, e, view)
        else:
            await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="Ranking", emoji="🏆", style=discord.ButtonStyle.secondary, custom_id="eco:rank", row=1)
    async def b_rank(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=self.cog.rank_embed(interaction.guild, interaction.user), ephemeral=True)


# ===================================== LOJA =====================================
class ShopView(BaseView):
    def __init__(self, cog: EconomyShop, guild: discord.Guild, owner_id: int, page: int = 0):
        super().__init__(owner_id)
        self.cog, self.guild = cog, guild
        items = list(cog.store.guild(guild.id)["shop"].items())
        self.pages = max(1, math.ceil(len(items) / PAGE_SIZE))
        self.page = max(0, min(page, self.pages - 1))
        chunk = items[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE]
        if chunk:
            c = cog.cfg(guild.id)
            select = discord.ui.Select(
                placeholder="🛍️ Escolhe um item…", row=0,
                options=[discord.SelectOption(
                    label=f"{it['emoji']} {it['name']}"[:100], value=iid,
                    description=(f"{fmt(it['price'])} {c['name']}" + (" • Esgotado" if it["stock"] == 0 else ""))[:100])
                    for iid, it in chunk])
            select.callback = self.on_select
            self.add_item(select)
        self.prev.disabled = self.page <= 0
        self.next.disabled = self.page >= self.pages - 1
        self.indicator.label = f"{self.page + 1}/{self.pages}"

    def build_embed(self) -> discord.Embed:
        return self.cog.shop_embed(self.guild, self.page)

    async def on_select(self, interaction: discord.Interaction):
        iid = interaction.data["values"][0]
        e = self.cog.item_embed(interaction.user, iid)
        if e is None:
            return await self.swap(interaction, self.build_embed(), ShopView(self.cog, self.guild, self.owner_id, self.page))
        await self.swap(interaction, e, ItemView(self.cog, interaction.user, iid, self.page))

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        v = ShopView(self.cog, self.guild, self.owner_id, self.page - 1)
        await self.swap(interaction, v.build_embed(), v)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True, row=1)
    async def indicator(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        v = ShopView(self.cog, self.guild, self.owner_id, self.page + 1)
        await self.swap(interaction, v.build_embed(), v)


class ItemView(BaseView):
    def __init__(self, cog: EconomyShop, member: discord.Member, iid: str, back_page: int):
        super().__init__(member.id)
        self.cog, self.member, self.iid, self.back_page = cog, member, iid, back_page

    async def _after_buy(self, interaction: discord.Interaction, receipt: discord.Embed):
        """Atualiza a página do item e envia o recibo em privado."""
        e = self.cog.item_embed(interaction.user, self.iid)
        self.origin = interaction
        if e is None:
            v = ShopView(self.cog, interaction.guild, self.owner_id, self.back_page)
            await self.swap(interaction, v.build_embed(), v)
        else:
            await interaction.response.edit_message(embed=e, view=self)
        await interaction.followup.send(embed=receipt, ephemeral=True)

    @discord.ui.button(label="Comprar", emoji="🛒", style=discord.ButtonStyle.success, row=0)
    async def buy_one(self, interaction: discord.Interaction, button: discord.ui.Button):
        _, receipt = await self.cog.purchase(interaction.user, self.iid, 1)
        await self._after_buy(interaction, receipt)

    @discord.ui.button(label="Comprar vários", emoji="🔢", style=discord.ButtonStyle.primary, row=0)
    async def buy_many(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(QuantityModal(self))

    @discord.ui.button(label="Voltar", emoji="↩️", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        v = ShopView(self.cog, interaction.guild, self.owner_id, self.back_page)
        await self.swap(interaction, v.build_embed(), v)


class QuantityModal(BaseModal, title="Quantidade a comprar"):
    qty = discord.ui.TextInput(label="Quantidade (1–100)", default="1", max_length=3)

    def __init__(self, parent: ItemView):
        super().__init__(timeout=300)
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(self.qty.value)
            if not 1 <= n <= 100:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(embed=error_embed("Indica um número entre 1 e 100."), ephemeral=True)
        _, receipt = await self.parent.cog.purchase(interaction.user, self.parent.iid, n)
        await self.parent._after_buy(interaction, receipt)


# =================================== INVENTÁRIO ===================================
class InventoryView(BaseView):
    def __init__(self, cog: EconomyShop, target: discord.Member, viewer_id: int):
        super().__init__(viewer_id)
        self.cog, self.target = cog, target
        if target.id != viewer_id:
            return
        shop = cog.store.guild(target.guild.id)["shop"]
        inv = cog.store.user(target.guild.id, target.id)["inventory"]
        usable = [(iid, q) for iid, q in inv.items() if q > 0 and iid in shop and not shop[iid].get("role_id")][:25]
        if usable:
            select = discord.ui.Select(
                placeholder="✨ Usar um item…", row=0,
                options=[discord.SelectOption(label=f"{shop[iid]['emoji']} {shop[iid]['name']}"[:100],
                                              value=iid, description=f"Tens {q}") for iid, q in usable])
            select.callback = self.on_select
            self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        iid = interaction.data["values"][0]

        async def do_use(i: discord.Interaction):
            _, e = await self.cog.use_item(i.user, iid)
            await i.response.edit_message(embed=e, view=None)

        name = self.cog.item_name(interaction.guild.id, iid)
        await self.swap(interaction, make_embed("✨ Usar item?", f"Queres mesmo usar **{name}**? Vai ser consumido.", COLOR_MAIN),
                        ConfirmView(interaction.user.id, do_use))


# ===================================== ADMIN =====================================
class AdminView(BaseView):
    def __init__(self, cog: EconomyShop, guild: discord.Guild, owner_id: int):
        super().__init__(owner_id, admin=True, timeout=300)
        self.cog, self.guild = cog, guild

    @discord.ui.button(label="Novo item", emoji="➕", style=discord.ButtonStyle.success, row=0)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ItemModal(self.cog, self.guild))

    @discord.ui.button(label="Editar item", emoji="✏️", style=discord.ButtonStyle.primary, row=0)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        async def pick(i: discord.Interaction, iid: str, view: BaseView):
            await i.response.send_modal(ItemModal(self.cog, self.guild, edit_id=iid))
        await self._pick_item(interaction, "✏️ Editar item", "Escolhe o item a editar.", pick)

    @discord.ui.button(label="Remover item", emoji="🗑️", style=discord.ButtonStyle.danger, row=0)
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        async def pick(i: discord.Interaction, iid: str, view: BaseView):
            async def do_remove(ci: discord.Interaction):
                async with self.cog.store.lock:
                    it = self.cog.store.guild(self.guild.id)["shop"].pop(iid, None)
                    await self.cog.store.save()
                msg = f"Item **{it['name']}** removido." if it else "O item já tinha sido removido."
                await ci.response.edit_message(embed=ok_embed("🗑️ Removido", msg), view=None)
            name = self.cog.item_name(self.guild.id, iid)
            await view.swap(i, make_embed("🗑️ Remover item?", f"Vais remover **{name}** da loja.", COLOR_ERR),
                            ConfirmView(i.user.id, do_remove, admin=True))
        await self._pick_item(interaction, "🗑️ Remover item", "Escolhe o item a remover.", pick)

    @discord.ui.button(label="Moedas", emoji="💰", style=discord.ButtonStyle.secondary, row=1)
    async def money(self, interaction: discord.Interaction, button: discord.ui.Button):
        v = MoneyView(self.cog, self.guild, interaction.user.id)
        await self.spawn(interaction, make_embed("💰 Gerir moedas", "Escolhe um membro e depois a ação.", COLOR_GOLD), v)

    @discord.ui.button(label="Moeda & valores", emoji="⚙️", style=discord.ButtonStyle.secondary, row=1)
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SettingsModal(self.cog, self.guild))

    @discord.ui.button(label="Cooldowns", emoji="⏱️", style=discord.ButtonStyle.secondary, row=1)
    async def cooldowns(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CooldownModal(self.cog, self.guild))

    @discord.ui.button(label="Canal de logs", emoji="📜", style=discord.ButtonStyle.secondary, row=1)
    async def logs(self, interaction: discord.Interaction, button: discord.ui.Button):
        v = LogView(self.cog, self.guild, interaction.user.id)
        await self.spawn(interaction, make_embed("📜 Canal de logs", "Compras e usos de itens serão registados no canal escolhido.", COLOR_MAIN), v)

    @discord.ui.button(label="Atualizar", emoji="🔄", style=discord.ButtonStyle.secondary, row=2)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.origin = interaction
        await interaction.response.edit_message(embed=self.cog.admin_embed(self.guild), view=self)

    async def _pick_item(self, interaction, title, desc, callback):
        shop = self.cog.store.guild(self.guild.id)["shop"]
        if not shop:
            return await interaction.response.send_message(embed=error_embed("A loja não tem itens."), ephemeral=True)
        view = ItemPickView(self.cog, self.guild, interaction.user.id, callback)
        extra = "\n*(a mostrar os primeiros 25 itens)*" if len(shop) > 25 else ""
        await self.spawn(interaction, make_embed(title, desc + extra, COLOR_MAIN), view)


class ItemPickView(BaseView):
    def __init__(self, cog: EconomyShop, guild: discord.Guild, owner_id: int,
                 callback: Callable[[discord.Interaction, str, BaseView], Awaitable[None]]):
        super().__init__(owner_id, admin=True)
        self.cog, self.callback = cog, callback
        shop = list(cog.store.guild(guild.id)["shop"].items())[:25]
        select = discord.ui.Select(
            placeholder="Escolhe um item…",
            options=[discord.SelectOption(label=f"{it['emoji']} {it['name']}"[:100], value=iid,
                                          description=f"{fmt(it['price'])}") for iid, it in shop])
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        await self.callback(interaction, interaction.data["values"][0], self)


class ItemModal(BaseModal):
    def __init__(self, cog: EconomyShop, guild: discord.Guild, edit_id: Optional[str] = None):
        super().__init__(title="Editar item" if edit_id else "Novo item", timeout=900)
        self.cog, self.guild, self.edit_id = cog, guild, edit_id
        it = cog.store.guild(guild.id)["shop"].get(edit_id) if edit_id else None
        self.name_in = discord.ui.TextInput(label="Nome", max_length=60, default=it["name"] if it else None)
        self.price_in = discord.ui.TextInput(label="Preço", max_length=10, default=str(it["price"]) if it else None)
        self.desc_in = discord.ui.TextInput(label="Descrição", style=discord.TextStyle.paragraph, required=False,
                                            max_length=200, default=it["description"] if it else None)
        self.stock_in = discord.ui.TextInput(label="Stock (-1 = ilimitado)", max_length=7,
                                             default=str(it["stock"]) if it else "-1")
        self.emoji_in = discord.ui.TextInput(label="Emoji (opcional)", required=False, max_length=40,
                                             default=it["emoji"] if it else None)
        for x in (self.name_in, self.price_in, self.desc_in, self.stock_in, self.emoji_in):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            price = int(self.price_in.value.replace(".", "").replace(" ", ""))
            stock = int(self.stock_in.value.strip())
            if not 1 <= price <= 1_000_000_000 or stock < -1:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                embed=error_embed("Preço (≥ 1) ou stock (≥ -1) inválido."), ephemeral=True)
        name = self.name_in.value.strip()
        async with self.cog.store.lock:
            shop = self.cog.store.guild(self.guild.id)["shop"]
            if self.edit_id and self.edit_id in shop:
                iid = self.edit_id
            else:
                base = slugify(name) or "item"
                iid, n = base, 2
                while iid in shop:
                    iid, n = f"{base}-{n}", n + 1
            old = shop.get(iid, {})
            shop[iid] = {
                "name": name, "price": price, "description": self.desc_in.value.strip(), "stock": stock,
                "emoji": clean_emoji(self.emoji_in.value, old.get("emoji", "📦")), "role_id": old.get("role_id"),
            }
            await self.cog.store.save()
        view = ItemRoleView(self.cog, self.guild, interaction.user.id, iid)
        await view.spawn(interaction, view.build(), view)


class ItemRoleView(BaseView):
    def __init__(self, cog: EconomyShop, guild: discord.Guild, owner_id: int, iid: str):
        super().__init__(owner_id, admin=True, timeout=300)
        self.cog, self.guild, self.iid = cog, guild, iid

    def build(self) -> discord.Embed:
        it = self.cog.store.guild(self.guild.id)["shop"][self.iid]
        e = ok_embed("✅ Item guardado", "Podes (opcionalmente) escolher um cargo para dar a quem comprar.")
        e.add_field(name="Item", value=f"{it['emoji']} **{it['name']}**")
        e.add_field(name="Preço", value=self.cog.money(self.guild.id, it["price"]))
        e.add_field(name="Stock", value="∞" if it["stock"] < 0 else str(it["stock"]))
        e.add_field(name="Cargo", value=f"<@&{it['role_id']}>" if it.get("role_id") else "nenhum", inline=False)
        return e

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="🎭 Cargo dado na compra (opcional)",
                       min_values=0, max_values=1, row=0)
    async def pick_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0] if select.values else None
        if role and (role.is_default() or role.managed):
            return await interaction.response.send_message(
                embed=error_embed("Esse cargo não pode ser atribuído (é @everyone ou gerido por uma integração)."), ephemeral=True)
        async with self.cog.store.lock:
            self.cog.store.guild(self.guild.id)["shop"][self.iid]["role_id"] = role.id if role else None
            await self.cog.store.save()
        e = self.build()
        if role and not role.is_assignable():
            e.add_field(name="⚠️ Atenção", value="O cargo do bot está abaixo deste cargo. Sobe o cargo do bot nas definições do servidor.", inline=False)
        self.origin = interaction
        await interaction.response.edit_message(embed=e, view=self)

    @discord.ui.button(label="Sem cargo", emoji="🚫", style=discord.ButtonStyle.secondary, row=1)
    async def no_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.cog.store.lock:
            self.cog.store.guild(self.guild.id)["shop"][self.iid]["role_id"] = None
            await self.cog.store.save()
        self.origin = interaction
        await interaction.response.edit_message(embed=self.build(), view=self)

    @discord.ui.button(label="Concluir", emoji="✅", style=discord.ButtonStyle.success, row=1)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.swap(interaction, self.build(), None)


class MoneyView(BaseView):
    def __init__(self, cog: EconomyShop, guild: discord.Guild, owner_id: int):
        super().__init__(owner_id, admin=True, timeout=300)
        self.cog, self.guild = cog, guild
        self.target_id: Optional[int] = None

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="👤 Escolhe um membro", row=0)
    async def pick_user(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        self.target_id = select.values[0].id
        bal = self.cog.store.user(self.guild.id, self.target_id)["balance"]
        e = make_embed("💰 Gerir moedas", f"Membro: <@{self.target_id}>\nSaldo atual: {self.cog.money(self.guild.id, bal)}", COLOR_GOLD)
        self.origin = interaction
        await interaction.response.edit_message(embed=e, view=self)

    async def _open(self, interaction: discord.Interaction, mode: str):
        if not self.target_id:
            return await interaction.response.send_message(embed=error_embed("Escolhe primeiro um membro."), ephemeral=True)
        await interaction.response.send_modal(AmountModal(self.cog, self.guild, self.target_id, mode))

    @discord.ui.button(label="Dar", emoji="➕", style=discord.ButtonStyle.success, row=1)
    async def give(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open(interaction, "give")

    @discord.ui.button(label="Tirar", emoji="➖", style=discord.ButtonStyle.danger, row=1)
    async def take(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open(interaction, "take")

    @discord.ui.button(label="Definir", emoji="📝", style=discord.ButtonStyle.secondary, row=1)
    async def set_(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open(interaction, "set")


class AmountModal(BaseModal):
    amount = discord.ui.TextInput(label="Quantia", max_length=12)

    def __init__(self, cog: EconomyShop, guild: discord.Guild, target_id: int, mode: str):
        titles = {"give": "Dar moedas", "take": "Tirar moedas", "set": "Definir saldo"}
        super().__init__(title=titles[mode], timeout=300)
        self.cog, self.guild, self.target_id, self.mode = cog, guild, target_id, mode

    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(self.amount.value.replace(".", "").replace(" ", ""))
            if n < (0 if self.mode == "set" else 1) or n > 1_000_000_000_000:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(embed=error_embed("Quantia inválida."), ephemeral=True)
        async with self.cog.store.lock:
            u = self.cog.store.user(self.guild.id, self.target_id)
            if self.mode == "give":
                u["balance"] += n
            elif self.mode == "take":
                u["balance"] = max(0, u["balance"] - n)
            else:
                u["balance"] = n
            await self.cog.store.save()
            bal = u["balance"]
        verb = {"give": "Deste", "take": "Retiraste", "set": "Definiste o saldo para"}[self.mode]
        await interaction.response.send_message(
            embed=ok_embed("✅ Feito", f"{verb} {self.cog.money(self.guild.id, n)} — <@{self.target_id}> tem agora {self.cog.money(self.guild.id, bal)}."),
            ephemeral=True)
        await self.cog.log(self.guild, make_embed(
            "🛠️ Ajuste de saldo", f"{interaction.user.mention} ({self.mode}) {self.cog.money(self.guild.id, n)} → <@{self.target_id}>",
            COLOR_MAIN, guild=self.guild))


class SettingsModal(BaseModal, title="Moeda & recompensas"):
    def __init__(self, cog: EconomyShop, guild: discord.Guild):
        super().__init__(timeout=600)
        self.cog, self.guild = cog, guild
        c = cog.cfg(guild.id)
        self.emoji_in = discord.ui.TextInput(label="Emoji da moeda", max_length=40, default=c["emoji"])
        self.name_in = discord.ui.TextInput(label="Nome da moeda (plural)", max_length=20, default=c["name"])
        self.daily_in = discord.ui.TextInput(label="Recompensa diária", max_length=8, default=str(c["daily"]))
        self.min_in = discord.ui.TextInput(label="Trabalho: mínimo", max_length=8, default=str(c["work_min"]))
        self.max_in = discord.ui.TextInput(label="Trabalho: máximo", max_length=8, default=str(c["work_max"]))
        for x in (self.emoji_in, self.name_in, self.daily_in, self.min_in, self.max_in):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            daily, wmin, wmax = int(self.daily_in.value), int(self.min_in.value), int(self.max_in.value)
            if min(daily, wmin, wmax) < 0 or wmin > wmax:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                embed=error_embed("Valores inválidos (usa números ≥ 0 e mínimo ≤ máximo)."), ephemeral=True)
        async with self.cog.store.lock:
            c = self.cog.cfg(self.guild.id)
            c["emoji"] = clean_emoji(self.emoji_in.value, c["emoji"])
            c["name"] = self.name_in.value.strip() or c["name"]
            c["daily"], c["work_min"], c["work_max"] = daily, wmin, wmax
            await self.cog.store.save()
        await interaction.response.send_message(embed=ok_embed("✅ Guardado", "Definições atualizadas."), ephemeral=True)


class CooldownModal(BaseModal, title="Cooldowns"):
    def __init__(self, cog: EconomyShop, guild: discord.Guild):
        super().__init__(timeout=600)
        self.cog, self.guild = cog, guild
        c = cog.cfg(guild.id)
        self.daily_in = discord.ui.TextInput(label="Diário (horas)", max_length=6, default=f"{c['daily_cd'] / 3600:g}")
        self.work_in = discord.ui.TextInput(label="Trabalhar (minutos)", max_length=6, default=f"{c['work_cd'] / 60:g}")
        self.add_item(self.daily_in)
        self.add_item(self.work_in)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            d = float(self.daily_in.value.replace(",", "."))
            w = float(self.work_in.value.replace(",", "."))
            if d < 0 or w < 0:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(embed=error_embed("Valores inválidos."), ephemeral=True)
        async with self.cog.store.lock:
            c = self.cog.cfg(self.guild.id)
            c["daily_cd"], c["work_cd"] = int(d * 3600), int(w * 60)
            await self.cog.store.save()
        await interaction.response.send_message(embed=ok_embed("✅ Guardado", "Cooldowns atualizados."), ephemeral=True)


class LogView(BaseView):
    def __init__(self, cog: EconomyShop, guild: discord.Guild, owner_id: int):
        super().__init__(owner_id, admin=True, timeout=300)
        self.cog, self.guild = cog, guild

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder="📜 Escolhe o canal de logs", row=0)
    async def pick(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        ch = select.values[0]
        async with self.cog.store.lock:
            self.cog.cfg(self.guild.id)["log_channel"] = ch.id
            await self.cog.store.save()
        await self.swap(interaction, ok_embed("✅ Logs ativados", f"As compras serão registadas em <#{ch.id}>."), None)

    @discord.ui.button(label="Desativar logs", emoji="🔕", style=discord.ButtonStyle.danger, row=1)
    async def off(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.cog.store.lock:
            self.cog.cfg(self.guild.id)["log_channel"] = None
            await self.cog.store.save()
        await self.swap(interaction, ok_embed("🔕 Logs desativados", "Já não vou registar compras."), None)


async def add_cog_safe(bot: commands.Bot, cog: commands.Cog):
    """Regista o cog; se houver conflito de nomes/aliases com o bot hospedeiro, tenta sem aliases."""
    try:
        await bot.add_cog(cog)
    except commands.CommandRegistrationError as e:
        print(f"[zany] conflito com o comando '{e.name}' — a tentar sem aliases…")
        retry = type(cog)(bot)
        for cmd in retry.walk_commands():
            cmd.aliases = []
        await bot.add_cog(retry)


async def setup(bot: commands.Bot):
    if bot.get_cog("EconomyShop") is None:
        await add_cog_safe(bot, EconomyShop(bot))
