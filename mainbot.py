import os
import sys
import logging
import threading
import asyncio
import urllib.parse
import aiohttp
import requests
import time
import json
import csv
import io
import re
import zoneinfo
from datetime import datetime, timedelta
import platform
import time as _time

import discord
from discord.ext import commands
from dotenv import load_dotenv
from flask import Flask, request, redirect, session, jsonify
from waitress import serve
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient
import redis.asyncio as redis
import redis as sync_redis
from jinja2.sandbox import SandboxedEnvironment

# Import detached frontend templates
from templates import DEFAULT_ROSTER_HTML, DEFAULT_LINK_HTML, DEFAULT_PLAYER_HTML, DEFAULT_ADMIN_HTML

# ---------------------------------------------------------------------------
# 1. SETUP
# ---------------------------------------------------------------------------
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mainbot")

REQUIRED_ENV_VARS = [
    "CR_TOKEN",
    "DISCORD_TOKEN",
    "FLASK_SECRET",
    "MONGO_URL",
    "REDIS_URL",
    "GUILD_ID",
]

missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
if missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET")
if not app.secret_key:
    raise RuntimeError("FLASK_SECRET environment variable is required")

CLAN_TAG = os.getenv("CLAN_TAG", "9LVY89UP").strip().upper().replace("#", "")
MAX_CARD_LEVEL = int(os.getenv("MAX_CARD_LEVEL", 15))
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

mongo_client_sync = MongoClient(os.getenv("MONGO_URL", "mongodb://localhost:27017"))
db_sync = mongo_client_sync["graveyardbot"]
users_sync = db_sync["users"]
custom_cmds_sync = db_sync["custom_commands"]
redis_sync_client = sync_redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))

cr_api_session = requests.Session()
cr_api_session.headers.update({
    "User-Agent": "GraveyardBot/1.0"
})

_cache_lock = threading.Lock()
_HTML_CACHE = {}
sandbox_env = SandboxedEnvironment(autoescape=True)

# Tracks harvest metadata
_harvest_meta = {
    "last_run": None,
    "snapshots_saved": 0,
    "profiles_saved": 0,
    "battles_saved": 0,
    "duration_s": None,
    "status": "never_run",
}

def _restore_harvest_meta():
    try:
        doc = db_sync["config"].find_one({"_id": "harvest_meta"})
        if doc:
            doc.pop("_id", None)
            _harvest_meta.update(doc)
            log.info("✅ Restored harvest metadata from DB.")
    except Exception as e:
        log.warning(f"Could not restore harvest meta: {e}")

_restore_harvest_meta()  # ← now called after definition
# ---------------------------------------------------------------------------
# 2. DATA ENRICHMENT & STREAK LOGIC
# ---------------------------------------------------------------------------
def calculate_streak(battles):
    sorted_battles = sorted(battles, key=lambda x: x.get('utcTime', ''), reverse=True)
    streak = 0
    for b in sorted_battles:
        if b.get('type') == 'PvP':
            if b.get('result') == 'win':
                streak += 1
            else:
                break
    return streak

def _enrich_members(raw_members, profiles, war_parts):
    players = []
    seen = set()
    for m in raw_members:
        tag = m.get('tag', '').replace('#', '')
        if tag in seen: continue
        seen.add(tag)
        m['name'] = re.sub(r"<c\d?>|</c>", "", m.get("name", "Unknown"), flags=re.IGNORECASE)
        p = profiles.get(tag, {})
        m['current_streak'] = calculate_streak(p.get('battles', []))
        m['warDayWins'] = p.get('warDayWins', 0) # 👈 ADD THIS LINE
        m['fame'] = war_parts.get(tag, {}).get('fame', 0)
        m['clean_tag'] = tag
        players.append(m)
    return sorted(players, key=lambda x: x.get('trophies', 0), reverse=True)

# ---------------------------------------------------------------------------
# UTILITY HELPERS
# ---------------------------------------------------------------------------
def clean_tag(tag: str) -> str:
    return tag.strip().upper().replace("#", "")

def _normalize_card_levels(data: dict) -> dict:
    for key in ("cards", "currentDeck"):
        for card in (data.get(key) or []):
            card["level"] = (
                MAX_CARD_LEVEL
                - card.get("maxLevel", MAX_CARD_LEVEL)
                + card.get("level", 1)
            )
    return data

