import os
import csv
import io
import json
import time
import asyncio
import logging
import discord
from collections import Counter
import zoneinfo
from datetime import datetime, time as dt_time
from discord.ext import commands, tasks
from thefuzz import process
from pymongo import UpdateOne
import mainbot

log = logging.getLogger("clashbot")

CONCURRENT_REQUESTS = 5
TTL_CARDS        = 60 * 60 * 24
TTL_CLAN         = 60 * 10
TTL_PLAYER       = 60 * 5
TTL_BATTLE_LOG   = 60 * 60 * 24
TTL_WAR          = 60 * 5
WARMUP_RELEVANT_COMMANDS = {"scout", "primetime", "cardstats", "whohas"}
HEAVY_COMMANDS_COOLDOWN  = 30


# ──────────────────────────────────────────────────────────────────────────────
# ProfileView
# ──────────────────────────────────────────────────────────────────────────────

class ProfileView(discord.ui.View):
    """Tabbed embed view for a player profile."""

    def __init__(self, data: dict, author_id: int, battle_history: list = None, war_data: dict = None):
        super().__init__(timeout=120)
        self.data          = data
        self.author_id     = author_id
        self.battle_history = battle_history or []
        self.war_data       = war_data or {}

        player_tag = data.get("tag", "").replace("#", "")
        self.add_item(discord.ui.Button(
            label="View Full Web Dashboard",
            style=discord.ButtonStyle.link,
            url=f"https://graveyardbot.onrender.com/player/{player_tag}",
            emoji="🌐",
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            "❌ Only the person who ran this command can use these buttons.", ephemeral=True
        )
        return False

    # ── Embed builders ────────────────────────────────────────────────────────

    def build_overview_embed(self) -> discord.Embed:
        e = discord.Embed(
            title=f"👑 {self.data.get('name')} | Lvl {self.data.get('expLevel')}",
            color=0x3498DB,
        )
        trophies      = self.data.get("trophies", 0)
        best_trophies = self.data.get("bestTrophies", 0)
        e.add_field(
            name="🏆 Trophies",
            value=f"Current: **{trophies}**\nBest: **{best_trophies}**",
            inline=True,
        )

        wins, losses = self.data.get("wins", 0), self.data.get("losses", 0)
        total    = wins + losses
        win_rate = round(wins / total * 100, 1) if total > 0 else 0
        e.add_field(
            name="⚔️ Combat Stats",
            value=f"Wins: **{wins}**\nLosses: **{losses}**\nWin Rate: **{win_rate}%**",
            inline=True,
        )

        # Trophy trend from battle history (last 7 battles used as a proxy)
        trend = self._trophy_trend()
        if trend:
            e.add_field(name="📈 Recent Trophy Trend", value=trend, inline=False)

        return e

    def build_social_embed(self) -> discord.Embed:
        e = discord.Embed(
            title=f"🛡️ Social & War | {self.data.get('name')}",
            color=0xE67E22,
        )
        clan = self.data.get("clan")
        if clan:
            e.add_field(
                name="Clan",
                value=f"**{clan.get('name')}** ({clan.get('tag')})\nRole: {self.data.get('role', 'Member').capitalize()}",
                inline=False,
            )
        e.add_field(
            name="🎁 Donations",
            value=f"Given: **{self.data.get('donations', 0)}**\nReceived: **{self.data.get('donationsReceived', 0)}**",
            inline=True,
        )

        # War data if available
        if self.war_data:
            decks_today = self.war_data.get("decksUsedToday", 0)
            bar         = "🟩" * decks_today + "⬜" * (4 - decks_today)
            e.add_field(
                name="⚔️ River Race",
                value=(
                    f"Fame: **{self.war_data.get('fame', 0)}**\n"
                    f"Decks Today: {bar} ({decks_today}/4)\n"
                    f"War Wins: **{self.war_data.get('warDayWins', 0)}**"
                ),
                inline=True,
            )
        return e

    def build_deck_embed(self) -> discord.Embed:
        e = discord.Embed(
            title=f"🃏 Deck | {self.data.get('name')}",
            color=0x9B59B6,
        )
        cards    = self.data.get("currentDeck", [])
        deck_str = "\n".join(f"• **{c['name']}** (Lvl {c['level']})" for c in cards)
        e.add_field(
            name="⚔️ Current Battle Deck",
            value=deck_str or "No deck found.",
            inline=False,
        )

        # Most-used cards from recent battle history
        top_cards = self._top_cards()
        if top_cards:
            e.add_field(
                name="📊 Most-Used Cards (last 50 battles)",
                value="\n".join(top_cards),
                inline=False,
            )
        return e

    def build_stats_embed(self) -> discord.Embed:
        e = discord.Embed(
            title=f"📈 Performance | {self.data.get('name')}",
            color=0x2ECC71,
        )

        recent = self.battle_history[:20]
        if recent:
            wins_recent = sum(1 for b in recent if b.get("result") == "win")
            streak      = 0
            for b in recent:
                if b.get("result") == "win":
                    streak += 1
                else:
                    break

            e.add_field(name="🔥 Current Win Streak",   value=str(streak),        inline=True)
            e.add_field(name="📊 Recent Form (last 20)", value=f"{wins_recent}W / {20 - wins_recent}L", inline=True)
        else:
            e.add_field(name="📊 Recent Battles", value="No recent battle data found.", inline=False)

        e.add_field(name="⭐ Challenge Max Wins",  value=str(self.data.get("challengeMaxWins", 0)),   inline=True)
        e.add_field(name="🏟️ Tournament Cards",    value=str(self.data.get("tournamentCardsWon", 0)), inline=True)
        e.add_field(name="🃏 Cards Found",         value=str(self.data.get("totalExpPoints", 0)),     inline=True)
        return e

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _trophy_trend(self) -> str | None:
        """Rough trophy delta based on battle results in history."""
        if not self.battle_history:
            return None
        recent = self.battle_history[:10]
        wins   = sum(1 for b in recent if b.get("result") == "win")
        losses = len(recent) - wins
        if wins > losses:
            return f"📈 {wins}W / {losses}L in last {len(recent)} battles"
        elif losses > wins:
            return f"📉 {wins}W / {losses}L in last {len(recent)} battles"
        return f"➡️ {wins}W / {losses}L in last {len(recent)} battles (even)"

    def _top_cards(self) -> list[str]:
        counter = Counter(
            card.get("name", "Unknown")
            for b in self.battle_history[:50]
            for card in b.get("team_cards", [])
        )
        return [f"• **{name}** — {count} uses" for name, count in counter.most_common(5)]

    # ── Buttons ───────────────────────────────────────────────────────────────

    @discord.ui.button(label="Overview",   style=discord.ButtonStyle.primary,   emoji="📊")
    async def btn_overview(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.build_overview_embed())

    @discord.ui.button(label="Social & War", style=discord.ButtonStyle.success, emoji="🛡️")
    async def btn_social(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.build_social_embed())

    @discord.ui.button(label="Deck & Cards", style=discord.ButtonStyle.secondary, emoji="🃏")
    async def btn_deck(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.build_deck_embed())

    @discord.ui.button(label="Performance", style=discord.ButtonStyle.secondary, emoji="📈")
    async def btn_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.build_stats_embed())


