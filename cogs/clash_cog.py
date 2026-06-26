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

CONCURRENT_REQUESTS      = 5
TTL_CARDS                = 60 * 60 * 24
TTL_CLAN                 = 60 * 10
TTL_PLAYER               = 60 * 5
TTL_BATTLE_LOG           = 60 * 5        # FIX: was 24h — battles change frequently
TTL_WAR                  = 60 * 5
WARMUP_RELEVANT_COMMANDS = {"scout", "primetime", "cardstats", "whohas"}
HEAVY_COMMANDS_COOLDOWN  = 30


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _safe_int(val, default: int = 0) -> int:
    """Coerce val to int safely, returning default on failure."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _win_rate(wins: int, losses: int) -> float:
    total = wins + losses
    return round(wins / total * 100, 1) if total > 0 else 0.0


def _deck_elixir(cards: list) -> float | None:
    """Return average elixir cost of a deck. Returns None if data is unavailable."""
    costs = [c.get("elixirCost") for c in cards if c.get("elixirCost") is not None]
    return round(sum(costs) / len(costs), 2) if costs else None


def _donation_ratio(given: int, received: int) -> str:
    """Human-friendly donation ratio string."""
    if received == 0:
        return f"{given}:0 (pure giver)" if given else "0:0"
    ratio = round(given / received, 2)
    return f"{given}/{received} ({ratio}x)"


def _progress_bar(value: int, maximum: int, filled: str = "🟩", empty: str = "⬜") -> str:
    """Generic segmented progress bar."""
    value   = max(0, min(value, maximum))
    n_full  = value
    n_empty = maximum - n_full
    return filled * n_full + empty * n_empty


def _format_battle_time(raw: str) -> str:
    """Convert CR battleTime string (20240101T120000.000Z) to readable form."""
    try:
        dt = datetime.strptime(raw[:15], "%Y%m%dT%H%M%S")
        return dt.strftime("%b %d, %Y %H:%M UTC")
    except (ValueError, TypeError):
        return raw or "Unknown"


def _resolve_tag(raw: str) -> str:
    """Uppercase and strip leading # from any player/clan tag."""
    return raw.strip().upper().lstrip("#")


# ──────────────────────────────────────────────────────────────────────────────
# ProfileView
# ──────────────────────────────────────────────────────────────────────────────