def fetch_cr_api(endpoint: str, retries: int = 3) -> dict | None:
    url = f"https://proxy.royaleapi.dev/v1/{endpoint}"
    headers = {
        "Authorization": f"Bearer {os.getenv('CR_TOKEN', '').strip()}",
        "Accept": "application/json",
    }
    for attempt in range(retries):
        try:
            response = cr_api_session.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict):
                    _normalize_card_levels(data)
                return data
            elif response.status_code == 429:
                wait = 2 ** attempt
                log.warning(f"Flask API rate-limited (429) on {endpoint}. Retrying in {wait}s…")
                time.sleep(wait)
            else:
                log.error(f"Flask API request failed [{response.status_code}] on {endpoint}")
                return None
        except Exception as e:
            wait = 2 ** attempt
            log.error(f"Flask API exception: {e}. Retrying in {wait}s…")
            time.sleep(wait)
    return None

def validate_jinja_syntax(html: str) -> tuple[bool, str, int | None]:
    try:
        sandbox_env.parse(html)
        return True, "Template syntax is valid.", None
    except Exception as e:
        line_match = re.search(r"line (\d+)", str(e))
        line = int(line_match.group(1)) if line_match else None
        return False, str(e), line

def _get_cached_system_config() -> dict:
    try:
        config = db_sync["config"].find_one({"_id": "system_config"})
        return config or {}
    except Exception as e:
        log.error(f"Failed to load system config: {e}")
        return {}

def is_admin() -> bool:
    if "discord_id" not in session:
        return False
    discord_id = str(session.get("discord_id"))
    if discord_id in ["751975709643112569"]: 
        return True
    master_admin = os.getenv("MASTER_ADMIN_ID", "")
    if master_admin and discord_id == master_admin:
        return True
    sys_config_db = _get_cached_system_config()
    allowed_roles = sys_config_db.get("admin_role_ids", [])
    allowed_users = sys_config_db.get("admin_user_ids", [])
    if discord_id in allowed_users:
        return True
    user_roles = session.get("user_roles", [])
    return any(str(role_id) in allowed_roles for role_id in user_roles)

def get_template(template_name: str) -> str:
    with _cache_lock:
        if template_name in _HTML_CACHE:
            return _HTML_CACHE[template_name]
    doc = db_sync["config"].find_one({"_id": "html_templates"})
    with _cache_lock:
        if doc and template_name in doc:
            _HTML_CACHE[template_name] = doc[template_name]
            return doc[template_name]
        fallback = globals().get(f"DEFAULT_{template_name.upper()}_HTML", "")
        _HTML_CACHE[template_name] = fallback
        return fallback

def invalidate_template_cache() -> None:
    global _HTML_CACHE
    with _cache_lock:
        _HTML_CACHE = {}

def render_sandboxed(template_str: str, **context) -> str:
    """Safely renders HTML content, blocking remote server code injection vectors."""
    template = sandbox_env.from_string(template_str)
    
    # Automatically inject Flask's session object for UI logic
    if "session" not in context:
        context["session"] = session
        
    return template.render(**context)

