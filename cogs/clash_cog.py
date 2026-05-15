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

CLAN_TAG            = "9LVY89UP"
MAX_CARD_LEVEL      = 16
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

        self.all_cards: list[str] = []
        self.active_warmups: set[str] = set()

        self.reminder_loop.start()
        self.bot.loop.create_task(self._cache_cards())

    def cog_unload(self):
        self.reminder_loop.cancel()

    async def cog_before_invoke(self, ctx):
        if ctx.command and ctx.command.name in WARMUP_RELEVANT_COMMANDS:
            await self._check_and_start_warmup()

    async def _check_and_start_warmup(self):
        warm_key = f"warmed_today:{CLAN_TAG}"
        is_warmed = await self._cache_get(warm_key)

        if is_warmed or CLAN_TAG in self.active_warmups:
            return

        self.active_warmups.add(CLAN_TAG)
        await self._cache_set(warm_key, True, TTL_BATTLE_LOG)
        asyncio.create_task(self._run_warmup_task())

    async def _run_warmup_task(self):
        try:
            log.info(f"⏰ Warming cache for clan #{CLAN_TAG}...")
            clan_data = await self._get_clan_data(CLAN_TAG)
            if not clan_data:
                return

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
            log.info(f"✅ Cache warming complete for #{CLAN_TAG}.")
        except Exception as exc:
            log.error(f"Error during warmup for #{CLAN_TAG}: {exc}")
        finally:
            self.active_warmups.discard(CLAN_TAG)

    async def wait_if_warming(self, ctx):
        if CLAN_TAG in self.active_warmups:
            msg = await ctx.send("⏳ **Waking up!** Gathering today's data for the clan. This takes about 10–15 seconds…")
            while CLAN_TAG in self.active_warmups:
                await asyncio.sleep(1)
            await msg.delete()

    async def _cache_get(self, key: str):
        if self.bot.redis_available:
            raw = await self.bot.redis.get(key)
            if raw:
                return json.loads(raw)
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
            if cached is not None:
                return cached

        delay = RETRY_BACKOFF
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with self.bot.http_session.get(url, headers=self.bot._cr_headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if cache_key:
                            await self._cache_set(cache_key, data, ttl)
                        return data

                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", delay))
                        log.warning(f"Rate limited (attempt {attempt}/{MAX_RETRIES}). Waiting {retry_after}s")
                        await asyncio.sleep(retry_after)
                        delay *= 2
                        continue

                    log.warning(f"CR API returned {resp.status} for {url}")
                    return None

            except asyncio.TimeoutError:
                log.warning(f"Timeout on attempt {attempt} for {url}")
                await asyncio.sleep(delay)
                delay *= 2
            except Exception as exc:
                log.error(f"Unexpected error fetching {url}: {exc}")
                return None

        log.error(f"All {MAX_RETRIES} retries exhausted for {url}")
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
            async with sem:
                return await self._get_player_data(member["tag"])
        results = await asyncio.gather(*[fetch_one(m) for m in member_list])
        return [r for r in results if r is not None]

    def _is_maxed(self, card: dict) -> bool:
        return card.get("level", 1) >= MAX_CARD_LEVEL

    async def _cache_cards(self):
        cache_key = "cards:all"
        cached = await self._cache_get(cache_key)
        if cached:
            self.all_cards = cached
            log.info(f"✅ Loaded {len(self.all_cards)} cards from cache.")
            return
        data = await self._api_get(f"{self.api_base}/cards", cache_key=cache_key, ttl=TTL_CARDS)
        if data:
            self.all_cards = [c["name"] for c in data.get("items", [])]
            log.info(f"✅ Fetched and cached {len(self.all_cards)} cards.")

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ **Woah there!** That's a heavy command. Please wait {error.retry_after:.1f} seconds before running it again.")
        else:
            log.error(f"Error in command '{ctx.command}': {error}")

    @commands.command(aliases=["profile", "analytics"])
    async def p(self, ctx, *, target: str = None):
        """Interactive RoyaleAPI-style deep dive into a player's profile."""
        tag_to_search = None
        if target:
            if target.startswith("<@"):
                user_id = "".join(filter(str.isdigit, target))
                user_doc = await self.users.find_one({"_id": user_id})
                if user_doc:
                    tag_to_search = user_doc["player_id"]
                else:
                    return await ctx.send("❌ That user hasn't linked their account.")
            else:
                tag_to_search = target.upper().replace("#", "")
        else:
            user_doc = await self.users.find_one({"_id": str(ctx.author.id)})
            if user_doc:
                tag_to_search = user_doc["player_id"]
            else:
                return await ctx.send("❌ You haven't linked your account. Use `!link <tag>` first.")

        msg = await ctx.send("🔍 Generating deep-dive analytics…")
        data = await self._get_player_data(tag_to_search)

        if not data:
            return await msg.edit(content="❌ Failed to fetch player profile from the API.")

        view = ProfileView(data, ctx.author.id)
        await msg.edit(content=None, embed=view.build_overview_embed(), view=view)

    @commands.command()
    async def link(self, ctx, tag: str):
        """Link your Discord account to a player tag."""
        clean_tag = tag.upper().lstrip("#")
        if len(clean_tag) < 3:
            return await ctx.send("❌ That doesn't look like a valid player tag.")

        data = await self._get_player_data(clean_tag)
        if not data:
            return await ctx.send(f"❌ Could not find a player with tag **#{clean_tag}**.")

        await self.users.update_one(
            {"_id": str(ctx.author.id)},
            {"$set": {"player_id": clean_tag}},
            upsert=True,
        )

        role = ctx.guild.get_role(self.role_id)
        if role:
            try:
                await ctx.author.add_roles(role)
                await ctx.send(f"✅ Linked **{data['name']}** (#{clean_tag}) and gave you **{role.name}**!")
            except discord.Forbidden:
                await ctx.send(f"✅ Linked **{data['name']}** (#{clean_tag}), but I can't manage roles.")
        else:
            await ctx.send(f"✅ Linked **{data['name']}** (#{clean_tag}).")

    @commands.command()
    async def stats(self, ctx, target: discord.Member = None):
        """Displays basic Clash Royale stats for a linked member."""
        member = target or ctx.author
        user_doc = await self.users.find_one({"_id": str(member.id)})

        if not user_doc:
            return await ctx.send("❌ Account not linked. Use `!link <tag>` first.")

        data = await self._get_player_data(user_doc["player_id"])
        if not data:
            return await ctx.send("❌ Failed to fetch player stats. Try again later.")

        embed = discord.Embed(title=f"📊 Stats for {data.get('name', 'Unknown')}", color=0x00FF00)
        embed.add_field(name="Trophies", value=f"🏆 {data.get('trophies', 'N/A')}", inline=True)
        embed.add_field(name="Best Trophies", value=f"🏅 {data.get('bestTrophies', 'N/A')}", inline=True)
        embed.add_field(name="Wins", value=f"⚔️ {data.get('wins', 'N/A')}", inline=True)
        embed.add_field(name="Losses", value=f"💀 {data.get('losses', 'N/A')}", inline=True)

        clan = data.get("clan")
        if clan:
            embed.add_field(name="Clan", value=f"🛡️ {clan.get('name')} ({clan.get('tag')})", inline=False)
        await ctx.send(embed=embed)

    @commands.command(aliases=["deck"])
    async def decks(self, ctx, target: discord.Member = None):
        """Displays the current battle deck of a linked member."""
        member = target or ctx.author
        user_doc = await self.users.find_one({"_id": str(member.id)})
        if not user_doc:
            return await ctx.send("❌ Account not linked.")

        data = await self._get_player_data(user_doc["player_id"])
        if not data:
            return await ctx.send("❌ Failed to fetch player data. Try again later.")

        embed = discord.Embed(title=f"⚔️ {data['name']}'s Current Deck", color=0xEE82EE)
        cards = data.get("currentDeck", [])
        deck_str = "\n".join([f"• **{c['name']}** (Lvl {c['level']})" for c in cards])
        embed.description = deck_str or "No deck found."
        await ctx.send(embed=embed)

    @commands.command()
    async def chests(self, ctx, target: discord.Member = None):
        """Determines when you're going to get rare chests."""
        member = target or ctx.author
        user_doc = await self.users.find_one({"_id": str(member.id)})
        if not user_doc:
            return await ctx.send("❌ Account not linked. Use `!link <tag>` first.")

        clean_tag = user_doc["player_id"]
        url = f"{self.api_base}/players/{quote('#' + clean_tag)}/upcomingchests"
        data = await self._api_get(url, cache_key=f"chests:{clean_tag}", ttl=60 * 5)
        if not data or "items" not in data:
            return await ctx.send("❌ Failed to fetch chest data.")

        embed = discord.Embed(title=f"🎁 Upcoming Chests for #{clean_tag}", color=0xFFD700)
        for chest in data["items"]:
            index = chest.get("index")
            name = chest.get("name", "Unknown Chest")
            if index == 0:
                embed.add_field(name="Next Chest", value=name, inline=False)
            elif "Silver" not in name and "Gold" not in name:
                embed.add_field(name=f"In {index} wins", value=f"✨ {name}", inline=True)
        await ctx.send(embed=embed)

    @commands.command()
    async def log(self, ctx, target: discord.Member = None):
        """Lists the results of your last 5 battles."""
        member = target or ctx.author
        user_doc = await self.users.find_one({"_id": str(member.id)})
        if not user_doc:
            return await ctx.send("❌ Account not linked.")

        clean_tag = user_doc["player_id"]
        url = f"{self.api_base}/players/{quote('#' + clean_tag)}/battlelog"
        data = await self._api_get(url, cache_key=f"battlelog:{clean_tag}", ttl=60 * 5)
        if not data:
            return await ctx.send("❌ Failed to fetch battle log.")

        embed = discord.Embed(title=f"⚔️ Last 5 Battles for #{clean_tag}", color=0x3498DB)
        for battle in data[:5]:
            team = battle["team"][0]
            opponent = battle["opponent"][0]
            crowns_team = team.get("crowns", 0)
            crowns_opp = opponent.get("crowns", 0)
            result = "🟢 Victory" if crowns_team > crowns_opp else "🔴 Defeat" if crowns_opp > crowns_team else "⚪ Draw"
            mode = battle.get("type", "Unknown Mode").replace("_", " ").title()
            embed.add_field(
                name=f"{result} ({crowns_team} – {crowns_opp})",
                value=f"**Mode:** {mode}\n**Vs:** {opponent.get('name', 'Unknown')}",
                inline=False,
            )
        await ctx.send(embed=embed)

    @commands.command()
    async def clan(self, ctx):
        """Shows a general overview of the clan."""
        data = await self._get_clan_data(CLAN_TAG)
        if not data:
            return await ctx.send("❌ Could not fetch clan data.")

        embed = discord.Embed(
            title=f"🛡️ {data.get('name')} ({data.get('tag')})",
            description=data.get("description", ""),
            color=0x9B59B6,
        )
        embed.add_field(name="Members", value=f"👥 {data.get('members', 0)}/50", inline=True)
        embed.add_field(name="Score", value=f"🏆 {data.get('clanScore', 0)}", inline=True)
        embed.add_field(name="War Trophies", value=f"🏅 {data.get('clanWarTrophies', 0)}", inline=True)
        embed.add_field(name="Location", value=f"🌍 {data.get('location', {}).get('name', 'Unknown')}", inline=True)
        embed.add_field(name="Required Trophies", value=f"🔒 {data.get('requiredTrophies', 0)}", inline=True)
        await ctx.send(embed=embed)

    @commands.command()
    async def war(self, ctx):
        """Shows the current status of our clan as a quick overview."""
        url = f"{self.api_base}/clans/{quote('#' + CLAN_TAG)}/currentriverrace"
        data = await self._api_get(url, cache_key=f"currentrace:{CLAN_TAG}", ttl=TTL_WAR)
        if not data:
            return await ctx.send("❌ Could not fetch war data.")

        state = data.get("state", "Unknown").title()
        clan_info = data.get("clan", {})

        embed = discord.Embed(title="⛵ Current War Status", color=0xE67E22)
        embed.add_field(name="Status", value=state, inline=False)
        embed.add_field(name="Fame", value=f"⭐ {clan_info.get('fame', 0)}", inline=True)
        embed.add_field(name="Decks Used Today", value=f"🃏 {clan_info.get('periodPoints', 0)}", inline=True)
        await ctx.send(embed=embed)

    @commands.command()
    async def race(self, ctx, period: str = "current"):
        """Gives the results of the current river race, or the last if specified."""
        embed = discord.Embed(color=0x1ABC9C)

        if period.lower() == "last":
            url = f"{self.api_base}/clans/{quote('#' + CLAN_TAG)}/riverracelog"
            data = await self._api_get(url, cache_key=f"racelog:{CLAN_TAG}", ttl=60 * 60)
            if not data or not data.get("items"):
                return await ctx.send("❌ Could not fetch past race log.")

            embed.title = "🏁 Last River Race Results"
            standings = data["items"][0].get("standings", [])
            for i, standing in enumerate(standings[:5], start=1):
                c_data = standing.get("clan", {})
                embed.add_field(
                    name=f"#{i} {c_data.get('name', 'Unknown')}",
                    value=f"⭐ {c_data.get('fame', 0)} Fame",
                    inline=False,
                )
        else:
            url = f"{self.api_base}/clans/{quote('#' + CLAN_TAG)}/currentriverrace"
            data = await self._api_get(url, cache_key=f"currentrace:{CLAN_TAG}", ttl=TTL_WAR)
            if not data:
                return await ctx.send("❌ Could not fetch current race data.")

            embed.title = "⛵ Current River Race Standings"
            standings = sorted(data.get("clans", []), key=lambda x: x.get("fame", 0), reverse=True)
            for i, c in enumerate(standings[:5], start=1):
                embed.add_field(
                    name=f"#{i} {c.get('name', 'Unknown')}",
                    value=f"⭐ {c.get('fame', 0)} Fame",
                    inline=False,
                )
        await ctx.send(embed=embed)

    @commands.command()
    async def whohas(self, ctx, *, card_name: str = None):
        """Fuzzy search for maxed card owners in the clan."""
        await self.wait_if_warming(ctx)
        clan_data = await self._get_clan_data(CLAN_TAG)
        if not clan_data:
            return await ctx.send("❌ Could not fetch clan data. Try again later.")

        members = clan_data.get("memberList", [])
        profiles = await self._fetch_members_concurrent(members)

        if card_name:
            if not self.all_cards:
                await self._cache_cards()
            match, score = process.extractOne(card_name, self.all_cards)
            if score < 60:
                return await ctx.send(f"❓ Could not find a card similar to '{card_name}'.")
            target_card = match
        else:
            await ctx.send("🔍 Finding your clan's most common maxed card…")
            counts = Counter(
                card["name"] for p in profiles for card in p.get("cards", []) if self._is_maxed(card)
            )
            if not counts:
                return await ctx.send("❌ No maxed cards found in the clan.")
            target_card = counts.most_common(1)[0][0]

        name_by_tag = {m["tag"].replace("#", "").upper(): m["name"] for m in members}
        hits = []
        for p in profiles:
            tag_clean = p.get("tag", "").replace("#", "").upper()
            player_name = name_by_tag.get(tag_clean, p.get("name", "Unknown"))
            for card in p.get("cards", []):
                if card["name"] == target_card and self._is_maxed(card):
                    hits.append(f"• **{player_name}** (Lvl {card['level']})")

        header = f"📊 **Owners of {target_card} (Lvl {MAX_CARD_LEVEL}):**"
        if hits:
            await ctx.send(f"{header}\n" + "\n".join(hits))
        else:
            await ctx.send(f"❌ Nobody in the clan has **{target_card}** maxed.")

    @commands.cooldown(1, HEAVY_COMMANDS_COOLDOWN, commands.BucketType.guild)
    @commands.command()
    async def cardstats(self, ctx):
        """Generates a CSV listing every member's maxed cards, sorted by popularity."""
        await self.wait_if_warming(ctx)
        msg = await ctx.send("📊 **Generating Detailed Card Power Report…**")

        clan_data = await self._get_clan_data(CLAN_TAG)
        if not clan_data:
            return await msg.edit(content="❌ Could not fetch clan data.")

        profiles = await self._fetch_members_concurrent(clan_data.get("memberList", []))

        card_to_members: dict[str, list[str]] = {}
        for p in profiles:
            player_name = p.get("name", "Unknown")
            for card in p.get("cards", []):
                if self._is_maxed(card):
                    card_to_members.setdefault(card["name"], []).append(player_name)

        sorted_cards = sorted(card_to_members.items(), key=lambda item: len(item[1]), reverse=True)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Card Name (Total Maxed)", "Members with Max (Lvl 16)"])
        for card_name, card_members in sorted_cards:
            writer.writerow([f"{card_name} ({len(card_members)})", ", ".join(sorted(card_members))])
        output.seek(0)

        csv_file = discord.File(fp=output, filename="Detailed_Clan_Card_Report.csv")
        await msg.edit(
            content=f"✅ Report complete! Found **{len(card_to_members)}** different maxed cards across {len(profiles)} members.",
            attachments=[csv_file]
        )

    @commands.command()
    async def forecast(self, ctx):
        """Calculates if your clan will finish the race today based on current pace."""
        url = f"{self.api_base}/clans/{quote('#' + CLAN_TAG)}/currentriverrace"
        data = await self._api_get(url, cache_key=f"currentrace:{CLAN_TAG}", ttl=TTL_WAR)
        if not data:
            return await ctx.send("❌ Could not fetch war data.")

        period_type = data.get("periodType", "warDay")
        if period_type == "training":
            return await ctx.send("📋 **Forecast:** Today is a **Training Day** — there's no finish line to forecast. Save your decks for War Day!")

        clan_info = data.get("clan", {})
        fame = clan_info.get("fame", 0)
        participants = clan_info.get("participants", [])

        if not participants:
            return await ctx.send("❌ Could not read participant data for the forecast.")

        goal = data.get("periodPoints", 10_000) or 10_000
        if fame >= goal:
            return await ctx.send("✅ **Forecast:** Your clan has already finished the race!")

        decks_used_today = sum(p.get("decksUsedToday", 0) for p in participants)
        fame_earned_today = sum(p.get("fame", 0) for p in participants)
        avg_fame_per_deck = fame_earned_today / decks_used_today if decks_used_today > 0 else 150
        decks_remaining = (len(participants) * 4) - decks_used_today
        projected_fame = fame + int(decks_remaining * avg_fame_per_deck)

        avg_label = f"{avg_fame_per_deck:.0f} fame/deck (clan average today)"
        if projected_fame >= goal:
            await ctx.send(f"📈 **Forecast:** Yes! You are at **{fame:,}/{goal:,}** Fame. At {avg_label}, you project to hit **{projected_fame:,}** Fame today.")
        else:
            shortfall = goal - projected_fame
            await ctx.send(f"📉 **Forecast:** Unlikely. You are at **{fame:,}/{goal:,}** Fame. At {avg_label}, you project to reach **{projected_fame:,}** Fame — **{shortfall:,}** short of the finish line.")

    @commands.cooldown(1, HEAVY_COMMANDS_COOLDOWN, commands.BucketType.guild)
    @commands.command()
    async def scout(self, ctx):
        """Analyzes recent battles from all clan members to show the current meta."""
        await self.wait_if_warming(ctx)
        msg = await ctx.send("🔍 Scouting enemy meta…")

        clan_data = await self._get_clan_data(CLAN_TAG)
        if not clan_data:
            return await msg.edit(content="❌ Could not fetch clan data.")

        members = clan_data.get("memberList", [])
        sem = asyncio.Semaphore(CONCURRENT_REQUESTS)

        async def fetch_battle_log(member):
            async with sem:
                raw_tag = member["tag"].replace("#", "")
                url = f"{self.api_base}/players/{quote('#' + raw_tag)}/battlelog"
                return await self._api_get(url, cache_key=f"battlelog:{raw_tag}", ttl=TTL_BATTLE_LOG)

        battle_logs = await asyncio.gather(*[fetch_battle_log(m) for m in members])

        opponent_cards: Counter = Counter()
        for battle_log in battle_logs:
            if not battle_log: continue
            for battle in battle_log[:3]:
                for opp in battle.get("opponent", []):
                    for card in opp.get("cards", []):
                        opponent_cards[card["name"]] += 1

        if not opponent_cards:
            return await msg.edit(content="❌ Could not analyze enough battle logs.")

        top_cards = opponent_cards.most_common(5)
        embed = discord.Embed(title="🕵️ Enemy Meta Scout", description="Most common cards faced by your clan recently:", color=0xE74C3C)
        for card, count in top_cards:
            embed.add_field(name=card, value=f"Seen {count} times", inline=False)
        await msg.edit(content=None, embed=embed)

    @commands.cooldown(1, HEAVY_COMMANDS_COOLDOWN, commands.BucketType.guild)
    @commands.command()
    async def primetime(self, ctx):
        """Analyzes recent battle logs to produce an EST Excel heatmap of clan activity."""
        await self.wait_if_warming(ctx)
        msg = await ctx.send("🕒 Calculating activity heatmap for all members…")

        clan_data = await self._get_clan_data(CLAN_TAG)
        if not clan_data:
            return await msg.edit(content="❌ Could not fetch clan data.")

        members = clan_data.get("memberList", [])
        sem = asyncio.Semaphore(CONCURRENT_REQUESTS)

        async def fetch_battle_log(member):
            async with sem:
                raw_tag = member["tag"].replace("#", "")
                url = f"{self.api_base}/players/{quote('#' + raw_tag)}/battlelog"
                battle_log = await self._api_get(url, cache_key=f"battlelog:{raw_tag}", ttl=TTL_BATTLE_LOG)
                return member["name"], battle_log

        results = await asyncio.gather(*[fetch_battle_log(m) for m in members])

        est_tz = zoneinfo.ZoneInfo("America/New_York")
        utc_tz = zoneinfo.ZoneInfo("UTC")

        player_counts: dict[str, Counter] = {}
        total_hour_counts: Counter = Counter()
        max_player_hour_count = 0

        for name, battle_log in results:
            player_counts[name] = Counter()
            if not battle_log: continue
            for battle in battle_log:
                ts = battle.get("battleTime", "")
                if len(ts) >= 15:
                    try:
                        utc_dt = datetime.strptime(ts[:15], "%Y%m%dT%H%M%S").replace(tzinfo=utc_tz)
                        hour_est = utc_dt.astimezone(est_tz).hour
                        player_counts[name][hour_est] += 1
                        total_hour_counts[hour_est] += 1
                        if player_counts[name][hour_est] > max_player_hour_count:
                            max_player_hour_count = player_counts[name][hour_est]
                    except ValueError:
                        pass

        if not total_hour_counts:
            return await msg.edit(content="❌ Not enough data to calculate Prime Time.")

        top_hour, top_count = total_hour_counts.most_common(1)[0]
        display_hour = top_hour % 12 or 12
        am_pm = "AM" if top_hour < 12 else "PM"

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Clan Prime Time (EST)"
        ws.cell(row=1, column=1, value="Player")
        for h in range(24):
            ws.cell(row=1, column=h + 2, value=f"{h:02d}:00")
        ws.freeze_panes = "B2"

        for row_idx, (name, counts) in enumerate(player_counts.items(), start=2):
            ws.cell(row=row_idx, column=1, value=name)
            for h in range(24):
                val = counts[h]
                cell = ws.cell(row=row_idx, column=h + 2, value=val if val > 0 else "")
                if val > 0 and max_player_hour_count > 0:
                    ratio = val / max_player_hour_count
                    gb = int(255 * (1 - ratio))
                    cell.fill = PatternFill(start_color=f"FFFF{gb:02X}{gb:02X}", end_color=f"FFFF{gb:02X}{gb:02X}", fill_type="solid")

        excel_buffer = io.BytesIO()
        wb.save(excel_buffer)
        excel_buffer.seek(0)

        embed = discord.Embed(
            title="🔥 Clan Prime Time",
            description=f"Your clan's busiest time is **{display_hour}:00 {am_pm} Eastern** ({top_count} recent battles).\n\nDownload the Excel file below for a full per-player heatmap.",
            color=0xF1C40F,
        )
        excel_file = discord.File(fp=excel_buffer, filename="Clan_Prime_Time_Heatmap.xlsx")
        await msg.edit(content=None, embed=embed, attachments=[excel_file])

    @commands.command()
    @commands.has_permissions(manage_guild=True)
    async def setreminders(self, ctx, channel: discord.TextChannel):
        await self.guilds.update_one({"_id": str(ctx.guild.id)}, {"$set": {"channel_id": channel.id}}, upsert=True)
        await ctx.send(f"✅ Reminders set to {channel.mention} every 12 hours.")

    @tasks.loop(hours=12)
    async def reminder_loop(self):
        async for g in self.guilds.find({"channel_id": {"$exists": True}}):
            channel_id = g["channel_id"]
            channel = self.bot.get_channel(channel_id)
            if not channel:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except discord.NotFound:
                    continue
                except discord.HTTPException:
                    continue
            try:
                await channel.send("⚔️ **War Reminder:** Use your attacks! The river race is active.")
            except discord.HTTPException:
                pass
            await asyncio.sleep(0.5)

    @reminder_loop.error
    async def reminder_loop_error(self, error):
        log.error(f"Reminder loop crashed: {error}", exc_info=error)
        await asyncio.sleep(60)
        self.reminder_loop.restart()

    @reminder_loop.before_loop
    async def before_reminder_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(ClashRoyale(bot))