class ProfileView(discord.ui.View):
    """Tabbed embed view for a player profile."""

    def __init__(
        self,
        data: dict,
        author_id: int,
        battle_history: list = None,
        war_data: dict = None,
        trophy_trend: str = None,
        top_cards_db: list = None,
    ):
        super().__init__(timeout=120)
        self.data           = data
        self.author_id      = author_id
        self.battle_history = battle_history or []
        self.war_data       = war_data or {}
        self.trophy_trend   = trophy_trend        # pre-fetched from DB snapshots
        self.top_cards_db   = top_cards_db or []  # pre-fetched from DB battle history

        player_tag = _resolve_tag(data.get("tag", ""))
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
        d = self.data
        e = discord.Embed(
            title=f"👑 {d.get('name', 'Unknown')} | Lvl {_safe_int(d.get('expLevel'))}",
            color=0x3498DB,
        )

        tag = d.get("tag", "")
        e.set_footer(text=f"Tag: {tag}")

        # Trophies
        trophies      = _safe_int(d.get("trophies"))
        best_trophies = _safe_int(d.get("bestTrophies"))
        pb_note       = " 🏅 Personal Best!" if trophies >= best_trophies and best_trophies > 0 else ""
        e.add_field(
            name="🏆 Trophies",
            value=f"Current: **{trophies:,}**\nBest: **{best_trophies:,}**{pb_note}",
            inline=True,
        )

        # Combat stats
        wins     = _safe_int(d.get("wins"))
        losses   = _safe_int(d.get("losses"))
        draws    = _safe_int(d.get("draws"))          # CR API occasionally includes draws
        win_rate = _win_rate(wins, losses)
        e.add_field(
            name="⚔️ Combat Stats",
            value=(
                f"Wins: **{wins:,}**\n"
                f"Losses: **{losses:,}**\n"
                f"Win Rate: **{win_rate}%**"
                + (f"\nDraws: **{draws:,}**" if draws else "")
            ),
            inline=True,
        )

        # Arena / league
        arena = d.get("arena", {})
        if arena:
            e.add_field(
                name="🏟️ Arena",
                value=f"**{arena.get('name', 'Unknown')}**\nID: {arena.get('id', '?')}",
                inline=True,
            )

        # Trophy trend — prefer DB snapshot history, fall back to live battle log
        trend = self.trophy_trend or self._trophy_trend_live()
        if trend:
            e.add_field(name="📈 Trophy Trend", value=trend, inline=False)

        return e

    def build_social_embed(self) -> discord.Embed:
        d = self.data
        e = discord.Embed(
            title=f"🛡️ Social & War | {d.get('name', 'Unknown')}",
            color=0xE67E22,
        )

        # Clan
        clan = d.get("clan")
        if clan:
            role = d.get("role", "member").capitalize()
            e.add_field(
                name="🏰 Clan",
                value=f"**{clan.get('name', '?')}** (`{clan.get('tag', '?')}`)\nRole: **{role}**",
                inline=False,
            )
        else:
            e.add_field(name="🏰 Clan", value="*Not in a clan*", inline=False)

        # Donations
        given    = _safe_int(d.get("donations"))
        received = _safe_int(d.get("donationsReceived"))
        e.add_field(
            name="🎁 Donations (This Season)",
            value=(
                f"Given: **{given:,}**\n"
                f"Received: **{received:,}**\n"
                f"Ratio: **{_donation_ratio(given, received)}**"
            ),
            inline=True,
        )

        # War / River Race
        if self.war_data:
            fame          = _safe_int(self.war_data.get("fame"))
            decks_today   = _safe_int(self.war_data.get("decksUsedToday"))
            war_day_wins  = _safe_int(self.war_data.get("warDayWins"))
            decks_used    = _safe_int(self.war_data.get("decksUsed"))
            bar           = _progress_bar(decks_today, 4)
            e.add_field(
                name="⚔️ River Race",
                value=(
                    f"Fame: **{fame:,}**\n"
                    f"Decks Today: {bar} ({decks_today}/4)\n"
                    f"Decks Used Total: **{decks_used}**\n"
                    f"War Day Wins: **{war_day_wins}**"
                ),
                inline=True,
            )
        else:
            e.add_field(name="⚔️ River Race", value="*Not participating in current war*", inline=True)

        return e

    def build_deck_embed(self) -> discord.Embed:
        d = self.data
        e = discord.Embed(
            title=f"🃏 Deck & Cards | {d.get('name', 'Unknown')}",
            color=0x9B59B6,
        )

        cards = d.get("currentDeck", [])
        if cards:
            avg_elixir = _deck_elixir(cards)
            deck_lines = []
            for c in cards:
                cost  = c.get("elixirCost")
                rarity = c.get("rarity", "")
                line  = f"• **{c['name']}** Lvl {_safe_int(c.get('level'))}"
                if cost is not None:
                    line += f" — {cost}💧"
                if rarity:
                    line += f" *({rarity})*"
                deck_lines.append(line)
            deck_str = "\n".join(deck_lines)
            elixir_note = f"\n\n⚡ Avg Elixir Cost: **{avg_elixir}**" if avg_elixir else ""
            e.add_field(
                name="⚔️ Current Battle Deck",
                value=deck_str + elixir_note,
                inline=False,
            )
        else:
            e.add_field(name="⚔️ Current Battle Deck", value="*No deck data available.*", inline=False)

        # Most-used cards — prefer DB history, fall back to live battle log
        top_cards = self.top_cards_db or self._top_cards_live()
        label     = "📊 Most-Used Cards (DB history)" if self.top_cards_db else "📊 Most-Used Cards (last 50 battles)"
        if top_cards:
            e.add_field(name=label, value="\n".join(top_cards), inline=False)
        else:
            e.add_field(name="📊 Most-Used Cards", value="*Not enough battle history yet.*", inline=False)

        return e

    def build_stats_embed(self) -> discord.Embed:
        d = self.data
        e = discord.Embed(
            title=f"📈 Performance | {d.get('name', 'Unknown')}",
            color=0x2ECC71,
        )

        # Recent form from live battle log
        recent = self.battle_history[:20]
        if recent:
            wins_recent = sum(1 for b in recent if b.get("result") == "win")
            losses_recent = len(recent) - wins_recent

            # Current win streak
            streak = 0
            for b in recent:
                if b.get("result") == "win":
                    streak += 1
                else:
                    break

            # Recent form bar (W = green, L = red as emoji)
            form_bar = "".join("🟩" if b.get("result") == "win" else "🟥" for b in recent[:10])

            e.add_field(name="🔥 Current Win Streak",    value=f"**{streak}** wins",                      inline=True)
            e.add_field(name="📊 Last 20 Battles",       value=f"**{wins_recent}W / {losses_recent}L** ({_win_rate(wins_recent, losses_recent)}%)", inline=True)
            e.add_field(name="📋 Last 10 Form",          value=form_bar,                                  inline=False)

            # Last battle info
            last = recent[0]
            last_time = _format_battle_time(last.get("battle_time", ""))
            last_result = "✅ Win" if last.get("result") == "win" else "❌ Loss"
            tc = _safe_int(last.get("team_crowns"))
            oc = _safe_int(last.get("opp_crowns"))
            e.add_field(
                name="🕐 Last Battle",
                value=f"{last_result} — {tc}👑 vs {oc}👑\n{last_time}",
                inline=False,
            )
        else:
            e.add_field(name="📊 Recent Battles", value="*No recent battle data found.*", inline=False)

        # Career stats
        e.add_field(name="⭐ Challenge Max Wins",  value=f"**{_safe_int(d.get('challengeMaxWins')):,}**",   inline=True)
        e.add_field(name="🏟️ Tournament Cards",    value=f"**{_safe_int(d.get('tournamentCardsWon')):,}**", inline=True)
        e.add_field(name="🃏 Total Cards Won",     value=f"**{_safe_int(d.get('totalCardsWon')):,}**",      inline=True)  # FIX: was totalExpPoints

        # Badges (optional — CR API returns an array)
        badges = d.get("badges", [])
        if badges:
            notable = [b for b in badges if _safe_int(b.get("progress")) > 0][:5]
            if notable:
                badge_lines = [f"• **{b.get('name', '?')}** — {_safe_int(b.get('progress')):,}" for b in notable]
                e.add_field(name="🎖️ Notable Badges", value="\n".join(badge_lines), inline=False)

        return e

    # ── Helpers (live fallbacks) ──────────────────────────────────────────────

    def _trophy_trend_live(self) -> str | None:
        """Rough trophy trend from live battle results (fallback if no DB snapshots)."""
        if not self.battle_history:
            return None
        recent = self.battle_history[:10]
        wins   = sum(1 for b in recent if b.get("result") == "win")
        losses = len(recent) - wins
        if wins > losses:
            return f"📈 {wins}W / {losses}L in last {len(recent)} battles (live)"
        elif losses > wins:
            return f"📉 {wins}W / {losses}L in last {len(recent)} battles (live)"
        return f"➡️ {wins}W / {losses}L in last {len(recent)} battles (even, live)"

    def _top_cards_live(self) -> list[str]:
        """Most-used cards from the live battle log (fallback)."""
        counter = Counter(
            card.get("name", "Unknown")
            for b in self.battle_history[:50]
            for card in b.get("team_cards", [])
            if card.get("name")
        )
        return [f"• **{name}** — {count} uses" for name, count in counter.most_common(5)]

    # ── Buttons ───────────────────────────────────────────────────────────────

    @discord.ui.button(label="Overview",     style=discord.ButtonStyle.primary,   emoji="📊")
    async def btn_overview(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.build_overview_embed())

    @discord.ui.button(label="Social & War", style=discord.ButtonStyle.success,   emoji="🛡️")
    async def btn_social(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.build_social_embed())

    @discord.ui.button(label="Deck & Cards", style=discord.ButtonStyle.secondary, emoji="🃏")
    async def btn_deck(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.build_deck_embed())

    @discord.ui.button(label="Performance",  style=discord.ButtonStyle.secondary, emoji="📈")
    async def btn_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.build_stats_embed())