# ---------------------------------------------------------------------------
# PUBLIC FRONTEND ROUTES
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    try:
        # 1. Try to Fetch Live Clan Roster
        clan_data = fetch_cr_api(f"clans/%23{CLAN_TAG}")
        is_fallback = False
        error_msg = None
        raw_members = []
        
        # Check if live data is valid
        if clan_data and "memberList" in clan_data:
            raw_members = clan_data["memberList"]
        else:
            # 🛑 FALLBACK LOGIC: Live API failed, pull from the latest DB snapshot
            log.warning("Live CR API unreachable. Falling back to latest DB snapshot.")
            is_fallback = True
            error_msg = "Live API unreachable. Displaying cached data from the latest snapshot."
            
            # Find the most recent date we have a snapshot for
            latest_snap = db_sync["historical_snapshots"].find_one(sort=[("date", -1)])
            
            if not latest_snap:
                # If we have NO live data and NO backups, we have to bail out
                return render_sandboxed(get_template("roster"), players=[], error="Clash Royale API connection failed and no database backups exist yet.")
            
            # Reconstruct the member list from the snapshot documents
            backup_docs = list(db_sync["historical_snapshots"].find({"date": latest_snap["date"]}))
            for doc in backup_docs:
                raw_members.append({
                    "tag": doc.get("tag"),
                    "name": doc.get("name", "Unknown"),
                    "trophies": doc.get("trophies", 0),
                    # Snapshots might not store role, so we default to Member to avoid UI crashes
                    "role": doc.get("role", "member") 
                })
        
        member_tags = [clean_tag(m["tag"]) for m in raw_members if "tag" in m]
        
        # 2. Grab Database Profiles for Streaks/Stats
        db_profiles = list(db_sync["player_profiles"].find({"_id": {"$in": member_tags}}))
        profiles_map = {p["_id"]: p for p in db_profiles}
        
        # 3. Fetch War Data (Only if we aren't already in fallback mode)
        war_participants = {}
        if not is_fallback:
            war_data = fetch_cr_api(f"clans/%23{CLAN_TAG}/currentriverrace")
            if war_data and isinstance(war_data, dict):
                if "clan" in war_data and war_data["clan"] and "participants" in war_data["clan"]:
                    war_participants = {clean_tag(p["tag"]): p for p in war_data["clan"]["participants"] if "tag" in p}
                else:
                    for clan in war_data.get("clans", []):
                        if clan and clean_tag(clan.get("tag", "")) == clean_tag(CLAN_TAG):
                            war_participants = {clean_tag(p["tag"]): p for p in clan.get("participants", [])}
                            break
        
        # 4. Enrich & Sort
        players = _enrich_members(raw_members, profiles_map, war_participants)
        
        # 5. Calculate Hall of Fame
        top_pusher = max(players, key=lambda x: x.get("trophies", 0)) if players else None
        top_streak = max(players, key=lambda x: x.get("current_streak", 0)) if players else None
        top_war = max(players, key=lambda x: x.get("warDayWins", 0)) if players else None

        return render_sandboxed(
            get_template("roster"),
            players=players,
            top_pusher=top_pusher,
            top_streak=top_streak,
            top_war=top_war,
            error=error_msg # Will display the warning banner if fallback was triggered
        )
    except Exception as e:
        log.exception("Index route crash prevented gracefully.")
        return render_sandboxed(get_template("roster"), players=[], error=f"Internal Server Error: {str(e)}")

@app.route("/favicon.ico")
def favicon():
    return "", 204
    
# ── /admin/api/battles ─────────────────────────────────────────────────────
@app.route("/admin/api/battles")
def api_get_battles():
    if not is_admin():
        return jsonify({"error": "unauthorized"}), 403
    
    try:
        # Fetch the 100 most recent battles sorted by time descending
        battles = list(db_sync["battle_history"].find().sort("battle_time", -1).limit(100))
        
        # Grab all profiles so we can match tags to player names
        profiles = {p["_id"]: p.get("name", "Unknown") for p in db_sync["player_profiles"].find()}
        
        # Format for JSON serialization
        for b in battles:
            b["_id"] = str(b["_id"])
            tag = b.get("player_tag", "")
            b["player_name"] = profiles.get(tag, "Unknown")
            
        return jsonify(battles)
    except Exception as e:
        log.error(f"Error fetching battles: {e}")
        return jsonify({"error": "Internal database error."}), 500
#################################################WAR MONITOR
# ── /admin/api/war ─────────────────────────────────────────────────────────
@app.route("/admin/api/war")
def api_get_war():
    if not is_admin():
        return jsonify({"error": "unauthorized"}), 403
        
    war_data = fetch_cr_api(f"clans/%23{CLAN_TAG}/currentriverrace")
    if not war_data:
        return jsonify({"error": "Failed to fetch war data from CR API."}), 500
        
    return jsonify(war_data)
