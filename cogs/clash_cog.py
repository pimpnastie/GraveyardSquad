import os
import csv
import io
import json
import time
import asyncio
import logging
import discord
from discord import app_commands
from collections import Counter
import zoneinfo
from datetime import datetime, time as dt_time, timezone
from discord.ext import commands, tasks
from thefuzz import process
from pymongo import UpdateOne
from data_harvester import get_harvester

log = logging.getLogger("clashbot")

# Idea #100: a staging/dry-run toggle — when set, DMs and channel posts are
# logged instead of actually sent, so new commands/automations can be trialed
# without pinging the live clan.
STAGING_MODE = os.getenv("STAGING_MODE", "").lower() in ("1", "true", "yes")

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
        e.add_field(name="🃏 Cards Found",         value=str(len(self.data.get("cards") or [])),      inline=True)
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

        self.process_pending_actions_loop.start()
        self.check_auto_strike_rules.start()
        asyncio.create_task(self._cache_cards())

    def cog_unload(self):
        self.process_pending_actions_loop.cancel()
        self.check_auto_strike_rules.cancel()

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
        docs = await self.db["player_snapshots"].find(
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
    # NOTE: The actual data harvest (clan/profiles/battles/snapshots/war) lives
    # in data_harvester.py's DataHarvester class, running on its own background
    # thread started from mainbot2.py. This cog no longer runs a second,
    # differently-schemad harvest — it only reads what that harvester wrote,
    # and can ask it to run early via the shared singleton (get_harvester()).

    async def run_harvest_logic(self):
        """Manually kick the shared harvester's full cycle without blocking the event loop."""
        harvester = get_harvester()
        await self.bot.loop.run_in_executor(None, harvester.run_full_cycle)

    # ── Pending admin actions (queued by the Flask dashboard) ──────────────────

    @tasks.loop(seconds=20)
    async def process_pending_actions_loop(self):
        pending = self.db["pending_actions"]
        actions = await pending.find({"processed": False}).to_list(50)
        if not actions:
            return

        guild = self.bot.get_guild(int(os.getenv("GUILD_ID", "0")))
        quiet = await self._in_quiet_hours()
        dm_kinds = {"dm_warning", "war_nudge", "war_nudge_tier", "first_week_checkin"}

        for action in actions:
            try:
                kind = action.get("kind")
                if quiet and kind in dm_kinds:
                    continue  # idea #99: leave unprocessed, retry after quiet hours
                if kind == "dm_warning":
                    await self._send_dm(guild, action.get("discord_id"),
                        f"⚔️ **Graveyard Squad Notice:** {action.get('message', 'Please remember to use your war decks!')}")
                elif kind == "first_week_checkin":
                    # Idea #123: friendly automated 7-ish-day check-in DM, queued
                    # by data_harvester.py's check_first_week_checkins().
                    await self._send_dm(guild, action.get("discord_id"), action.get("message", "Welcome to the clan!"))
                elif kind == "war_nudge":
                    for discord_id in action.get("discord_ids", []):
                        await self._send_dm(guild, discord_id,
                            "⚔️ **War Reminder:** You still have decks left to use in the current River Race. Please jump in before the war ends!")
                elif kind == "lfg_ping":
                    channel_id = self.bot.war_channel_id
                    if channel_id and guild:
                        channel = guild.get_channel(int(channel_id))
                        if channel:
                            await channel.send(f"⚔️ **{action.get('user', 'A clan member')}** is looking for a practice/2v2 partner!")
                elif kind == "war_summary_post":
                    # Idea #12: auto-compiled post-war recap, queued by
                    # data_harvester.py's _queue_war_summary_post() the first time
                    # a completed race is seen. Posted once per race since the
                    # harvester only queues on first-seen (see backfill_missed_wars).
                    channel_id = await self._announcements_channel_id()
                    if channel_id and guild:
                        channel = guild.get_channel(int(channel_id))
                        if channel:
                            recap_msg = await channel.send(action.get("message", "War recap unavailable."))
                            # Idea #95: spin up a thread off the recap so war-day
                            # chatter doesn't clutter the main channel.
                            if isinstance(channel, discord.TextChannel):
                                try:
                                    await recap_msg.create_thread(
                                        name=f"War Recap Discussion — {datetime.now(timezone.utc):%b %d}",
                                        auto_archive_duration=1440,
                                    )
                                except discord.HTTPException as e:
                                    log.warning(f"Could not create war recap thread: {e}")
                elif kind in ("role_change_post", "milestone_post", "war_start_reminder", "anniversary_shoutout", "weekly_digest_post", "stream_live_post"):
                    # Ideas #111 (role-change), #105 (milestone), #136 (war-start
                    # lead-up reminders), #137 (anniversary shoutouts), #135
                    # (weekly digest), #243 (stream-live announcement) — all
                    # queued by data_harvester.py, all simple channel
                    # announcements routed to the same dedicated
                    # #bot-announcements channel convention (idea #134).
                    channel_id = await self._announcements_channel_id()
                    if channel_id and guild:
                        channel = guild.get_channel(int(channel_id))
                        if channel:
                            await channel.send(action.get("message", ""))
                elif kind == "leadership_escalation":
                    # Idea #131/#139: escalate to leadership specifically, not the
                    # general announcements channel, once a member has ignored
                    # every nudge tier for the day.
                    channel_id = await self._leadership_channel_id()
                    if channel_id and guild:
                        channel = guild.get_channel(int(channel_id))
                        if channel:
                            await channel.send(action.get("message", ""))
                elif kind == "weekly_digest_email":
                    # Idea #138: email digest option — no SMTP is configured in
                    # this project yet, so this logs rather than silently
                    # dropping the action. Wire in a real mail send here once
                    # SMTP credentials exist.
                    log.info(f"[EMAIL DIGEST STUB] Would email {len(action.get('recipient_emails', []))} recipient(s): {action.get('message', '')[:80]}...")
                elif kind == "changelog_post":
                    # Idea #98: auto-post a changelog line whenever settings/templates change.
                    channel_id = (await self.db["config"].find_one({"_id": "bot_settings"}) or {}).get("changelog_channel_id") or await self._announcements_channel_id()
                    if channel_id and guild:
                        channel = guild.get_channel(int(channel_id))
                        if channel:
                            await channel.send(f"🛠️ **Bot Update:** {action.get('message', 'Settings changed.')}")
                elif kind == "war_nudge_tier":
                    # Idea #13: tiered war-day reminders queued by
                    # data_harvester.py's check_tiered_war_reminders() at ~50%
                    # ("soft") and ~90% ("firm") of the war day elapsed.
                    tier = action.get("tier", "soft")
                    wording = (
                        "⚔️ **Friendly reminder:** the war day is half over and you've still got decks left. Jump in when you can!"
                        if tier == "soft" else
                        "🚨 **Last call:** the war day is almost over and you haven't used any decks yet. Please get your battles in!"
                    )
                    for discord_id in action.get("discord_ids", []):
                        await self._send_dm(guild, discord_id, wording)
                await pending.update_one({"_id": action["_id"]}, {"$set": {"processed": True}})
            except Exception as e:
                log.error(f"Failed to process pending action {action.get('_id')}: {e}")
                await pending.update_one({"_id": action["_id"]}, {"$set": {"processed": True, "error": str(e)}})

    @process_pending_actions_loop.before_loop
    async def before_pending_actions_loop(self):
        await self.bot.wait_until_ready()

    async def _announcements_channel_id(self):
        """Idea #134: a dedicated #bot-announcements channel convention for all
        automated posts (war recaps, milestones, digests, role changes), so
        recurring bot content doesn't bury real conversation in the war channel.
        Falls back to war_channel_id if nothing more specific is configured, so
        existing setups keep working without any required migration."""
        settings = await self.db["config"].find_one({"_id": "bot_settings"}) or {}
        return settings.get("announcements_channel_id") or self.bot.war_channel_id

    async def _leadership_channel_id(self):
        settings = await self.db["config"].find_one({"_id": "bot_settings"}) or {}
        return settings.get("leadership_channel_id") or await self._announcements_channel_id()

    async def _in_quiet_hours(self) -> bool:
        """Idea #99: configurable quiet hours (UTC) — automated DMs are held
        rather than dropped; the pending_actions loop just leaves them
        unprocessed so they retry on the next 20s pass once quiet hours end."""
        settings = await self.db["config"].find_one({"_id": "bot_settings"}) or {}
        start_h, end_h = settings.get("quiet_hours_start"), settings.get("quiet_hours_end")
        if start_h is None or end_h is None:
            return False
        hour = datetime.now(timezone.utc).hour
        if start_h <= end_h:
            return start_h <= hour < end_h
        return hour >= start_h or hour < end_h  # wraps past midnight UTC

    async def _send_dm(self, guild, discord_id, content):
        if not guild or not discord_id:
            return
        if STAGING_MODE:
            log.info(f"[STAGING] Would DM {discord_id}: {content}")
            return
        member = guild.get_member(int(discord_id))
        if not member:
            try:
                member = await guild.fetch_member(int(discord_id))
            except discord.NotFound:
                return
        await member.send(content)

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

    def _normalize_battles(self, battles: list) -> list[dict]:
        """Shared normalizer for raw CR battlelog entries -> ProfileView's expected shape."""
        out = []
        for b in (battles or []):
            team = b.get("team", [{}])[0]
            opp  = b.get("opponent", [{}])[0]
            out.append({
                "result":         "win" if team.get("crowns", 0) > opp.get("crowns", 0) else "loss",
                "battle_time":    b.get("battleTime", ""),
                "team_crowns":    team.get("crowns", 0),
                "opp_crowns":     opp.get("crowns", 0),
                "team_cards":     team.get("cards", []),
                "opponent_cards": opp.get("cards", []),
            })
        return out

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

    @commands.command(name="cardstats")
    async def cardstats(self, ctx, *, target: str = None):
        """Fetches maxed and elite card statistics for a player."""
        tag = None
        if target:
            tag = target.upper().lstrip("#")
        else:
            user_doc = await self.users.find_one({"discord_id": ctx.author.id})
            if user_doc:
                tag = user_doc.get("cr_tag", "").upper().lstrip("#")
        
        if not tag:
            await ctx.send("❌ Please provide a player tag, e.g. `!cardstats #ABC123`, or link your account first.")
            return
            
        async with ctx.typing():
            # Use your existing cache helper to fetch the profile
            profile = await self._get_player_data(tag)
            
        if not profile:
            await ctx.send(f"❌ Could not find data for tag `#{tag}`.")
            return
            
        cards_maxed = sum(1 for c in profile.get("cards", []) if c.get("level", 1) >= 14)
        cards_elite = sum(1 for c in profile.get("cards", []) if c.get("level", 1) == 15)
        
        embed = discord.Embed(title=f"📈 Card Stats | {profile.get('name', 'Unknown')}", color=0x2ECC71)
        embed.add_field(name="Maxed Cards (Lvl 14+)", value=str(cards_maxed))
        embed.add_field(name="Elite Cards (Lvl 15)", value=str(cards_elite))
        
        await ctx.send(embed=embed)


    # ── Slash commands (250-ideas pass, section 5: items 81-100) ────────────────
    # discord.py auto-registers @app_commands.command methods defined on a Cog
    # into the bot's CommandTree when the cog is added — mainbot.py's
    # setup_hook() then syncs that tree to GUILD_ID.

    async def _resolve_tag(self, discord_id: int, tag: str | None) -> str | None:
        if tag:
            return tag.upper().lstrip("#")
        user_doc = await self.users.find_one({"discord_id": discord_id})
        if user_doc:
            return (user_doc.get("cr_tag") or "").upper().lstrip("#")
        return None

    @app_commands.command(name="mystats", description="Show a Clash Royale profile (yours, or another tag).")
    @app_commands.describe(tag="Player tag e.g. #ABC123 — leave blank to use your linked account")
    @app_commands.checks.cooldown(1, HEAVY_COMMANDS_COOLDOWN, key=lambda i: i.channel_id)
    async def mystats(self, interaction: discord.Interaction, tag: str = None):
        """Idea #81: slash-command equivalent of !p, so it shows up in Discord's
        command picker and works with autocomplete/typing instead of a prefix."""
        await interaction.response.defer()
        resolved = await self._resolve_tag(interaction.user.id, tag)
        if not resolved:
            await interaction.followup.send("❌ Provide a `tag`, or link your account first with `/link`.", ephemeral=True)
            return
        profile, battles, war_api = await asyncio.gather(
            self._get_player_data(resolved), self._get_player_battles(resolved), self._get_war_data(),
        )
        if not profile:
            await interaction.followup.send(f"❌ Could not find a player with tag `#{resolved}`.", ephemeral=True)
            return
        war_entry = {}
        if war_api:
            participants = war_api.get("clan", {}).get("participants", [])
            war_entry = next((p for p in participants if p.get("tag", "").replace("#", "").upper() == resolved), {})
        view  = ProfileView(profile, interaction.user.id, battle_history=self._normalize_battles(battles), war_data=war_entry)
        await interaction.followup.send(embed=view.build_overview_embed(), view=view)

    @app_commands.command(name="deck", description="Show a player's current battle deck and most-used cards.")
    @app_commands.describe(tag="Player tag e.g. #ABC123 — leave blank to use your linked account")
    async def deck(self, interaction: discord.Interaction, tag: str = None):
        await interaction.response.defer()
        resolved = await self._resolve_tag(interaction.user.id, tag)
        if not resolved:
            await interaction.followup.send("❌ Provide a `tag`, or link your account first with `/link`.", ephemeral=True)
            return
        profile, battles = await asyncio.gather(self._get_player_data(resolved), self._get_player_battles(resolved))
        if not profile:
            await interaction.followup.send(f"❌ Could not find a player with tag `#{resolved}`.", ephemeral=True)
            return
        view = ProfileView(profile, interaction.user.id, battle_history=self._normalize_battles(battles))
        await interaction.followup.send(embed=view.build_deck_embed())

    @app_commands.command(name="leaderboard", description="Top clan members by trophies (from our own tracked data).")
    @app_commands.describe(metric="Which stat to rank by")
    @app_commands.choices(metric=[
        app_commands.Choice(name="Trophies", value="trophies"),
        app_commands.Choice(name="War Fame (current race)", value="fame"),
        app_commands.Choice(name="Win Rate", value="win_rate"),
    ])
    @app_commands.checks.cooldown(1, HEAVY_COMMANDS_COOLDOWN, key=lambda i: i.channel_id)
    async def leaderboard(self, interaction: discord.Interaction, metric: app_commands.Choice[str] = None):
        await interaction.response.defer()
        key = metric.value if metric else "trophies"
        if key == "fame":
            war_api = await self._get_war_data()
            rows = sorted(
                (war_api or {}).get("clan", {}).get("participants", []),
                key=lambda p: p.get("fame", 0), reverse=True,
            )[:10]
            lines = [f"`{i:>2}.` **{p['name']}** — {p.get('fame', 0):,} fame" for i, p in enumerate(rows, 1)]
            title = "🏅 War Fame Leaderboard (current race)"
        else:
            profiles = await self.db["player_profiles"].find({}, {"name": 1, "trophies": 1, "wins": 1, "losses": 1}).to_list(200)
            if key == "win_rate":
                for p in profiles:
                    total = p.get("wins", 0) + p.get("losses", 0)
                    p["_rank_val"] = round(p.get("wins", 0) / total * 100, 1) if total else 0
                profiles.sort(key=lambda p: -p["_rank_val"])
                lines = [f"`{i:>2}.` **{p.get('name','?')}** — {p['_rank_val']}% win rate" for i, p in enumerate(profiles[:10], 1)]
                title = "🏅 Win Rate Leaderboard"
            else:
                profiles.sort(key=lambda p: -p.get("trophies", 0))
                lines = [f"`{i:>2}.` **{p.get('name','?')}** — {p.get('trophies', 0):,} 🏆" for i, p in enumerate(profiles[:10], 1)]
                title = "🏅 Trophy Leaderboard"
        e = discord.Embed(title=title, description="\n".join(lines) or "No data yet.", color=0xF1C40F)
        await interaction.followup.send(embed=e)

    @app_commands.command(name="scout", description="Peek at the opposing clan(s) in the current River Race.")
    @app_commands.checks.cooldown(1, HEAVY_COMMANDS_COOLDOWN, key=lambda i: i.channel_id)
    async def scout(self, interaction: discord.Interaction):
        """Member-facing version of the admin panel's scouting tool (idea #3) —
        this one is read-only and has no admin gate, since it's just clan-vs-clan
        public info already visible in the CR app."""
        await interaction.response.defer()
        war_api = await self._get_war_data()
        if not war_api:
            await interaction.followup.send("❌ No active race to scout right now.", ephemeral=True)
            return
        own_tag = f"#{self.clan_tag}"
        e = discord.Embed(title="🔭 River Race Scouting Report", color=0x8E44AD)
        for c in war_api.get("clans", []):
            tag = c.get("tag", "")
            if tag == own_tag or not tag:
                continue
            detail  = await self._get_clan_data(tag.replace("#", ""))
            members = (detail or {}).get("memberList", [])
            avg     = round(sum(m.get("trophies", 0) for m in members) / len(members)) if members else "?"
            e.add_field(
                name=f"{c.get('name', 'Unknown')} ({tag})",
                value=f"Fame: **{c.get('fame', 0):,}**\nMembers: **{len(members) or '?'}**\nAvg Trophies: **{avg}**",
                inline=True,
            )
        if not e.fields:
            e.description = "Couldn't find any rival clans in the current race data."
        await interaction.followup.send(embed=e)

    @app_commands.command(name="link", description="Link your Discord account to your Clash Royale tag.")
    async def link(self, interaction: discord.Interaction):
        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label="Link Account", style=discord.ButtonStyle.link,
            url="https://graveyardbot.onrender.com/link", emoji="🔗",
        ))
        user_doc = await self.users.find_one({"discord_id": interaction.user.id})
        status = f"Currently linked to `{user_doc['cr_tag']}`." if user_doc and user_doc.get("cr_tag") else "Not linked yet."
        await interaction.response.send_message(
            f"🔗 **Account Linking**\n{status}\nClick below to link (or re-link) via Discord OAuth.",
            view=view, ephemeral=True,
        )

    @app_commands.command(name="botstatus", description="Show harvester/bot health — last data refresh, maintenance mode, uptime.")
    async def botstatus(self, interaction: discord.Interaction):
        """Idea #85: a lightweight public health-check, separate from the
        admin-only diagnostics panel — no sensitive info, just is-it-working."""
        await interaction.response.defer(ephemeral=True)
        config = self.db["config"]
        heartbeat, harvest_meta, system_config = await asyncio.gather(
            config.find_one({"_id": "bot_heartbeat"}),
            config.find_one({"_id": "harvest_meta"}),
            config.find_one({"_id": "system_config"}),
        )
        heartbeat, harvest_meta, system_config = heartbeat or {}, harvest_meta or {}, system_config or {}
        e = discord.Embed(title="🤖 Bot Status", color=0x2ECC71 if not system_config.get("maintenance_mode") else 0xE74C3C)
        e.add_field(name="Maintenance Mode", value="🔴 ON" if system_config.get("maintenance_mode") else "🟢 OFF", inline=True)
        e.add_field(name="Staging Mode", value="🧪 ON (DMs logged only)" if STAGING_MODE else "OFF", inline=True)
        last_harvest = harvest_meta.get("last_run_at")
        e.add_field(name="Last Data Refresh", value=last_harvest.strftime("%Y-%m-%d %H:%M UTC") if last_harvest else "Unknown", inline=True)
        last_beat = heartbeat.get("last_beat_at")
        e.add_field(name="Last Heartbeat", value=last_beat.strftime("%Y-%m-%d %H:%M UTC") if last_beat else "Unknown", inline=True)
        await interaction.followup.send(embed=e, ephemeral=True)

    @app_commands.command(name="recruit", description="[Admin] Add a scouted player to the recruiting shortlist.")
    @app_commands.describe(tag="Their player tag", notes="Optional notes")
    @app_commands.checks.has_permissions(administrator=True)
    async def recruit(self, interaction: discord.Interaction, tag: str, notes: str = ""):
        clean = tag.upper().lstrip("#")
        profile = await self._get_player_data(clean)
        await self.db["recruit_shortlist"].update_one(
            {"tag": clean},
            {"$set": {
                "tag": clean, "name": (profile or {}).get("name", ""), "notes": notes,
                "status": "scouted", "updated_at": datetime.now(timezone.utc),
            }, "$setOnInsert": {"added_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        name_suffix = f" ({profile['name']})" if profile and profile.get("name") else ""
        await interaction.response.send_message(
            f"✅ Added `#{clean}`{name_suffix} to the recruiting shortlist.",
            ephemeral=True,
        )

    @app_commands.command(name="syncbeta", description="[Admin] Manually sync nicknames for the beta-test group (no auto-sync).")
    async def syncbeta(self, interaction: discord.Interaction):
        """Idea #82/#83: the user explicitly did NOT want an automatic role/
        nickname sync loop — this is an on-demand, admin-triggered command,
        scoped to whichever role is configured as the beta-test group via
        bot_settings.beta_sync_role_id (set through the admin Settings tab)."""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        settings = await self.db["config"].find_one({"_id": "bot_settings"}) or {}
        role_id = settings.get("beta_sync_role_id")
        if not role_id:
            await interaction.followup.send(
                "⚠️ No beta-test group role is configured yet. Set `beta_sync_role_id` in the admin Settings tab first.",
                ephemeral=True,
            )
            return
        role = interaction.guild.get_role(int(role_id))
        if not role:
            await interaction.followup.send("⚠️ Configured beta-sync role no longer exists on this server.", ephemeral=True)
            return
        synced, skipped = 0, 0
        for member in role.members:
            user_doc = await self.users.find_one({"discord_id": member.id})
            tag = (user_doc or {}).get("cr_tag", "").lstrip("#")
            if not tag:
                skipped += 1
                continue
            profile = await self._get_player_data(tag)
            if not profile or not profile.get("name"):
                skipped += 1
                continue
            try:
                await member.edit(nick=profile["name"][:32])
                synced += 1
            except discord.Forbidden:
                skipped += 1
        await interaction.followup.send(
            f"✅ Beta sync complete: **{synced}** nickname(s) updated, **{skipped}** skipped (unlinked or missing permissions).",
            ephemeral=True,
        )

    @app_commands.command(name="reactionrole", description="[Admin] Post a message where a reaction grants/removes a role.")
    @app_commands.describe(message="The message to post", emoji="The emoji members react with", role="The role to grant")
    @app_commands.checks.has_permissions(administrator=True)
    async def reactionrole(self, interaction: discord.Interaction, message: str, emoji: str, role: discord.Role):
        """Idea #96: reaction-role setup for things like LFG pings / war
        reminders opt-in, without needing a separate bot."""
        await interaction.response.defer(ephemeral=True)
        sent = await interaction.channel.send(message)
        try:
            await sent.add_reaction(emoji)
        except discord.HTTPException:
            await interaction.followup.send("⚠️ Message posted, but that emoji couldn't be added — pick a standard/server emoji.", ephemeral=True)
            return
        await self.db["reaction_roles"].insert_one({
            "message_id": sent.id, "channel_id": sent.channel.id, "emoji": str(emoji),
            "role_id": role.id, "created_at": datetime.now(timezone.utc),
        })
        await interaction.followup.send(f"✅ Reaction role set up: react with {emoji} on that message for **{role.name}**.", ephemeral=True)

    # ── Reaction-role listeners (idea #96) ──────────────────────────────────────

    async def _reaction_role_lookup(self, payload):
        return await self.db["reaction_roles"].find_one({
            "message_id": payload.message_id, "emoji": str(payload.emoji),
        })

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.user_id == self.bot.user.id:
            return
        mapping = await self._reaction_role_lookup(payload)
        if not mapping:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        role = guild.get_role(mapping["role_id"])
        member = guild.get_member(payload.user_id)
        if role and member:
            try:
                await member.add_roles(role, reason="Reaction role")
            except discord.Forbidden:
                log.warning(f"Missing permissions to grant reaction role {role.id} to {member.id}")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        mapping = await self._reaction_role_lookup(payload)
        if not mapping:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        role = guild.get_role(mapping["role_id"])
        member = guild.get_member(payload.user_id)
        if role and member:
            try:
                await member.remove_roles(role, reason="Reaction role")
            except discord.Forbidden:
                pass

    # ── Voice-channel activity tracking (idea #91) ──────────────────────────────
    # Lightweight participation signal — logs join/leave timestamps so a future
    # "active member" metric doesn't have to rely on message activity alone.

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if before.channel == after.channel:
            return
        now = datetime.now(timezone.utc)
        if after.channel and not before.channel:
            await self.db["voice_activity"].insert_one({
                "discord_id": member.id, "channel_id": after.channel.id, "joined_at": now, "left_at": None,
            })
        elif before.channel and not after.channel:
            await self.db["voice_activity"].update_one(
                {"discord_id": member.id, "channel_id": before.channel.id, "left_at": None},
                {"$set": {"left_at": now}},
                sort=[("joined_at", -1)],
            )

    # ── Configurable auto-strike rules (idea #86) ───────────────────────────────
    # Off by default. Admins opt in via bot_settings.auto_strike_missed_war_threshold
    # (e.g. 3 = auto-strike after 3 consecutive war weeks with 0 decks used).
    # De-duped per member via player_profiles.last_auto_strike_streak so the same
    # streak length never double-strikes on repeated loop runs.

    @tasks.loop(hours=6)
    async def check_auto_strike_rules(self):
        settings = await self.db["config"].find_one({"_id": "bot_settings"}) or {}
        threshold = int(settings.get("auto_strike_missed_war_threshold") or 0)
        if threshold <= 0:
            return
        races = await self.db["war_history"].find({}, {"data.clan.participants": 1}).sort("data.seasonId", -1).to_list(10)
        per_member = {}
        for race in races:
            for p in ((race.get("data", {}).get("clan") or {}).get("participants") or []):
                tag = p.get("tag")
                if not tag:
                    continue
                entry = per_member.setdefault(tag, {"name": p.get("name"), "streak": 0, "broke": False})
                if entry["broke"]:
                    continue
                if p.get("decksUsed", p.get("decksUsedToday", 0)) == 0:
                    entry["streak"] += 1
                else:
                    entry["broke"] = True

        guild = self.bot.get_guild(int(os.getenv("GUILD_ID", "0")))
        for tag, info in per_member.items():
            if info["streak"] < threshold:
                continue
            profile = await self.db["player_profiles"].find_one({"tag": tag}, {"last_auto_strike_streak": 1})
            if profile and profile.get("last_auto_strike_streak") == info["streak"]:
                continue  # already struck for this exact streak length
            await self.db["player_profiles"].update_one(
                {"tag": tag},
                {"$inc": {"strikes": 1}, "$set": {"last_auto_strike_streak": info["streak"]}},
                upsert=True,
            )
            user_doc = await self.users.find_one({"cr_tag": tag})
            if user_doc and user_doc.get("discord_id"):
                await self._send_dm(
                    guild, user_doc["discord_id"],
                    f"⚠️ **Auto-Strike Notice:** you've had {info['streak']} consecutive war weeks with 0 decks used, "
                    "so a strike was automatically added to your record. Reach out to leadership if this is a mistake.",
                )

    @check_auto_strike_rules.before_loop
    async def before_check_auto_strike_rules(self):
        await self.bot.wait_until_ready()


# ──────────────────────────────────────────────────────────────────────────────
# Context menu command (idea #84): right-click a member -> quick CR profile
# lookup, without typing a tag, for whoever has already linked their account.
# ──────────────────────────────────────────────────────────────────────────────

@app_commands.context_menu(name="View CR Profile")
async def view_cr_profile_ctx(interaction: discord.Interaction, member: discord.Member):
    cog: ClashRoyale = interaction.client.get_cog("ClashRoyale")
    if not cog:
        await interaction.response.send_message("❌ Clash Royale cog isn't loaded.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    user_doc = await cog.users.find_one({"discord_id": member.id})
    tag = (user_doc or {}).get("cr_tag", "").upper().lstrip("#")
    if not tag:
        await interaction.followup.send(f"⚠️ {member.display_name} hasn't linked a Clash Royale account yet.", ephemeral=True)
        return
    profile, battles = await asyncio.gather(cog._get_player_data(tag), cog._get_player_battles(tag))
    if not profile:
        await interaction.followup.send(f"❌ Could not find data for `#{tag}`.", ephemeral=True)
        return
    view = ProfileView(profile, interaction.user.id, battle_history=cog._normalize_battles(battles))
    await interaction.followup.send(embed=view.build_overview_embed(), view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(ClashRoyale(bot))
    bot.tree.add_command(view_cr_profile_ctx)