# cogs/clash_cog.py
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

MAX_CARD_LEVEL    = 16
CLAN_SCAN_LIMIT   = 20
CONCURRENT_REQUESTS = 5

# --- Cache TTLs (seconds) ---
TTL_CARDS      = 60 * 60 * 24   # 24 hours
TTL_CLAN       = 60 * 10        # 10 minutes
TTL_PLAYER     = 60 * 5         # 5 minutes
TTL_CLAN_TAG   = 60 * 60        # 1 hour
TTL_DAILY_LOG  = 86400          # 24 hours

# CR API retry settings
MAX_RETRIES    = 3
RETRY_BACKOFF  = 1.5            # seconds, doubles each attempt

# Commands that benefit from cache warming and should trigger the warmup check.
# All other commands skip the DB hit in cog_before_invoke entirely.
WARMUP_RELEVANT_COMMANDS = {"scout", "primetime", "cardstats"}


class ClashRoyale(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.users = bot.db_users
        self.guilds = self.db["guilds"]
        self.mongo_cache = self.db["api_cache"]
        self.api_base = "https://proxy.royaleapi.dev/v1"
        self.role_id = int(os.getenv("BADGE_ROLE_ID", 1464091054960803893))

        self.all_cards = []
        self.active_warmups = set() # Tracks clans currently downloading historical data

        self.reminder_loop.start()
        self.bot.loop.create_task(self._cache_cards())

    def cog_unload(self):
        self.reminder_loop.cancel()

    # ------------------------------------------------------------------ #
    # CACHE WARMING (First-Touch Tripwire)                               #
    # ------------------------------------------------------------------ #

    async def cog_before_invoke(self, ctx):
        """Fires before ANY command. Only triggers warmup for commands that use battle logs,
        avoiding an unnecessary DB hit on every single command invocation."""
        # FIX: Only check warmup for commands that actually need the cached battle logs.
        # Previously this fired a DB lookup for every command (e.g. !setreminders, !stats).
        if ctx.command and ctx.command.name in WARMUP_RELEVANT_COMMANDS:
            await self._check_and_start_warmup(ctx.author.id)

    async def _check_and_start_warmup(self, user_id: int):
        clan_tag = await self.get_clan_tag(user_id)
        if not clan_tag: 
            return
        
        warm_key = f"warmed_today:{clan_tag}"
        is_warmed = await self._cache_get(warm_key)
        
        # If we already warmed up today, or are actively warming right now, skip.
        if is_warmed or clan_tag in self.active_warmups: 
            return
        
        # Set flags synchronously to lock out duplicate triggers
        self.active_warmups.add(clan_tag)
        await self._cache_set(warm_key, True, TTL_DAILY_LOG)
        
        # Start heavy lifting in the background so the user doesn't have to wait
        asyncio.create_task(self._run_warmup_task(clan_tag))

    async def _run_warmup_task(self, clan_tag: str):
        """Silently downloads 50 battle logs in the background."""
        try:
            log.info(f"⏰ Bot woken up! Warming historical cache for clan #{clan_tag}...")
            clan_data = await self._get_clan_data(clan_tag)
            if not clan_data: 
                return
                
            members = clan_data.get("memberList", [])
            sem = asyncio.Semaphore(CONCURRENT_REQUESTS)
            
            async def fetch_log(member):
                async with sem:
                    m_tag = member['tag'].replace("#", "")
                    url = f"{self.api_base}/players/{quote('#' + m_tag)}/battlelog"
                    # Cache the log for 24 hours
                    await self._api_get(url, cache_key=f"daily_log:{m_tag}", ttl=TTL_DAILY_LOG)
                    
            await asyncio.gather(*[fetch_log(m) for m in members])
            log.info(f"✅ Background cache warming complete for #{clan_tag}.")
        except Exception as e:
            log.error(f"Error during warmup for #{clan_tag}: {e}")
        finally:
            if clan_tag in self.active_warmups:
                self.active_warmups.remove(clan_tag)

    async def wait_if_warming(self, ctx, clan_tag: str):
        """Used by heavy commands to politely wait if the bot is actively fetching logs."""
        if clan_tag in self.active_warmups:
            msg = await ctx.send("⏳ **Waking up!** Gathering today's historical data for the clan. This takes about 10-15 seconds...")
            # Loop silently until the background task finishes and removes the tag
            while clan_tag in self.active_warmups:
                await asyncio.sleep(1)
            await msg.delete()

    # ------------------------------------------------------------------ #
    # CACHING LAYER                                                      #
    # ------------------------------------------------------------------ #

    async def _cache_get(self, key: str) -> dict | None:
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

    async def _cache_set(self, key: str, value: dict, ttl: int):
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

    # ------------------------------------------------------------------ #
    # API LAYER (with retry + rate-limit handling)                        #
    # ------------------------------------------------------------------ #

    async def _api_get(self, url: str, cache_key: str = None, ttl: int = TTL_PLAYER) -> dict | None:
        if cache_key:
            cached = await self._cache_get(cache_key)
            if cached is not None:
                return cached

        delay = RETRY_BACKOFF
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with self.bot.http_session.get(
                    url, headers=self.bot._cr_headers()
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if cache_key:
                            await self._cache_set(cache_key, data, ttl)
                        return data

                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", delay))
                        log.warning(f"Rate limited by CR API (attempt {attempt}/{MAX_RETRIES}). Waiting {retry_after}s")
                        await asyncio.sleep(retry_after)
                        delay *= 2
                        continue

                    log.warning(f"CR API returned {resp.status} for {url}")
                    return None

            except asyncio.TimeoutError:
                log.warning(f"Timeout on attempt {attempt} for {url}")
                await asyncio.sleep(delay)
                delay *= 2
            except Exception as e:
                log.error(f"Unexpected error fetching {url}: {e}")
                return None

        log.error(f"All {MAX_RETRIES} retries exhausted for {url}")
        return None

    async def _get_player_data(self, tag: str) -> dict | None:
        clean_tag = tag.upper().replace("#", "")
        url = f"{self.api_base}/players/{quote('#' + clean_tag)}"
        return await self._api_get(url, cache_key=f"player:{clean_tag}", ttl=TTL_PLAYER)

    async def _get_clan_data(self, clan_tag: str) -> dict | None:
        url = f"{self.api_base}/clans/{quote('#' + clan_tag)}"
        return await self._api_get(url, cache_key=f"clan:{clan_tag}", ttl=TTL_CLAN)

    async def _fetch_members_concurrent(self, member_list: list) -> list:
        sem = asyncio.Semaphore(CONCURRENT_REQUESTS)
        async def fetch_one(member):
            async with sem:
                return await self._get_player_data(member['tag'])
        results = await asyncio.gather(*[fetch_one(m) for m in member_list])
        return [r for r in results if r is not None]

    def _is_maxed(self, card: dict) -> bool:
        return card.get('level', 1) >= MAX_CARD_LEVEL

    async def _cache_cards(self):
        cache_key = "cards:all"
        cached = await self._cache_get(cache_key)
        if cached:
            self.all_cards = cached
            log.info(f"✅ Loaded {len(self.all_cards)} cards from cache.")
            return

        data = await self._api_get(f"{self.api_base}/cards", cache_key=cache_key, ttl=TTL_CARDS)
        if data:
            self.all_cards = [c['name'] for c in data.get('items', [])]
            log.info(f"✅ Fetched and cached {len(self.all_cards)} cards.")

    async def get_clan_tag(self, user_id: int) -> str | None:
        d_id = str(user_id)
        cache_key = f"clan_tag:{d_id}"

        if self.bot.redis_available:
            cached = await self.bot.redis.get(cache_key)
            if cached: return cached 
        else:
            cached = await self._cache_get(cache_key)
            if cached: return cached.get("tag")

        user_doc = await self.users.find_one({"_id": d_id})
        if not user_doc: return None

        data = await self._get_player_data(user_doc["player_id"])
        if data and "clan" in data:
            tag = data["clan"]["tag"].replace("#", "")
            if self.bot.redis_available:
                await self.bot.redis.setex(cache_key, TTL_CLAN_TAG, tag)
            else:
                await self._cache_set(cache_key, {"tag": tag}, TTL_CLAN_TAG)
            return tag
        return None

    # ------------------------------------------------------------------ #
    # PLAYER COMMANDS                                                    #
    # ------------------------------------------------------------------ #

    @commands.command()
    async def link(self, ctx, tag: str):
        """Link your Discord account to a player tag."""
        clean_tag = tag.upper().replace("#", "")
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
        await self._cache_delete(f"clan_tag:{ctx.author.id}")

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
        # FIX: card['name'] was missing from the f-string, so every card showed as blank.
        deck_str = "\n".join(
            f"• **{card['name']}** (Lvl {card['level']})"
            for card in data.get("currentDeck", [])
        )
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
                name=f"{result} ({crowns_team} - {crowns_opp})", 
                value=f"**Mode:** {mode}\n**Vs:** {opponent.get('name', 'Unknown')}", 
                inline=False
            )

        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ #
    # CLAN COMMANDS                                                      #
    # ------------------------------------------------------------------ #

    @commands.command()
    async def clan(self, ctx):
        """Shows a general overview of the clan."""
        clan_tag = await self.get_clan_tag(ctx.author.id)
        if not clan_tag:
            return await ctx.send("❌ Link your account and join a clan first.")

        data = await self._get_clan_data(clan_tag)
        if not data:
            return await ctx.send("❌ Could not fetch clan data.")

        embed = discord.Embed(title=f"🛡️ {data.get('name')} ({data.get('tag')})", description=data.get('description', ''), color=0x9B59B6)
        embed.add_field(name="Members", value=f"👥 {data.get('members', 0)}/50", inline=True)
        embed.add_field(name="Score", value=f"🏆 {data.get('clanScore', 0)}", inline=True)
        embed.add_field(name="War Trophies", value=f"🏅 {data.get('clanWarTrophies', 0)}", inline=True)
        embed.add_field(name="Location", value=f"🌍 {data.get('location', {}).get('name', 'Unknown')}", inline=True)
        embed.add_field(name="Required Trophies", value=f"🔒 {data.get('requiredTrophies', 0)}", inline=True)
        
        await ctx.send(embed=embed)

    @commands.command()
    async def war(self, ctx):
        """Shows the current status of our clan as a quick overview."""
        clan_tag = await self.get_clan_tag(ctx.author.id)
        if not clan_tag:
            return await ctx.send("❌ Link your account and join a clan first.")

        # NOTE: Intentionally shares cache key "war:{clan_tag}" with !forecast since
        # both commands read from the same /currentriverrace endpoint.
        url = f"{self.api_base}/clans/{quote('#' + clan_tag)}/currentriverrace"
        data = await self._api_get(url, cache_key=f"war:{clan_tag}", ttl=60 * 5)
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
        clan_tag = await self.get_clan_tag(ctx.author.id)
        if not clan_tag:
            return await ctx.send("❌ Link your account and join a clan first.")

        embed = discord.Embed(color=0x1ABC9C)

        if period.lower() == "last":
            url = f"{self.api_base}/clans/{quote('#' + clan_tag)}/riverracelog"
            data = await self._api_get(url, cache_key=f"racelog:{clan_tag}", ttl=60 * 60)
            if not data or not data.get("items"):
                return await ctx.send("❌ Could not fetch past race log.")

            embed.title = "🏁 Last River Race Results"
            # FIX: The last race standings have a different shape: each entry is
            # { "rank": N, "trophyChange": N, "clan": { "name": ..., "fame": ... } }
            # Extract the nested clan object explicitly instead of relying on the
            # ambiguous .get("clan", clan) fallback that was used before.
            standings = data["items"][0].get("standings", [])
            for i, standing in enumerate(standings[:5], start=1):
                c_data = standing.get("clan", {})
                name = c_data.get("name", "Unknown")
                fame = c_data.get("fame", 0)
                embed.add_field(name=f"#{i} {name}", value=f"⭐ {fame} Fame", inline=False)
        else:
            url = f"{self.api_base}/clans/{quote('#' + clan_tag)}/currentriverrace"
            data = await self._api_get(url, cache_key=f"racecurrent:{clan_tag}", ttl=60 * 5)
            if not data:
                return await ctx.send("❌ Could not fetch current race data.")

            embed.title = "⛵ Current River Race Standings"
            # FIX: Current race clans are top-level objects (no nested "clan" key),
            # so we read them directly — no .get("clan", ...) needed here.
            standings = data.get("clans", [])
            standings.sort(key=lambda x: x.get("fame", 0), reverse=True)
            for i, clan in enumerate(standings[:5], start=1):
                name = clan.get("name", "Unknown")
                fame = clan.get("fame", 0)
                embed.add_field(name=f"#{i} {name}", value=f"⭐ {fame} Fame", inline=False)

        await ctx.send(embed=embed)

    @commands.command()
    async def whohas(self, ctx, *, card_name: str = None):
        """Fuzzy search for maxed card owners."""
        clan_tag = await self.get_clan_tag(ctx.author.id)
        if not clan_tag:
            return await ctx.send("❌ Link your account and join a clan first.")

        clan_data = await self._get_clan_data(clan_tag)
        if not clan_data:
            return await ctx.send("❌ Could not fetch clan data. Try again later.")

        # FIX: Apply CLAN_SCAN_LIMIT consistently here, matching the cardstats
        # command. Previously whohas sliced to 20 but then re-fetched all 50
        # anyway on the second profiles call. Now we fetch once and reuse.
        members = clan_data.get("memberList", [])[:CLAN_SCAN_LIMIT]

        # FIX: Fetch profiles once and reuse for both the "most common" detection
        # and the final hit list. Previously profiles were fetched twice.
        profiles = await self._fetch_members_concurrent(members)

        if card_name:
            if not self.all_cards:
                await self._cache_cards()
            match, score = process.extractOne(card_name, self.all_cards)
            if score < 60:
                return await ctx.send(f"❓ Could not find a card similar to '{card_name}'.")
            target_card = match
        else:
            await ctx.send("🔍 Finding your clan's most common maxed card...")
            counts = Counter(
                card['name']
                for p in profiles
                for card in p.get("cards", [])
                if self._is_maxed(card)
            )
            if not counts:
                return await ctx.send("❌ No maxed cards found in top members.")
            target_card = counts.most_common(1)[0][0]

        await ctx.send(f"📊 **Searching for owners of {target_card}:**")
        name_by_tag = {m['tag'].replace("#", "").upper(): m['name'] for m in members}

        hits = []
        for p in profiles:
            tag_clean = p.get('tag', '').replace("#", "").upper()
            player_name = name_by_tag.get(tag_clean, p.get('name', 'Unknown'))
            for card in p.get("cards", []):
                if card['name'] == target_card and self._is_maxed(card):
                    hits.append(f"• **{player_name}** (Lvl {card['level']})")

        if hits:
            await ctx.send("\n".join(hits))
        else:
            await ctx.send(f"❌ Nobody in the top {CLAN_SCAN_LIMIT} members has {target_card} maxed.")

    @commands.command()
    async def cardstats(self, ctx):
        """Generates a CSV listing every member's maxed cards, sorted by popularity."""
        clan_tag = await self.get_clan_tag(ctx.author.id)
        if not clan_tag:
            return await ctx.send("❌ Link first.")

        # If the bot was just woken up/warming, we wait so the data is fresh
        await self.wait_if_warming(ctx, clan_tag)

        await ctx.send("📊 **Generating Detailed Card Power Report...**")

        clan_data = await self._get_clan_data(clan_tag)
        if not clan_data:
            return await ctx.send("❌ Could not fetch clan data.")

        # Fetch all 50 profiles
        profiles = await self._fetch_members_concurrent(clan_data.get("memberList", []))
        
        # Map: Card Name -> List of Player Names
        card_to_members = {}

        for p in profiles:
            player_name = p.get('name', 'Unknown')
            for card in p.get("cards", []):
                if self._is_maxed(card):
                    card_name = card['name']
                    if card_name not in card_to_members:
                        card_to_members[card_name] = []
                    card_to_members[card_name].append(player_name)

        # Sort the cards by the number of people who have them (most popular first)
        sorted_cards = sorted(
            card_to_members.items(), 
            key=lambda item: len(item[1]), 
            reverse=True
        )

        # Build the CSV
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Card Name (Total Maxed)", "Members with Max (Lvl 16)"])

        for card_name, members in sorted_cards:
            # Join names with a semicolon or comma so they stay in one cell
            member_list_str = ", ".join(sorted(members)) 
            writer.writerow([f"{card_name} ({len(members)})", member_list_str])

        output.seek(0)
        
        # Send to Discord
        await ctx.send(
            content=f"✅ Report complete! Found **{len(card_to_members)}** different maxed cards across the clan.",
            file=discord.File(fp=output, filename="Detailed_Clan_Card_Report.csv")
        )
            

    @commands.command()
    async def forecast(self, ctx):
        """Calculates if your clan will finish the race today based on current speed."""
        clan_tag = await self.get_clan_tag(ctx.author.id)
        if not clan_tag:
            return await ctx.send("❌ Link your account first.")

        # NOTE: Intentionally shares cache key "war:{clan_tag}" with !war since
        # both commands read from the same /currentriverrace endpoint.
        url = f"{self.api_base}/clans/{quote('#' + clan_tag)}/currentriverrace"
        data = await self._api_get(url, cache_key=f"war:{clan_tag}", ttl=60 * 5)
        if not data:
            return await ctx.send("❌ Could not fetch war data.")

        clan_info = data.get("clan", {})
        fame = clan_info.get("fame", 0)
        
        goal = 10000 
        if fame >= goal:
            return await ctx.send("✅ **Forecast:** Your clan has already finished the race!")
            
        members = clan_info.get("participants", [])

        # FIX: Guard against an empty participants list to avoid a misleading projection.
        if not members:
            return await ctx.send("❌ Could not read participant data for the forecast.")

        decks_used = sum(p.get("decksUsedToday", 0) for p in members)
        decks_remaining = (len(members) * 4) - decks_used
        
        projected_fame = fame + (decks_remaining * 100) 
        
        if projected_fame >= goal:
            await ctx.send(f"📈 **Forecast:** Yes! You are at {fame}/{goal} Fame. If everyone attacks, you project to hit {projected_fame} Fame today.")
        else:
            await ctx.send(f"📉 **Forecast:** Unlikely. You are at {fame}/{goal} Fame. Even if everyone attacks, you project to only reach {projected_fame} Fame today.")

    @commands.command()
    async def scout(self, ctx):
        """Analyzes recent battles from all 50 players to show the current Meta."""
        clan_tag = await self.get_clan_tag(ctx.author.id)
        if not clan_tag:
            return await ctx.send("❌ Link your account and join a clan first.")

        # If the bot was just woken up, pause execution politely until data is ready.
        await self.wait_if_warming(ctx, clan_tag)

        msg = await ctx.send("🔍 Scouting enemy meta...")
        
        clan_data = await self._get_clan_data(clan_tag)
        if not clan_data:
            return await msg.edit(content="❌ Could not fetch clan data.")

        members = clan_data.get("memberList", [])
        sem = asyncio.Semaphore(CONCURRENT_REQUESTS)
        
        async def fetch_log(member):
            async with sem:
                tag = member['tag'].replace("#", "")
                url = f"{self.api_base}/players/{quote('#' + tag)}/battlelog"
                # Pulls instantly from the 24hr cache 
                return await self._api_get(url, cache_key=f"daily_log:{tag}", ttl=TTL_DAILY_LOG)

        logs = await asyncio.gather(*[fetch_log(m) for m in members])
        
        opponent_cards = Counter()
        for log_data in logs:
            if not log_data: continue
            for battle in log_data[:3]: 
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

    @commands.command()
    async def primetime(self, ctx):
        """Analyzes recent battle logs to create an EST Excel heatmap of entire clan activity."""
        clan_tag = await self.get_clan_tag(ctx.author.id)
        if not clan_tag:
            return await ctx.send("❌ Link your account and join a clan first.")

        # If the bot was just woken up, pause execution politely until data is ready.
        await self.wait_if_warming(ctx, clan_tag)

        msg = await ctx.send("🕒 Calculating activity heatmap for all 50 members...")
        
        clan_data = await self._get_clan_data(clan_tag)
        if not clan_data:
            return await msg.edit(content="❌ Could not fetch clan data.")

        members = clan_data.get("memberList", [])
        sem = asyncio.Semaphore(CONCURRENT_REQUESTS)
        
        async def fetch_log(member):
            async with sem:
                tag = member['tag'].replace("#", "")
                url = f"{self.api_base}/players/{quote('#' + tag)}/battlelog"
                # Pulls instantly from the 24hr cache
                data = await self._api_get(url, cache_key=f"daily_log:{tag}", ttl=TTL_DAILY_LOG)
                return member['name'], data

        results = await asyncio.gather(*[fetch_log(m) for m in members])
        
        est_tz = zoneinfo.ZoneInfo("America/New_York")
        utc_tz = zoneinfo.ZoneInfo("UTC")
        
        player_counts = {}
        total_hour_counts = Counter()
        max_player_hour_count = 0 
        
        for name, log_data in results:
            player_counts[name] = Counter()
            if not log_data: continue
            
            for battle in log_data:
                ts = battle.get("battleTime", "") 
                if len(ts) >= 15:
                    try:
                        utc_dt = datetime.strptime(ts[:15], "%Y%m%dT%H%M%S")
                        utc_dt = utc_dt.replace(tzinfo=utc_tz)
                        est_dt = utc_dt.astimezone(est_tz)
                        hour_est = est_dt.hour
                        
                        player_counts[name][hour_est] += 1
                        total_hour_counts[hour_est] += 1
                        
                        if player_counts[name][hour_est] > max_player_hour_count:
                            max_player_hour_count = player_counts[name][hour_est]
                    except ValueError:
                        pass
        
        if not total_hour_counts:
            return await msg.edit(content="❌ Not enough data to calculate Prime Time.")

        top_hour, top_count = total_hour_counts.most_common(1)[0]
        am_pm = "AM" if top_hour < 12 else "PM"
        display_hour = top_hour if top_hour > 0 else 12
        display_hour = display_hour if display_hour <= 12 else display_hour - 12
        
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Clan Prime Time (EST)"
        
        ws.cell(row=1, column=1, value="Player")
        for h in range(24):
            ws.cell(row=1, column=h+2, value=f"{h:02d}:00")
            
        ws.freeze_panes = "B2" 
        
        row_idx = 2
        for name, counts in player_counts.items():
            ws.cell(row=row_idx, column=1, value=name)
            for h in range(24):
                val = counts[h]
                cell = ws.cell(row=row_idx, column=h+2, value=val if val > 0 else "")
                
                if val > 0 and max_player_hour_count > 0:
                    ratio = val / max_player_hour_count
                    # FIX: Renamed 'gb' to 'green_blue' to clarify that this value
                    # controls both the green and blue channels together, fading them
                    # out as activity increases to produce a white -> red heat gradient.
                    green_blue = int(255 * (1 - ratio))
                    hex_color = f"FFFF{green_blue:02X}{green_blue:02X}"
                    
                    fill = PatternFill(start_color=hex_color, end_color=hex_color, fill_type="solid")
                    cell.fill = fill
            row_idx += 1
            
        excel_buffer = io.BytesIO()
        wb.save(excel_buffer)
        excel_buffer.seek(0)
        
        embed = discord.Embed(
            title="🔥 Clan Prime Time", 
            description=f"Your clan's absolute busiest time is **{display_hour}:00 {am_pm} Eastern Time** ({top_count} recent battles).\n\nI have generated a detailed per-player heatmap! Download the Excel file below.", 
            color=0xF1C40F
        )
        
        await msg.delete()
        await ctx.send(embed=embed, file=discord.File(fp=excel_buffer, filename="Clan_Prime_Time_Heatmap.xlsx"))

    # ------------------------------------------------------------------ #
    # REMINDERS                                                          #
    # ------------------------------------------------------------------ #

    @commands.command()
    @commands.has_permissions(manage_guild=True)
    async def setreminders(self, ctx, channel: discord.TextChannel):
        await self.guilds.update_one(
            {"_id": str(ctx.guild.id)},
            {"$set": {"channel_id": channel.id}},
            upsert=True,
        )
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
                    log.warning(f"Reminder channel {channel_id} not found, skipping.")
                    continue
                except discord.HTTPException as e:
                    log.error(f"Error fetching channel {channel_id}: {e}")
                    continue
            try:
                await channel.send("⚔️ **War Reminder:** Use your attacks! The river race is active.")
            except discord.HTTPException as e:
                log.warning(f"Failed to send reminder to channel {channel_id}: {e}")
            await asyncio.sleep(0.5)

    @reminder_loop.before_loop
    async def before_reminder_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(ClashRoyale(bot))