###############################ROSTERLOGIC
# ── /player/<tag> ────────────────────────────────────────────────────────
@app.route("/player/<tag>")
def player_profile(tag):
    clean_t = clean_tag(tag)
    
    # 1. Fetch live data from the Clash Royale API
    player_data = fetch_cr_api(f"players/%23{clean_t}")
    
    if not player_data:
        # If the API fails or the tag is invalid, return a friendly error
        return render_sandboxed(
            get_template("player"), 
            player=None, 
            error=f"Could not find player with tag #{clean_t}"
        ), 404

    # 2. Grab Database Profile to calculate the Win Streak
    db_profile = db_sync["player_profiles"].find_one({"_id": clean_t})
    if db_profile and "battles" in db_profile:
        player_data["current_streak"] = calculate_streak(db_profile.get("battles", []))
    else:
        player_data["current_streak"] = 0

    # 3. Render your custom 'player' UI Template
    return render_sandboxed(
        get_template("player"),
        data=player_data,          # FIXED: 'player' -> 'data'
        max_lvl=MAX_CARD_LEVEL,    # ADDED: Needed for max-level UI logic
        clan_tag=CLAN_TAG
    )



 
# ── /admin ────────────────────────────────────────────────────────────────
@app.route("/admin")
def admin_panel():
    if not is_admin():
        return "Unauthorized", 403
    return render_sandboxed(
        get_template("admin"),
        clan_tag=CLAN_TAG,
    )
#----/battles-------------------------------------------------------------------------------------
@app.route("/api/player/<tag>/battles")
def api_player_battles(tag):
    # Ensure tag matches db format (no #)
    clean_t = tag.replace("#", "").upper()
    try:
        # Fetch the 15 most recent battles for this specific tag
        battles = list(
            db_sync["battle_history"]
            .find({"player_tag": clean_t})
            .sort("battle_time", -1)
            .limit(15)
        )
        
        # Convert ObjectId to string for JSON serialization
        for b in battles:
            b["_id"] = str(b["_id"])
            
        return jsonify(battles)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
# ── /admin/harvest/manual ──────────────────────────────────────────────────
@app.route("/admin/harvest/manual", methods=["POST"])
def manual_harvest():
    if not is_admin():
        return jsonify({"error": "unauthorized"}), 403
    ok = _try_redis_publish({"action": "MANUAL_HARVEST"})
    if ok:
        return jsonify({"message": "Manual Harvest initiated successfully! Please check diagnostics in 30 seconds."})
    return jsonify({"message": "Failed to send command. Redis offline."}), 500

# ── /admin/api/template/<name> ─────────────────────────────────────────────
@app.route("/admin/api/template/<name>")
def api_get_template(name):
    if not is_admin():
        return jsonify({"error": "unauthorized"}), 403
    source = request.args.get("source", "current")
    if source == "default":
        return jsonify({"html": globals().get(f"DEFAULT_{name.upper()}_HTML", "")})
    else:
        doc = db_sync["config"].find_one({"_id": "html_templates"})
        if doc and name in doc:
            return jsonify({"html": doc[name]})
        return jsonify({"html": globals().get(f"DEFAULT_{name.upper()}_HTML", "")})

# ── /admin/api/snapshot/<date> ─────────────────────────────────────────────
@app.route("/admin/api/snapshot/<date>")
def api_get_snapshot(date):
    if not is_admin():
        return jsonify({"error": "unauthorized"}), 403

    try:
        # Fetch all records for the given date from MongoDB
        records = list(db_sync["historical_snapshots"].find({"date": date}))
        
        # Flask cannot serialize MongoDB's ObjectId, so we remove it
        for record in records:
            if "_id" in record:
                del record["_id"]

        if not records:
            return jsonify({"error": f"No records found for date: {date}"}), 404

        return jsonify(records)
    except Exception as e:
        log.error(f"Error fetching snapshot for {date}: {e}")
        return jsonify({"error": "Internal server error while fetching snapshot."}), 500
        