# ──────────────────────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────────────────────

class ClashRoyale(commands.Cog):
    def __init__(self, bot):
        self.bot          = bot
        self.db           = bot.db
        self.users        = bot.db_users
        self.guilds       = self.db["guilds"]
        self.mongo_cache  = self.db["api_cache"]
        self.clan_tag     = "9LVY89UP"
        self.all_cards    = []
        self.active_warmups = set()

        self.daily_snapshot_loop.start()
        asyncio.create_task(self._cache_cards())

    def cog_unload(self):
        self.reminder_loop.cancel()
        self.daily_snapshot_loop.cancel()

    # ── Cache helpers ─────────────────────────────────────────────────────────

    async def _cache_get(self, key: str):
        if self.bot.redis_available:
            raw = await self.bot.redis.get(key)
            if raw:
                return json.loads(raw)
        else:
            doc = await self.mongo_cache.find_one({"_id": key})
            if doc and doc.get("expires_at", 0) > time.time():
                return doc["data"]
        return None

    async def _cache_set(self, key: str, value, ttl: int):
        if self.bot.redis_available:
            await self.bot.redis.setex(key, ttl, json.dumps(value))
        else:
            await self.mongo_cache.update_one(
                {"_id": key},
                {"$set": {"data": value, "expires_at": time.time() + ttl}},
                upsert=True,
            )

    # ── API helpers ───────────────────────────────────────────────────────────

    async def _get_player_data(self, tag: str):
        clean = tag.upper().replace("#", "")
        cached = await self._cache_get(f"player:{clean}")
        if cached:
            return cached
        data = await self.bot.async_fetch_cr_api(f"players/%23{clean}")
        if data:
            await self._cache_set(f"player:{clean}", data, TTL_PLAYER)
        return data

    async def _get_player_battles(self, tag: str) -> list:
        clean = tag.upper().replace("#", "")
        cached = await self._cache_get(f"battles:{clean}")
        if cached:
            return cached
        data = await self.bot.async_fetch_cr_api(f"players/%23{clean}/battlelog")
        battles = data if isinstance(data, list) else []
        if battles:
            await self._cache_set(f"battles:{clean}", battles, TTL_BATTLE_LOG)
        return battles

    async def _get_clan_data(self, clan_tag: str):
        cached = await self._cache_get(f"clan:{clan_tag}")
        if cached:
            return cached
        data = await self.bot.async_fetch_cr_api(f"clans/%23{clan_tag}")
        if data:
            await self._cache_set(f"clan:{clan_tag}", data, TTL_CLAN)
        return data

    async def _get_war_data(self):
        cached = await self._cache_get(f"war:{self.clan_tag}")
        if cached:
            return cached
        data = await self.bot.async_fetch_cr_api(f"clans/%23{self.clan_tag}/currentriverrace")
        if data:
            await self._cache_set(f"war:{self.clan_tag}", data, TTL_WAR)
        return data

    async def _cache_cards(self):
        cached = await self._cache_get("cards:all")
        if cached:
            self.all_cards = cached
            return
        data = await self.bot.async_fetch_cr_api("cards")
        if data:
            self.all_cards = data.get("items", [])
            await self._cache_set("cards:all", self.all_cards, TTL_CARDS)

    # ── Trophy trend from snapshots ───────────────────────────────────────────

    async def _get_trophy_trend(self, tag: str) -> str:
        """Compare current trophies against historical snapshots."""
        docs = await self.db["historical_snapshots"].find(
            {"tag": tag.upper().replace("#", "")}
        ).sort("date", -1).limit(8).to_list(8)

        if len(docs) < 2:
            return "Not enough snapshot history yet."

        delta = docs[0].get("trophies", 0) - docs[-1].get("trophies", 0)
        days  = len(docs) - 1
        arrow = "📈" if delta > 0 else ("📉" if delta < 0 else "➡️")
        return f"{arrow} {delta:+d} trophies over ~{days} days"

    # ── Top cards from DB battle history ──────────────────────────────────────

    async def _get_top_cards_from_db(self, tag: str) -> list[str]:
        battles = await self.db["battle_history"].find(
            {"player_tag": tag.upper().replace("#", "")}
        ).sort("battle_time", -1).limit(50).to_list(50)

        counter = Counter(
            card.get("name", "Unknown")
            for b in battles
            for card in b.get("team_cards", [])
        )
        return [f"• **{name}** — {count} uses" for name, count in counter.most_common(5)]

    # ── Harvest ───────────────────────────────────────────────────────────────

    async def run_harvest_logic(self):
        harvest_start = time.monotonic()

        clan_data = await self.bot.async_fetch_cr_api(f"clans/%23{self.clan_tag}")
        war_data  = await self.bot.async_fetch_cr_api(f"clans/%23{self.clan_tag}/currentriverrace")

        if not clan_data:
            log.warning("Harvest aborted: could not fetch clan data.")
            return

        snapshot_date = datetime.now(zoneinfo.ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        members       = clan_data.get("memberList", [])

        war_participants = {}
        if war_data:
            war_participants = {
                p["tag"].replace("#", "").upper(): p
                for p in war_data.get("clan", {}).get("participants", [])
            }

        snapshot_ops = []
        battle_ops   = []
        sem = asyncio.Semaphore(CONCURRENT_REQUESTS)

        async def harvest_member(member):
            tag = member["tag"].replace("#", "").upper()
            async with sem:
                profile, blog = await asyncio.gather(
                    self.bot.async_fetch_cr_api(f"players/%23{tag}"),
                    self.bot.async_fetch_cr_api(f"players/%23{tag}/battlelog"),
                )
            return tag, member, profile, blog

        results = await asyncio.gather(*(harvest_member(m) for m in members))

        for tag, m, profile, blog in results:
            # Daily snapshot
            flat = {
                "date":     snapshot_date,
                "name":     m.get("name", ""),
                "tag":      tag,
                "trophies": m.get("trophies", 0),
                "role":     m.get("role", "member"),
            }
            if tag in war_participants:
                wp = war_participants[tag]
                flat.update({
                    "fame":          wp.get("fame", 0),
                    "decksUsedToday": wp.get("decksUsedToday", 0),
                    "warDayWins":    wp.get("warDayWins", 0),
                })
            snapshot_ops.append(
                UpdateOne({"tag": tag, "date": snapshot_date}, {"$set": flat}, upsert=True)
            )

            # Battle history
            if blog and isinstance(blog, list):
                for battle in blog:
                    if not battle.get("battleTime"):
                        continue
                    battle_id = f"{tag}_{battle['battleTime']}"
                    team = battle.get("team", [{}])[0]
                    opp  = battle.get("opponent", [{}])[0]
                    # Inside run_harvest_logic, inside the battle log loop:
                    doc = {
                        "player_tag":      tag,
                        "player_name":     m.get("name", ""),
                        "battle_time":     battle["battleTime"],
                        "result":          "win" if team.get("crowns", 0) > opp.get("crowns", 0) else "loss",
                        "team_crowns":     team.get("crowns", 0),
                        "opp_crowns":      opp.get("crowns", 0),
                        "opp_name":        opp.get("name", "Unknown"),
                        "type":            battle.get("type", "PvP"),
                        # IMPORTANT: Save the full objects!
                        "team_cards":      team.get("cards", []), 
                        "opponent_cards":  opp.get("cards", []),
                    }
                    battle_ops.append(
                        UpdateOne({"_id": battle_id}, {"$set": doc}, upsert=True)
                    )

        if snapshot_ops:
            await self.db["historical_snapshots"].bulk_write(snapshot_ops, ordered=False)
        if battle_ops:
            await self.db["battle_history"].bulk_write(battle_ops, ordered=False)

        log.info(f"✅ Harvest complete in {round(time.monotonic() - harvest_start, 1)}s — "
                 f"{len(members)} members, {len(battle_ops)} battle records.")
    # ── Loops ─────────────────────────────────────────────────────────────────

    @tasks.loop(time=dt_time(hour=23, minute=55, tzinfo=zoneinfo.ZoneInfo("America/New_York")))
    async def daily_snapshot_loop(self):
        await self.run_harvest_logic()

    @daily_snapshot_loop.before_loop
    async def before_snapshot_loop(self):
        await self.bot.wait_until_ready()

    # ── Commands ──────────────────────────────────────────────────────────────

    @commands.command(name="p", aliases=["profile"])
    async def p(self, ctx, *, target: str = None):
        """Show a player profile. Pass a tag or leave blank to use your linked account."""
        # Resolve tag
        tag = None
        if target:
            tag = target.upper().lstrip("#")
        else:
            user_doc = await self.users.find_one({"discord_id": ctx.author.id})
            if user_doc:
                tag = user_doc.get("cr_tag", "").upper().lstrip("#")

        if not tag:
            await ctx.send("❌ Please provide a player tag, e.g. `!p #ABC123`, or link your account first.")
            return

        async with ctx.typing():
            # Fetch profile, battles, and war data concurrently
            profile, battles, war_api = await asyncio.gather(
                self._get_player_data(tag),
                self._get_player_battles(tag),
                self._get_war_data(),
            )

        if not profile:
            await ctx.send(f"❌ Could not find a player with tag `#{tag}`. Double-check the tag and try again.")
            return

        # Find this player's war entry if present
        war_entry = {}
        if war_api:
            participants = war_api.get("clan", {}).get("participants", [])
            war_entry = next(
                (p for p in participants if p.get("tag", "").replace("#", "").upper() == tag),
                {}
            )

        # Build battle history list for the view (normalised from live API)
        battle_history = []
        for b in (battles or []):
            team = b.get("team", [{}])[0]
            opp  = b.get("opponent", [{}])[0]
            battle_history.append({
                "result":         "win" if team.get("crowns", 0) > opp.get("crowns", 0) else "loss",
                "battle_time":    b.get("battleTime", ""),
                "team_crowns":    team.get("crowns", 0),
                "opp_crowns":     opp.get("crowns", 0),
                "team_cards":     team.get("cards", []),
                "opponent_cards": opp.get("cards", []),
            })

        view  = ProfileView(profile, ctx.author.id, battle_history=battle_history, war_data=war_entry)
        embed = view.build_overview_embed()
        await ctx.send(embed=embed, view=view)

    @commands.command(name="war", aliases=["riverrace"])
    async def war(self, ctx):
        """Show the current River Race standings for the clan."""
        async with ctx.typing():
            war_api = await self._get_war_data()

        if not war_api:
            await ctx.send("❌ Could not fetch war data right now. Try again in a moment.")
            return

        participants = war_api.get("clan", {}).get("participants", [])
        if not participants:
            await ctx.send("No active war participants found.")
            return

        sorted_p     = sorted(participants, key=lambda x: x.get("fame", 0), reverse=True)
        total_fame   = sum(p.get("fame", 0) for p in sorted_p)
        total_decks  = sum(p.get("decksUsedToday", 0) for p in sorted_p)

        e = discord.Embed(title="⚔️ River Race Standings", color=0xE67E22)
        e.add_field(name="Total Fame",  value=f"**{total_fame:,}**",          inline=True)
        e.add_field(name="Decks Used",  value=f"**{total_decks}/{len(sorted_p)*4}**", inline=True)

        rows = []
        for i, p in enumerate(sorted_p[:15], 1):
            decks = p.get("decksUsedToday", 0)
            bar   = "🟩" * decks + "⬜" * (4 - decks)
            rows.append(f"`{i:>2}.` **{p['name']}** — {p.get('fame', 0):,} fame {bar}")

        e.add_field(name="Participants (top 15)", value="\n".join(rows) or "None", inline=False)
        await ctx.send(embed=e)

    @commands.command(name="harvest")
    @commands.has_permissions(administrator=True)
    async def harvest(self, ctx):
        """Manually trigger a full data harvest (admin only)."""
        msg = await ctx.send("🌾 Harvest started…")
        await self.run_harvest_logic()
        await msg.edit(content="✅ Harvest complete.")


async def setup(bot):
    await bot.add_cog(ClashRoyale(bot))