# ──────────────────────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────────────────────

class ClashRoyale(commands.Cog):
    def __init__(self, bot):
        self.bot         = bot
        self.db          = bot.db
        self.users       = bot.db_users
        self.guilds      = self.db["guilds"]
        self.mongo_cache = self.db["api_cache"]
        self.clan_tag    = "9LVY89UP"
        self.all_cards   = []
        self.active_warmups = set()

        self.daily_snapshot_loop.start()
        self.warmup_cache_loop.start()   # FIX: replaces bare asyncio.create_task

    def cog_unload(self):
        # FIX: removed self.reminder_loop.cancel() — that loop doesn't exist
        self.daily_snapshot_loop.cancel()
        self.warmup_cache_loop.cancel()

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
        clean  = _resolve_tag(tag)
        cached = await self._cache_get(f"player:{clean}")
        if cached:
            return cached
        data = await self.bot.async_fetch_cr_api(f"players/%23{clean}")
        if data:
            await self._cache_set(f"player:{clean}", data, TTL_PLAYER)
        return data

    async def _get_player_battles(self, tag: str) -> list:
        clean  = _resolve_tag(tag)
        cached = await self._cache_get(f"battles:{clean}")
        if cached:
            return cached
        data    = await self.bot.async_fetch_cr_api(f"players/%23{clean}/battlelog")
        battles = data if isinstance(data, list) else []
        if battles:
            await self._cache_set(f"battles:{clean}", battles, TTL_BATTLE_LOG)
        return battles

    async def _get_clan_data(self, clan_tag: str = None):
        tag    = _resolve_tag(clan_tag or self.clan_tag)
        cached = await self._cache_get(f"clan:{tag}")
        if cached:
            return cached
        data = await self.bot.async_fetch_cr_api(f"clans/%23{tag}")
        if data:
            await self._cache_set(f"clan:{tag}", data, TTL_CLAN)
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

    # ── Trophy trend from DB snapshots ────────────────────────────────────────

    async def _get_trophy_trend(self, tag: str) -> str | None:
        """Compare trophies across stored daily snapshots."""
        clean = _resolve_tag(tag)
        docs  = await self.db["historical_snapshots"].find(
            {"tag": clean}
        ).sort("date", -1).limit(8).to_list(8)

        if len(docs) < 2:
            return None   # fall back to live data in the view

        newest = _safe_int(docs[0].get("trophies"))
        oldest = _safe_int(docs[-1].get("trophies"))
        delta  = newest - oldest
        days   = len(docs) - 1
        arrow  = "📈" if delta > 0 else ("📉" if delta < 0 else "➡️")
        sign   = "+" if delta >= 0 else ""
        return f"{arrow} {sign}{delta:,} trophies over ~{days} days (DB snapshots)"

    # ── Top cards from DB battle history ──────────────────────────────────────

    async def _get_top_cards_from_db(self, tag: str) -> list[str]:
        clean   = _resolve_tag(tag)
        battles = await self.db["battle_history"].find(
            {"player_tag": clean}
        ).sort("battle_time", -1).limit(50).to_list(50)

        counter = Counter(
            card.get("name", "Unknown")
            for b in battles
            for card in b.get("team_cards", [])
            if card.get("name")
        )
        return [f"• **{name}** — {count} uses" for name, count in counter.most_common(5)]

    # ── Harvest ───────────────────────────────────────────────────────────────

    async def run_harvest_logic(self):
        harvest_start = time.monotonic()

        clan_data, war_data = await asyncio.gather(
            self.bot.async_fetch_cr_api(f"clans/%23{self.clan_tag}"),
            self.bot.async_fetch_cr_api(f"clans/%23{self.clan_tag}/currentriverrace"),
        )

        if not clan_data:
            log.warning("Harvest aborted: could not fetch clan data.")
            return

        # Refresh the clan cache immediately so commands see fresh data
        await self._cache_set(f"clan:{self.clan_tag}", clan_data, TTL_CLAN)
        if war_data:
            await self._cache_set(f"war:{self.clan_tag}", war_data, TTL_WAR)

        snapshot_date = datetime.now(zoneinfo.ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        members       = clan_data.get("memberList", [])

        war_participants: dict[str, dict] = {}
        if war_data:
            war_participants = {
                _resolve_tag(p["tag"]): p
                for p in war_data.get("clan", {}).get("participants", [])
                if p.get("tag")
            }

        snapshot_ops: list[UpdateOne] = []
        battle_ops:   list[UpdateOne] = []
        sem = asyncio.Semaphore(CONCURRENT_REQUESTS)

        async def harvest_member(member):
            tag = _resolve_tag(member["tag"])
            async with sem:
                profile, blog = await asyncio.gather(
                    self.bot.async_fetch_cr_api(f"players/%23{tag}"),
                    self.bot.async_fetch_cr_api(f"players/%23{tag}/battlelog"),
                )
            return tag, member, profile, blog

        results = await asyncio.gather(*(harvest_member(m) for m in members))

        for tag, m, profile, blog in results:
            # ── Daily snapshot ────────────────────────────────────────────────
            flat: dict = {
                "date":     snapshot_date,
                "name":     m.get("name", ""),
                "tag":      tag,
                "trophies": _safe_int(m.get("trophies")),
                "role":     m.get("role", "member"),
            }
            # Enrich snapshot with full profile data if available
            if profile:
                flat.update({
                    "wins":              _safe_int(profile.get("wins")),
                    "losses":            _safe_int(profile.get("losses")),
                    "donations":         _safe_int(profile.get("donations")),
                    "donationsReceived": _safe_int(profile.get("donationsReceived")),
                    "expLevel":          _safe_int(profile.get("expLevel")),
                    "bestTrophies":      _safe_int(profile.get("bestTrophies")),
                    "totalCardsWon":     _safe_int(profile.get("totalCardsWon")),   # FIX: was totalExpPoints
                })
                # Invalidate player cache so next fetch gets updated data
                await self._cache_set(f"player:{tag}", profile, TTL_PLAYER)

            if tag in war_participants:
                wp = war_participants[tag]
                flat.update({
                    "fame":            _safe_int(wp.get("fame")),
                    "decksUsedToday":  _safe_int(wp.get("decksUsedToday")),
                    "decksUsed":       _safe_int(wp.get("decksUsed")),
                    "warDayWins":      _safe_int(wp.get("warDayWins")),
                })

            snapshot_ops.append(
                UpdateOne({"tag": tag, "date": snapshot_date}, {"$set": flat}, upsert=True)
            )

            # ── Battle history ────────────────────────────────────────────────
            if blog and isinstance(blog, list):
                for battle in blog:
                    bt = battle.get("battleTime")
                    if not bt:
                        continue
                    battle_id  = f"{tag}_{bt}"
                    team       = battle.get("team", [{}])[0]
                    opp        = battle.get("opponent", [{}])[0]
                    team_crowns = _safe_int(team.get("crowns"))
                    opp_crowns  = _safe_int(opp.get("crowns"))
                    doc = {
                        "player_tag":       tag,
                        "player_name":      m.get("name", ""),
                        "battle_time":      bt,
                        "game_mode":        battle.get("gameMode", {}).get("name", ""),
                        "type":             battle.get("type", ""),
                        "result":           "win" if team_crowns > opp_crowns else ("draw" if team_crowns == opp_crowns else "loss"),
                        "team_crowns":      team_crowns,
                        "opp_crowns":       opp_crowns,
                        "team_trophies":    _safe_int(team.get("startingTrophies")),
                        "opp_trophies":     _safe_int(opp.get("startingTrophies")),
                        "team_cards":       team.get("cards", []),
                        "opponent_cards":   opp.get("cards", []),
                        "opp_name":         opp.get("name", ""),
                        "opp_tag":          _resolve_tag(opp.get("tag", "")),
                        "trophy_change":    _safe_int(team.get("trophyChange")),
                    }
                    battle_ops.append(
                        UpdateOne({"_id": battle_id}, {"$set": doc}, upsert=True)
                    )

        if snapshot_ops:
            await self.db["historical_snapshots"].bulk_write(snapshot_ops, ordered=False)
        if battle_ops:
            await self.db["battle_history"].bulk_write(battle_ops, ordered=False)

        elapsed = round(time.monotonic() - harvest_start, 1)
        log.info(
            f"✅ Harvest complete in {elapsed}s — "
            f"{len(members)} members, "
            f"{len(snapshot_ops)} snapshots, "
            f"{len(battle_ops)} battle records."
        )

    # ── Loops ─────────────────────────────────────────────────────────────────

    @tasks.loop(time=dt_time(hour=23, minute=55, tzinfo=zoneinfo.ZoneInfo("America/New_York")))
    async def daily_snapshot_loop(self):
        await self.run_harvest_logic()

    @daily_snapshot_loop.before_loop
    async def before_snapshot_loop(self):
        await self.bot.wait_until_ready()

    # FIX: safe card warmup — replaces bare asyncio.create_task in __init__
    @tasks.loop(count=1)
    async def warmup_cache_loop(self):
        await self._cache_cards()

    @warmup_cache_loop.before_loop
    async def before_warmup_cache(self):
        await self.bot.wait_until_ready()

    # ── Commands ──────────────────────────────────────────────────────────────

    @commands.command(name="p", aliases=["profile"])
    async def p(self, ctx, *, target: str = None):
        """Show a player profile. Pass a tag or leave blank to use your linked account."""
        tag = None
        if target:
            tag = _resolve_tag(target)
        else:
            user_doc = await self.users.find_one({"discord_id": ctx.author.id})
            if user_doc:
                tag = _resolve_tag(user_doc.get("cr_tag", ""))

        if not tag:
            await ctx.send(
                "❌ Please provide a player tag, e.g. `!p #ABC123`, or link your account first."
            )
            return

        async with ctx.typing():
            profile, battles, war_api, trophy_trend, top_cards_db = await asyncio.gather(
                self._get_player_data(tag),
                self._get_player_battles(tag),
                self._get_war_data(),
                self._get_trophy_trend(tag),
                self._get_top_cards_from_db(tag),
            )

        if not profile:
            await ctx.send(
                f"❌ Could not find a player with tag `#{tag}`. Double-check the tag and try again."
            )
            return

        # Find this player's war entry
        war_entry = {}
        if war_api:
            participants = war_api.get("clan", {}).get("participants", [])
            war_entry = next(
                (p for p in participants if _resolve_tag(p.get("tag", "")) == tag),
                {}
            )

        # Normalise live battle log for the view
        battle_history = []
        for b in (battles or []):
            team = b.get("team", [{}])[0]
            opp  = b.get("opponent", [{}])[0]
            tc   = _safe_int(team.get("crowns"))
            oc   = _safe_int(opp.get("crowns"))
            battle_history.append({
                "result":         "win" if tc > oc else ("draw" if tc == oc else "loss"),
                "battle_time":    b.get("battleTime", ""),
                "team_crowns":    tc,
                "opp_crowns":     oc,
                "team_cards":     team.get("cards", []),
                "opponent_cards": opp.get("cards", []),
                "trophy_change":  _safe_int(team.get("trophyChange")),
            })

        view  = ProfileView(
            profile,
            ctx.author.id,
            battle_history=battle_history,
            war_data=war_entry,
            trophy_trend=trophy_trend,
            top_cards_db=top_cards_db,
        )
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

        sorted_p    = sorted(participants, key=lambda x: _safe_int(x.get("fame")), reverse=True)
        total_fame  = sum(_safe_int(p.get("fame")) for p in sorted_p)
        total_decks = sum(_safe_int(p.get("decksUsedToday")) for p in sorted_p)
        max_decks   = len(sorted_p) * 4

        # Clan-level war info
        clan_info = war_api.get("clan", {})
        period_type = war_api.get("periodType", "")

        e = discord.Embed(
            title=f"⚔️ River Race — {clan_info.get('name', 'Our Clan')}",
            color=0xE67E22,
        )
        if period_type:
            e.set_footer(text=f"Period type: {period_type.capitalize()}")

        e.add_field(name="Total Fame",   value=f"**{total_fame:,}**",               inline=True)
        e.add_field(name="Decks Used",   value=f"**{total_decks}/{max_decks}**",    inline=True)
        e.add_field(name="Participants", value=f"**{len(sorted_p)}**",              inline=True)

        # Identify members who haven't used any decks today
        idle = [p["name"] for p in sorted_p if _safe_int(p.get("decksUsedToday")) == 0]
        if idle:
            e.add_field(
                name=f"⚠️ No Decks Used Today ({len(idle)})",
                value=", ".join(idle[:15]) + ("…" if len(idle) > 15 else ""),
                inline=False,
            )

        rows = []
        for i, p in enumerate(sorted_p[:15], 1):
            decks = _safe_int(p.get("decksUsedToday"))
            fame  = _safe_int(p.get("fame"))
            bar   = _progress_bar(decks, 4)
            rows.append(f"`{i:>2}.` **{p['name']}** — {fame:,} fame {bar}")

        e.add_field(name="Standings (top 15)", value="\n".join(rows) or "None", inline=False)
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