# ── /admin/preview ─────────────────────────────────────────────────────────
@app.route("/admin/preview", methods=["POST"])
def preview_template():
    if not is_admin():
        return "Unauthorized", 403
    html = request.form.get("html", "")
    template_name = request.form.get("template", "roster")
    
    # Safe validation check before execution
    ok, message, line = validate_jinja_syntax(html)
    if not ok:
        return f"<h3>Syntax Error on line {line}</h3><p>{message}</p>", 400
        
    # Provide dummy context specific to testing
    dummy_context = {
        "players": [
            {"name": "TestPlayer", "trophies": 5500, "role": "leader", "current_streak": 3, "fame": 2000, "warDayWins": 15, "donations": 400, "clean_tag": "XXXX"}
        ],
        "data": {
            "name": "TestPlayer", 
            "tag": "#XXXX", 
            "expLevel": 50, 
            "trophies": 5500, 
            "bestTrophies": 6000,
            "wins": 100, 
            "losses": 50, 
            "threeCrownWins": 35,
            "battleCount": 150,
            "current_streak": 5,
            "donations": 400,
            "donationsReceived": 250,
            "warDayWins": 15,
            "currentFavouriteCard": {"name": "Graveyard"},
            "arena": {"name": "Legendary Arena"},
            "clan": {"name": "Graveyard Squad"},
            "role": "coLeader",
            "currentDeck": [
                {"name": "Graveyard", "level": 14, "maxLevel": 14},
                {"name": "Poison", "level": 13, "maxLevel": 14},
                {"name": "Tornado", "level": 15, "maxLevel": 14}
            ]
        },
        "top_pusher": {"name": "TestPlayer", "trophies": 5500},
        "top_streak": {"name": "TestPlayer", "current_streak": 3},
        "top_war": {"name": "TestPlayer", "warDayWins": 15},
        "clan_tag": CLAN_TAG,
        "max_lvl": MAX_CARD_LEVEL
    }
 
# ── /admin/diagnostics ────────────────────────────────────────────────────
@app.route("/admin/diagnostics")
def admin_diagnostics():
    if not is_admin():
        return jsonify({"error": "unauthorized"}), 403
 
    result = {}
    try:
        t0 = _time.monotonic()
        redis_sync_client.ping()
        ping_ms = round((_time.monotonic() - t0) * 1000, 1)
        info = redis_sync_client.info()
        result["redis"] = {
            "status": "ok",
            "ping_ms": ping_ms,
            "used_memory": info.get("used_memory_human", "N/A"),
            "total_keys": redis_sync_client.dbsize(),
            "mode": info.get("redis_mode", "N/A"),
            "redis_version": info.get("redis_version", "N/A"),
        }
    except Exception as e:
        result["redis"] = {"status": "error", "error": str(e)}
 
    try:
        t0 = _time.monotonic()
        mongo_client_sync.admin.command("ping")
        ping_ms = round((_time.monotonic() - t0) * 1000, 1)
        result["mongo"] = {
            "status": "ok",
            "ping_ms": ping_ms,
            "db_name": db_sync.name,
            "snapshot_count": db_sync["historical_snapshots"].estimated_document_count(),
            "battle_count":   db_sync["battle_history"].estimated_document_count(),
            "profile_count":  db_sync["player_profiles"].estimated_document_count(),
        }
    except Exception as e:
        result["mongo"] = {"status": "error", "error": str(e)}
 
    try:
        endpoint = f"https://proxy.royaleapi.dev/v1/clans/%23{CLAN_TAG}"
        t0 = _time.monotonic()
        auth_headers = {
            "Authorization": f"Bearer {os.getenv('CR_TOKEN', '').strip()}",
            "Accept": "application/json"
        }
        resp = cr_api_session.get(endpoint, headers=auth_headers, timeout=5)
        latency_ms = round((_time.monotonic() - t0) * 1000, 1)
        
        api_status = "error"
        if resp.status_code == 200: api_status = "ok"
        elif resp.status_code == 429: api_status = "rate_limited"
        elif resp.status_code == 403: api_status = "forbidden"
        
        result["cr_api"] = {
            "status": api_status,
            "status_code": resp.status_code,
            "latency_ms": latency_ms,
            "endpoint_tested": endpoint,
        }
    except Exception as e:
        result["cr_api"] = {"status": "unreachable", "error": str(e)}
 
    bot_inst = globals().get("_bot_instance")
    if bot_inst:
        try:
            uptime_s = int(_time.monotonic() - getattr(bot_inst, "_start_time", _time.monotonic()))
            h, rem = divmod(uptime_s, 3600)
            m, s = divmod(rem, 60)
            result["bot"] = {
                "connected": not bot_inst.is_closed(),
                "latency_ms": round(bot_inst.latency * 1000, 1),
                "guild_count": len(bot_inst.guilds),
                "uptime": f"{h}h {m}m {s}s",
                "prefix": getattr(bot_inst, "active_prefix", "?"),
            }
        except Exception as e:
            result["bot"] = {"connected": False, "error": str(e)}
    else:
        result["bot"] = {"connected": False, "error": "No bot instance registered"}
 
    try:
        backend = "redis" if result.get("redis", {}).get("status") == "ok" else "mongo_fallback"
        with _cache_lock: html_entries = len(_HTML_CACHE)
        
        if backend == "redis":
            all_keys = redis_sync_client.keys("*")
            total = len(all_keys)
        else:
            total = db_sync["api_cache"].estimated_document_count()
            
        result["cache"] = {
            "backend": backend,
            "total_keys": total,
            "html_cache_entries": html_entries,
        }
    except Exception as e:
        result["cache"] = {"error": str(e)}
 
    # Compile harvest metadata + history
    # Compile harvest metadata + history
    harv = _harvest_meta.copy()
    try:
        history_dates = db_sync["historical_snapshots"].distinct("date")
        sorted_dates = sorted(history_dates, reverse=True)
        harv["history_dates"] = sorted_dates
        
        # 🛑 RESTART FALLBACK: If RAM is wiped, pull the latest data from the database
        if not harv.get("last_run") and sorted_dates:
            latest_date = sorted_dates[0]
            harv["last_run"] = f"{latest_date} (DB Restored)"
            harv["status"] = "loaded_from_db"
            harv["snapshots_saved"] = db_sync["historical_snapshots"].count_documents({"date": latest_date})
            harv["battles_saved"] = "N/A (Memory reset)"
    except Exception as e:
        harv["history_dates"] = []
        log.error(f"Error compiling harvest history: {e}")
        
    result["harvest"] = harv
 
    cog = bot_inst.cogs.get("ClashRoyale") if bot_inst else None
    if cog:
        next_snap = cog.daily_snapshot_loop.next_iteration.strftime("%Y-%m-%d %H:%M:%S UTC") if cog.daily_snapshot_loop.next_iteration else None
        result["tasks"] = {
            "snapshot_loop": "running" if cog.daily_snapshot_loop.is_running() else "stopped",
            "next_snapshot": next_snap,
        }
    else:
        result["tasks"] = {"snapshot_loop": "cog not loaded"}
 
    tmpl_doc = db_sync["config"].find_one({"_id": "html_templates"}) or {}
    result["templates"] = {name: ("db" if tmpl_doc.get(name) else "fallback") for name in ["roster", "player", "admin"]}
 
    result["version"] = "1.1.0"
    result["environment"] = os.getenv("ENVIRONMENT", "production")
    result["hostname"] = platform.node()
    return jsonify(result)
 
