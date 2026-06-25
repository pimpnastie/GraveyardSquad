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
import mainbot  # Required to update the harvest metadata

log = logging.getLogger("clashbot")

CONCURRENT_REQUESTS = 5

# --- Cache TTLs (seconds) ---
TTL_CARDS       = 60 * 60 * 24
TTL_CLAN        = 60 * 10
TTL_PLAYER      = 60 * 5
TTL_BATTLE_LOG  = 60 * 60 * 24
TTL_WAR         = 60 * 5

WARMUP_RELEVANT_COMMANDS = {"scout", "primetime", "cardstats", "whohas"}
HEAVY_COMMANDS_COOLDOWN = 30


class ProfileView(discord.ui.View):
    def __init__(self, data: dict, author_id: int):
        super().__init__(timeout=120)
        self.data = data
        self.author_id = author_id
        player_tag = data.get("tag", "").replace("#", "")
        web_url = f"https://graveyardbot.onrender.com/player/{player_tag}"
        self.add_item(discord.ui.Button(label="View Full Web Dashboard", style=discord.ButtonStyle.link, url=web_url, emoji="🌐"))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id: return True
        await interaction.response.send_message("❌ Only the person who ran this command can use these buttons.", ephemeral=True)
        return False

    def build_overview_embed(self) -> discord.Embed:
        e = discord.Embed(title=f"👑 {self.data.get('name')} | Lvl {self.data.get('expLevel')}", color=0x3498DB)
        e.add_field(name="🏆 Trophies", value=f"Current: **{self.data.get('trophies', 0)}**\nBest: **{self.data.get('bestTrophies', 0)}**", inline=True)
        wins, losses = self.data.get("wins", 0), self.data.get("losses", 0)
        total = wins + losses
        win_rate = round((wins / total * 100), 1) if total > 0 else 0
        e.add_field(name="⚔️ Combat Stats", value=f"Wins: **{wins}**\nLosses: **{losses}**\nWin Rate: **{win_rate}%**", inline=True)
        return e

    def build_social_embed(self) -> discord.Embed:
        e = discord.Embed(title=f"🛡️ Social & War | {self.data.get('name')}", color=0xE67E22)
        clan = self.data.get("clan")
        if clan: e.add_field(name="Clan", value=f"**{clan.get('name')}** ({clan.get('tag')})\nRole: {self.data.get('role', 'Member').capitalize()}", inline=False)
        e.add_field(name="🎁 Donations", value=f"Given: **{self.data.get('donations', 0)}**\nReceived: **{self.data.get('donationsReceived', 0)}**", inline=True)
        return e

    def build_deck_embed(self) -> discord.Embed:
        e = discord.Embed(title=f"🃏 Deck | {self.data.get('name')}", color=0x9B59B6)
        cards = self.data.get("currentDeck", [])
        deck_str = "\n".join([f"• **{c['name']}** (Lvl {c['level']})" for c in cards])
        e.add_field(name="⚔️ Current Battle Deck", value=deck_str or "No deck found.", inline=False)
        return e

    @discord.ui.button(label="Overview", style=discord.ButtonStyle.primary, emoji="📊")
    async def btn_overview(self, interaction: discord.Interaction, button: discord.ui.Button): await interaction.response.edit_message(embed=self.build_overview_embed())
    @discord.ui.button(label="Social & War", style=discord.ButtonStyle.success, emoji="🛡️")
    async def btn_social(self, interaction: discord.Interaction, button: discord.ui.Button): await interaction.response.edit_message(embed=self.build_social_embed())
    @discord.ui.button(label="Deck & Cards", style=discord.ButtonStyle.secondary, emoji="🃏")
    async def btn_deck(self, interaction: discord.Interaction, button: discord.ui.Button): await interaction.response.edit_message(embed=self.build_deck_embed())


