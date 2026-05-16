import os
import csv
import io
import json
import asyncio
import logging
import discord
from collections import Counter
from urllib.parse import quote
from datetime import datetime
import zoneinfo
import openpyxl
from openpyxl.styles import PatternFill
from discord.ext import commands, tasks
from thefuzz import process

log = logging.getLogger("clashbot")

CONCURRENT_REQUESTS = 5

# --- Cache TTLs (seconds) ---
TTL_CARDS       = 60 * 60 * 24
TTL_CLAN        = 60 * 10
TTL_PLAYER      = 60 * 5
TTL_BATTLE_LOG  = 60 * 60 * 24
TTL_WAR         = 60 * 5
TTL_PROFILES    = 60 * 60 * 24

MAX_RETRIES     = 3
RETRY_BACKOFF   = 1.5

WARMUP_RELEVANT_COMMANDS = {"scout", "primetime", "cardstats", "whohas"}
HEAVY_COMMANDS_COOLDOWN = 30


class ProfileView(discord.ui.View):
    def __init__(self, data: dict, author_id: int):
        super().__init__(timeout=120)
        self.data = data
        self.author_id = author_id

        player_tag = data.get("tag", "").replace("#", "")
        web_url = f"https://graveyardbot.onrender.com/player/{player_tag}"
        self.add_item(
            discord.ui.Button(
                label="View Full Web Dashboard",
                style=discord.ButtonStyle.link,
                url=web_url,
                emoji="🌐",
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            "❌ Only the person who ran this command can use these buttons.", ephemeral=True
        )
        return False

    def build_overview_embed(self) -> discord.Embed:
        e = discord.Embed(
            title=f"👑 {self.data.get('name')} | Lvl {self.data.get('expLevel')}",
            color=0x3498DB,
        )
        e.set_thumbnail(url="https://royaleapi.github.io/cr-api-assets/arenas/arena_gold.png")
        e.add_field(
            name="🏆 Trophies",
            value=f"Current: **{self.data.get('trophies', 0)}**\nBest: **{self.data.get('bestTrophies', 0)}**",
            inline=True,
        )
        wins = self.data.get("wins", 0)
        losses = self.data.get("losses", 0)
        total = wins + losses
        win_rate = round((wins / total * 100), 1) if total > 0 else 0
        e.add_field(
            name="⚔️ Combat Stats",
            value=f"Wins: **{wins}**\nLosses: **{losses}**\nWin Rate: **{win_rate}%**",
            inline=True,
        )
        e.add_field(
            name="👑 Crowns",
            value=f"3-Crown Wins: **{self.data.get('threeCrownWins', 0)}**\nTotal Battles: **{self.data.get('battleCount', 0)}**",
            inline=False,
        )
        return e

    def build_social_embed(self) -> discord.Embed:
        e = discord.Embed(title=f"🛡️ Social & War | {self.data.get('name')}", color=0xE67E22)
        clan = self.data.get("clan")
        if clan:
            e.add_field(
                name="Clan",
                value=f"**{clan.get('name')}** ({clan.get('tag')})\nRole: {self.data.get('role', 'Member').capitalize()}",
                inline=False,
            )
        else:
            e.add_field(name="Clan", value="Not currently in a clan.", inline=False)
        e.add_field(
            name="🎁 Donations",
            value=f"Given: **{self.data.get('donations', 0)}**\nReceived: **{self.data.get('donationsReceived', 0)}**\nLifetime: **{self.data.get('totalDonations', 0)}**",
            inline=True,
        )
        e.add_field(
            name="⛵ Clan Wars",
            value=f"War Day Wins: **{self.data.get('warDayWins', 0)}**",
            inline=True,
        )
        return e

    def build_deck_embed(self) -> discord.Embed:
        e = discord.Embed(title=f"🃏 Deck & Collection | {self.data.get('name')}", color=0x9B59B6)
        fav_card = self.data.get("currentFavouriteCard", {})
        if fav_card:
            e.add_field(
                name="⭐ Favorite Card",
                value=f"**{fav_card.get('name', 'Unknown')}**",
                inline=False,
            )

        cards = self.data.get("currentDeck", [])
        deck_str = "\n".join([f"• **{c['name']}** (Lvl {c['level']})" for c in cards])

        e.add_field(
            name="⚔️ Current Battle Deck",
            value=deck_str or "No deck found.",
            inline=False,
        )
        e.add_field(
            name="📚 Collection",
            value=f"Cards Found: **{self.data.get('cardsFound', 0)}**",
            inline=False,
        )
        return e

    @discord.ui.button(label="Overview", style=discord.ButtonStyle.primary, emoji="📊")
    async def btn_overview(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.build_overview_embed())

    @discord.ui.button(label="Social & War", style=discord.ButtonStyle.success, emoji="🛡️")
    async def btn_social(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.build_social_embed())

    @discord.ui.button(label="Deck & Cards", style=discord.ButtonStyle.secondary, emoji="🃏")
    async def btn_deck(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.build_deck_embed())


class ClashRoyale(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.users = bot.db_users
        self.guilds = self.db["guilds"]
        self.mongo_cache = self.db["api_cache"]
        self.api_base = "https://proxy.royaleapi.dev/v1"
        self.role_id = int(os.getenv("BADGE_ROLE_ID", 1464091054960803893))

        # Core Configuration Parameters
        self.clan_tag = "9LVY89UP"  
        self.max_card_level = 16    

        self.all_cards: list[str] = []
        self.active_warmups: set[str] = set()

        self.reminder_loop.start()
        
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._cache_cards())
            else:
                loop.create_task(self._cache_cards())
        except RuntimeError:
            pass

    def cog_unload(self):
        self.reminder_loop.cancel()

    async def cog_before_invoke(self, ctx):
        if ctx.command and ctx.command.name in WARMUP_RELEVANT_COMMANDS:
            await self._check_and_start_warmup()
        if not self.all_cards and ctx.command and ctx.command.name in {"whohas"}:
            await self._cache_cards()

    async def _check_and_start_warmup(self):
        warm_key = f"warmed_today:{self.clan_tag}"
        is_warmed = await self._cache_get(warm_key)

        if is_warmed or self.clan_tag in self.active_warmups:
            return

        self.active_warmups.add(self.clan_tag)
        await self._cache_set(warm_key, True, TTL_BATTLE_LOG)
        asyncio.create_task(self._run_warmup_task())

    async def _run_warmup_task(self):
        try:
            log.info(f"⏰ Warming cache for clan #{self.clan_tag}...")
            clan_data = await self._get_clan_data(self.clan_tag)
            if not clan_data: return

            members = clan_data.get("memberList", [])
            sem = asyncio.Semaphore(CONCURRENT_REQUESTS)

            async def warm_member(member):
                async with sem:
                    raw_tag = member["tag"].replace("#", "")
                    url_log = f"{self.api_base}/players/{quote('#' + raw_tag)}/battlelog"
                    await self._api_get(url_log, cache_key=f"battlelog:{raw_tag}", ttl=TTL_BATTLE_LOG)
                    url_player = f"{self.api_base}/players/{quote('#' + raw_tag)}"
                    await self._api_get(url_player, cache_key=f"player:{raw_tag}", ttl=TTL_PROFILES)

            await asyncio.gather(*[warm_member(m) for m in members])
            log.info(f"✅ Cache warming complete for #{self.clan_tag}.")
        except Exception as exc:
            log.error(f"Error during warmup for #{self.clan_tag}: {exc}")
        finally:
            self.active_warmups.discard(self.clan_tag)

    async def wait_if_warming(self, ctx):
        if self.clan_tag in self.active_warmups:
            msg = await ctx.send("⏳ **Waking up!** Gathering today's data for the clan. This takes about 10–15 seconds…")
            while self.clan_tag in self.active_warmups:
                await asyncio.sleep(1)
            await msg.delete()

    async def _cache_get(self, key: str):
        if self.bot.redis_available:
            raw = await self.bot.redis.get(key)
            if raw: return json.loads(raw)
        else:
            import time
            doc = await self.mongo_cache.find_one({"_id": key})
            if doc and doc.get("expires_at", 0) > time.time():
                return doc["data"]
        return None

    async def _cache_set(self, key: str, value, ttl: int):
        if self.bot.redis_available:
            await self.bot.redis.setex(key, ttl, json.dumps(value))
        else:
            import time
            await self.mongo_cache.update_one(
                {"_id": key},
                {"$set": {"data": value, "expires_at": time.time() + ttl}},
                upsert=True,
            )

    async def _cache_delete(self, key: str):
        if self.bot.redis_available:
            await self.bot.redis.delete(key)
        else:
            await self.mongo_cache.delete_one({"_id": key})

    async def _api_get(self, url: str, cache_key: str = None, ttl: int = TTL_PLAYER):
        if cache_key:
            cached = await self._cache_get(cache_key)
            if cached is not None: return cached

        delay = RETRY_BACKOFF
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with self.bot.http_session.get(url, headers=self.bot._cr_headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()

                        if isinstance(data, dict):
                            if "cards" in data:
                                for c in data["cards"]:
                                    c["level"] = self.max_card_level - c.get("maxLevel", self.max_card_level) + c.get("level", 1)
                            if "currentDeck" in data:
                                for c in data["currentDeck"]:
                                    c["level"] = self.max_card_level - c.get("maxLevel", self.max_card_level) + c.get("level", 1)

                        if cache_key: await self._cache_set(cache_key, data, ttl)
                        return data

                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", delay))
                        await asyncio.sleep(retry_after)
                        delay *= 2
                        continue
                    return None
            except asyncio.TimeoutError:
                await asyncio.sleep(delay)
                delay *= 2
            except Exception as exc:
                log.error(f"Unexpected error fetching {url}: {exc}")
                return None
        return None

    async def _get_player_data(self, tag: str):
        clean_tag = tag.upper().replace("#", "")
        url = f"{self.api_base}/players/{quote('#' + clean_tag)}"
        return await self._api_get(url, cache_key=f"player:{clean_tag}", ttl=TTL_PLAYER)

    async def _get_clan_data(self, clan_tag: str):
        url = f"{self.api_base}/clans/{quote('#' + clan_tag)}"
        return await self._api_get(url, cache_key=f"clan:{clan_tag}", ttl=TTL_CLAN)

    async def _fetch_members_concurrent(self, member_list: list) -> list:
        sem = asyncio.Semaphore(CONCURRENT_REQUESTS)
        async def fetch_one(member):
            async with sem: return await self._get_player_data(member["tag"])
        results = await asyncio.gather(*[fetch_one(m) for m in member_list])
        return [r for r in results if r is not None]

    def _is_maxed(self, card: dict) -> bool:
        return card.get("level", 1) >= self.max_card_level

    async def _cache_cards(self):
        cache_key = "cards:all"
        cached = await self._cache_get(cache_key)
        if cached:
            self.all_cards = cached
            return
        data = await self._api_get(f"{self.api_base}/cards", cache_key=cache_key, ttl=TTL_CARDS)
        if data:
            self.all_cards = [c["name"] for c in data.get("items", [])]

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ **Woah there!** Please wait {error.retry_after:.1f} seconds.")
        else:
            log.error(f"Error in command '{ctx.command}': {error}")

    @commands.command(aliases=["profile", "analytics"])
    async def p(self, ctx, *, target: str = None):
        tag_to_search = None
        if target:
            if target.startswith("<@"):
                user_id = "".join(filter(str.isdigit, target))
                user_doc = await self.users.find_one({"_id": user_id})
                if user_doc: tag_to_search = user_doc["player_id"]
                else: return await ctx.send("❌ That user hasn't linked their account.")
            else:
                tag_to_search = target.upper().replace("#", "")
        else:
            user_doc = await self.users.find_one({"_id": str(ctx.author.id)})
            if user_doc: tag_to_search = user_doc["player_id"]
            else: return await ctx.send(f"❌ Account unlinked. Use `{self.bot.active_prefix}link <tag>` first.")

        msg = await ctx.send("🔍 Generating analytics…")
        data = await self._get_player_data(tag_to_search)
        if not data: return await msg.edit(content="❌ Failed to fetch player profile.")

        view = ProfileView(data, ctx.author.id)
        await msg.edit(content=None, embed=view.build_overview_embed(), view=view)

    @commands.command()
    async def link(self, ctx, tag: str):
        clean_tag = tag.upper().replace("#", "")
        if len(clean_tag) < 3: return await ctx.send("❌ Invalid player tag.")

        data = await self._get_player_data(clean_tag)
        if not data: return await ctx.send(f"❌ Player not found: **#{clean_tag}**.")

        await self.users.update_one({"_id": str(ctx.author.id)}, {"$set": {"player_id": clean_tag}}, upsert=True)
        role = ctx.guild.get_role(self.role_id)
        if role:
            try:
                await ctx.author.add_roles(role)
                await ctx.send(f"✅ Linked **{data['name']}** and granted **{role.name}**!")
            except discord.Forbidden:
                await ctx.send(f"✅ Linked **{data['name']}**, but I lack role permissions.")
        else:
            await ctx.send(f"✅ Linked **{data['name']}**.")

    @commands.command()
    async def stats(self, ctx, target: discord.Member = None):
        member = target or ctx.author
        user_doc = await self.users.find_one({"_id": str(member.id)})
        if not user_doc: return await ctx.send(f"❌ Account unlinked. Use `{self.bot.active_prefix}link <tag>` first.")

        data = await self._get_player_data(user_doc["player_id"])
        if not data: return await ctx.send("❌ Failed to fetch parameters.")

        embed = discord.Embed(title=f"📊 Stats for {data.get('name', 'Unknown')}", color=0x00FF00)
        embed.add_field(name="Trophies", value=f"🏆 {data.get('trophies', 'N/A')}", inline=True)
        embed.add_field(name="Best Trophies", value=f"🏅 {data.get('bestTrophies', 'N/A')}", inline=True)
        embed.add_field(name="Wins", value=f"⚔️ {data.get('wins', 'N/A')}", inline=True)
        embed.add_field(name="Losses", value=f"💀 {data.get('losses', 'N/A')}", inline=True)
        await ctx.send(embed=embed)

    @commands.command(aliases=["deck"])
    async def decks(self, ctx, target: discord.Member = None):
        member = target or ctx.author
        user_doc = await self.users.find_one({"_id": str(member.id)})
        if not user_doc: return await ctx.send("❌ Account not linked.")

        data = await self._get_player_data(user_doc["player_id"])
        if not data: return await ctx.send("❌ Failed to fetch player deck.")

        embed = discord.Embed(title=f"⚔️ {data['name']}'s Current Deck", color=0xEE82EE)
        cards = data.get("currentDeck", [])
        embed.description = "\n".join([f"• **{c['name']}** (Lvl {c['level']})" for c in cards]) or "No deck found."
        await ctx.send(embed=embed)

    @commands.command()
    async def chests(self, ctx, target: discord.Member = None):
        member = target or ctx.author
        user_doc = await self.users.find_one({"_id": str(member.id)})
        if not user_doc: return await ctx.send("❌ Account unlinked.")

        clean_tag = user_doc["player_id"]
        url = f"{self.api_base}/players/{quote('#' + clean_tag)}/upcomingchests"
        data = await self._api_get(url, cache_key=f"chests:{clean_tag}", ttl=60 * 5)
        if not data or "items" not in data: return await ctx.send("❌ Failed to fetch chest items.")

        embed = discord.Embed(title=f"🎁 Upcoming Chests for #{clean_tag}", color=0xFFD700)
        for chest in data["items"]:
            index = chest.get("index")
            name = chest.get("name", "Unknown Chest")
            if index == 0: embed.add_field(name="Next Chest", value=name, inline=False)
            elif "Silver" not in name and "Gold" not in name:
                embed.add_field(name=f"In {index} wins", value=f"✨ {name}", inline=True)
        await ctx.send(embed=embed)

    @commands.command()
    async def log(self, ctx, target: discord.Member = None):
        member = target or ctx.author
        user_doc = await self.users.find_one({"_id": str(member.id)})
        if not user_doc: return await ctx.send("❌ Account unlinked.")

        clean_tag = user_doc["player_id"]
        url = f"{self.api_base}/players/{quote('#' + clean_tag)}/battlelog"
        data = await self._api_get(url, cache_key=f"battlelog:{clean_tag}", ttl=60 * 5)
        if not data: return await ctx.send("❌ Failed to fetch logs.")

        embed = discord.Embed(title=f"⚔️ Last 5 Battles for #{clean_tag}", color=0x3498DB)
        for battle in data[:5]:
            team = battle["team"][0]
            opponent = battle["opponent"][0]
            crowns_team = team.get("crowns", 0)
            crowns_opp = opponent.get("crowns", 0)
            result = "🟢 Victory" if crowns_team > crowns_opp else "🔴 Defeat" if crowns_opp > crowns_team else "⚪ Draw"
            embed.add_field(name=f"{result} ({crowns_team} – {crowns_opp})", value=f"**Vs:** {opponent.get('name', 'Unknown')}", inline=False)
        await ctx.send(embed=embed)

    @commands.command()
    async def clan(self, ctx):
        data = await self._get_clan_data(self.clan_tag)
        if not data: return await ctx.send("❌ Could not fetch clan logs.")

        embed = discord.Embed(title=f"🛡️ {data.get('name')} ({data.get('tag')})", description=data.get("description", ""), color=0x9B59B6)
        embed.add_field(name="Members", value=f"👥 {data.get('members', 0)}/50", inline=True)
        embed.add_field(name="Score", value=f"🏆 {data.get('clanScore', 0)}", inline=True)
        await ctx.send(embed=embed)

    @commands.command()
    async def war(self, ctx):
        url = f"{self.api_base}/clans/{quote('#' + self.clan_tag)}/currentriverrace"
        data = await self._api_get(url, cache_key=f"currentrace:{self.clan_tag}", ttl=TTL_WAR)
        if not data: return await ctx.send("❌ Failed to parse war data.")

        state = data.get("state", "Unknown").title()
        clan_info = data.get("clan", {})
        embed = discord.Embed(title="⛵ Current War Status", color=0xE67E22)
        embed.add_field(name="Status", value=state, inline=False)
        embed.add_field(name="Fame", value=f"⭐ {clan_info.get('fame', 0)}", inline=True)
        await ctx.send(embed=embed)

    @commands.command()
    async def race(self, ctx, period: str = "current"):
        embed = discord.Embed(color=0x1ABC9C)
        if period.lower() == "last":
            url = f"{self.api_base}/clans/{quote('#' + self.clan_tag)}/riverracelog"
            data = await self._api_get(url, cache_key=f"racelog:{self.clan_tag}", ttl=60 * 60)
            if not data or not data.get("items"): return await ctx.send("❌ No past logs found.")
            standings = data["items"][0].get("standings", [])
        else:
            url = f"{self.api_base}/clans/{quote('#' + self.clan_tag)}/currentriverrace"
            data = await self._api_get(url, cache_key=f"currentrace:{self.clan_tag}", ttl=TTL_WAR)
            if not data: return await ctx.send("❌ Failed to map current race data.")
            standings = data.get("clans", [])

        embed.title = "⛵ River Race Standings"
        standings = sorted(standings, key=lambda x: x.get("fame", 0) if "fame" in x else x.get("clan", {}).get("fame", 0), reverse=True)
        for i, c in enumerate(standings[:5], start=1):
            c_info = c.get("clan", c)
            embed.add_field(name=f"#{i} {c_info.get('name', 'Unknown')}", value=f"⭐ {c_info.get('fame', 0)} Fame", inline=False)
        await ctx.send(embed=embed)

    @commands.command()
    async def whohas(self, ctx, *, card_name: str = None):
        await self.wait_if_warming(ctx)
        clan_data = await self._get_clan_data(self.clan_tag)
        if not clan_data: return await ctx.send("❌ Could not resolve roster analytics.")

        members = clan_data.get("memberList", [])
        profiles = await self._fetch_members_concurrent(members)

        if card_name:
            if not self.all_cards: await self._cache_cards()
            match, score = process.extractOne(card_name, self.all_cards)
            if score < 60: return await ctx.send(f"❓ Card match low for '{card_name}'.")
            target_card = match
        else:
            counts = Counter(card["name"] for p in profiles for card in p.get("cards", []) if self._is_maxed(card))
            if not counts: return await ctx.send("❌ No maxed cards tracked inside database logs.")
            target_card = counts.most_common(1)[0][0]

        name_by_tag = {m["tag"].replace("#", "").upper(): m["name"] for m in members}
        hits = [f"• **{name_by_tag.get(p.get('tag', '').replace('#', '').upper(), p.get('name'))}**" for p in profiles for card in p.get("cards", []) if card["name"] == target_card and self._is_maxed(card)]

        header = f"📊 **Owners of {target_card} (Lvl {self.max_card_level}+):**"
        response = f"{header}\n" + "\n".join(hits) if hits else f"❌ Nobody has **{target_card}** maxed."
        if len(response) > 1900: response = response[:1900] + "\n… and more."
        await ctx.send(response)

    @commands.cooldown(1, HEAVY_COMMANDS_COOLDOWN, commands.BucketType.guild)
    @commands.command()
    async def cardstats(self, ctx):
        await self.wait_if_warming(ctx)
        msg = await ctx.send("📊 **Compiling Report…**")
        clan_data = await self._get_clan_data(self.clan_tag)
        if not clan_data: return await msg.edit(content="❌ Could not fetch data.")

        profiles = await self._fetch_members_concurrent(clan_data.get("memberList", []))
        card_to_members: dict[str, list[str]] = {}
        for p in profiles:
            for card in p.get("cards", []):
                if self._is_maxed(card): card_to_members.setdefault(card["name"], []).append(p.get("name", "Unknown"))

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Card Name", "Members"])
        for card_name, card_members in sorted(card_to_members.items(), key=lambda x: len(x[1]), reverse=True):
            writer.writerow([f"{card_name} ({len(card_members)})", ", ".join(sorted(card_members))])
        output.seek(0)

        await msg.delete()
        await ctx.send(content="✅ Card stats successfully compiled!", file=discord.File(fp=output, filename="Card_Report.csv"))

    @commands.command()
    async def forecast(self, ctx):
        url = f"{self.api_base}/clans/{quote('#' + self.clan_tag)}/currentriverrace"
        data = await self._api_get(url, cache_key=f"currentrace:{self.clan_tag}", ttl=TTL_WAR)
        if not data: return await ctx.send("❌ Error capturing metrics.")

        clan_info = data.get("clan", {})
        fame = clan_info.get("fame", 0)
        
        # FIX 9: Re-applied smooth multi-tier structural fallback checks across array elements
        participants = clan_info.get("participants", [])
        if not participants and "clans" in data:
            for c in data["clans"]:
                if c.get("tag", "").replace("#", "").upper() == self.clan_tag.upper():
                    participants = c.get("participants", [])
                    break

        if not participants: return await ctx.send("❌ No activity participants verified today.")
        if fame >= 10_000: return await ctx.send("✅ Clan race completed!")

        decks_used = sum(p.get("decksUsedToday", 0) for p in participants)
        fame_earned = sum(p.get("fame", 0) for p in participants)
        avg_fame = fame_earned / decks_used if decks_used > 0 else 150
        projected = fame + int(((len(participants) * 4) - decks_used) * avg_fame)

        if projected >= 10_000: await ctx.send(f"📈 On pace! Projected calculation: **{projected:,}** Fame.")
        else: await ctx.send(f"📉 Under pace. Projected target ceiling: **{projected:,}** Fame.")

    @commands.cooldown(1, HEAVY_COMMANDS_COOLDOWN, commands.BucketType.guild)
    @commands.command()
    async def scout(self, ctx):
        await self.wait_if_warming(ctx)
        msg = await ctx.send("🔍 Scanning enemy deck lists…")
        clan_data = await self._get_clan_data(self.clan_tag)
        if not clan_data: return await msg.edit(content="❌ Meta mapping failed.")

        battle_logs = await asyncio.gather(*[self._api_get(f"{self.api_base}/players/{quote('#' + m['tag'].replace('#',''))}/battlelog", cache_key=f"battlelog:{m['tag'].replace('#','')}", ttl=TTL_BATTLE_LOG) for m in clan_data.get("memberList", [])])
        opponent_cards: Counter = Counter()
        for log_entry in [b for b in battle_logs if b]:
            for battle in log_entry[:3]:
                for opp in battle.get("opponent", []):
                    for card in opp.get("cards", []): opponent_cards[card["name"]] += 1

        embed = discord.Embed(title="🕵️ Meta Analysis", description="Top observed enemy selections:", color=0xE74C3C)
        for card, count in opponent_cards.most_common(5): embed.add_field(name=card, value=f"Seen {count} times", inline=False)
        await msg.edit(content=None, embed=embed)

    @commands.cooldown(1, HEAVY_COMMANDS_COOLDOWN, commands.BucketType.guild)
    @commands.command()
    async def primetime(self, ctx):
        await self.wait_if_warming(ctx)
        msg = await ctx.send("🕒 Tracking time arrays…")
        clan_data = await self._get_clan_data(self.clan_tag)
        if not clan_data: return await msg.edit(content="❌ Heatmap metrics unreachable.")

        results = await asyncio.gather(*[self._api_get(f"{self.api_base}/players/{quote('#' + m['tag'].replace('#',''))}/battlelog", cache_key=f"battlelog:{m['tag'].replace('#','')}", ttl=TTL_BATTLE_LOG) for m in clan_data.get("memberList", [])])
        
        total_counts = Counter()
        for log_entry in [r for r in results if r]:
            for b in log_entry:
                ts = b.get("battleTime", "")
                if len(ts) >= 15:
                    h = datetime.strptime(ts[:15], "%Y%m%dT%H%M%S").replace(tzinfo=zoneinfo.ZoneInfo("UTC")).astimezone(zoneinfo.ZoneInfo("America/New_York")).hour
                    total_counts[h] += 1

        top_hour = total_hour_counts.most_common(1)[0][0] if total_counts else 20
        await msg.delete()
        await ctx.send(f"🔥 Prime active window evaluates to **{top_hour:02d}:00 Eastern Time Zone** metrics.")

    @commands.command()
    @commands.has_permissions(manage_guild=True)
    async def setreminders(self, ctx, channel: discord.TextChannel):
        await self.guilds.update_one({"_id": str(ctx.guild.id)}, {"$set": {"channel_id": channel.id}}, upsert=True)
        await ctx.send(f"✅ Reminders successfully targeted on {channel.mention}.")

    @tasks.loop(hours=12)
    async def reminder_loop(self):
        async for g in self.guilds.find({"channel_id": {"$exists": True}}):
            channel = self.bot.get_channel(g["channel_id"])
            if channel:
                try: await channel.send("⚔️ **War Reminder:** River Race is active. Burn your remaining card logs immediately!")
                except discord.HTTPException: pass
            await asyncio.sleep(0.5)

    @reminder_loop.before_loop
    async def before_reminder_loop(self): await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(ClashRoyale(bot))