@app.route("/admin/reset-html", methods=["POST"])
def admin_reset_html():
    if not is_admin():
        return jsonify({"error": "unauthorized"}), 403
    db_sync["config"].delete_one({"_id": "html_templates"})
    invalidate_template_cache()
    return jsonify({"message": "All templates reset. Pages now use Python fallbacks."})
 
@app.route("/admin/flush-cache", methods=["POST"])
def admin_flush_cache():
    if not is_admin():
        return jsonify({"error": "unauthorized"}), 403
    flushed = 0
    try:
        keys = redis_sync_client.keys("*")
        cr_keys = [k for k in keys if any((k.decode() if isinstance(k, bytes) else k).startswith(p) for p in ("player:", "clan:", "battlelog:", "currentrace:", "racelog:", "cards:", "chests:", "warmed_today:"))]
        if cr_keys:
            flushed = redis_sync_client.delete(*cr_keys)
    except Exception:
        result = db_sync["api_cache"].delete_many({})
        flushed = result.deleted_count
    invalidate_template_cache()
    return jsonify({"message": f"Flushed {flushed} cache keys."})

@app.route("/admin/update-html", methods=["POST"])
def update_html():
    if not is_admin():
        return "Unauthorized", 403
    template_name = request.form.get("template_name")
    html_content = request.form.get("html_content")
    if template_name in ["roster", "player", "link", "admin"]:
        db_sync["config"].update_one(
            {"_id": "html_templates"},
            {"$set": {template_name: html_content}},
            upsert=True,
        )
        invalidate_template_cache()
        return redirect("/admin?success=UI+Code+Deployed+Live!")
    return redirect("/admin?error=Invalid+Template+Name")

