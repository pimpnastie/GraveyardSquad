import os
import json
import time
import logging
import tempfile
import zipfile
import threading
from datetime import datetime, timezone, timedelta
import requests
from bson import json_util
from pymongo import MongoClient, UpdateOne
import redis as _redis
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("harvester")

def _as_aware_utc(dt):
    """PyMongo returns naive datetimes by default (this project's MongoClient
    doesn't set tz_aware=True), even though every value written here is UTC.
    Re-attach tzinfo before subtracting from datetime.now(timezone.utc) so this
    doesn't raise "can't subtract offset-naive and offset-aware datetimes" the
    first time a value makes a real Mongo round-trip."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [HARVESTER] %(message)s")

# Idea #17 (from the 250-ideas pass, revised in review): when a member leaves the
# clan we don't want to lose their historical stats immediately, but we also don't
# want player_profiles/player_snapshots/battle_history growing forever with dead
# tags. Keep departed members' data queryable for this many weeks, then purge.
DEPARTED_MEMBER_RETENTION_WEEKS = 23

# River Race format: 3 training days (periodType "training") + 4 war days
# (periodType "warDay") = 7 total periods per race. Used by the "projected finish"
# calc (idea #4) to turn periodIndex into an elapsed/total fraction. Supercell
# doesn't expose this as a constant in the API response, so it's hardcoded here —
# if Supercell ever changes the race format, this needs updating too.
RIVER_RACE_TOTAL_PERIODS = 7
RIVER_RACE_MAX_FAME = 10000

# Idea #105: round-number milestones that trigger an auto-congratulation post.
# Lifetime `wins` and `bestTrophies` are both monotonically-increasing-ish fields
# the CR API already gives us, so no extra tracking is needed to detect a crossing.
MILESTONE_WIN_STEP = 500
MILESTONE_TROPHY_STEP = 1000

# Idea #107: how recently-joined a member can be and still count as "rising star"
# eligible, and idea #115's weekly rivalry re-pairing cadence.
RISING_STAR_WINDOW_DAYS = 45
RIVALRY_REASSIGN_DAYS = 7

# How many days back "this week"/"7-day" superlatives (Hall of Fame, Spotlights)
# look for battle-log-derived categories (win rate, 3-crowns, shutouts, etc.).
WEEKLY_LOOKBACK_DAYS = 7
# Minimum battles logged in the lookback window before a member is eligible for
# a rate-based category (win rate, 3-crown rate) -- otherwise one lucky/unlucky
# battle out of 1 would swing a whole leaderboard.
MIN_BATTLES_FOR_RATE_CATEGORY = 3

# Idea #123: first-week check-in DM window — checked against joined_clan_at,
# a few-day window (not an exact "day 7" match) so a 30-min harvest cadence
# can't skip past the single instant a stricter equality check would require.
FIRST_WEEK_CHECKIN_MIN_DAYS = 6
FIRST_WEEK_CHECKIN_MAX_DAYS = 9

# Idea #231/#232: must match web_routes.py's CR_API_CACHE_TTL_SECONDS /
# _cr_api_cache_key exactly, since the harvester pre-warms the same Redis keys
# the web process reads. Duplicated (not imported) to avoid a circular import
# between the two modules — web_routes.py already imports FROM data_harvester.py.
CR_API_CACHE_TTL_SECONDS = int(os.getenv("CR_API_CACHE_TTL_SECONDS", "60"))

def _cr_api_cache_key(endpoint: str) -> str:
    return f"crapi_cache:{endpoint}"

class DataHarvester:
    def __init__(self):
        # Idea #238: explicit, env-tunable pool size — see the matching comment
        # in web_routes.py's mongo_client_sync for why this was made explicit
        # rather than left on the pymongo default (maxPoolSize=100/client).
        mongo_max_pool_size = int(os.getenv("MONGO_MAX_POOL_SIZE", "50"))
        self.mongo_client = MongoClient(os.getenv("MONGO_URL", "mongodb://localhost:27017"), maxPoolSize=mongo_max_pool_size)
        self.db = self.mongo_client["graveyardbot"]

        self.col_battles = self.db["battle_history"]
        self.col_profiles = self.db["player_profiles"]
        self.col_war = self.db["war_tracking"]
        self.col_war_history = self.db["war_history"]
        self.col_snapshots = self.db["clan_snapshots"]
        self.col_player_snapshots = self.db["player_snapshots"]
        # Task #151: one row per member per day capturing their equipped deck
        # (currentDeck from the raw player API response) -- separate from
        # player_profiles' live overwrite-in-place copy of the same field, so
        # deck composition can be analyzed over time (popularity trends, meta
        # shifts, "how often does this player change their deck") instead of
        # only ever seeing whatever the deck happens to be right now.
        self.col_deck_snapshots = self.db["deck_snapshots"]
        self.col_config = self.db["config"]
        try:
            self.redis_client = _redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))
        except Exception as e:
            log.warning(f"Redis unavailable at harvester startup (cache pre-warming disabled, non-fatal): {e}")
            self.redis_client = None

        self._ensure_indexes()

    def _ensure_indexes(self):
        """Create indexes defensively — a single bad index (e.g. a unique index that
        can't build because of pre-existing duplicate/null values) must not crash the
        whole harvester. Before this fix, a failure here propagated out of __init__,
        which killed get_harvester()'s singleton permanently: every future caller
        (the scheduled loop, the manual harvest button, clash_cog's card cache) hit
        the same exception forever, since _harvester_instance never got set.
        """
        index_specs = [
            (self.col_battles, [("player_tag", 1), ("battle_time", -1)], {}),
            (self.col_battles, "unique_battle_id", {"unique": True}),
            (self.col_player_snapshots, [("tag", 1), ("date", 1)], {"unique": True}),
            (self.col_player_snapshots, "date", {}),
            (self.col_deck_snapshots, [("tag", 1), ("date", 1)], {"unique": True}),
            (self.col_deck_snapshots, "date", {}),
        ]
        for collection, keys, opts in index_specs:
            try:
                collection.create_index(keys, **opts)
            except Exception as e:
                log.error(f"Failed to create index {keys!r} on '{collection.name}': {e}. "
                          f"Harvester will continue without this index — clean up the "
                          f"underlying data (e.g. duplicate/null unique_battle_id records) "
                          f"to restore it.")

        self.cr_token = os.getenv("CR_TOKEN", "").strip()
        self.clan_tag = os.getenv("CLAN_TAG", "9LVY89UP").strip().upper().replace("#", "")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.cr_token}",
            "Accept": "application/json"
        })

        # Restoring your original metadata tracker logic
        self._harvest_meta = {
            "last_run": None,
            "snapshots_saved": 0,
            "profiles_saved": 0,
            "battles_saved": 0,
            "duration_s": None,
            "status": "never_run",
        }
        self._restore_harvest_meta()

    def _restore_harvest_meta(self):
        try:
            doc = self.col_config.find_one({"_id": "harvest_meta"})
            if doc:
                doc.pop("_id", None)
                self._harvest_meta.update(doc)
                log.info(f"✅ Restored harvest metadata. Last run: {self._harvest_meta['last_run']}")
        except Exception as e:
            log.warning(f"Could not restore harvest meta: {e}")

    def _save_harvest_meta(self):
        self.col_config.update_one(
            {"_id": "harvest_meta"},
            {"$set": self._harvest_meta},
            upsert=True
        )

    def fetch_api(self, endpoint: str, retries=4):
        url = f"https://proxy.royaleapi.dev/v1/{endpoint}"
        for attempt in range(retries):
            try:
                resp = self.session.get(url, timeout=10)
                if resp.status_code == 200: return resp.json()
                elif resp.status_code == 429:
                    time.sleep(2 ** attempt)
            except Exception as e:
                time.sleep(2 ** attempt)
        return None

    def _fire_webhooks(self, event: str, payload: dict) -> None:
        """Idea #242/#246: generic outbound-webhook dispatch for third-party
        tools that want to react to clan events (a leadership spreadsheet
        workflow, a Discord relay in another server, etc.) without us building
        a bespoke integration for each one. Registered subscribers live in a
        dedicated `webhooks` collection as {url, events: [...]} docs, managed
        via the admin UI's Webhooks card / the /admin/api/webhooks routes in
        web_routes.py. This is deliberately just "POST a JSON body to a URL" —
        that shape is directly compatible with Zapier's "Webhooks by Zapier"
        trigger and Make.com's "Custom webhook" module (idea #246), so those
        platforms are supported for free with no dedicated Zapier/Make app.
        Delivery is fire-and-forget/best-effort with a short timeout: a slow or
        dead subscriber endpoint must never block or crash a harvest cycle.
        """
        try:
            subs = list(self.col_config.database["webhooks"].find({"events": event}))
        except Exception as e:
            log.warning(f"Webhook subscriber lookup failed (non-fatal): {e}")
            return
        if not subs:
            return
        body = {"event": event, "fired_at": datetime.now(timezone.utc).isoformat(), "data": payload}
        for sub in subs:
            url = sub.get("url")
            if not url:
                continue
            try:
                requests.post(url, json=body, timeout=5)
            except Exception as e:
                log.warning(f"Webhook delivery to {url} failed (non-fatal): {e}")

    def backfill_missed_wars(self):
        """Checks the historical riverracelog to fill gaps if the bot missed a war end."""
        log.info("Checking for missed historical wars...")
        log_data = self.fetch_api(f"clans/%23{self.clan_tag}/riverracelog")
        if log_data and "items" in log_data:
            for race in log_data["items"]:
                season_id = race.get("seasonId")
                section_index = race.get("sectionIndex")
                unique_war_id = f"season_{season_id}_section_{section_index}"

                # Idea #12/#20: only queue a post-war Discord summary + open a retro
                # notes slot the FIRST time we see this race complete, so re-running
                # backfill on every boot doesn't spam the war channel with reposts.
                already_seen = self.col_war_history.find_one({"unique_war_id": unique_war_id}) is not None

                # Upsert into war history so we don't save duplicates
                self.col_war_history.update_one(
                    {"unique_war_id": unique_war_id},
                    {"$set": {"unique_war_id": unique_war_id, "data": race},
                     "$setOnInsert": {"retro_notes": "", "retro_notes_updated_at": None}},
                    upsert=True
                )

                if not already_seen:
                    self._queue_war_summary_post(unique_war_id, race)
                    try:
                        self._update_war_streaks_and_points(race)
                    except Exception as e:
                        log.error(f"War streak/points update failed (non-fatal): {e}")
                    try:
                        self._fire_webhooks("war_end", {
                            "unique_war_id": unique_war_id,
                            "season_id": season_id,
                            "section_index": section_index,
                        })
                    except Exception as e:
                        log.error(f"war_end webhook dispatch failed (non-fatal): {e}")

    def _queue_war_summary_post(self, unique_war_id: str, race: dict):
        """Idea #12: auto-compile a post-war recap (top fame, MVP comeback proxy,
        slacker list) and queue it as a pending_action. clash_cog.py's existing
        pending-actions consumer loop (process_pending_actions_loop) delivers it to
        the configured war channel — this only ever queues, never posts directly,
        since only the bot process holds a live Discord connection.
        """
        clan = race.get("clan", {})
        participants = sorted(clan.get("participants", []), key=lambda p: p.get("fame", 0), reverse=True)
        if not participants:
            return
        top_fame = participants[:3]
        slackers = [p for p in participants if p.get("decksUsed", p.get("decksUsedToday", 0)) == 0]

        lines = [f"**War recap — {clan.get('name', 'Clan')} finished with {clan.get('fame', 0):,} fame**"]
        if top_fame:
            lines.append("Top fame: " + ", ".join(f"{p.get('name')} ({p.get('fame', 0):,})" for p in top_fame))
        if slackers:
            lines.append("Zero decks used: " + ", ".join(p.get("name", "?") for p in slackers[:10]))
        else:
            lines.append("Every participant used at least one deck. Nice work, squad.")

        # Rec #12: shareable image version, generated on-demand by the Flask
        # app (war_recap_png in web_routes.py) from this same war_history doc
        # -- clash_cog.py just points a Discord embed's image at this public
        # URL rather than us trying to render Pillow from the harvester
        # process, which has no web server of its own.
        self.col_config.database["pending_actions"].insert_one({
            "kind": "war_summary_post",
            "message": "\n".join(lines),
            "unique_war_id": unique_war_id,
            "image_url": f"https://graveyardbot.onrender.com/war/{unique_war_id}/recap.png",
            "created_at": datetime.now(timezone.utc),
            "processed": False,
        })

    def _update_war_streaks_and_points(self, race: dict):
        """Idea #103 (streak counters with streak-shields) + #104 (clan points
        currency): runs once per newly-completed race (called from
        backfill_missed_wars alongside _queue_war_summary_post, so it fires
        exactly once per race the same way that already does).

        Streak: +1 per race where the member used all 4 decks. Missing decks
        would normally reset the streak to 0, but a banked "shield" absorbs one
        bad week instead (research cited in 250_IDEAS.md #103 shows shields
        meaningfully reduce drop-off after a single missed day). Shields are
        earned back every 5-streak milestone, capped at 2 banked at a time.

        Points: 1 clan point per deck used (0-4) plus a 10-point bonus for full
        (4/4) participation — a lightweight currency redeemable via the player
        page's flair "shop" (idea #104/#112).
        """
        participants = (race.get("clan", {}) or {}).get("participants", [])
        ops = []
        for p in participants:
            tag = p.get("tag")
            if not tag:
                continue
            decks_used = p.get("decksUsed", p.get("decksUsedToday", 0))
            points_earned = decks_used + (10 if decks_used >= 4 else 0)
            profile = self.col_profiles.find_one({"tag": tag}, {"war_participation_streak": 1, "streak_shields": 1})
            streak = (profile or {}).get("war_participation_streak", 0)
            shields = (profile or {}).get("streak_shields", 1)
            if decks_used >= 4:
                streak += 1
                if streak % 5 == 0:
                    shields = min(2, shields + 1)
            elif shields > 0:
                shields -= 1  # missed week absorbed by a shield; streak preserved
            else:
                streak = 0
            ops.append(UpdateOne(
                {"tag": tag},
                {"$set": {"war_participation_streak": streak, "streak_shields": shields},
                 "$inc": {"clan_points": points_earned}},
                upsert=True,
            ))
        if ops:
            self.col_profiles.bulk_write(ops)

    @staticmethod
    def _lb_entry(tag, name, value, value_label):
        """Build one leaderboard-category entry. `value` is the raw number used
        to decide "is this cycle's result actually better than nothing" (see
        _merge_leaderboard_entry); `value_label` is the pre-formatted display
        string (e.g. "1,234 donations", "68% win rate over 5 games")."""
        if not tag or value is None:
            return None
        return {"tag": tag, "name": name, "value": value, "value_label": value_label}

    @staticmethod
    def _merge_leaderboard_entry(existing_categories: dict, key: str, new_entry: dict | None, computed_at):
        """Zero-handling for every Hall of Fame / Spotlight category: if nobody
        qualifies this cycle (new_entry is None, or its value is falsy — 0
        donations, 0 battles played, etc.), that means the category genuinely
        isn't applicable *this cycle*, not that the previous leader suddenly
        stopped being the leader. Keep whatever was last stored for this key
        (a real, previously-nonzero result) and mark it "stale" so the frontend
        can show something like "(last known, as of Jul 14)" instead of
        silently displaying a misleading "Name: 0".
        Returns None only if there has truly never been any data for this
        category (a legitimate "not enough data yet" case, not a zero).
        """
        if new_entry and new_entry.get("value"):
            new_entry = dict(new_entry)
            new_entry["stale"] = False
            new_entry["as_of"] = computed_at.strftime("%Y-%m-%d")
            return new_entry
        old = existing_categories.get(key)
        if old:
            old = dict(old)
            old["stale"] = True
            # as_of is left untouched from whenever it was last genuinely refreshed.
            return old
        return None

    # Definitions live alongside the compute function (not at module level) so the
    # label/emoji/tooltip travel with the exact logic that decides who wins each
    # category — a reader shouldn't have to jump elsewhere to see both halves.
    def compute_weekly_hall_of_fame(self):
        """Weekly Hall of Fame: 10 quantitative "who's on top right now"
        superlatives, separate in spirit from Weekly Spotlights (more
        community/improvement-flavored) and All-Time Legends (career-spanning).
        Every category is computed from data already harvested (player_profiles,
        player_snapshots, battle_history, the latest war_tracking doc) — no new
        Clash Royale API calls. Stored in config.weekly_hall_of_fame for the
        roster page to read; each category independently falls back to its
        last known non-zero leader via _merge_leaderboard_entry when nobody
        qualifies this cycle (e.g. first harvest of a fresh war before anyone's
        played yet)."""
        now = datetime.now(timezone.utc)
        existing_doc = self.col_config.find_one({"_id": "weekly_hall_of_fame"}) or {}
        existing_categories = existing_doc.get("categories", {})

        profiles = list(self.col_profiles.find(
            {"left_clan_at": {"$exists": False}},
            {"tag": 1, "name": 1, "donations": 1, "donationsReceived": 1, "trophies": 1}
        ))
        by_tag = {p["tag"]: p for p in profiles if p.get("tag")}

        categories = {}

        # 1. Highest Donator (season-to-date) — straight from the CR API's
        # per-member donation counter, refreshed every harvest cycle.
        new_entry = None
        if profiles:
            top = max(profiles, key=lambda p: p.get("donations", 0) or 0)
            new_entry = self._lb_entry(top.get("tag"), top.get("name"), top.get("donations", 0),
                                       f"{top.get('donations', 0):,} donations")
        categories["top_donator"] = self._merge_leaderboard_entry(existing_categories, "top_donator", new_entry, now)

        # 2. Most Donations Received (season-to-date).
        new_entry = None
        if profiles:
            top = max(profiles, key=lambda p: p.get("donationsReceived", 0) or 0)
            new_entry = self._lb_entry(top.get("tag"), top.get("name"), top.get("donationsReceived", 0),
                                       f"{top.get('donationsReceived', 0):,} received")
        categories["most_donations_received"] = self._merge_leaderboard_entry(existing_categories, "most_donations_received", new_entry, now)

        # 3. Top Trophy Pusher (current trophies).
        new_entry = None
        if profiles:
            top = max(profiles, key=lambda p: p.get("trophies", 0) or 0)
            new_entry = self._lb_entry(top.get("tag"), top.get("name"), top.get("trophies", 0),
                                       f"{top.get('trophies', 0):,} 🏆")
        categories["top_trophy_pusher"] = self._merge_leaderboard_entry(existing_categories, "top_trophy_pusher", new_entry, now)

        # 4. Biggest Trophy Climb (7-day delta) — same baseline as the individual
        # player page's trophy_trend_7d, just across every member to find the max.
        week_ago = (now - timedelta(days=WEEKLY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        best_climb = None
        for p in profiles:
            tag = p.get("tag")
            old_snap = self.col_player_snapshots.find_one({"tag": tag, "date": {"$gte": week_ago}}, sort=[("date", 1)])
            if not old_snap:
                continue
            delta = (p.get("trophies", 0) or 0) - old_snap.get("trophies", 0)
            if delta > 0 and (best_climb is None or delta > best_climb[0]):
                best_climb = (delta, tag, p.get("name"))
        new_entry = self._lb_entry(best_climb[1], best_climb[2], best_climb[0], f"+{best_climb[0]:,} 🏆 this week") if best_climb else None
        categories["biggest_trophy_climb"] = self._merge_leaderboard_entry(existing_categories, "biggest_trophy_climb", new_entry, now)

        # 5. War Fame Leader (current war) — from the latest stored war_tracking doc.
        latest_war = self.col_war.find_one({}, sort=[("harvest_time", -1)]) or {}
        war_participants = ((latest_war.get("clan") or {}).get("participants")) or []
        new_entry = None
        if war_participants:
            top = max(war_participants, key=lambda p: p.get("fame", 0) or 0)
            if top.get("fame", 0):
                new_entry = self._lb_entry(top.get("tag"), top.get("name"), top.get("fame", 0), f"{top.get('fame', 0):,} fame")
        categories["war_fame_leader"] = self._merge_leaderboard_entry(existing_categories, "war_fame_leader", new_entry, now)

        # 6. Perfect War Attendance (all 4 war-day decks used, current war).
        perfect = [p for p in war_participants if p.get("decksUsedToday", p.get("decksUsed", 0)) >= 4]
        new_entry = None
        if perfect:
            # Multiple members can qualify; surface whichever has the most fame among them.
            top = max(perfect, key=lambda p: p.get("fame", 0) or 0)
            new_entry = self._lb_entry(top.get("tag"), top.get("name"), 1, "4/4 war decks used")
        categories["perfect_war_attendance"] = self._merge_leaderboard_entry(existing_categories, "perfect_war_attendance", new_entry, now)

        # 7-9: battle_history-derived categories over the last WEEKLY_LOOKBACK_DAYS.
        week_ago_battle_str = (now - timedelta(days=WEEKLY_LOOKBACK_DAYS)).strftime("%Y%m%d")
        recent_battles = list(self.col_battles.find(
            {"battle_time": {"$gte": week_ago_battle_str}},
            {"player_tag": 1, "result": 1, "team_crowns": 1}
        ))
        per_player = {}
        for b in recent_battles:
            tag = b.get("player_tag")
            if not tag:
                continue
            stats = per_player.setdefault(tag, {"games": 0, "wins": 0, "three_crowns": 0})
            stats["games"] += 1
            if b.get("result") == "win":
                stats["wins"] += 1
                if b.get("team_crowns", 0) == 3:
                    stats["three_crowns"] += 1

        def _name_for(tag):
            p = by_tag.get(f"#{tag}") or by_tag.get(tag)
            return p.get("name") if p else None

        # 7. Best Win Rate (7-day, min MIN_BATTLES_FOR_RATE_CATEGORY games).
        best_wr = None
        for tag, s in per_player.items():
            if s["games"] < MIN_BATTLES_FOR_RATE_CATEGORY:
                continue
            wr = s["wins"] / s["games"]
            if wr > 0 and (best_wr is None or wr > best_wr[0]):
                best_wr = (wr, tag, s["games"])
        new_entry = None
        if best_wr:
            full_tag = f"#{best_wr[1]}"
            new_entry = self._lb_entry(full_tag, _name_for(best_wr[1]), best_wr[0],
                                        f"{round(best_wr[0]*100)}% over {best_wr[2]} games")
        categories["best_win_rate"] = self._merge_leaderboard_entry(existing_categories, "best_win_rate", new_entry, now)

        # 8. Most 3-Crown Wins (7-day).
        best_3c = None
        for tag, s in per_player.items():
            if s["three_crowns"] > 0 and (best_3c is None or s["three_crowns"] > best_3c[0]):
                best_3c = (s["three_crowns"], tag)
        new_entry = None
        if best_3c:
            full_tag = f"#{best_3c[1]}"
            new_entry = self._lb_entry(full_tag, _name_for(best_3c[1]), best_3c[0], f"{best_3c[0]} three-crown wins")
        categories["most_three_crowns"] = self._merge_leaderboard_entry(existing_categories, "most_three_crowns", new_entry, now)

        # 9. Most Active (most battles logged, 7-day).
        best_active = None
        for tag, s in per_player.items():
            if s["games"] > 0 and (best_active is None or s["games"] > best_active[0]):
                best_active = (s["games"], tag)
        new_entry = None
        if best_active:
            full_tag = f"#{best_active[1]}"
            new_entry = self._lb_entry(full_tag, _name_for(best_active[1]), best_active[0], f"{best_active[0]} battles this week")
        categories["most_active"] = self._merge_leaderboard_entry(existing_categories, "most_active", new_entry, now)

        # 10. Longest Active Win Streak (current running streak, all-time —
        # capped to each member's last 100 logged battles for performance).
        best_streak = None
        for p in profiles:
            tag = p.get("tag")
            clean = (tag or "").replace("#", "")
            chrono = list(self.col_battles.find({"player_tag": clean}, {"result": 1}).sort("battle_time", -1).limit(100))
            streak = 0
            for b in chrono:  # most-recent-first; count back until the first non-win
                if b.get("result") == "win":
                    streak += 1
                else:
                    break
            if streak > 0 and (best_streak is None or streak > best_streak[0]):
                best_streak = (streak, tag, p.get("name"))
        new_entry = self._lb_entry(best_streak[1], best_streak[2], best_streak[0], f"{best_streak[0]} wins in a row") if best_streak else None
        categories["longest_win_streak"] = self._merge_leaderboard_entry(existing_categories, "longest_win_streak", new_entry, now)

        doc = {"computed_at": now, "categories": {k: v for k, v in categories.items() if v}}
        self.col_config.update_one({"_id": "weekly_hall_of_fame"}, {"$set": doc}, upsert=True)

    def compute_weekly_spotlights(self, war_data: dict | None):
        """Idea #102 (weekly MVP) + #107 (rising star), expanded with 8 more
        community/improvement-flavored categories — deliberately distinct in
        character from the more purely quantitative Weekly Hall of Fame above
        (this is "who deserves recognition this week", not just "who's on top
        of a raw leaderboard"). Recomputed every harvest cycle — "weekly" here
        describes what the numbers represent, not a special weekly-only code
        path. Stored in config.weekly_spotlights in the same
        {computed_at, categories: {key: {...}}} shape as weekly_hall_of_fame,
        with the same per-category zero-fallback via _merge_leaderboard_entry.
        """
        now = datetime.now(timezone.utc)
        existing_doc = self.col_config.find_one({"_id": "weekly_spotlights"}) or {}
        existing_categories = existing_doc.get("categories", {})
        categories = {}

        # 1. War MVP — highest fame + win-rate composite score, current war.
        new_entry = None
        if war_data:
            participants = (war_data.get("clan", {}) or {}).get("participants", [])
            scored = []
            for p in participants:
                profile = self.col_profiles.find_one({"tag": p.get("tag")}, {"wins": 1, "losses": 1}) or {}
                total = profile.get("wins", 0) + profile.get("losses", 0)
                win_rate = (profile.get("wins", 0) / total) if total else 0
                scored.append((p.get("fame", 0) + win_rate * 1000, p))
            if scored:
                score, mvp = max(scored, key=lambda x: x[0])
                if mvp.get("fame", 0):
                    new_entry = self._lb_entry(mvp.get("tag"), mvp.get("name"), mvp.get("fame", 0), f"{mvp.get('fame', 0):,} fame this war")
        categories["mvp"] = self._merge_leaderboard_entry(existing_categories, "mvp", new_entry, now)

        # 2. Rising Star — fastest-climbing member who joined within RISING_STAR_WINDOW_DAYS.
        cutoff = now - timedelta(days=RISING_STAR_WINDOW_DAYS)
        new_members = list(self.col_profiles.find(
            {"joined_clan_at": {"$gte": cutoff}, "left_clan_at": {"$exists": False}}, {"tag": 1, "name": 1}
        ))
        best_delta = 0
        rising_star = None
        for m in new_members:
            snaps = list(self.col_player_snapshots.find({"tag": m["tag"]}, {"trophies": 1, "date": 1}).sort("date", 1))
            if len(snaps) >= 2:
                delta = snaps[-1].get("trophies", 0) - snaps[0].get("trophies", 0)
                if delta > best_delta:
                    best_delta = delta
                    rising_star = m
        new_entry = self._lb_entry(rising_star["tag"], rising_star.get("name"), best_delta, f"+{best_delta:,} 🏆 since joining") if rising_star else None
        categories["rising_star"] = self._merge_leaderboard_entry(existing_categories, "rising_star", new_entry, now)

        # Shared lookups for the community-flavored categories below.
        week_ago_dt = now - timedelta(days=WEEKLY_LOOKBACK_DAYS)
        # Not every `users` doc necessarily has a discord_id (e.g. a manually-
        # inserted or partially-cleaned-up record) -- indexing with a direct
        # u["discord_id"] blew up the whole weekly-spotlight computation with
        # a bare KeyError: 'discord_id' in production logs. Skip docs missing it.
        users_by_discord_id = {
            u["discord_id"]: u
            for u in self.col_config.database["users"].find({}, {"discord_id": 1, "cr_tag": 1})
            if u.get("discord_id")
        }

        def _profile_for_discord_id(discord_id):
            u = users_by_discord_id.get(discord_id)
            if not u or not u.get("cr_tag"):
                return None
            return self.col_profiles.find_one({"tag": u["cr_tag"]}, {"tag": 1, "name": 1})

        # 3. Most Helpful — most kudos RECEIVED this week.
        kudos_recent = list(self.col_config.database["kudos"].find({"created_at": {"$gte": week_ago_dt}}, {"to_tag": 1}))
        received_counts = {}
        for k in kudos_recent:
            to_tag = k.get("to_tag")
            if to_tag:
                received_counts[to_tag] = received_counts.get(to_tag, 0) + 1
        new_entry = None
        if received_counts:
            top_tag, count = max(received_counts.items(), key=lambda kv: kv[1])
            profile = self.col_profiles.find_one({"tag": top_tag}, {"name": 1}) or {}
            new_entry = self._lb_entry(top_tag, profile.get("name"), count, f"{count} kudos received this week")
        categories["most_helpful"] = self._merge_leaderboard_entry(existing_categories, "most_helpful", new_entry, now)

        # 4. Most Generous — most kudos GIVEN this week (kudos are logged by
        # Discord account, not CR tag, so this joins through `users` first).
        # kudos_recent above was projected to to_tag only, so re-query for from_discord_id.
        given_counts = {}
        kudos_recent_full = list(self.col_config.database["kudos"].find({"created_at": {"$gte": week_ago_dt}}, {"from_discord_id": 1}))
        for k in kudos_recent_full:
            did = k.get("from_discord_id")
            if did:
                given_counts[did] = given_counts.get(did, 0) + 1
        new_entry = None
        if given_counts:
            top_did, count = max(given_counts.items(), key=lambda kv: kv[1])
            profile = _profile_for_discord_id(top_did)
            if profile:
                new_entry = self._lb_entry(profile.get("tag"), profile.get("name"), count, f"{count} kudos given this week")
        categories["most_generous"] = self._merge_leaderboard_entry(existing_categories, "most_generous", new_entry, now)

        # 5. Most Improved — win rate this week vs. the week before, min
        # MIN_BATTLES_FOR_RATE_CATEGORY battles logged in BOTH windows.
        two_weeks_ago_str = (now - timedelta(days=WEEKLY_LOOKBACK_DAYS * 2)).strftime("%Y%m%d")
        week_ago_str = (now - timedelta(days=WEEKLY_LOOKBACK_DAYS)).strftime("%Y%m%d")
        prior_window = list(self.col_battles.find(
            {"battle_time": {"$gte": two_weeks_ago_str, "$lt": week_ago_str}}, {"player_tag": 1, "result": 1}
        ))
        recent_window = list(self.col_battles.find(
            {"battle_time": {"$gte": week_ago_str}}, {"player_tag": 1, "result": 1}
        ))
        def _wr_by_player(battles):
            agg = {}
            for b in battles:
                tag = b.get("player_tag")
                if not tag:
                    continue
                s = agg.setdefault(tag, {"games": 0, "wins": 0})
                s["games"] += 1
                if b.get("result") == "win":
                    s["wins"] += 1
            return agg
        prior_wr, recent_wr = _wr_by_player(prior_window), _wr_by_player(recent_window)
        best_improvement = None
        for tag, recent_s in recent_wr.items():
            prior_s = prior_wr.get(tag)
            if not prior_s or recent_s["games"] < MIN_BATTLES_FOR_RATE_CATEGORY or prior_s["games"] < MIN_BATTLES_FOR_RATE_CATEGORY:
                continue
            improvement = (recent_s["wins"] / recent_s["games"]) - (prior_s["wins"] / prior_s["games"])
            if improvement > 0 and (best_improvement is None or improvement > best_improvement[0]):
                best_improvement = (improvement, tag)
        new_entry = None
        if best_improvement:
            full_tag = f"#{best_improvement[1]}"
            profile = self.col_profiles.find_one({"tag": full_tag}, {"name": 1}) or {}
            new_entry = self._lb_entry(full_tag, profile.get("name"), best_improvement[0], f"win rate up {round(best_improvement[0]*100)} pts week-over-week")
        categories["most_improved"] = self._merge_leaderboard_entry(existing_categories, "most_improved", new_entry, now)

        # 6. Comeback Player — most wins that immediately followed a loss, this week.
        battles_by_player_chrono = {}
        for b in list(self.col_battles.find({"battle_time": {"$gte": week_ago_str}}, {"player_tag": 1, "result": 1, "battle_time": 1}).sort("battle_time", 1)):
            tag = b.get("player_tag")
            if tag:
                battles_by_player_chrono.setdefault(tag, []).append(b)
        best_comeback = None
        for tag, battles in battles_by_player_chrono.items():
            comebacks = sum(1 for i in range(1, len(battles)) if battles[i-1].get("result") == "loss" and battles[i].get("result") == "win")
            if comebacks > 0 and (best_comeback is None or comebacks > best_comeback[0]):
                best_comeback = (comebacks, tag)
        new_entry = None
        if best_comeback:
            full_tag = f"#{best_comeback[1]}"
            profile = self.col_profiles.find_one({"tag": full_tag}, {"name": 1}) or {}
            new_entry = self._lb_entry(full_tag, profile.get("name"), best_comeback[0], f"bounced back from a loss {best_comeback[0]}x this week")
        categories["comeback_player"] = self._merge_leaderboard_entry(existing_categories, "comeback_player", new_entry, now)

        # 7. Defensive Wall — most shutout wins (opponent 0 crowns) this week.
        shutouts_recent = list(self.col_battles.find(
            {"battle_time": {"$gte": week_ago_str}, "result": "win", "opponent_crowns": 0}, {"player_tag": 1}
        ))
        shutout_counts = {}
        for b in shutouts_recent:
            tag = b.get("player_tag")
            if tag:
                shutout_counts[tag] = shutout_counts.get(tag, 0) + 1
        new_entry = None
        if shutout_counts:
            top_tag, count = max(shutout_counts.items(), key=lambda kv: kv[1])
            full_tag = f"#{top_tag}"
            profile = self.col_profiles.find_one({"tag": full_tag}, {"name": 1}) or {}
            new_entry = self._lb_entry(full_tag, profile.get("name"), count, f"{count} shutout wins this week")
        categories["defensive_wall"] = self._merge_leaderboard_entry(existing_categories, "defensive_wall", new_entry, now)

        # 8. Sharpshooter — highest 3-crown rate this week, min MIN_BATTLES_FOR_RATE_CATEGORY games.
        # recent_window (from category 5 above) wasn't projected with team_crowns, so re-query.
        three_crown_stats = {}
        three_crown_battles = list(self.col_battles.find(
            {"battle_time": {"$gte": week_ago_str}}, {"player_tag": 1, "result": 1, "team_crowns": 1}
        ))
        for b in three_crown_battles:
            tag = b.get("player_tag")
            if not tag:
                continue
            s = three_crown_stats.setdefault(tag, {"games": 0, "three_crowns": 0})
            s["games"] += 1
            if b.get("result") == "win" and b.get("team_crowns", 0) == 3:
                s["three_crowns"] += 1
        best_rate = None
        for tag, s in three_crown_stats.items():
            if s["games"] < MIN_BATTLES_FOR_RATE_CATEGORY:
                continue
            rate = s["three_crowns"] / s["games"]
            if rate > 0 and (best_rate is None or rate > best_rate[0]):
                best_rate = (rate, tag, s["games"])
        new_entry = None
        if best_rate:
            full_tag = f"#{best_rate[1]}"
            profile = self.col_profiles.find_one({"tag": full_tag}, {"name": 1}) or {}
            new_entry = self._lb_entry(full_tag, profile.get("name"), best_rate[0], f"{round(best_rate[0]*100)}% three-crown rate over {best_rate[2]} games")
        categories["sharpshooter"] = self._merge_leaderboard_entry(existing_categories, "sharpshooter", new_entry, now)

        # 9. Community Voice — most comments posted (on others' profiles) this week.
        comments_recent = list(self.col_config.database["profile_comments"].find({"created_at": {"$gte": week_ago_dt}}, {"from_discord_id": 1}))
        comment_counts = {}
        for c in comments_recent:
            did = c.get("from_discord_id")
            if did:
                comment_counts[did] = comment_counts.get(did, 0) + 1
        new_entry = None
        if comment_counts:
            top_did, count = max(comment_counts.items(), key=lambda kv: kv[1])
            profile = _profile_for_discord_id(top_did)
            if profile:
                new_entry = self._lb_entry(profile.get("tag"), profile.get("name"), count, f"{count} comments posted this week")
        categories["community_voice"] = self._merge_leaderboard_entry(existing_categories, "community_voice", new_entry, now)

        # 10. Clan Points Leader — all-time clan_points total (war participation
        # currency, idea #104). A running total rather than a weekly delta
        # (no weekly clan_points snapshot exists to diff against), framed here
        # as ongoing recognition rather than "this week specifically".
        points_profiles = list(self.col_profiles.find({"clan_points": {"$gt": 0}}, {"tag": 1, "name": 1, "clan_points": 1}))
        new_entry = None
        if points_profiles:
            top = max(points_profiles, key=lambda p: p.get("clan_points", 0))
            new_entry = self._lb_entry(top.get("tag"), top.get("name"), top.get("clan_points", 0), f"{top.get('clan_points', 0):,} clan points")
        categories["clan_points_leader"] = self._merge_leaderboard_entry(existing_categories, "clan_points_leader", new_entry, now)

        doc = {"computed_at": now, "categories": {k: v for k, v in categories.items() if v}}
        self.col_config.update_one({"_id": "weekly_spotlights"}, {"$set": doc}, upsert=True)

    def compute_clan_legends(self):
        """Idea #110: an all-time 'legends' record book, separate from the
        current-week Hall of Fame already shown on the roster page. Deliberately
        avoids any per-member battle-log scan so this stays cheap to run every
        harvest cycle regardless of how much history has piled up — the
        "most war MVPs" record instead reuses the already-stored war_history
        race docs (capped at the last 50 races)."""
        profiles = list(self.col_profiles.find({}, {"tag": 1, "name": 1, "bestTrophies": 1, "donations": 1, "joined_clan_at": 1}))
        if not profiles:
            return
        trophy_legend   = max(profiles, key=lambda p: p.get("bestTrophies", 0))
        donation_legend = max(profiles, key=lambda p: p.get("donations", 0))
        veteran = min((p for p in profiles if p.get("joined_clan_at")), key=lambda p: p["joined_clan_at"], default=None)

        races = list(self.col_war_history.find({}, {"data.clan.participants": 1}).sort("data.seasonId", -1).limit(50))
        mvp_counts = {}
        for race in races:
            participants = ((race.get("data", {}).get("clan") or {}).get("participants")) or []
            if not participants:
                continue
            top = max(participants, key=lambda p: p.get("fame", 0))
            key = (top.get("tag"), top.get("name"))
            mvp_counts[key] = mvp_counts.get(key, 0) + 1
        most_mvp = max(mvp_counts.items(), key=lambda kv: kv[1], default=((None, None), 0))

        doc = {
            "computed_at": datetime.now(timezone.utc),
            "highest_trophies_ever": {"tag": trophy_legend.get("tag"), "name": trophy_legend.get("name"), "value": trophy_legend.get("bestTrophies", 0)},
            # NOTE: "donations" is the CR API's current-season counter, not a true
            # lifetime total (the API doesn't expose lifetime donations) — this is
            # a documented approximation, same pattern as the war-timing approximations.
            "top_season_donator": {"tag": donation_legend.get("tag"), "name": donation_legend.get("name"), "value": donation_legend.get("donations", 0)},
            "most_war_mvps": {"tag": most_mvp[0][0], "name": most_mvp[0][1], "count": most_mvp[1]} if most_mvp[0][0] else None,
            "clan_veteran": {"tag": veteran.get("tag"), "name": veteran.get("name"), "since": veteran.get("joined_clan_at")} if veteran else None,
        }
        self.col_config.update_one({"_id": "clan_legends"}, {"$set": doc}, upsert=True)

    def assign_weekly_rivalries(self):
        """Idea #115: pair up similar-trophy members for a fun head-to-head
        tracked rivalry, re-shuffled roughly weekly. Head-to-head records are
        computed on read (from battle_history opponent_tag matches) rather than
        stored here, since re-pairing only needs to decide who's paired with whom."""
        meta = self.col_config.find_one({"_id": "rivalries_meta"}) or {}
        last_at = _as_aware_utc(meta.get("last_assigned_at"))
        if last_at and (datetime.now(timezone.utc) - last_at) < timedelta(days=RIVALRY_REASSIGN_DAYS):
            return
        active = list(self.col_profiles.find(
            {"left_clan_at": {"$exists": False}}, {"tag": 1, "name": 1, "trophies": 1}
        ).sort("trophies", 1))
        db = self.col_config.database
        db["rivalries"].update_many({"active": True}, {"$set": {"active": False}})
        pool = active[:]
        while len(pool) >= 2:
            a = pool.pop(0)
            b = min(pool, key=lambda m: abs(m.get("trophies", 0) - a.get("trophies", 0)))
            pool.remove(b)
            db["rivalries"].insert_one({
                "tag_a": a["tag"], "name_a": a.get("name"),
                "tag_b": b["tag"], "name_b": b.get("name"),
                "assigned_at": datetime.now(timezone.utc), "active": True,
            })
        self.col_config.update_one({"_id": "rivalries_meta"}, {"$set": {"last_assigned_at": datetime.now(timezone.utc)}}, upsert=True)

    def check_first_week_checkins(self):
        """Idea #123: a friendly automated DM about a week after a member joins,
        instead of pure silence until their first war judgment. One-shot per
        member via the `checkin_sent` flag — never re-sent even if this runs
        every 30 minutes for days within the window."""
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=FIRST_WEEK_CHECKIN_MAX_DAYS)
        window_end = now - timedelta(days=FIRST_WEEK_CHECKIN_MIN_DAYS)
        candidates = list(self.col_profiles.find(
            {
                "joined_clan_at": {"$gte": window_start, "$lte": window_end},
                "checkin_sent": {"$ne": True},
                "left_clan_at": {"$exists": False},
            },
            {"tag": 1, "name": 1},
        ))
        if not candidates:
            return
        db = self.col_config.database
        for c in candidates:
            user = db["users"].find_one({"cr_tag": c["tag"]})
            if user and user.get("discord_id"):
                db["pending_actions"].insert_one({
                    "kind": "first_week_checkin",
                    "discord_id": user["discord_id"],
                    "message": (
                        f"👋 Hey {c.get('name', 'there')}! You've been in Graveyard Squad about a week now — "
                        "how's it going? If you have any questions about war expectations, donations, or "
                        "anything else, just ask in the server. Glad to have you here!"
                    ),
                    "created_at": now,
                    "processed": False,
                })
            self.col_profiles.update_one({"tag": c["tag"]}, {"$set": {"checkin_sent": True}})

    def auto_decline_stale_applications(self):
        """Idea #214 — per your note, off by default: only runs when an admin
        has explicitly turned on `bot_settings.auto_decline_stale_applications_enabled`.
        Applications (from the /apply form, idea #212) still sitting at
        "pending" after `auto_decline_days` get auto-marked declined, to keep
        the recruiting pipeline from accumulating dead entries nobody ever
        responded to."""
        db = self.col_config.database
        settings = self.col_config.find_one({"_id": "bot_settings"}) or {}
        if not settings.get("auto_decline_stale_applications_enabled"):
            return
        days = int(settings.get("auto_decline_days", 14) or 14)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        result = db["applications"].update_many(
            {"status": "pending", "created_at": {"$lte": cutoff}},
            {"$set": {"status": "declined", "auto_declined": True}},
        )
        if result.modified_count:
            log.info(f"[HARVESTER] Auto-declined {result.modified_count} stale application(s) older than {days} days.")

    def check_streaming_status(self):
        """Idea #243 — per your note, hidden behind
        `bot_settings.streaming_integration_enabled` (the toggle itself was
        added to the Settings UI in section 12's batch; this is the actual
        plumbing behind it). Polls Twitch/YouTube for a configured channel
        going live and, on the not-live -> live transition, queues a
        "stream_live_post" pending_action for clash_cog.py to announce
        (added to its generic announcements-channel bucket alongside
        role_change_post/milestone_post/etc.).

        This is a real implementation, not just a TODO stub, but it's a NO-OP
        until real API credentials exist: no Twitch/YouTube developer account
        has been set up for this clan, so TWITCH_CLIENT_ID/TWITCH_CLIENT_SECRET
        and YOUTUBE_API_KEY are unset in this environment. Rather than crash or
        silently do nothing forever, it logs a one-time "not configured"
        notice (rate-limited via a config marker so it doesn't spam every
        30-minute cycle) so whoever eventually sets up those credentials knows
        exactly which env vars to fill in.
        """
        settings = self.col_config.find_one({"_id": "bot_settings"}) or {}
        if not settings.get("streaming_integration_enabled"):
            return
        twitch_channel = settings.get("streaming_twitch_channel")
        youtube_channel = settings.get("streaming_youtube_channel")
        if not twitch_channel and not youtube_channel:
            return  # toggle is on but no channel configured yet — nothing to poll

        twitch_client_id = os.getenv("TWITCH_CLIENT_ID")
        twitch_client_secret = os.getenv("TWITCH_CLIENT_SECRET")
        youtube_api_key = os.getenv("YOUTUBE_API_KEY")
        if not (twitch_client_id and twitch_client_secret) and not youtube_api_key:
            self._log_once_per_day("streaming_creds_missing",
                "streaming_integration_enabled is on but no TWITCH_CLIENT_ID/"
                "TWITCH_CLIENT_SECRET or YOUTUBE_API_KEY is set — nothing to poll yet.")
            return

        status_doc = self.col_config.find_one({"_id": "streaming_status"}) or {}
        was_live = status_doc.get("is_live", False)
        now_live = False
        live_url = None

        if twitch_channel and twitch_client_id and twitch_client_secret:
            try:
                token_resp = requests.post(
                    "https://id.twitch.tv/oauth2/token",
                    params={"client_id": twitch_client_id, "client_secret": twitch_client_secret, "grant_type": "client_credentials"},
                    timeout=8,
                )
                app_token = token_resp.json().get("access_token") if token_resp.status_code == 200 else None
                if app_token:
                    streams_resp = requests.get(
                        "https://api.twitch.tv/helix/streams",
                        headers={"Client-Id": twitch_client_id, "Authorization": f"Bearer {app_token}"},
                        params={"user_login": twitch_channel}, timeout=8,
                    )
                    if streams_resp.status_code == 200 and streams_resp.json().get("data"):
                        now_live = True
                        live_url = f"https://twitch.tv/{twitch_channel}"
            except Exception as e:
                log.warning(f"Twitch live-status check failed (non-fatal): {e}")

        if not now_live and youtube_channel and youtube_api_key:
            try:
                resp = requests.get(
                    "https://www.googleapis.com/youtube/v3/search",
                    params={"part": "snippet", "channelId": youtube_channel, "eventType": "live", "type": "video", "key": youtube_api_key},
                    timeout=8,
                )
                items = resp.json().get("items", []) if resp.status_code == 200 else []
                if items:
                    now_live = True
                    live_url = f"https://youtube.com/watch?v={items[0]['id']['videoId']}"
            except Exception as e:
                log.warning(f"YouTube live-status check failed (non-fatal): {e}")

        self.col_config.update_one(
            {"_id": "streaming_status"},
            {"$set": {"is_live": now_live, "checked_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        if now_live and not was_live:
            self.col_config.database["pending_actions"].insert_one({
                "kind": "stream_live_post",
                "message": f"🔴 **Going live now!** {live_url or 'Check the clan Discord for details.'}",
                "created_at": datetime.now(timezone.utc),
                "processed": False,
            })

    def post_recruitment_to_reddit(self):
        """Idea #247 — per your note, hidden behind
        `bot_settings.reddit_autopost_enabled`. Only worth posting when the
        clan is actually under-strength (reuses the same <50-member condition
        as the recruiting banner, idea #215), and rate-limited to at most once
        every 7 days via a config marker so it can never spam a subreddit.

        Same "real plumbing, but a no-op without credentials" situation as
        check_streaming_status() above: submitting a Reddit post requires a
        registered Reddit "script" app (REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET)
        plus a REDDIT_REFRESH_TOKEN for the posting account, none of which
        exist in this environment yet. Logs a one-time notice instead of
        either crashing or posting nothing with no explanation.
        """
        settings = self.col_config.find_one({"_id": "bot_settings"}) or {}
        if not settings.get("reddit_autopost_enabled"):
            return
        subreddit = settings.get("reddit_subreddit")
        if not subreddit:
            return

        member_count = self.col_profiles.count_documents({"left_clan_at": {"$exists": False}})
        if member_count >= 50:
            return  # clan isn't under-strength right now — nothing to recruit for

        marker = self.col_config.find_one({"_id": "reddit_autopost_meta"}) or {}
        last_posted = marker.get("last_posted_at")
        if last_posted and (datetime.now(timezone.utc) - last_posted) < timedelta(days=7):
            return

        client_id = os.getenv("REDDIT_CLIENT_ID")
        client_secret = os.getenv("REDDIT_CLIENT_SECRET")
        refresh_token = os.getenv("REDDIT_REFRESH_TOKEN")
        if not (client_id and client_secret and refresh_token):
            self._log_once_per_day("reddit_creds_missing",
                "reddit_autopost_enabled is on but REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET/"
                "REDDIT_REFRESH_TOKEN aren't all set — nothing to post with yet.")
            return

        try:
            token_resp = requests.post(
                "https://www.reddit.com/api/v1/access_token",
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                auth=(client_id, client_secret),
                headers={"User-Agent": "GraveyardSquadBot/1.0"},
                timeout=8,
            )
            access_token = token_resp.json().get("access_token") if token_resp.status_code == 200 else None
            if not access_token:
                log.warning("Reddit auto-post: failed to obtain access token (non-fatal).")
                return
            title = f"[Recruiting] Graveyard Squad — active Clash Royale clan looking for members ({member_count}/50)"
            body = "We're a bit under-strength right now and looking for active war participants. Reply or DM if interested!"
            submit_resp = requests.post(
                "https://oauth.reddit.com/api/submit",
                data={"sr": subreddit, "kind": "self", "title": title, "text": body},
                headers={"Authorization": f"Bearer {access_token}", "User-Agent": "GraveyardSquadBot/1.0"},
                timeout=8,
            )
            if submit_resp.status_code == 200:
                self.col_config.update_one(
                    {"_id": "reddit_autopost_meta"},
                    {"$set": {"last_posted_at": datetime.now(timezone.utc)}},
                    upsert=True,
                )
                log.info(f"[HARVESTER] Posted recruitment thread to r/{subreddit}.")
            else:
                log.warning(f"Reddit auto-post submit failed (non-fatal): HTTP {submit_resp.status_code}")
        except Exception as e:
            log.warning(f"Reddit auto-post failed (non-fatal): {e}")

    def _log_once_per_day(self, marker_key: str, message: str):
        """Shared helper for the two credential-missing notices above — logs at
        most once per 24h per marker_key instead of every single harvest cycle."""
        marker_id = f"log_once_{marker_key}"
        doc = self.col_config.find_one({"_id": marker_id}) or {}
        last = doc.get("last_logged_at")
        if last and (datetime.now(timezone.utc) - last) < timedelta(hours=24):
            return
        log.info(f"[HARVESTER] {message}")
        self.col_config.update_one({"_id": marker_id}, {"$set": {"last_logged_at": datetime.now(timezone.utc)}}, upsert=True)

    def purge_expired_departed_members(self):
        """Idea #17 (revised): a member who has left keeps their historical stats
        queryable for DEPARTED_MEMBER_RETENTION_WEEKS, then gets cleaned up so the
        DB doesn't grow forever with dead tags. Only ever deletes profiles that
        were explicitly marked `left_clan_at` by _track_departures below — current
        members are never touched by this method.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(weeks=DEPARTED_MEMBER_RETENTION_WEEKS)
        expired = list(self.col_profiles.find(
            {"left_clan_at": {"$lte": cutoff}}, {"tag": 1}
        ))
        if not expired:
            return
        tags = [p["tag"].replace("#", "") for p in expired]
        self.col_profiles.delete_many({"tag": {"$in": [p["tag"] for p in expired]}})
        self.col_player_snapshots.delete_many({"tag": {"$in": tags}})
        self.col_battles.delete_many({"player_tag": {"$in": tags}})
        log.info(f"Purged {len(tags)} departed member(s) past the {DEPARTED_MEMBER_RETENTION_WEEKS}-week retention window.")

    def _track_departures(self, current_member_tags: list):
        """Idea #17 (revised): diff the live clan roster against everyone we have a
        non-departed profile for for. Anyone who's no longer in the clan gets
        `left_clan_at` stamped once (idempotent — never overwritten on subsequent
        runs), starting their retention countdown. Rejoining before the countdown
        expires clears the flag so they're treated as a continuously-tracked member.
        """
        current_set = set(current_member_tags)
        known_active = self.col_profiles.find(
            {"left_clan_at": {"$exists": False}}, {"tag": 1}
        )
        for doc in known_active:
            tag = doc["tag"].replace("#", "")
            if tag not in current_set:
                self.col_profiles.update_one(
                    {"tag": doc["tag"]},
                    {"$set": {"left_clan_at": datetime.now(timezone.utc)}}
                )
        # Rejoiners: clear the flag so they're not silently purged mid-membership.
        self.col_profiles.update_many(
            {"tag": {"$in": [f"#{t}" for t in current_member_tags]}},
            {"$unset": {"left_clan_at": ""}}
        )

    def harvest_clan_and_profiles(self):
        clan_data = self.fetch_api(f"clans/%23{self.clan_tag}")
        if not clan_data: return []

        # Idea #232: pre-warm the web process's Redis cache (idea #231) with
        # the clan data we just fetched anyway, so the first visitor to hit
        # the roster page right after a harvest cycle gets an instant cache
        # hit instead of paying live-fetch latency themselves.
        if self.redis_client:
            try:
                self.redis_client.setex(
                    _cr_api_cache_key(f"clans/%23{self.clan_tag}"),
                    CR_API_CACHE_TTL_SECONDS,
                    json.dumps(clan_data),
                )
            except Exception as e:
                log.warning(f"Cache pre-warm failed (non-fatal): {e}")

        self.col_snapshots.insert_one({
            "timestamp": datetime.now(timezone.utc),
            "tag": clan_data.get("tag"),
            "name": clan_data.get("name"),
            "clanScore": clan_data.get("clanScore"),
            "memberCount": clan_data.get("members")
        })

        member_tags = [m["tag"].replace("#", "") for m in clan_data.get("memberList", [])]
        self._track_departures(member_tags)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        profile_operations = []
        snapshot_operations = []
        deck_snapshot_operations = []
        for tag in member_tags:
            profile_data = self.fetch_api(f"players/%23{tag}")
            if profile_data:
                # Idea #39: log role changes (promotion/demotion) into the same
                # audit_log array player.html's "Clan History Log" already reads,
                # so leadership changes show up next to admin actions instead of
                # being invisible. Compares against whatever role we last stored.
                prior = self.col_profiles.find_one(
                    {"tag": profile_data["tag"]},
                    {"role": 1, "wins": 1, "bestTrophies": 1, "donations": 1, "donationsReceived": 1,
                     "lifetime_donations_banked": 1, "lifetime_donations_received_banked": 1},
                )
                new_role = profile_data.get("role")
                if prior and prior.get("role") and new_role and prior["role"] != new_role:
                    self.col_profiles.update_one(
                        {"tag": profile_data["tag"]},
                        {"$push": {"audit_log": {
                            "date": today,
                            "action": f"Role changed: {prior['role']} → {new_role}",
                        }},
                        # Rec #3: officer/co-leader term tracking. role_since
                        # resets every time the role actually changes, so
                        # "how long has this person been Co-Leader" is a
                        # simple date-diff against this field rather than
                        # scanning the whole audit_log every time it's asked.
                        "$set": {"role_since": today}}
                    )
                    # Idea #111: an actual announcement instead of a silent DB update.
                    self.col_config.database["pending_actions"].insert_one({
                        "kind": "role_change_post",
                        "message": f"🔔 **{profile_data.get('name', 'A member')}** is now **{new_role.capitalize()}** (was {prior['role'].capitalize()}).",
                        "created_at": datetime.now(timezone.utc),
                        "processed": False,
                    })

                # Idea #105: round-number milestone celebrations.
                for msg in self._check_milestones(prior, profile_data):
                    self.col_config.database["pending_actions"].insert_one({
                        "kind": "milestone_post", "message": msg,
                        "created_at": datetime.now(timezone.utc), "processed": False,
                    })

                # Task #143: "Total Donations (All-Time)" has always read a totalDonations
                # field that nothing ever populated -- the CR API has no lifetime-donations
                # concept at all, only a season-scoped counter that resets to 0 at every
                # season boundary. donations/donationsReceived can only ever go DOWN
                # within a season if a reset just happened, so that's a reliable signal
                # to bank the last-known pre-reset value into a running total. This lets
                # the dashboard show a real (bot-tracked-history) lifetime figure as
                # banked + current, instead of a phantom stat that always reads 0.
                donation_bank_updates = {}
                if prior:
                    old_donations = prior.get("donations", 0) or 0
                    new_donations = profile_data.get("donations", 0) or 0
                    if new_donations < old_donations:
                        donation_bank_updates["lifetime_donations_banked"] = (
                            (prior.get("lifetime_donations_banked", 0) or 0) + old_donations
                        )
                    old_received = prior.get("donationsReceived", 0) or 0
                    new_received = profile_data.get("donationsReceived", 0) or 0
                    if new_received < old_received:
                        donation_bank_updates["lifetime_donations_received_banked"] = (
                            (prior.get("lifetime_donations_received_banked", 0) or 0) + old_received
                        )

                profile_operations.append(UpdateOne(
                    {"tag": profile_data["tag"]},
                    {
                        "$set": {**profile_data, "last_updated": datetime.now(timezone.utc), **donation_bank_updates},
                        # First time we see this tag: stamp when we started tracking them
                        # (idea #107's rising-star window) and seed the gamification fields
                        # (ideas #103/#104) so later $inc/$set calls never hit a missing field.
                        "$setOnInsert": {
                            "joined_clan_at": datetime.now(timezone.utc),
                            "clan_points": 0,
                            "war_participation_streak": 0,
                            "streak_shields": 1,
                            "lifetime_donations_banked": 0,
                            "lifetime_donations_received_banked": 0,
                            # Rec #3: seeds role_since for a brand-new profile so it's
                            # never missing -- the role-change block elsewhere in this
                            # loop overwrites it going forward whenever the role
                            # actually changes.
                            "role_since": today,
                        },
                    },
                    upsert=True
                ))
                # One row per player per calendar day — later harvests the same day just
                # refresh the same doc, so this stays cheap and idempotent.
                snapshot_operations.append(UpdateOne(
                    {"tag": profile_data["tag"], "date": today},
                    {"$set": {
                        "tag": profile_data["tag"],
                        "name": profile_data.get("name"),
                        "date": today,
                        "trophies": profile_data.get("trophies", 0),
                        "bestTrophies": profile_data.get("bestTrophies", 0),
                        "donations": profile_data.get("donations", 0),
                        "donationsReceived": profile_data.get("donationsReceived", 0),
                        "clanRank": profile_data.get("clanRank"),
                        "role": profile_data.get("role"),
                        "expLevel": profile_data.get("expLevel"),
                        "wins": profile_data.get("wins", 0),
                        "losses": profile_data.get("losses", 0),
                        "threeCrownWins": profile_data.get("threeCrownWins", 0),
                        "recorded_at": datetime.now(timezone.utc),
                    }},
                    upsert=True
                ))

                # Task #151: record this member's equipped deck once per calendar
                # day (same one-row-per-day cadence as player_snapshots above) so
                # deck composition can be analyzed over time. currentDeck cards
                # keep their level/evolutionLevel so "was this an evo deck"
                # analytics stay possible later, not just card names.
                current_deck = profile_data.get("currentDeck") or []
                if current_deck:
                    deck_snapshot_operations.append(UpdateOne(
                        {"tag": profile_data["tag"], "date": today},
                        {"$set": {
                            "tag": profile_data["tag"],
                            "name": profile_data.get("name"),
                            "date": today,
                            "deck": [
                                {
                                    "name": c.get("name"),
                                    "level": c.get("level"),
                                    "maxLevel": c.get("maxLevel"),
                                    "evolutionLevel": c.get("evolutionLevel", 0),
                                    "rarity": c.get("rarity"),
                                    "elixirCost": c.get("elixirCost"),
                                }
                                for c in current_deck if isinstance(c, dict)
                            ],
                            "support_cards": [
                                c.get("name") for c in (profile_data.get("currentDeckSupportCards") or []) if isinstance(c, dict)
                            ],
                            "favourite_card": (profile_data.get("currentFavouriteCard") or {}).get("name"),
                            "recorded_at": datetime.now(timezone.utc),
                        }},
                        upsert=True
                    ))
            time.sleep(0.05)

        if profile_operations:
            self.col_profiles.bulk_write(profile_operations)
            self._harvest_meta["profiles_saved"] += len(profile_operations)

        if snapshot_operations:
            self.col_player_snapshots.bulk_write(snapshot_operations)

        if deck_snapshot_operations:
            self.col_deck_snapshots.bulk_write(deck_snapshot_operations)

        return member_tags

    def _check_milestones(self, prior: dict | None, profile_data: dict) -> list[str]:
        """Idea #105: detect a round-number crossing (e.g. 500 lifetime wins,
        6000 personal-best trophies) between the last-harvested value and this
        one. Returns zero or more ready-to-post celebration messages."""
        if not prior:
            return []
        name = profile_data.get("name", "A member")
        msgs = []
        for field, step, label in (
            ("wins", MILESTONE_WIN_STEP, "lifetime wins"),
            ("bestTrophies", MILESTONE_TROPHY_STEP, "personal best trophies"),
        ):
            old_v = prior.get(field, 0) or 0
            new_v = profile_data.get(field, 0) or 0
            if new_v > 0 and new_v // step > old_v // step:
                crossed = (new_v // step) * step
                msgs.append(f"🎉 **{name}** just crossed **{crossed:,} {label}**!")
        return msgs

    def _cache_card_icons(self, cards):
        """Upsert name->icon-URL pairs into the card_icons collection.

        `cards` is a list of raw CR API card objects (as returned in a
        battlelog's team/opponent "cards" array), i.e. still shaped like
        {"name": ..., "iconUrls": {"medium": "https://..."}, "level": ...}
        -- the *original* shape, before harvest_battles() flattens it down
        to plain name strings for storage in team_cards/opponent_cards.

        This is intentionally a separate, additive, name-keyed collection
        rather than storing the icon URL on the battle record itself, so
        that team_cards/opponent_cards can stay plain string lists (see the
        comment at the call site in harvest_battles()). Safe to call
        repeatedly with overlapping data -- it's just upserts keyed by name.
        """
        if not cards:
            return
        ops = []
        for c in cards:
            if not isinstance(c, dict):
                continue
            name = c.get("name")
            icon_url = (c.get("iconUrls") or {}).get("medium")
            if not name or not icon_url:
                continue
            ops.append(UpdateOne(
                {"_id": name},
                {"$set": {"_id": name, "icon_url": icon_url}},
                upsert=True
            ))
        if ops:
            try:
                self.db["card_icons"].bulk_write(ops, ordered=False)
            except Exception:
                log.exception("Failed to cache card icon URLs (non-fatal)")

    def harvest_battles(self, member_tags):
        battle_operations = []
        for tag in member_tags:
            battles = self.fetch_api(f"players/%23{tag}/battlelog")
            if not battles: continue

            for b in battles:
                unique_id = f"{tag}_{b.get('battleTime', '')}"
                b["player_tag"] = tag
                b["unique_battle_id"] = unique_id

                # The CR API nests everything under team[0]/opponent[0]; flatten the
                # fields the dashboard actually reads so they aren't silently empty.
                team = (b.get("team") or [{}])[0]
                opponent = (b.get("opponent") or [{}])[0]
                team_crowns = team.get("crowns", 0)
                opp_crowns = opponent.get("crowns", 0)
                all_cards_this_battle = list(team.get("cards") or []) + list(opponent.get("cards") or [])
                # BUGFIX (card images never showing up on the player page's
                # battle log): team_cards/opponent_cards have always been
                # flattened to plain name strings below (needed — storing raw
                # card objects here is what caused the earlier "unhashable
                # dict"/"'<' not supported" crashes across every analytics
                # route once mixed with legacy data). But that meant the
                # iconUrls the CR API actually returns per card were being
                # discarded entirely, so templates/player.html's card-chip
                # renderer (which expects an icon URL) never had one to show
                # and silently fell back to a placeholder for every single
                # card, on every battle, always. Rather than putting rich
                # objects back into team_cards/opponent_cards (which would
                # reintroduce that exact crash class), cache name->icon-URL
                # pairs in a small separate collection as they're seen — see
                # _cache_card_icons() below — and serve that as its own
                # lookup (GET /api/cards/icons in web_routes.py) that the
                # frontend joins against by card name instead.
                self._cache_card_icons(all_cards_this_battle)
                b["team_cards"] = [c.get("name", "") for c in (team.get("cards") or [])]
                b["opponent_cards"] = [c.get("name", "") for c in (opponent.get("cards") or [])]
                b["team_crowns"] = team_crowns
                b["opponent_crowns"] = opp_crowns
                b["opponent_name"] = opponent.get("name", "")
                b["opponent_tag"] = (opponent.get("tag") or "").replace("#", "")
                b["battle_type"] = b.get("type", "")
                b["result"] = "win" if team_crowns > opp_crowns else ("loss" if team_crowns < opp_crowns else "draw")
                # BUGFIX (found while building section 14's archetype-trend feature,
                # which sorts/filters on battle_time): the CR API's raw field is
                # camelCase `battleTime`, and this function never renamed it to the
                # snake_case `battle_time` every query elsewhere in web_routes.py,
                # data_harvester.py, and clash_cog.py actually reads/sorts/filters on.
                # That means every battle_time-based feature built across this whole
                # project — trophy history, activity heatmap, war-participation streak
                # math, the "active in last 24h" indicator, battle log ordering, and
                # now section 14's trend/diversity calculations — has been silently
                # querying a field that was never populated on real harvested battles.
                # Fixing it here for all newly-harvested battles; see
                # backfill_missing_battle_time() below for already-stored documents.
                b["battle_time"] = b.get("battleTime", "")

                battle_operations.append(UpdateOne(
                    {"unique_battle_id": unique_id},
                    {"$set": b},
                    upsert=True
                ))
            time.sleep(0.05)

        if battle_operations:
            result = self.col_battles.bulk_write(battle_operations)
            self._harvest_meta["battles_saved"] += result.upserted_count
            log.info(f"Battle Catch-Up: {result.upserted_count} new battles found.")

    def backfill_missing_battle_time(self):
        """One-time (well, idempotent-forever) self-healing migration for the
        battle_time bug documented above in harvest_battles(): any
        battle_history document already stored before that fix went in has
        `battleTime` but not `battle_time`. Rather than requiring you to run a
        manual Mongo command, this runs once per harvest cycle, is cheap once
        caught up (finds zero matching documents and does nothing), and
        copies the value across for anything still missing it."""
        to_fix = list(self.col_battles.find(
            {"battle_time": {"$exists": False}, "battleTime": {"$exists": True}},
            {"_id": 1, "battleTime": 1},
        ).limit(2000))  # bounded per cycle so one huge backlog can't stall a harvest run
        if not to_fix:
            return
        ops = [UpdateOne({"_id": d["_id"]}, {"$set": {"battle_time": d.get("battleTime", "")}}) for d in to_fix]
        result = self.col_battles.bulk_write(ops)
        log.info(f"[HARVESTER] Backfilled battle_time on {result.modified_count} previously-stored battle(s).")

    def _day_progress_fraction(self, war_reset_hour_utc: int = 10) -> float:
        """Approximate how far into the current war day we are, as a 0-1 fraction.
        The CR API doesn't expose an exact "day started at" timestamp, so this
        assumes a fixed daily reset hour (configurable via bot_settings.war_reset_hour_utc,
        default 10 UTC) — documented approximation, not an exact read from the API.
        """
        now = datetime.now(timezone.utc)
        elapsed_minutes = (now.hour * 60 + now.minute) - (war_reset_hour_utc * 60)
        elapsed_minutes %= 24 * 60
        return elapsed_minutes / (24 * 60)

    def check_tiered_war_reminders(self, war_data: dict):
        """Idea #13: a soft nudge once the war day is ~50% gone, a firmer one at
        ~90%, instead of a single flat "DM all slackers" button. Queues into the
        same pending_actions collection the manual nudge button already uses;
        clash_cog.py's consumer loop is extended to handle these new kinds.
        De-dupes per calendar day + tier via a small marker doc in `config` so a
        30-minute harvest cadence doesn't re-queue the same tier repeatedly.
        """
        if not war_data or war_data.get("periodType") != "warDay":
            return
        bot_settings = self.col_config.find_one({"_id": "bot_settings"}) or {}
        if not bot_settings.get("feature_auto_pings", False):
            return
        reset_hour = int(bot_settings.get("war_reset_hour_utc", 10))
        fraction = self._day_progress_fraction(reset_hour)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        participants = (war_data.get("clan") or {}).get("participants", [])

        def already_sent(tier):
            marker_id = f"war_nudge_tier_{tier}_{today}"
            existing = self.col_config.find_one({"_id": marker_id})
            if existing:
                return True
            self.col_config.update_one({"_id": marker_id}, {"$set": {"sent_at": datetime.now(timezone.utc)}}, upsert=True)
            return False

        # Idea #131 (third tier: leadership escalation) + #139 (configurable
        # escalation path): anyone still at 0 decks once the war day is almost
        # over has, by construction, already been through both the soft (50%)
        # and firm (90%) nudge tiers above — that's the "ignored 3 nudges"
        # signal, without needing separate per-nudge-ignored counters.
        if fraction >= 0.98:
            ignored_everything = [p for p in participants if p.get("decksUsedToday", 0) == 0]
            if ignored_everything and not already_sent(98):
                self._queue_leadership_escalation(ignored_everything)

        if fraction >= 0.9:
            behind = [p for p in participants if p.get("decksUsedToday", 0) == 0]
            if behind and not already_sent(90):
                self._queue_tiered_nudge(behind, tier="firm")
        elif fraction >= 0.5:
            behind = [p for p in participants if p.get("decksUsedToday", 0) < 4]
            if behind and not already_sent(50):
                self._queue_tiered_nudge(behind, tier="soft")

    def _eligible_discord_ids(self, tags: list, dm_kind_for_dedupe: str, dedupe_window_minutes: int = 90) -> list:
        """Shared filter used by every DM-queuing path below:
        - Idea #132: skip anyone who's opted out via users.notif_prefs.war_reminders.
        - Idea #143: skip anyone who already has an unprocessed or very-recently-
          queued DM of the same kind, so a 30-min harvest cadence can't double-DM
          someone inside a short window.
        """
        db = self.col_config.database
        users = list(db["users"].find({"cr_tag": {"$in": tags}}))
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=dedupe_window_minutes)
        eligible = []
        for u in users:
            discord_id = u.get("discord_id")
            if not discord_id:
                continue
            if (u.get("notif_prefs") or {}).get("war_reminders", True) is False:
                continue
            snoozed_until = _as_aware_utc(u.get("snoozed_until"))
            if snoozed_until and snoozed_until > datetime.now(timezone.utc):
                continue  # idea #141: member snoozed reminders — skip until it expires
            recent = db["pending_actions"].find_one({
                "discord_id": discord_id, "kind": dm_kind_for_dedupe,
                "$or": [{"processed": False}, {"created_at": {"$gte": cutoff}}],
            })
            if recent:
                continue
            eligible.append(discord_id)
        return eligible

    def _queue_tiered_nudge(self, behind_participants: list, tier: str):
        tags = [p.get("tag") for p in behind_participants if p.get("tag")]
        discord_ids = self._eligible_discord_ids(tags, "war_nudge_tier")
        if not discord_ids:
            return
        self.col_config.database["pending_actions"].insert_one({
            "kind": "war_nudge_tier",
            "tier": tier,
            "discord_ids": discord_ids,
            "created_at": datetime.now(timezone.utc),
            "processed": False,
        })

    def _queue_leadership_escalation(self, ignored_participants: list):
        """Idea #131/#139: notify leadership (not the member) once someone has
        blown through every nudge tier for the day, instead of requiring a
        leader to notice the slacker list manually."""
        names = ", ".join(p.get("name", "?") for p in ignored_participants[:10])
        self.col_config.database["pending_actions"].insert_one({
            "kind": "leadership_escalation",
            "message": f"🚨 **Escalation:** the following member(s) used 0 war decks all day despite reminders: {names}",
            "created_at": datetime.now(timezone.utc),
            "processed": False,
        })

    def check_meta_shifts(self):
        """Idea #218: notify leadership when a previously-dominant archetype's
        win rate drops sharply week over week — signaling a balance patch or
        meta shift worth discussing, instead of leadership having to notice it
        themselves in the Analytics tab. De-duped per ISO week, same pattern
        as send_weekly_digest, so this only ever fires once a week even
        though the harvest loop runs every 30 minutes."""
        now = datetime.now(timezone.utc)
        iso_year, iso_week, _ = now.isocalendar()
        marker_id = f"meta_shift_check_{iso_year}_{iso_week}"
        if self.col_config.find_one({"_id": marker_id}):
            return
        self.col_config.update_one({"_id": marker_id}, {"$set": {"checked_at": now}}, upsert=True)

        week_ago = (now - timedelta(days=7)).strftime("%Y%m%dT%H%M%S")
        two_weeks_ago = (now - timedelta(days=14)).strftime("%Y%m%dT%H%M%S")

        def _bucket(start, end):
            battles = list(self.col_battles.find(
                {"battle_time": {"$gte": start, "$lt": end}},
                {"team_cards": 1, "result": 1},
            ))
            archetypes = {}
            for b in battles:
                result = b.get("result")
                if result not in ("win", "loss"):
                    continue
                cards = [c for c in (b.get("team_cards") or []) if c]
                if len(cards) < 8:
                    continue
                sig = tuple(sorted(cards[:8]))
                entry = archetypes.setdefault(sig, {"wins": 0, "games": 0})
                entry["games"] += 1
                if result == "win":
                    entry["wins"] += 1
            return {sig: v for sig, v in archetypes.items() if v["games"] >= 5}

        this_week = _bucket(week_ago, now.strftime("%Y%m%dT%H%M%S"))
        last_week = _bucket(two_weeks_ago, week_ago)

        shifts = []
        for sig, prev in last_week.items():
            prev_wr = prev["wins"] / prev["games"] * 100
            if prev_wr < 55:
                continue  # only flag drops from previously-strong (dominant) decks
            cur = this_week.get(sig)
            if not cur:
                continue
            cur_wr = cur["wins"] / cur["games"] * 100
            if prev_wr - cur_wr >= 15:  # a 15+ point win-rate drop is a real signal, not noise
                shifts.append((sig, prev_wr, cur_wr))

        if not shifts:
            return
        lines = ["📉 **Meta Shift Alert** — the following previously-strong decks dropped sharply this week:"]
        for sig, prev_wr, cur_wr in shifts[:5]:
            deck_desc = ", ".join(sig[:3]) + "..."
            lines.append(f"- {deck_desc}: {prev_wr:.0f}% → {cur_wr:.0f}% win rate")
        self.col_config.database["pending_actions"].insert_one({
            "kind": "leadership_escalation", "message": "\n".join(lines),
            "created_at": now, "processed": False,
        })

    def send_weekly_meta_report(self):
        """Rec #16: a regular Monday "what's everyone playing" report to the
        whole server -- deliberately different from check_meta_shifts above,
        which is a leadership-only ALERT that only fires when something looks
        wrong. This one fires every week regardless, same de-dup pattern as
        send_weekly_digest, and goes to the normal announcements channel
        (clash_cog.py's generic simple-announcement branch already covers
        this kind, no admin login needed to see it -- it's just a Discord
        message)."""
        now = datetime.now(timezone.utc)
        if now.weekday() != 0:  # Monday only, same cadence as the weekly digest
            return
        iso_year, iso_week, _ = now.isocalendar()
        marker_id = f"weekly_meta_report_{iso_year}_{iso_week}"
        if self.col_config.find_one({"_id": marker_id}):
            return
        self.col_config.update_one({"_id": marker_id}, {"$set": {"sent_at": now}}, upsert=True)

        week_ago = (now - timedelta(days=7)).strftime("%Y%m%dT%H%M%S")
        battles = list(self.col_battles.find(
            {"battle_time": {"$gte": week_ago}}, {"team_cards": 1, "result": 1},
        ))
        archetypes = {}
        for b in battles:
            result = b.get("result")
            if result not in ("win", "loss"):
                continue
            cards = [c for c in (b.get("team_cards") or []) if c]
            if len(cards) < 8:
                continue
            sig = tuple(sorted(cards[:8]))
            entry = archetypes.setdefault(sig, {"wins": 0, "games": 0})
            entry["games"] += 1
            if result == "win":
                entry["wins"] += 1
        qualifying = {sig: v for sig, v in archetypes.items() if v["games"] >= 3}
        if not qualifying:
            return  # not enough logged battles this week for a meaningful report

        by_usage = sorted(qualifying.items(), key=lambda kv: -kv[1]["games"])[:5]
        total_games = sum(v["games"] for v in qualifying.values())
        top_share = max(v["games"] for v in qualifying.values()) / total_games if total_games else 0
        diversity_score = round(1 - top_share, 2)

        lines = ["🧠 **Weekly Meta Report** — what the clan's been playing this week:"]
        for sig, v in by_usage:
            win_rate = round(v["wins"] / v["games"] * 100)
            deck_desc = ", ".join(sig[:3]) + "..."
            lines.append(f"- {deck_desc} — {v['games']} games, {win_rate}% win rate")
        lines.append(f"Clan deck diversity score: {diversity_score} (1.0 = everyone plays something different, 0 = everyone plays the same deck).")
        self.col_config.database["pending_actions"].insert_one({
            "kind": "weekly_meta_report_post", "message": "\n".join(lines),
            "created_at": now, "processed": False,
        })

    def send_weekly_digest(self):
        """Idea #135: a Monday digest — top chatters (most logged battles this
        week), top war contributors (fame in the most recent completed race),
        and a "what's new" recap of recent changelog/milestone posts. De-duped
        per ISO week via a marker doc so this only ever fires once a week
        regardless of how often the 30-min harvest loop runs on a Monday."""
        now = datetime.now(timezone.utc)
        if now.weekday() != 0:  # Monday only
            return
        iso_year, iso_week, _ = now.isocalendar()
        marker_id = f"weekly_digest_{iso_year}_{iso_week}"
        if self.col_config.find_one({"_id": marker_id}):
            return
        self.col_config.update_one({"_id": marker_id}, {"$set": {"sent_at": now}}, upsert=True)

        week_ago = (now - timedelta(days=7)).strftime("%Y%m%d")
        chatter_pipeline = [
            {"$match": {"battle_time": {"$gte": week_ago}}},
            {"$group": {"_id": "$player_tag", "battles": {"$sum": 1}}},
            {"$sort": {"battles": -1}}, {"$limit": 5},
        ]
        top_chatters = list(self.col_battles.aggregate(chatter_pipeline))

        latest_race = self.col_war_history.find_one({}, sort=[("data.seasonId", -1)]) or {}
        participants = ((latest_race.get("data", {}).get("clan") or {}).get("participants")) or []
        top_fame = sorted(participants, key=lambda p: p.get("fame", 0), reverse=True)[:5]

        lines = ["📊 **Weekly Digest**"]
        if top_chatters:
            names = {p["tag"].replace("#", ""): p.get("name") for p in participants}
            lines.append("Most battles logged: " + ", ".join(
                f"{names.get(c['_id'], c['_id'])} ({c['battles']})" for c in top_chatters
            ))
        if top_fame:
            lines.append("Top war contributors (last race): " + ", ".join(
                f"{p.get('name')} ({p.get('fame', 0):,})" for p in top_fame
            ))
        self.col_config.database["pending_actions"].insert_one({
            "kind": "weekly_digest_post", "message": "\n".join(lines),
            "created_at": now, "processed": False,
        })
        # Idea #138: email digest option — stubbed since sending real email needs
        # SMTP credentials this project doesn't currently have configured.
        # clash_cog.py's consumer just logs this one rather than dropping it
        # silently, so it's visible in logs once email delivery is wired up.
        email_users = list(self.col_config.database["users"].find({"email": {"$exists": True, "$ne": ""}}))
        if email_users:
            self.col_config.database["pending_actions"].insert_one({
                "kind": "weekly_digest_email", "message": "\n".join(lines),
                "recipient_emails": [u["email"] for u in email_users],
                "created_at": now, "processed": False,
            })

    def check_war_start_reminders(self):
        """Idea #136: event lead-up reminders (2 days out → tomorrow → now)
        instead of a single start-of-war ping. Approximated from the clan's
        stated Thu-Sun war window (documented in how_it_works.html / link.html)
        since the CR API doesn't expose an exact "next war starts at" timestamp —
        same documented-approximation pattern as this project's other timing
        calculations. War days are Thursday(3)-Sunday(6) in Python's weekday()."""
        now = datetime.now(timezone.utc)
        bot_settings = self.col_config.find_one({"_id": "bot_settings"}) or {}
        reset_hour = int(bot_settings.get("war_reset_hour_utc", 10))
        weekday = now.weekday()  # Mon=0 .. Sun=6
        days_until_thursday = (3 - weekday) % 7
        tier = None
        if days_until_thursday == 2 and now.hour == reset_hour:
            tier = "2_days"
        elif days_until_thursday == 1 and now.hour == reset_hour:
            tier = "tomorrow"
        elif days_until_thursday == 0 and now.hour == reset_hour:
            tier = "now"
        if not tier:
            return
        marker_id = f"war_start_reminder_{tier}_{now.strftime('%Y-%W')}"
        if self.col_config.find_one({"_id": marker_id}):
            return
        self.col_config.update_one({"_id": marker_id}, {"$set": {"sent_at": now}}, upsert=True)
        wording = {
            "2_days": "📅 Heads up — war starts in 2 days.",
            "tomorrow": "📅 War starts tomorrow — get your decks ready!",
            "now": "⚔️ War day is here — go use those decks!",
        }[tier]
        self.col_config.database["pending_actions"].insert_one({
            "kind": "war_start_reminder", "message": wording,
            "created_at": now, "processed": False,
        })

    def check_anniversaries(self):
        """Idea #137: a lightweight community-warmth touch — shout out a
        member's clan-join anniversary using the joined_clan_at date this
        project already tracks (idea #107's rising-star field, reused here).
        De-duped per calendar year via `last_anniversary_shoutout_year`."""
        now = datetime.now(timezone.utc)
        candidates = list(self.col_profiles.find(
            {"joined_clan_at": {"$exists": True}, "left_clan_at": {"$exists": False}},
            {"tag": 1, "name": 1, "joined_clan_at": 1, "last_anniversary_shoutout_year": 1},
        ))
        for c in candidates:
            joined = _as_aware_utc(c.get("joined_clan_at"))
            if not joined or joined.year == now.year:
                continue  # joined this same calendar year — not an anniversary yet
            if (joined.month, joined.day) != (now.month, now.day):
                continue
            if c.get("last_anniversary_shoutout_year") == now.year:
                continue
            years = now.year - joined.year
            self.col_config.database["pending_actions"].insert_one({
                "kind": "anniversary_shoutout",
                "message": f"🎉 Happy {years}-year clan anniversary, **{c.get('name', 'friend')}**! Thanks for being part of Graveyard Squad.",
                "created_at": now, "processed": False,
            })
            self.col_profiles.update_one({"tag": c["tag"]}, {"$set": {"last_anniversary_shoutout_year": now.year}})

    CONFIG_BACKUP_RETENTION = 14  # keep roughly two weeks of daily snapshots

    def backup_config_collection(self):
        """Idea #152: a bad template deploy currently has no rollback path
        beyond the disk fallback (which only covers templates that were never
        deployed to Mongo in the first place) or the last-5-versions history
        idea #67 already tracks per-template. This adds a coarser daily safety
        net: the WHOLE `config` collection, snapshotted once a day, so even a
        catastrophic accidental `db.config.deleteMany({})` has a same-day
        restore point. De-duped per calendar day; prunes anything older than
        CONFIG_BACKUP_RETENTION days."""
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        db = self.col_config.database
        if db["config_backups"].find_one({"backup_date": today}):
            return
        docs = list(self.col_config.find({}))
        for d in docs:
            d["_original_id"] = d.pop("_id")
        db["config_backups"].insert_one({
            "backup_date": today, "created_at": now, "docs": docs,
        })
        cutoff = now - timedelta(days=self.CONFIG_BACKUP_RETENTION)
        db["config_backups"].delete_many({"created_at": {"$lt": cutoff}})

    DB_BACKUP_LOG_RETENTION_DAYS = 30  # how long to keep the metadata log (the
    # actual zip is deleted right after a successful Discord upload -- this
    # collection never holds the backup payload itself, just its stats)
    DB_BACKUP_EXCLUDED_COLLECTIONS = {
        # config already gets its own daily snapshot (backup_config_collection
        # above) -- backing that collection up again here would just be a
        # backup of a backup, growing forever for no extra safety.
        "config_backups",
        # This method's own metadata log -- excluding it avoids a collection
        # backing up a record of itself.
        "db_backups_log",
        # Pure re-derivable CR API response cache, not data that's ever a real
        # loss if it disappears (and can get large/churny).
        "api_cache", "mongo_cache",
    }

    def backup_full_database(self, force: bool = False, triggered_by: str = "scheduled") -> dict | None:
        """A full-Mongo-database safety net, one level broader than
        backup_config_collection() above (which only ever covered the
        `config` collection). Every other collection in the main
        `graveyardbot` database (player_profiles, battle_history, war_tracking,
        users, etc. -- everything except DB_BACKUP_EXCLUDED_COLLECTIONS) gets
        dumped to one JSON file each via bson's json_util (which round-trips
        ObjectId/datetime correctly, unlike plain json.dumps), zipped into a
        single archive, and handed off to the bot process via the existing
        pending_actions queue (kind="db_backup_post") so it can actually post
        the file to Discord -- this process has no live Discord connection of
        its own, same reasoning as every other Discord-facing action already
        routed through pending_actions elsewhere in this file.

        De-duped per calendar day like the config backup, unless force=True
        (the manual "Backup Now" admin button bypasses the dedupe). The
        forum's separate `graveyardbot_forum` database (its own MongoClient,
        deliberately isolated -- see forum_routes.py) is NOT included; it's a
        distinct, lower-stakes dataset and backing it up would require a
        second connection and separate handling this pass didn't scope in.

        Returns a small metadata dict (or None if skipped/failed) rather than
        raising -- callers (run_full_cycle and the manual-trigger route) both
        treat a failed backup as non-fatal.
        """
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        db = self.col_config.database
        if not force and db["db_backups_log"].find_one({"backup_date": today}):
            return None

        tmp_dir = tempfile.mkdtemp(prefix="graveyardbot_backup_")
        zip_path = os.path.join(tmp_dir, f"graveyardbot_backup_{now.strftime('%Y%m%d_%H%M%S')}.zip")
        collection_count = 0
        total_docs = 0
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for name in db.list_collection_names():
                    if name in self.DB_BACKUP_EXCLUDED_COLLECTIONS:
                        continue
                    docs = list(db[name].find({}))
                    total_docs += len(docs)
                    collection_count += 1
                    # json_util.dumps preserves ObjectId/datetime/etc. as
                    # round-trippable {"$oid": ...}/{"$date": ...} wrappers --
                    # a real restore path (json_util.loads back into pymongo),
                    # not just a human-readable snapshot.
                    zf.writestr(f"{name}.json", json_util.dumps(docs, indent=None))
            size_bytes = os.path.getsize(zip_path)
        except Exception:
            # Clean up the temp dir even on failure -- don't leak partial
            # backup files if a collection dump throws partway through.
            try:
                os.remove(zip_path)
            except OSError:
                pass
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass
            raise

        db["db_backups_log"].insert_one({
            "backup_date": today, "created_at": now, "size_bytes": size_bytes,
            "collection_count": collection_count, "total_docs": total_docs,
            "triggered_by": triggered_by,
        })
        cutoff = now - timedelta(days=self.DB_BACKUP_LOG_RETENTION_DAYS)
        db["db_backups_log"].delete_many({"created_at": {"$lt": cutoff}})

        # Hand off to the bot process for actual Discord delivery. The bot's
        # pending_actions consumer (cogs/clash_cog.py) is responsible for
        # deleting zip_path/tmp_dir once it's done uploading (or given up).
        db["pending_actions"].insert_one({
            "kind": "db_backup_post",
            "zip_path": zip_path,
            "size_bytes": size_bytes,
            "collection_count": collection_count,
            "total_docs": total_docs,
            "backup_date": today,
            "triggered_by": triggered_by,
            "created_at": now,
            "processed": False,
        })
        log.info(f"Full DB backup ready: {collection_count} collections, {total_docs} docs, {size_bytes} bytes -> {zip_path}")
        return {
            "backup_date": today, "size_bytes": size_bytes,
            "collection_count": collection_count, "total_docs": total_docs,
        }

    def apply_due_scheduled_settings(self):
        """Idea #77: settings changes scheduled for a future time (queued via
        web_routes.py's /admin/api/settings/schedule) get applied here once due."""
        now = datetime.now(timezone.utc)
        db = self.col_config.database
        due = list(db["scheduled_settings"].find({"applied": False}))
        for item in due:
            try:
                apply_at = item.get("apply_at")
                # Stored as an ISO string from the browser's `Date().toISOString()`.
                apply_dt = datetime.fromisoformat(str(apply_at).replace("Z", "+00:00")) if isinstance(apply_at, str) else apply_at
                if apply_dt and apply_dt <= now:
                    self.col_config.update_one({"_id": "bot_settings"}, {"$set": item.get("changes", {})}, upsert=True)
                    db["scheduled_settings"].update_one({"_id": item["_id"]}, {"$set": {"applied": True, "applied_at": now}})
                    log.info(f"Applied scheduled setting change: {item.get('changes')}")
            except Exception as e:
                log.error(f"Failed to apply scheduled setting {item.get('_id')}: {e}")

    def run_full_cycle(self, is_startup=False):
        start_time = time.time()
        log.info("Starting Harvester Cycle...")

        if is_startup:
            self.backfill_missed_wars()

        tags = self.harvest_clan_and_profiles()
        war_data = None
        if tags:
            self.harvest_battles(tags)

            # Live war tracking
            war_data = self.fetch_api(f"clans/%23{self.clan_tag}/currentriverrace")
            if war_data:
                war_data["harvest_time"] = datetime.now(timezone.utc)
                self.col_war.insert_one(war_data)
                try:
                    self.check_tiered_war_reminders(war_data)
                except Exception as e:
                    log.error(f"Tiered war reminder check failed (non-fatal): {e}")

        # Section 6 (gamification, ideas #102/#107/#110/#115): all cheap,
        # bounded-cost aggregates, each independently non-fatal so one failing
        # doesn't take down the rest of the harvest cycle.
        try:
            self.compute_weekly_spotlights(war_data)
        except Exception as e:
            log.error(f"Weekly spotlight computation failed (non-fatal): {e}")
        try:
            self.compute_weekly_hall_of_fame()
        except Exception as e:
            log.error(f"Weekly Hall of Fame computation failed (non-fatal): {e}")
        try:
            self.compute_clan_legends()
        except Exception as e:
            log.error(f"Clan legends computation failed (non-fatal): {e}")
        try:
            self.assign_weekly_rivalries()
        except Exception as e:
            log.error(f"Rivalry assignment failed (non-fatal): {e}")
        try:
            self.check_first_week_checkins()
        except Exception as e:
            log.error(f"First-week check-in failed (non-fatal): {e}")

        # Idea #218: weekly, non-fatal, same pattern as the rest of this cycle.
        try:
            self.check_meta_shifts()
        except Exception as e:
            log.error(f"Meta-shift check failed (non-fatal): {e}")

        # Rec #16: weekly, non-fatal, same pattern as the rest of this cycle.
        try:
            self.send_weekly_meta_report()
        except Exception as e:
            log.error(f"Weekly meta report failed (non-fatal): {e}")

        # Section 8 (notifications, ideas #135/#136/#137): all independently
        # non-fatal, same pattern as the rest of this cycle.
        try:
            self.send_weekly_digest()
        except Exception as e:
            log.error(f"Weekly digest failed (non-fatal): {e}")
        try:
            self.check_war_start_reminders()
        except Exception as e:
            log.error(f"War-start reminder check failed (non-fatal): {e}")
        try:
            self.check_anniversaries()
        except Exception as e:
            log.error(f"Anniversary check failed (non-fatal): {e}")
        try:
            self.backup_config_collection()
        except Exception as e:
            log.error(f"Config backup failed (non-fatal): {e}")
        try:
            self.backup_full_database()
        except Exception as e:
            log.error(f"Full database backup failed (non-fatal): {e}")

        # Idea #214: off unless an admin explicitly enabled it in Settings.
        try:
            self.auto_decline_stale_applications()
        except Exception as e:
            log.error(f"Auto-decline stale applications failed (non-fatal): {e}")

        try:
            self.check_streaming_status()
        except Exception as e:
            log.error(f"Streaming status check failed (non-fatal): {e}")

        try:
            self.post_recruitment_to_reddit()
        except Exception as e:
            log.error(f"Reddit auto-post failed (non-fatal): {e}")

        # Idea #17 (revised): cheap query, safe to run every cycle — only ever
        # touches profiles already flagged departed past the retention window.
        try:
            self.purge_expired_departed_members()
        except Exception as e:
            log.error(f"Departed-member retention purge failed (non-fatal): {e}")

        # Self-healing migration for the battle_time bug found while building
        # section 14 — cheap no-op once existing data is caught up.
        try:
            self.backfill_missing_battle_time()
        except Exception as e:
            log.error(f"battle_time backfill failed (non-fatal): {e}")

        # Idea #77: apply any due scheduled settings changes. Piggybacks on this
        # already-running 30-min loop rather than a dedicated scheduler process.
        try:
            self.apply_due_scheduled_settings()
        except Exception as e:
            log.error(f"Scheduled settings apply failed (non-fatal): {e}")

        self._harvest_meta["last_run"] = datetime.now(timezone.utc).isoformat()
        self._harvest_meta["duration_s"] = round(time.time() - start_time, 2)
        self._save_harvest_meta()
        log.info(f"✅ Cycle Complete in {self._harvest_meta['duration_s']}s.")

        try:
            self._fire_webhooks("harvest_complete", {
                "duration_s": self._harvest_meta["duration_s"],
                "last_run": self._harvest_meta["last_run"],
                "is_startup": is_startup,
            })
        except Exception as e:
            log.error(f"harvest_complete webhook dispatch failed (non-fatal): {e}")

# ---------------------------------------------------------------------------
# SHARED SINGLETON (web_routes.py and clash_cog.py both call get_harvester())
# ---------------------------------------------------------------------------
_harvester_instance = None
_harvester_lock = threading.Lock()

def get_harvester() -> "DataHarvester":
    """Return the single shared DataHarvester instance, creating it on first use."""
    global _harvester_instance
    if _harvester_instance is None:
        with _harvester_lock:
            if _harvester_instance is None:
                _harvester_instance = DataHarvester()
    return _harvester_instance

# ---------------------------------------------------------------------------
# BACKGROUND WORKER LOOP (For mainbot.py integration)
# ---------------------------------------------------------------------------
def start_harvester_loop(interval_minutes=30):
    harvester = get_harvester()
    
    # Run immediate catch-up on boot
    log.info("Executing initial boot Catch-Up cycle...")
    try:
        harvester.run_full_cycle(is_startup=True)
    except Exception as e:
        log.error(f"Error during boot catch-up: {e}")

    # Enter normal looping state
    log.info(f"Harvester entering standard {interval_minutes}-minute loop.")
    while True:
        time.sleep(interval_minutes * 60)
        try:
            harvester.run_full_cycle(is_startup=False)
        except Exception as e:
            log.error(f"Critical error in Harvester Loop: {e}")