class ClashRoyale(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.users = bot.db_users
        self.guilds = self.db["guilds"]
        self.mongo_cache = self.db["api_cache"]

        self.api_base = "https://proxy.royaleapi.dev/v1"
        self.role_id = int(os.getenv("BADGE_ROLE_ID", 1464091054960803893))
        self.clan_tag = "9LVY89UP"
        self.all_cards = []
        self.active_warmups = set()

        self.reminder_loop.start()
        self.daily_snapshot_loop.start()

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
        self.daily_snapshot_loop.cancel()

    async def cog_before_invoke(self, ctx):
        if ctx.command and ctx.command.name in WARMUP_RELEVANT_COMMANDS:
            await self._check_and_start_warmup()
        if not self.all_cards and ctx.command and ctx.command.name in {"whohas"}:
            await self._cache_cards()

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ **Woah there!** Please wait {error.retry_after:.1f} seconds.")
        else:
            log.error(f"Error in command '{ctx.command}': {error}")

    # --- Cache & Helper Logic ---
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
            clan_data = await self.bot.async_fetch_cr_api(f"clans/%23{self.clan_tag}")
            if not clan_data: return
            members = clan_data.get("memberList", [])
            sem = asyncio.Semaphore(CONCURRENT_REQUESTS)
            async def warm_member(member):
                async with sem:
                    raw_tag = member["tag"].replace("#", "")
                    await self.bot.async_fetch_cr_api(f"players/%23{raw_tag}/battlelog")
                    await self.bot.async_fetch_cr_api(f"players/%23{raw_tag}")
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
            doc = await self.mongo_cache.find_one({"_id": key})
            if doc and doc.get("expires_at", 0) > time.time(): return doc["data"]
        return None

    async def _cache_set(self, key: str, value, ttl: int):
        if self.bot.redis_available:
            await self.bot.redis.setex(key, ttl, json.dumps(value))
        else:
            await self.mongo_cache.update_one({"_id": key}, {"$set": {"data": value, "expires_at": time.time() + ttl}}, upsert=True)

    async def _cache_delete(self, key: str):
        if self.bot.redis_available:
            await self.bot.redis.delete(key)
        else:
            await self.mongo_cache.delete_one({"_id": key})

    async def _get_player_data(self, tag: str):
        clean_tag = tag.upper().replace("#", "")
        cached = await self._cache_get(f"player:{clean_tag}")
        if cached: return cached
        data = await self.bot.async_fetch_cr_api(f"players/%23{clean_tag}")
        if data: await self._cache_set(f"player:{clean_tag}", data, TTL_PLAYER)
        return data

    async def _get_clan_data(self, clan_tag: str):
        cached = await self._cache_get(f"clan:{clan_tag}")
        if cached: return cached
        data = await self.bot.async_fetch_cr_api(f"clans/%23{clan_tag}")
        if data: await self._cache_set(f"clan:{clan_tag}", data, TTL_CLAN)
        return data

    async def _fetch_members_concurrent(self, member_list: list) -> list:
        if not member_list:
            log.warning("_fetch_members_concurrent called with empty member list.")
            return []
        sem = asyncio.Semaphore(CONCURRENT_REQUESTS)
        async def fetch_one(member):
            async with sem:
                return await self._get_player_data(member["tag"])
        results = await asyncio.gather(*[fetch_one(m) for m in member_list])
        fetched = [r for r in results if r is not None]
        failed = len(results) - len(fetched)
        if failed:
            log.warning(f"_fetch_members_concurrent: {failed}/{len(results)} member(s) failed to fetch.")
        return fetched

    def _is_maxed(self, card: dict) -> bool:
        """
        Returns True if a card is at its max level.
        Uses the API-provided maxLevel field so this stays correct
        regardless of rarity or future level cap increases.
        Falls back to >= comparison in case maxLevel is missing.
        """
        level = card.get("level")
        max_level = card.get("maxLevel")
        if level is None:
            return False
        if max_level is not None:
            return level >= max_level
        # Fallback: derive max from rarity if the API omits maxLevel
        rarity = card.get("rarity", "").lower()
        rarity_max = {
            "common": 14,
            "rare": 12,
            "epic": 10,
            "legendary": 9,
            "champion": 9,
        }
        fallback = rarity_max.get(rarity)
        if fallback is None:
            log.warning(f"_is_maxed: unknown rarity '{rarity}' for card '{card.get('name')}', skipping.")
            return False
        return level >= fallback

    async def _cache_cards(self):
        cache_key = "cards:all"
        cached = await self._cache_get(cache_key)
        if cached:
            self.all_cards = cached
            return
        data = await self.bot.async_fetch_cr_api("cards")
        if data:
            self.all_cards = [c["name"] for c in data.get("items", [])]
            await self._cache_set(cache_key, self.all_cards, TTL_CARDS)

    async def run_harvest_logic(self):
    harvest_start = time.monotonic()
    mainbot._harvest_meta["status"] = "running"
    mainbot._harvest_meta["last_run"] = datetime.now().isoformat()

    # 1. Fetch clan and war data
    clan_data = await self.bot.async_fetch_cr_api(f"clans/%23{self.clan_tag}")
    war_data = await self.bot.async_fetch_cr_api(f"clans/%23{self.clan_tag}/currentriverrace")
    
    if not clan_data:
        mainbot._harvest_meta["status"] = "failed: clan API returned nothing"
        return

    snapshot_date = datetime.now(zoneinfo.ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    members = clan_data.get("memberList", [])
    
    # 2. War participant lookup — handles both API response shapes
    war_participants = {}
    if war_data:
        if "clan" in war_data and war_data["clan"] and "participants" in war_data["clan"]:
            war_participants = {
                p["tag"].replace("#", "").upper(): p
                for p in war_data["clan"]["participants"]
            }
        else:
            for clan in war_data.get("clans", []):
                if clan and clan.get("tag", "").replace("#", "").upper() == self.clan_tag.upper():
                    war_participants = {
                        p["tag"].replace("#", "").upper(): p
                        for p in clan.get("participants", [])
                    }
                    break

    snapshot_ops, profile_ops, battle_ops = [], [], []
    sem = asyncio.Semaphore(5)

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
        # 3. Snapshot document — include war data if available
        flat_data = {
            "date": snapshot_date,
            "name": m.get("name", ""),
            "tag": tag,
            "trophies": m.get("trophies", 0),
            "role": m.get("role", "member"),
            "donations": m.get("donations", 0),
            "donationsReceived": m.get("donationsReceived", 0),
        }
        if tag in war_participants:
            wp = war_participants[tag]
            flat_data.update({
                "fame": wp.get("fame", 0),
                "decksUsedToday": wp.get("decksUsedToday", 0),
                "decksRemaining": wp.get("decksRemaining", 4),
            })
        snapshot_ops.append(
            UpdateOne({"tag": tag, "date": snapshot_date}, {"$set": flat_data}, upsert=True)
        )

        # 4. Player profile — store the full API response
        if profile:
            profile_ops.append(
                UpdateOne({"_id": tag}, {"$set": profile}, upsert=True)
            )

        # 5. Battle history — deduplicated by battle ID
        if blog and isinstance(blog, list):
            for battle in blog:
                if not battle.get("battleTime"):
                    continue
                battle_id = f"{tag}_{battle['battleTime']}"
                
                team = battle.get("team", [{}])
                opponent = battle.get("opponent", [{}])
                team_data = team[0] if team else {}
                opp_data = opponent[0] if opponent else {}
                
                crowns_team = team_data.get("crowns", 0)
                crowns_opp = opp_data.get("crowns", 0)
                
                if crowns_team > crowns_opp:
                    result_str = "win"
                elif crowns_opp > crowns_team:
                    result_str = "loss"
                else:
                    result_str = "draw"

                battle_doc = {
                    "player_tag": tag,
                    "battle_time": battle["battleTime"],
                    "type": battle.get("type", "unknown"),
                    "result": result_str,
                    "team_crowns": crowns_team,
                    "opp_crowns": crowns_opp,
                    "opp_name": opp_data.get("name", "Unknown"),
                    "opp_tag": opp_data.get("tag", ""),
                }
                battle_ops.append(
                    UpdateOne({"_id": battle_id}, {"$set": battle_doc}, upsert=True)
                )

    # 6. Bulk write all operations
    snap_count = prof_count = battle_count = 0
    if snapshot_ops:
        await self.db["historical_snapshots"].bulk_write(snapshot_ops, ordered=False)
        snap_count = len(snapshot_ops)
    if profile_ops:
        await self.db["player_profiles"].bulk_write(profile_ops, ordered=False)
        prof_count = len(profile_ops)
    if battle_ops:
        await self.db["battle_history"].bulk_write(battle_ops, ordered=False)
        battle_count = len(battle_ops)

    # 7. Update in-memory metadata and persist to DB
    mainbot._harvest_meta.update({
        "status": "ok",
        "last_run": datetime.now().isoformat(),
        "snapshots_saved": snap_count,
        "profiles_saved": prof_count,
        "battles_saved": battle_count,
        "duration_s": round(time.monotonic() - harvest_start, 1),
        "member_count": len(members),
        "war_participants_found": len(war_participants),
    })
    await self.db["config"].update_one(
        {"_id": "harvest_meta"},
        {"$set": {**mainbot._harvest_meta, "last_run": datetime.now().isoformat()}},
        upsert=True,
    )
    log.info(
        f"✅ Harvest complete — {snap_count} snapshots, {prof_count} profiles, "
        f"{battle_count} battles in {mainbot._harvest_meta['duration_s']}s"
    )

    # --- Loops ---
    @tasks.loop(hours=12)
    async def reminder_loop(self):
        if getattr(self.bot, "feature_auto_pings", False):
            async for g in self.guilds.find({"channel_id": {"$exists": True}}):
                channel = self.bot.get_channel(g["channel_id"])
                if channel:
                    try:
                        await channel.send("⚔️ **War Reminder:** River Race is active. Burn your remaining card logs immediately!")
                    except discord.HTTPException:
                        pass
                await asyncio.sleep(0.5)

    @reminder_loop.before_loop
    async def before_reminder_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=dt_time(hour=23, minute=55, tzinfo=zoneinfo.ZoneInfo("America/New_York")))
    async def daily_snapshot_loop(self):
        await self.run_harvest_logic()

    @daily_snapshot_loop.before_loop
    async def before_daily_snapshot_loop(self):
        await self.bot.wait_until_ready()
        
    @daily_snapshot_loop.error
    async def on_snapshot_error(error):
        log.error(f"Snapshot loop crashed: {error}")
        mainbot._harvest_meta["status"] = f"crashed: {error}"
        await asyncio.sleep(60)
        self.daily_snapshot_loop.restart()

    # --- Commands ---
    @commands.command(aliases=["profile", "analytics"])
    async def p(self, ctx, *, target: str = None):
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
                clean_target = target.upper().replace("#", "")
                valid_chars = set("0289CGJLPQRUVY")
                if all(c in valid_chars for c in clean_target) and len(clean_target) > 3:
                    tag_to_search = clean_target
                else:
                    clan_data = await self._get_clan_data(self.clan_tag)
                    if clan_data and "memberList" in clan_data:
                        members = clan_data["memberList"]
                        names = [m["name"] for m in members]
                        match, score = process.extractOne(target, names)
                        if score >= 70:
                            for m in members:
                                if m["name"] == match:
                                    tag_to_search = m["tag"].replace("#", "")
                                    break

                    if not tag_to_search:
                        return await ctx.send(f"❌ Could not find a valid tag or clan member matching **'{target}'**.")
        else:
            user_doc = await self.users.find_one({"_id": str(ctx.author.id)})
            if user_doc:
                tag_to_search = user_doc["player_id"]
            else:
                return await ctx.send(f"❌ Account unlinked. Use `{self.bot.active_prefix}link <tag>` first.")

        msg = await ctx.send("🔍 Generating analytics…")
        data = await self._get_player_data(tag_to_search)
        if not data:
            return await msg.edit(content="❌ Failed to fetch player profile.")

        view = ProfileView(data, ctx.author.id)
        await msg.edit(content=None, embed=view.build_overview_embed(), view=view)

    @commands.command()
    async def link(self, ctx, tag: str):
        clean_tag = tag.upper().replace("#", "")
        if len(clean_tag) < 3:
            return await ctx.send("❌ Invalid player tag.")

        data = await self._get_player_data(clean_tag)
        if not data:
            return await ctx.send(f"❌ Player not found: **#{clean_tag}**.")

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
        if not user_doc:
            return await ctx.send(f"❌ Account unlinked. Use `{self.bot.active_prefix}link <tag>` first.")

        data = await self._get_player_data(user_doc["player_id"])
        if not data:
            return await ctx.send("❌ Failed to fetch parameters.")

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
        if not user_doc:
            return await ctx.send("❌ Account not linked.")

        data = await self._get_player_data(user_doc["player_id"])
        if not data:
            return await ctx.send("❌ Failed to fetch player deck.")

        embed = discord.Embed(title=f"⚔️ {data['name']}'s Current Deck", color=0xEE82EE)
        cards = data.get("currentDeck", [])
        embed.description = "\n".join([f"• **{c['name']}** (Lvl {c['level']})" for c in cards]) or "No deck found."
        await ctx.send(embed=embed)

    @commands.command()
    async def chests(self, ctx, target: discord.Member = None):
        member = target or ctx.author
        user_doc = await self.users.find_one({"_id": str(member.id)})
        if not user_doc:
            return await ctx.send("❌ Account unlinked.")

        clean_tag = user_doc["player_id"]
        cached = await self._cache_get(f"chests:{clean_tag}")
        if cached:
            data = cached
        else:
            data = await self.bot.async_fetch_cr_api(f"players/%23{clean_tag}/upcomingchests")
            if data:
                await self._cache_set(f"chests:{clean_tag}", data, 60 * 5)

        if not data or "items" not in data:
            return await ctx.send("❌ Failed to fetch chest items.")

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
        member = target or ctx.author
        user_doc = await self.users.find_one({"_id": str(member.id)})
        if not user_doc:
            return await ctx.send("❌ Account unlinked.")

        clean_tag = user_doc["player_id"]
        cached = await self._cache_get(f"battlelog:{clean_tag}")
        if cached:
            data = cached
        else:
            data = await self.bot.async_fetch_cr_api(f"players/%23{clean_tag}/battlelog")
            if data:
                await self._cache_set(f"battlelog:{clean_tag}", data, 60 * 5)

        if not data:
            return await ctx.send("❌ Failed to fetch logs.")

        embed = discord.Embed(title=f"⚔️ Last 5 Battles for #{clean_tag}", color=0x3498DB)
        for battle in data[:5]:
            team = battle["team"][0]
            opponent = battle["opponent"][0]
            crowns_team = team.get("crowns", 0)
            crowns_opp = opponent.get("crowns", 0)
            result = "🟢 Victory" if crowns_team > crowns_opp else "🔴 Defeat" if crowns_opp > crowns_team else "⚪ Draw"
            embed.add_field(
                name=f"{result} ({crowns_team} – {crowns_opp})",
                value=f"**Vs:** {opponent.get('name', 'Unknown')}",
                inline=False,
            )
        await ctx.send(embed=embed)

    @commands.command()
    async def clan(self, ctx):
        data = await self._get_clan_data(self.clan_tag)
        if not data:
            return await ctx.send("❌ Could not fetch clan logs.")

        embed = discord.Embed(
            title=f"🛡️ {data.get('name')} ({data.get('tag')})",
            description=data.get("description", ""),
            color=0x9B59B6,
        )
        embed.add_field(name="Members", value=f"👥 {data.get('members', 0)}/50", inline=True)
        embed.add_field(name="Score", value=f"🏆 {data.get('clanScore', 0)}", inline=True)
        await ctx.send(embed=embed)

    @commands.command()
    async def war(self, ctx):
        cached = await self._cache_get(f"currentrace:{self.clan_tag}")
        if cached:
            data = cached
        else:
            data = await self.bot.async_fetch_cr_api(f"clans/%23{self.clan_tag}/currentriverrace")
            if data:
                await self._cache_set(f"currentrace:{self.clan_tag}", data, TTL_WAR)

        if not data:
            return await ctx.send("❌ Failed to parse war data.")

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
            cached = await self._cache_get(f"racelog:{self.clan_tag}")
            if cached:
                data = cached
            else:
                data = await self.bot.async_fetch_cr_api(f"clans/%23{self.clan_tag}/riverracelog")
                if data:
                    await self._cache_set(f"racelog:{self.clan_tag}", data, 60 * 60)
            if not data or not data.get("items"):
                return await ctx.send("❌ No past logs found.")
            standings = data["items"][0].get("standings", [])
        else:
            cached = await self._cache_get(f"currentrace:{self.clan_tag}")
            if cached:
                data = cached
            else:
                data = await self.bot.async_fetch_cr_api(f"clans/%23{self.clan_tag}/currentriverrace")
                if data:
                    await self._cache_set(f"currentrace:{self.clan_tag}", data, TTL_WAR)
            if not data:
                return await ctx.send("❌ Failed to map current race data.")
            standings = data.get("clans", [])

        embed.title = "⛵ River Race Standings"
        standings = sorted(
            standings,
            key=lambda x: x.get("fame", 0) if "fame" in x else x.get("clan", {}).get("fame", 0),
            reverse=True,
        )
        for i, c in enumerate(standings[:5], start=1):
            c_info = c.get("clan", c)
            embed.add_field(
                name=f"#{i} {c_info.get('name', 'Unknown')}",
                value=f"⭐ {c_info.get('fame', 0)} Fame",
                inline=False,
            )
        await ctx.send(embed=embed)

    @commands.command()
    async def whohas(self, ctx, *, card_name: str = None):
        await self.wait_if_warming(ctx)
        clan_data = await self._get_clan_data(self.clan_tag)
        if not clan_data:
            return await ctx.send("❌ Could not resolve roster analytics.")

        members = clan_data.get("memberList", [])
        profiles = await self._fetch_members_concurrent(members)
        valid_profiles = [p for p in profiles if p and "cards" in p]

        # Warn if some members couldn't be fetched
        failed = len(members) - len(valid_profiles)
        if failed:
            await ctx.send(f"⚠️ {failed} member profile(s) could not be fetched and were excluded from results.")

        if not valid_profiles:
            return await ctx.send("❌ No member profiles could be loaded.")

        if card_name:
            if not self.all_cards:
                await self._cache_cards()
            match, score = process.extractOne(card_name, self.all_cards)
            if score < 60:
                return await ctx.send(f"❓ Card match low for '{card_name}'.")
            target_card = match
        else:
            # Find the most commonly maxed card across all profiles
            counts = Counter(
                card["name"]
                for p in valid_profiles
                for card in p.get("cards", [])
                if self._is_maxed(card)
            )
            if not counts:
                return await ctx.send("❌ No maxed cards found across any profiles.")
            target_card = counts.most_common(1)[0][0]

        name_by_tag = {m["tag"].replace("#", "").upper(): m["name"] for m in members}
        hits = [
            f"• **{name_by_tag.get(p.get('tag', '').replace('#', '').upper(), p.get('name'))}**"
            for p in valid_profiles
            for card in p.get("cards", [])
            if card["name"] == target_card and self._is_maxed(card)
        ]

        header = f"📊 **Owners of {target_card} (maxed):**"
        response = f"{header}\n" + "\n".join(hits) if hits else f"❌ Nobody has **{target_card}** maxed."
        if len(response) > 1900:
            response = response[:1900] + "\n… and more."
        await ctx.send(response)

    @commands.cooldown(1, HEAVY_COMMANDS_COOLDOWN, commands.BucketType.guild)
    @commands.command()
    async def cardstats(self, ctx):
        await self.wait_if_warming(ctx)
        msg = await ctx.send("📊 **Compiling Report…**")
        clan_data = await self._get_clan_data(self.clan_tag)
        if not clan_data:
            return await msg.edit(content="❌ Could not fetch clan data.")

        member_list = clan_data.get("memberList", [])
        if not member_list:
            return await msg.edit(content="❌ Clan has no members.")

        profiles = await self._fetch_members_concurrent(member_list)
        valid_profiles = [p for p in profiles if p and "cards" in p]

        if not valid_profiles:
            return await msg.edit(content="❌ Fetched profiles, but no card data was found. API might be returning partial data.")

        # Warn in the channel if some members were silently dropped
        failed = len(member_list) - len(valid_profiles)
        if failed:
            await ctx.send(f"⚠️ {failed} member profile(s) could not be fetched and are excluded from this report.")

        # Log a sample card so we can verify the API shape looks right
        sample_card = valid_profiles[0].get("cards", [{}])[0]
        log.info(f"[cardstats] Sample card from API: {sample_card}")

        card_to_members: dict[str, list[str]] = {}
        for p in valid_profiles:
            for card in p.get("cards", []):
                if self._is_maxed(card):
                    card_to_members.setdefault(card["name"], []).append(p.get("name", "Unknown"))

        log.info(f"[cardstats] {len(card_to_members)} unique maxed cards found across {len(valid_profiles)} profiles.")

        if not card_to_members:
            return await msg.edit(content="❌ No maxed cards found in any profiles.")

        output = io.StringIO()
        writer = csv.writer(output)
        # Separate Count into its own column for easier programmatic use
        writer.writerow(["Card Name", "Count", "Members"])
        for card_name, card_members in sorted(card_to_members.items(), key=lambda x: len(x[1]), reverse=True):
            writer.writerow([card_name, len(card_members), ", ".join(sorted(card_members))])
        output.seek(0)

        await msg.delete()
        await ctx.send(
            content=f"✅ Card stats compiled! ({len(card_to_members)} maxed cards found)",
            file=discord.File(fp=output, filename="Card_Report.csv"),
        )

    @commands.command()
    async def forecast(self, ctx):
        cached = await self._cache_get(f"currentrace:{self.clan_tag}")
        if cached:
            data = cached
        else:
            data = await self.bot.async_fetch_cr_api(f"clans/%23{self.clan_tag}/currentriverrace")
            if data:
                await self._cache_set(f"currentrace:{self.clan_tag}", data, TTL_WAR)

        if not data:
            return await ctx.send("❌ Error capturing metrics.")

        clan_info = data.get("clan", {})
        fame = clan_info.get("fame", 0)

        participants = clan_info.get("participants", [])
        if not participants and "clans" in data:
            for c in data["clans"]:
                if c.get("tag", "").replace("#", "").upper() == self.clan_tag.upper():
                    participants = c.get("participants", [])
                    break

        if not participants:
            return await ctx.send("❌ No activity participants verified today.")
        if fame >= 10_000:
            return await ctx.send("✅ Clan race completed!")

        decks_used = sum(p.get("decksUsedToday", 0) for p in participants)
        fame_earned = sum(p.get("fame", 0) for p in participants)
        avg_fame = fame_earned / decks_used if decks_used > 0 else 150
        projected = fame + int(((len(participants) * 4) - decks_used) * avg_fame)

        if projected >= 10_000:
            await ctx.send(f"📈 On pace! Projected calculation: **{projected:,}** Fame.")
        else:
            await ctx.send(f"📉 Under pace. Projected target ceiling: **{projected:,}** Fame.")

    @commands.cooldown(1, HEAVY_COMMANDS_COOLDOWN, commands.BucketType.guild)
    @commands.command()
    async def scout(self, ctx):
        await self.wait_if_warming(ctx)
        msg = await ctx.send("🔍 Scanning enemy deck lists…")
        clan_data = await self._get_clan_data(self.clan_tag)
        if not clan_data:
            return await msg.edit(content="❌ Meta mapping failed.")

        async def fetch_blog(tag):
            cached = await self._cache_get(f"battlelog:{tag}")
            if cached:
                return cached
            data = await self.bot.async_fetch_cr_api(f"players/%23{tag}/battlelog")
            if data:
                await self._cache_set(f"battlelog:{tag}", data, TTL_BATTLE_LOG)
            return data

        battle_logs = await asyncio.gather(
            *[fetch_blog(m["tag"].replace("#", "")) for m in clan_data.get("memberList", [])]
        )
        opponent_cards: Counter = Counter()
        for log_entry in [b for b in battle_logs if b]:
            for battle in log_entry[:3]:
                for opp in battle.get("opponent", []):
                    for card in opp.get("cards", []):
                        opponent_cards[card["name"]] += 1

        embed = discord.Embed(
            title="🕵️ Meta Analysis",
            description="Top observed enemy selections:",
            color=0xE74C3C,
        )
        for card, count in opponent_cards.most_common(5):
            embed.add_field(name=card, value=f"Seen {count} times", inline=False)
        await msg.edit(content=None, embed=embed)

    @commands.cooldown(1, HEAVY_COMMANDS_COOLDOWN, commands.BucketType.guild)
    @commands.command()
    async def primetime(self, ctx):
        await self.wait_if_warming(ctx)
        msg = await ctx.send("🕒 Tracking time arrays…")
        clan_data = await self._get_clan_data(self.clan_tag)
        if not clan_data:
            return await msg.edit(content="❌ Heatmap metrics unreachable.")

        async def fetch_blog(tag):
            cached = await self._cache_get(f"battlelog:{tag}")
            if cached:
                return cached
            data = await self.bot.async_fetch_cr_api(f"players/%23{tag}/battlelog")
            if data:
                await self._cache_set(f"battlelog:{tag}", data, TTL_BATTLE_LOG)
            return data

        results = await asyncio.gather(
            *[fetch_blog(m["tag"].replace("#", "")) for m in clan_data.get("memberList", [])]
        )

        total_counts = Counter()
        for log_entry in [r for r in results if r]:
            for b in log_entry:
                ts = b.get("battleTime", "")
                if len(ts) >= 15:
                    h = (
                        datetime.strptime(ts[:15], "%Y%m%dT%H%M%S")
                        .replace(tzinfo=zoneinfo.ZoneInfo("UTC"))
                        .astimezone(zoneinfo.ZoneInfo("America/New_York"))
                        .hour
                    )
                    total_counts[h] += 1

        top_hour = total_counts.most_common(1)[0][0] if total_counts else 20
        await msg.delete()
        await ctx.send(
            f"🔥 Prime active window evaluates to **{top_hour:02d}:00 Eastern Time Zone** metrics."
        )

    @commands.command()
    @commands.has_permissions(manage_guild=True)
    async def setreminders(self, ctx, channel: discord.TextChannel):
        await self.guilds.update_one(
            {"_id": str(ctx.guild.id)},
            {"$set": {"channel_id": channel.id}},
            upsert=True,
        )
        await ctx.send(f"✅ Reminders successfully targeted on {channel.mention}.")


async def setup(bot):
    await bot.add_cog(ClashRoyale(bot))