@app.route("/admin/export/custom", methods=["POST"])
def export_custom_csv():
    if not is_admin():
        return "Unauthorized", 403

    clan_data = fetch_cr_api(f"clans/%23{CLAN_TAG}")
    members = clan_data.get("memberList", []) if clan_data else []
    member_tags = [clean_tag(m["tag"]) for m in members]

    db_profiles = list(db_sync["player_profiles"].find({"_id": {"$in": member_tags}}))
    profiles_map = {p["_id"]: p for p in db_profiles}

    war_data = fetch_cr_api(f"clans/%23{CLAN_TAG}/currentriverrace")
    war_participants = {}
    if war_data and "clan" in war_data and "participants" in war_data["clan"]:
        war_participants = {clean_tag(p["tag"]): p for p in war_data["clan"]["participants"] if "tag" in p}
    else:
        for clan in (war_data.get("clans", []) if war_data else []):
            if clan and clean_tag(clan.get("tag", "")) == clean_tag(CLAN_TAG):
                war_participants = {clean_tag(p["tag"]): p for p in clan.get("participants", [])}
                break

    export_format = request.form.get("export_format", "csv")
    selected_fields = request.form.getlist("fields")

    field_extractors = {
        "name":              lambda m, p, wp: m.get("name"),
        "tag":               lambda m, p, wp: clean_tag(m["tag"]),
        "role":              lambda m, p, wp: m.get("role"),
        "expLevel":          lambda m, p, wp: m.get("expLevel"),
        "trophies":          lambda m, p, wp: m.get("trophies"),
        "donations":         lambda m, p, wp: m.get("donations"),
        "donationsReceived": lambda m, p, wp: m.get("donationsReceived"),
        "fame":              lambda m, p, wp: wp.get("fame", 0),
        "decksUsedToday":    lambda m, p, wp: wp.get("decksUsedToday", 0),
        "decksRemaining":    lambda m, p, wp: wp.get("decksRemaining", 4),
        "totalWins":         lambda m, p, wp: p.get("wins", 0),
        "totalLosses":       lambda m, p, wp: p.get("losses", 0),
        "current_streak":    lambda m, p, wp: p.get("current_streak", 0),
        "warDayWins":        lambda m, p, wp: p.get("warDayWins", 0),
        "favoriteCard":      lambda m, p, wp: p.get("currentFavouriteCard", {}).get("name", "N/A"),
    }
    
    rows = []
    for m in members:
        tag = clean_tag(m["tag"])
        p = profiles_map.get(tag, {})
        wp = war_participants.get(tag, {})
        row = {f: field_extractors[f](m, p, wp) for f in selected_fields if f in field_extractors}
        rows.append(row)

    if export_format == "json":
        return jsonify(rows)

    si = io.StringIO()
    cw = csv.writer(si)
    if selected_fields:
        cw.writerow(selected_fields)
        for row in rows:
            cw.writerow([row.get(f, "N/A") for f in selected_fields])
    else:
        cw.writerow(["Name", "Tag", "Trophies", "Current Win Streak"])
        for m in members:
            tag = clean_tag(m["tag"])
            p = profiles_map.get(tag, {})
            cw.writerow([m.get("name"), tag, m.get("trophies"), p.get("current_streak", 0)])

    output = si.getvalue()
    return app.response_class(
        output, mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=Graveyard_Custom_Export.csv"}
    )

def _try_redis_publish(payload: dict) -> bool:
    try:
        redis_sync_client.publish("graveyard_bot_signals", json.dumps(payload))
        return True
    except Exception as e:
        log.warning(f"Redis publish failed: {e}")
        return False

def run_flask():
    port = int(os.getenv("PORT", 5000))
    log.info(f"🌐 Flask dashboard running on port {port}")
    serve(app, host="0.0.0.0", port=port)

def get_dynamic_prefix(bot_instance, message):
    return bot_instance.active_prefix

class GraveyardBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix=get_dynamic_prefix, intents=intents)
        self.http_session = None
        self.redis_available = False
        self.active_prefix = "!"
        self.maintenance_mode = False
        self.feature_auto_pings = False
        self.ignored_channels = []
        self.war_channel_id = 0
        self._last_config_load = 0.0
        self.mongo_client = AsyncIOMotorClient(os.getenv("MONGO_URL", "mongodb://localhost:27017"))
        self.db = self.mongo_client["graveyardbot"]
        self.db_users = self.db["users"]
        self.custom_cmds = self.db["custom_commands"]

    def _cr_headers(self) -> dict:
        return {"Authorization": f"Bearer {os.getenv('CR_TOKEN', '').strip()}", "Accept": "application/json"}

    async def async_fetch_cr_api(self, endpoint: str, retries: int = 3) -> dict | None:
        url = f"https://proxy.royaleapi.dev/v1/{endpoint}"
        timeout = aiohttp.ClientTimeout(total=10)
        for attempt in range(retries):
            try:
                async with self.http_session.get(url, headers=self._cr_headers(), timeout=timeout) as response:
                    if response.status == 200:
                        data = await response.json()
                        if isinstance(data, dict): _normalize_card_levels(data)
                        return data
                    elif response.status == 429:
                        wait = 2 ** attempt
                        log.warning(f"Bot API rate-limited (429) on {endpoint}. Retrying in {wait}s…")
                        await asyncio.sleep(wait)
                    else:
                        log.error(f"Async API request failed [{response.status}] on {endpoint}")
                        return None
            except aiohttp.ClientError as e:
                wait = 2 ** attempt
                log.error(f"Async API exception: {e}. Retrying in {wait}s…")
                await asyncio.sleep(wait)
        return None

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession()
        await self.load_system_config()
        redis_url = os.getenv("REDIS_URL")
        if redis_url:
            try:
                self.redis = redis.from_url(redis_url, decode_responses=True)
                await self.redis.ping()
                self.redis_available = True
                self.loop.create_task(self.listen_to_web_ui())
            except Exception as e:
                self.redis_available = False
        else:
            self.redis_available = False
        await self.load_extension("cogs.clash_cog")

    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        if not self.redis_available:
            now = time.time()
            if now - self._last_config_load > 30:
                try: await self.load_system_config(); self._last_config_load = now
                except Exception as e: log.error(f"Fallback config reload failed: {e}")
        prefix = self.active_prefix
        if message.content.startswith(prefix):
            cmd_name = message.content[len(prefix):].split()[0].lower()
            custom_cmd = await self.custom_cmds.find_one({"_id": cmd_name})
            if custom_cmd:
                await message.channel.send(custom_cmd["response"])
                return
        await self.process_commands(message)

    async def load_system_config(self):
        config_doc = await self.db["config"].find_one({"_id": "system_config_file"})
        if config_doc:
            self.active_prefix = config_doc.get("command_prefix", "!")
            self.war_channel_id = int(config_doc.get("war_channel_id") or 0)
        else:
            self.active_prefix = "!"
            self.war_channel_id = 0

    async def listen_to_web_ui(self):
        backoff, max_backoff = 5, 60
        while not self.is_closed():
            pubsub = None
            try:
                pubsub = self.redis.pubsub()
                await pubsub.subscribe("graveyard_bot_signals")
                backoff = 5
                async for msg in pubsub.listen():
                    if msg["type"] != "message": continue
                    try: await self._handle_redis_action(json.loads(msg["data"]))
                    except json.JSONDecodeError: pass
            except Exception as e:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
            finally:
                if pubsub:
                    try: await pubsub.close()
                    except Exception: pass

    async def _handle_redis_action(self, payload: dict):
        action = payload.get("action")
        if action == "RELOAD":
            await self.load_system_config()
        elif action == "RELOAD_COG":
            cog = payload.get("cog", "cogs.clash_cog")
            try: await self.reload_extension(cog)
            except Exception as e: log.error(f"❌ Cog reload failed: {e}")
        elif action == "MANUAL_HARVEST":
            cog = self.get_cog("ClashRoyale")
            if cog:
                log.info("⚡ Executing Manual Harvest trigger requested via UI...")
                # Call underlying loop coroutine directly
                self.loop.create_task(cog.daily_snapshot_loop.coro(cog))

    async def close(self):
        if self.http_session: await self.http_session.close()
        if self.redis_available: await self.redis.aclose()
        await super().close()

if __name__ == "__main__":
    app.config["RAW_CSV_TEMPLATE_FALLBACK"] = "Native field selector extraction logic active."
    bot = GraveyardBot()
    _bot_instance = bot
    threading.Thread(target=run_flask, daemon=True).start()
    bot.run(os.getenv("DISCORD_TOKEN"))