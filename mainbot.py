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
import importlib.metadata
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

# ---------------------------------------------------------------------------
# 2. DATA ENRICHMENT & STREAK LOGIC
# ---------------------------------------------------------------------------
def calculate_streak(battles):
    # Sort chronological (newest first)
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
        m['fame'] = war_parts.get(tag, {}).get('fame', 0)
        players.append(m)
    return sorted(players, key=lambda x: x.get('trophies', 0), reverse=True)

# ---------------------------------------------------------------------------
# 3. DIAGNOSTICS ENGINE
# ---------------------------------------------------------------------------
def get_system_health():
    try:
        mongo_check = mongo_client_sync.admin.command('ping')
        redis_check = redis_sync_client.ping()
        return {
            "MongoDB": "✅ Connected" if mongo_check else "❌ Failed",
            "Redis": "✅ Connected" if redis_check else "❌ Failed",
            "Templates": f"{len(_HTML_CACHE)} in cache",
            "Uptime": f"{int((time.time() - start_time)/60)} mins"
        }
    except Exception as e:
        log.exception("Health check failed")
        return {"Status": f"❌ Error checking health: {e}"}

start_time = time.time()

# ---------------------------------------------------------------------------
# UTILITY HELPERS
# ---------------------------------------------------------------------------
def clean_tag(tag: str) -> str:
    """Normalises Clash Royale tags to uppercase, stripping whitespace and '#'."""
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
    """Synchronous CR API fetcher with exponential back-off for rate limits."""
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


def get_user_guild_roles(token: str) -> list:
    url = f"https://discord.com/api/users/@me/guilds/{GUILD_ID}/member"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200:
            return r.json().get("roles", [])
        log.warning(f"Could not fetch guild roles — HTTP {r.status_code}")
    except Exception as e:
        log.error(f"Failed to fetch guild roles: {e}")
    return []


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

    # Hardcoded fallback override to bypass configuration lockout loops
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
    return template.render(**context)


# ---------------------------------------------------------------------------
# HTML TEMPLATE DEFAULTS
# ---------------------------------------------------------------------------
DEFAULT_ROSTER_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Graveyard Squad | Roster</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0b0c10; color: #c5c6c7; font-family: 'Segoe UI', system-ui, sans-serif; padding-bottom: 50px; }

  .hero { background: #111418; padding: 40px 20px; text-align: center; border-bottom: 1px solid #1e2530; }
  .hero h1 { font-size: 2rem; color: #fff; font-weight: 700; margin-bottom: 14px; }
  .hero h1 span { color: #f1c40f; }
  .hero-btns { display: flex; gap: 12px; justify-content: center; flex-wrap: wrap; margin-bottom: 16px; }
  .btn { padding: 10px 22px; border-radius: 6px; font-weight: 700; font-size: 0.9rem; text-decoration: none; display: inline-block; transition: opacity 0.2s; }
  .btn:hover { opacity: 0.85; }
  .btn-green { background: #2ecc71; color: #0b0c10; }
  .btn-discord { background: #5865F2; color: #fff; }
  .hero-sub { font-size: 0.85rem; color: #6b7785; }

  .container { max-width: 900px; margin: 36px auto; padding: 0 20px; display: flex; gap: 28px; flex-wrap: wrap; }
  .main-col { flex: 2; min-width: 320px; }
  .side-col { flex: 1; min-width: 260px; }

  h2 { color: #fff; font-size: 1.1rem; font-weight: 700; margin-bottom: 14px; padding-bottom: 8px; border-bottom: 1px solid #1e2530; }

  .player-card {
    background: #161b22; border: 1px solid #1e2530; border-radius: 10px;
    padding: 14px 18px; margin-bottom: 10px; display: flex;
    align-items: center; justify-content: space-between;
    text-decoration: none; transition: border-color 0.2s, transform 0.15s;
    gap: 12px; flex-wrap: wrap;
  }
  .player-card:hover { border-color: #45a29e; transform: translateY(-1px); }

  .p-left { display: flex; flex-direction: column; gap: 3px; }
  .p-name { font-size: 1rem; font-weight: 700; color: #fff; }
  .p-role { font-size: 0.75rem; color: #6b7785; }

  .p-right { display: flex; flex-direction: column; align-items: flex-end; gap: 5px; }
  .p-trophies { font-size: 1rem; font-weight: 700; color: #f1c40f; }
  .p-stats-row { display: flex; gap: 12px; font-size: 0.75rem; color: #6b7785; }
  .p-stats-row span { color: #a0aab5; }

  .hof-card { background: #161b22; border: 1px solid #1e2530; border-left: 3px solid var(--hof-color, #45a29e); border-radius: 8px; padding: 14px 16px; margin-bottom: 10px; }
  .hof-label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 1px; color: #6b7785; margin-bottom: 4px; font-weight: 700; }
  .hof-name { font-size: 1rem; color: #fff; font-weight: 700; margin-bottom: 2px; }
  .hof-stat { font-size: 0.85rem; color: var(--hof-color, #45a29e); font-weight: 600; }
</style>
</head>
<body>

<header class="hero">
  <h1>🛡️ <span>Graveyard</span> Clan Roster</h1>
  <div class="hero-btns">
    {% if session.get('is_admin_user') %}
      <a href="/admin" class="btn btn-green">💀 Go to HQ Control Panel</a>
    {% endif %}
    <a href="/login" class="btn btn-discord">Log in with Discord</a>
  </div>
  <div class="hero-sub">{{ players | length }} members &middot; Click a name to view their profile</div>
</header>

<div class="container">
  <div class="main-col">
    {% for p in players %}
    <a href="/player/{{ p.clean_tag }}" class="player-card">
      <div class="p-left">
        <div class="p-name cr-name">{{ p.name }}</div>
        <div class="p-role">
          {% if p.role == 'leader' %}Leader
          {% elif p.role == 'coLeader' %}CoLeader
          {% elif p.role == 'elder' %}Elder
          {% else %}Member{% endif %}
        </div>
      </div>
      <div class="p-right">
        <div class="p-trophies">🏆 {{ p.trophies }}</div>
        <div class="p-stats-row">
          <div>⭐ <span>{{ p.fame | default(0) }}</span></div>
          <div>🔥 <span>{{ p.current_streak | default(0) }}</span></div>
          <div>⚔️ <span>{{ p.warDayWins | default(0) }}</span></div>
        </div>
      </div>
    </a>
    {% endfor %}
  </div>

  <div class="side-col">
    <h2>Hall of Fame</h2>
    <div class="hof-card" style="--hof-color: #3498db;">
      <div class="hof-label">Top Pusher</div>
      <div class="hof-name cr-name">{{ top_pusher.name if top_pusher else 'N/A' }}</div>
      <div class="hof-stat">🏆 {{ top_pusher.trophies if top_pusher else 0 }} Trophies</div>
    </div>
    <div class="hof-card" style="--hof-color: #e74c3c;">
      <div class="hof-label">Highest Win Streak</div>
      <div class="hof-name cr-name">{{ top_streak.name if top_streak else 'N/A' }}</div>
      <div class="hof-stat">🔥 {{ top_streak.current_streak if top_streak else 0 }} Wins</div>
    </div>
    <div class="hof-card" style="--hof-color: #f1c40f;">
      <div class="hof-label">War Legend</div>
      <div class="hof-name cr-name">{{ top_war.name if top_war else 'N/A' }}</div>
      <div class="hof-stat">⚔️ {{ top_war.warDayWins if top_war else 0 }} Lifetime Wins</div>
    </div>
  </div>
</div>

<script>
  document.querySelectorAll('.cr-name').forEach(el => {
    el.innerHTML = el.innerHTML.replace(/<c\d+>|<\/c>/gi, '');
  });
</script>
</body>
</html>
"""

DEFAULT_LINK_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Link Account</title>
    <style>
        body { background: #121212; color: white; font-family: 'Segoe UI', sans-serif; text-align: center; padding: 50px; }
        .box { background: #1e1e1e; padding: 40px; border-radius: 10px; max-width: 400px; margin: auto; border: 1px solid #333; }
        h2 { color: #f1c40f; margin-bottom: 10px; }
        input { width: 100%; padding: 12px; margin: 15px 0; background: #2a2a2a; border: 1px solid #444; color: white; border-radius: 5px; font-size: 1rem;}
        button { width: 100%; background: #5865F2; color: white; padding: 12px; border: none; border-radius: 5px; font-weight: bold; cursor: pointer; font-size: 1rem;}
        button:hover { background: #4752C4; }
        .error { color: #e74c3c; margin-bottom: 15px; font-weight: bold; }
    </style>
</head>
<body>
    <div class="box">
        <h2>Link Clash Royale Tag</h2>
        <p style="color: #aaa; margin-bottom: 20px;">Authenticated as <strong>@{{ name }}</strong></p>
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <form method="POST">
            <input type="text" name="tag" placeholder="e.g. #2Y8JLYPQ2" required>
            <button type="submit">Link to Discord</button>
        </form>
    </div>
</body>
</html>
"""

DEFAULT_PLAYER_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>{{ data.name }} - Analytics</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: #0f0f0f; color: #eee; font-family: 'Segoe UI', sans-serif; padding: 40px 30px; max-width: 1000px; margin: auto; }
        a.back { color: #f1c40f; text-decoration: none; font-weight: bold; font-size: 0.9rem; }
        a.back:hover { text-decoration: underline; }
        .header { border-bottom: 2px solid #f1c40f; padding-bottom: 14px; margin: 20px 0 30px; display: flex; justify-content: space-between; align-items: flex-end; flex-wrap: wrap; gap: 10px; }
        .header h1 { font-size: 1.8rem; }
        .header .tag { color: #5dade2; font-size: 1rem; font-weight: normal; margin-left: 8px; }
        .header .clan-badge { background: #1e1e1e; border: 1px solid #333; border-radius: 6px; padding: 6px 14px; font-size: 0.85rem; color: #ccc; }
        .header .clan-badge strong { color: #f1c40f; }
        h2 { color: #f1c40f; margin: 30px 0 14px; font-size: 1.1rem; text-transform: uppercase; letter-spacing: 1px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; }
        .stat-box { background: #1a1a1a; padding: 18px 20px; border-radius: 10px; border: 1px solid #2a2a2a; }
        .label { color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
        .value { font-size: 1.6rem; font-weight: bold; color: #f1c40f; }
        .value.blue  { color: #5dade2; }
        .value.green { color: #2ecc71; }
        .value.red   { color: #e74c3c; }
        .value.white { color: #eee; }
        .deck-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
        @media (max-width: 600px) { .deck-grid { grid-template-columns: repeat(2, 1fr); } }
        .card-box { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 8px; padding: 14px 10px; text-align: center; }
        .card-box .card-name { font-size: 0.82rem; font-weight: bold; margin-bottom: 5px; }
        .card-box .card-level { display: inline-block; background: #2a2a2a; color: #aaa; font-size: 0.75rem; border-radius: 4px; padding: 2px 7px; }
        .card-box.maxed { border-color: #f1c40f !important; }
        .card-box.maxed .card-level { color: #f1c40f; }
    </style>
</head>
<body>
    <a class="back" href="/">← Back to Roster</a>
    <div class="header">
        <div><h1>{{ data.name }}<span class="tag">{{ data.tag }}</span></h1></div>
        {% if data.clan %}
            <div class="clan-badge">🛡️ <strong>{{ data.clan.name }}</strong> &nbsp;·&nbsp; {{ data.role | replace('_', ' ') | title }}</div>
        {% else %}
            <div class="clan-badge">No Clan</div>
        {% endif %}
    </div>

    <h2>📈 Progression</h2>
    <div class="grid">
        <div class="stat-box"><div class="label">XP Level</div><div class="value white">⭐ {{ data.expLevel }}</div></div>
        <div class="stat-box"><div class="label">Current Trophies</div><div class="value blue">🏆 {{ data.trophies }}</div></div>
        <div class="stat-box"><div class="label">Best Trophies</div><div class="value blue">🏅 {{ data.bestTrophies }}</div></div>
        <div class="stat-box"><div class="label">Arena</div><div class="value white" style="font-size:1rem; padding-top:4px;">{{ data.arena.name if data.arena else '—' }}</div></div>
    </div>

    <h2>⚔️ Battle Stats</h2>
    <div class="grid">
        <div class="stat-box"><div class="label">Total Wins</div><div class="value green">{{ data.wins }}</div></div>
        <div class="stat-box"><div class="label">Losses</div><div class="value red">{{ data.losses }}</div></div>
        <div class="stat-box"><div class="label">3-Crown Wins</div><div class="value green">👑 {{ data.threeCrownWins }}</div></div>
        <div class="stat-box"><div class="label">Total Battles</div><div class="value white">{{ data.battleCount }}</div></div>
        <div class="stat-box"><div class="label">Win Rate</div><div class="value {% if data.battleCount > 0 and (data.wins / data.battleCount * 100) >= 50 %}green{% else %}red{% endif %}">
            {% if data.battleCount > 0 %}{{ "%.1f" | format(data.wins / data.battleCount * 100) }}%{% else %}—{% endif %}
        </div></div>
    </div>

    <h2>🎁 Social & Misc</h2>
    <div class="grid">
        <div class="stat-box"><div class="label">Total Donations</div><div class="value white">{{ data.totalDonations }}</div></div>
        <div class="stat-box"><div class="label">War Day Wins</div><div class="value white">{{ data.warDayWins }}</div></div>
    </div>

    <h2>🃏 Current Battle Deck</h2>
    <div class="deck-grid">
        {% for card in data.currentDeck %}
            <div class="card-box {% if card.level >= max_lvl %}maxed{% endif %}">
                <div class="card-name">{{ card.name }}</div>
                <span class="card-level">Lvl {{ card.level }}{% if card.level >= max_lvl %} ✓{% endif %}</span>
            </div>
        {% endfor %}
    </div>
</body>
</html>
"""

DEFAULT_ADMIN_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Graveyard HQ</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow+Condensed:wght@400;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:       #080a0f;
    --surface:  #0d1117;
    --panel:    #111820;
    --border:   #1e2d3d;
    --accent:   #00e5ff;
    --accent2:  #ff3d71;
    --ok:       #00e096;
    --warn:     #ffaa00;
    --err:      #ff3d71;
    --text:     #c9d1d9;
    --dim:      #4a5568;
    --font-mono: 'Share Tech Mono', monospace;
    --font-ui:   'Barlow Condensed', sans-serif;
  }
 
  * { box-sizing: border-box; margin: 0; padding: 0; }
 
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-ui);
    font-size: 15px;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
  }
 
  /* ── TOP BAR ── */
  .topbar {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 12px 24px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    z-index: 100;
  }
  .topbar-title {
    font-size: 22px;
    font-weight: 700;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: var(--accent);
    text-shadow: 0 0 18px rgba(0,229,255,0.35);
    flex: 1;
  }
  .topbar-title span { color: var(--dim); font-weight: 400; }
  .topbar-badge {
    font-family: var(--font-mono);
    font-size: 11px;
    padding: 3px 10px;
    border-radius: 3px;
    background: rgba(0,229,255,0.08);
    border: 1px solid var(--accent);
    color: var(--accent);
    letter-spacing: 1px;
  }
  .topbar a {
    color: var(--dim);
    text-decoration: none;
    font-size: 13px;
    letter-spacing: 1px;
    text-transform: uppercase;
    transition: color .2s;
  }
  .topbar a:hover { color: var(--text); }
 
  /* ── LAYOUT ── */
  .shell {
    display: flex;
    flex: 1;
    height: calc(100vh - 53px);
  }
 
  /* ── SIDEBAR ── */
  .sidebar {
    width: 200px;
    background: var(--surface);
    border-right: 1px solid var(--border);
    padding: 20px 0;
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .nav-section {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 2px;
    color: var(--dim);
    padding: 14px 20px 6px;
    text-transform: uppercase;
  }
  .nav-btn {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 20px;
    background: none;
    border: none;
    color: var(--dim);
    font-family: var(--font-ui);
    font-size: 14px;
    font-weight: 600;
    letter-spacing: .5px;
    text-transform: uppercase;
    cursor: pointer;
    text-align: left;
    width: 100%;
    border-left: 3px solid transparent;
    transition: all .15s;
  }
  .nav-btn:hover { color: var(--text); background: rgba(255,255,255,0.03); }
  .nav-btn.active {
    color: var(--accent);
    border-left-color: var(--accent);
    background: rgba(0,229,255,0.06);
  }
  .nav-icon { font-size: 16px; width: 20px; text-align: center; }
 
  /* ── MAIN ── */
  .main {
    flex: 1;
    overflow-y: auto;
    padding: 28px 32px;
  }
 
  /* ── TAB PANES ── */
  .tab-pane { display: none; }
  .tab-pane.active { display: block; }
 
  /* ── PAGE HEADER ── */
  .page-header {
    display: flex;
    align-items: baseline;
    gap: 14px;
    margin-bottom: 24px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 14px;
  }
  .page-title {
    font-size: 28px;
    font-weight: 700;
    letter-spacing: 1px;
    text-transform: uppercase;
    color: #fff;
  }
  .page-sub {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--dim);
    letter-spacing: 1px;
  }
 
  /* ── STAT ROW ── */
  .stat-row {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 14px;
    margin-bottom: 28px;
  }
  .stat-card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 16px 18px;
    position: relative;
    overflow: hidden;
  }
  .stat-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: var(--accent);
    opacity: .6;
  }
  .stat-card.ok::before  { background: var(--ok);   }
  .stat-card.warn::before{ background: var(--warn);  }
  .stat-card.err::before { background: var(--err);   }
  .stat-label {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 2px;
    color: var(--dim);
    text-transform: uppercase;
    margin-bottom: 8px;
  }
  .stat-value {
    font-family: var(--font-mono);
    font-size: 26px;
    font-weight: 700;
    color: #fff;
    line-height: 1;
  }
  .stat-value.ok   { color: var(--ok);   }
  .stat-value.warn { color: var(--warn); }
  .stat-value.err  { color: var(--err);  }
  .stat-note {
    font-size: 11px;
    color: var(--dim);
    margin-top: 5px;
  }
 
  /* ── DIAG GRID ── */
  .diag-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: 16px;
    margin-bottom: 28px;
  }
  .diag-card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
  }
  .diag-card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    background: rgba(255,255,255,0.02);
  }
  .diag-card-title {
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: #fff;
  }
  .status-pill {
    font-family: var(--font-mono);
    font-size: 10px;
    padding: 3px 9px;
    border-radius: 20px;
    letter-spacing: 1px;
    text-transform: uppercase;
    font-weight: 700;
  }
  .pill-ok   { background: rgba(0,224,150,0.12); color: var(--ok);   border: 1px solid rgba(0,224,150,0.3); }
  .pill-warn { background: rgba(255,170,0,0.12);  color: var(--warn); border: 1px solid rgba(255,170,0,0.3); }
  .pill-err  { background: rgba(255,61,113,0.12); color: var(--err);  border: 1px solid rgba(255,61,113,0.3); }
  .pill-loading { background: rgba(255,255,255,0.05); color: var(--dim); border: 1px solid var(--border); }
 
  .diag-body { padding: 14px 16px; }
  .diag-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 6px 0;
    border-bottom: 1px solid rgba(255,255,255,0.04);
    font-family: var(--font-mono);
    font-size: 12px;
  }
  .diag-row:last-child { border-bottom: none; }
  .diag-key   { color: var(--dim); }
  .diag-val   { color: var(--text); text-align: right; }
  .diag-val.ok   { color: var(--ok);   }
  .diag-val.warn { color: var(--warn); }
  .diag-val.err  { color: var(--err);  }
 
  /* ── SECTION DIVIDER ── */
  .section-label {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 3px;
    text-transform: uppercase;
    color: var(--dim);
    margin-bottom: 12px;
    margin-top: 24px;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .section-label::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--border);
  }
 
  /* ── REFRESH / CONTROLS ── */
  .toolbar {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 24px;
  }
  .btn-refresh {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 18px;
    background: rgba(0,229,255,0.08);
    border: 1px solid var(--accent);
    border-radius: 4px;
    color: var(--accent);
    font-family: var(--font-ui);
    font-weight: 700;
    font-size: 13px;
    letter-spacing: 1px;
    text-transform: uppercase;
    cursor: pointer;
    transition: all .2s;
  }
  .btn-refresh:hover { background: rgba(0,229,255,0.16); }
  .btn-refresh:disabled { opacity: .4; cursor: not-allowed; }
  .btn-danger {
    display: flex; align-items: center; gap: 8px;
    padding: 8px 18px;
    background: rgba(255,61,113,0.08);
    border: 1px solid var(--err);
    border-radius: 4px;
    color: var(--err);
    font-family: var(--font-ui);
    font-weight: 700;
    font-size: 13px;
    letter-spacing: 1px;
    text-transform: uppercase;
    cursor: pointer;
    transition: all .2s;
  }
  .btn-danger:hover { background: rgba(255,61,113,0.16); }
 
  .last-refresh {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--dim);
    margin-left: auto;
  }
 
  /* ── SPINNER ── */
  @keyframes spin { to { transform: rotate(360deg); } }
  .spin { display: inline-block; animation: spin .8s linear infinite; }
 
  /* ── WAR TAB ── */
  .war-table {
    width: 100%;
    border-collapse: collapse;
    font-family: var(--font-mono);
    font-size: 12px;
  }
  .war-table th {
    text-align: left;
    font-size: 10px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: var(--dim);
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
  }
  .war-table td {
    padding: 9px 12px;
    border-bottom: 1px solid rgba(255,255,255,0.04);
    color: var(--text);
  }
  .war-table tr:hover td { background: rgba(255,255,255,0.03); }
  .bar-wrap {
    width: 120px;
    height: 6px;
    background: var(--border);
    border-radius: 3px;
    overflow: hidden;
  }
  .bar-fill {
    height: 100%;
    border-radius: 3px;
    background: var(--accent);
    transition: width .4s ease;
  }
 
  /* ── TOAST ── */
  .toast-wrap {
    position: fixed;
    bottom: 24px; right: 24px;
    display: flex; flex-direction: column; gap: 8px;
    z-index: 9999;
  }
  .toast {
    padding: 10px 18px;
    border-radius: 5px;
    font-family: var(--font-mono);
    font-size: 12px;
    border: 1px solid;
    animation: fadeIn .25s ease;
    cursor: pointer;
  }
  @keyframes fadeIn { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:translateY(0); } }
  .toast-ok   { background: rgba(0,224,150,0.1); border-color: var(--ok);  color: var(--ok);  }
  .toast-err  { background: rgba(255,61,113,0.1); border-color: var(--err); color: var(--err); }
  .toast-info { background: rgba(0,229,255,0.1);  border-color: var(--accent); color: var(--accent); }
 
  /* ── SCROLLBAR ── */
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
 
  /* ── LOG VIEWER ── */
  .log-box {
    background: #050709;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 14px;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--dim);
    max-height: 240px;
    overflow-y: auto;
    line-height: 1.7;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .log-line-ok   { color: var(--ok);   }
  .log-line-warn { color: var(--warn); }
  .log-line-err  { color: var(--err);  }
  .log-line-info { color: var(--accent); }
</style>
</head>
<body>
 
<!-- TOP BAR -->
<header class="topbar">
  <div class="topbar-title">☠ Graveyard <span>HQ</span></div>
  <span class="topbar-badge" id="clan-tag-badge">CLAN: #{{ clan_tag }}</span>
  <a href="/">← Roster</a>
  <a href="/logout">Logout</a>
</header>
 
<div class="shell">
  <!-- SIDEBAR -->
  <nav class="sidebar">
    <div class="nav-section">Navigation</div>
    <button class="nav-btn active" onclick="showTab('diag', this)">
      <span class="nav-icon">🔍</span>Diagnostics
    </button>
    <button class="nav-btn" onclick="showTab('war', this)">
      <span class="nav-icon">⚔️</span>War Monitor
    </button>
    <button class="nav-btn" onclick="showTab('cache', this)">
      <span class="nav-icon">⚡</span>Cache
    </button>
    <button class="nav-btn" onclick="showTab('harvest', this)">
      <span class="nav-icon">📡</span>Harvest Log
    </button>
    
    <!-- ADD THIS BUTTON -->
    <button class="nav-btn" onclick="showTab('editor', this)">
      <span class="nav-icon">🎨</span>UI Editor
    </button>

    <div class="nav-section">Danger Zone</div>
    <button class="nav-btn" onclick="showTab('admin', this)">
      <span class="nav-icon">⚙️</span>Admin Tools
    </button>
  </nav>
 
  <!-- MAIN CONTENT -->
  <main class="main">
    <!-- ═══════════════════════ UI EDITOR TAB ═══════════════════════ -->
    <div class="tab-pane" id="tab-editor">
      <div class="page-header">
        <div class="page-title">UI Editor</div>
        <div class="page-sub">Live deploy custom HTML to MongoDB</div>
      </div>
      <div class="diag-card" style="padding: 24px; background: var(--panel);">
        <form action="/admin/update-html" method="POST">
          <label style="color: var(--dim); font-family: var(--font-mono); font-size: 12px; text-transform: uppercase; letter-spacing: 1px;">Target Template</label><br>
          <input type="text" name="template_name" placeholder="e.g., roster, player, admin, link" style="margin-top: 8px; margin-bottom: 24px; padding: 10px; background: #050709; color: #fff; border: 1px solid var(--border); border-radius: 4px; width: 100%; max-width: 300px; font-family: var(--font-mono);"><br>
          
          <label style="color: var(--dim); font-family: var(--font-mono); font-size: 12px; text-transform: uppercase; letter-spacing: 1px;">HTML Source Code</label><br>
          <textarea name="html_content" rows="25" style="width: 100%; margin-top: 8px; margin-bottom: 24px; padding: 16px; background: #050709; color: var(--accent); font-family: var(--font-mono); font-size: 13px; border: 1px solid var(--border); border-radius: 4px; line-height: 1.5;"></textarea><br>
          
          <button type="submit" class="btn-refresh" style="display: inline-flex; border-color: var(--ok); color: var(--ok); background: rgba(0,224,150,0.08);">
            🚀 Deploy Update Live
          </button>
        </form>
      </div>
    </div>
    <!-- ═══════════════════════ DIAGNOSTICS TAB ═══════════════════════ -->
    <div class="tab-pane active" id="tab-diag">
      <div class="page-header">
        <div class="page-title">Diagnostics</div>
        <div class="page-sub" id="diag-env">Initializing...</div>
      </div>
 
      <div class="toolbar">
        <button class="btn-refresh" id="btn-diag-refresh" onclick="loadDiagnostics()">
          <span id="diag-spin">↻</span> Refresh
        </button>
        <span class="last-refresh" id="diag-last-refresh">Never refreshed</span>
      </div>
 
      <!-- Quick Status Row -->
      <div class="stat-row" id="stat-row">
        <div class="stat-card" id="sc-redis">
          <div class="stat-label">Redis</div>
          <div class="stat-value">—</div>
          <div class="stat-note">Checking...</div>
        </div>
        <div class="stat-card" id="sc-mongo">
          <div class="stat-label">MongoDB</div>
          <div class="stat-value">—</div>
          <div class="stat-note">Checking...</div>
        </div>
        <div class="stat-card" id="sc-crapi">
          <div class="stat-label">CR API</div>
          <div class="stat-value">—</div>
          <div class="stat-note">Checking...</div>
        </div>
        <div class="stat-card" id="sc-cache-keys">
          <div class="stat-label">Cache Keys</div>
          <div class="stat-value">—</div>
          <div class="stat-note">Redis key count</div>
        </div>
        <div class="stat-card" id="sc-harvest">
          <div class="stat-label">Last Harvest</div>
          <div class="stat-value" style="font-size:15px">—</div>
          <div class="stat-note">Snapshot timestamp</div>
        </div>
        <div class="stat-card" id="sc-warmup">
          <div class="stat-label">Warmup</div>
          <div class="stat-value">—</div>
          <div class="stat-note">Active warmups</div>
        </div>
      </div>
 
      <!-- Detailed Cards -->
      <div class="section-label">Infrastructure</div>
      <div class="diag-grid">
 
        <!-- Redis -->
        <div class="diag-card">
          <div class="diag-card-header">
            <div class="diag-card-title">⚡ Redis</div>
            <span class="status-pill pill-loading" id="pill-redis">LOADING</span>
          </div>
          <div class="diag-body" id="body-redis">
            <div class="diag-row"><span class="diag-key">Status</span><span class="diag-val">—</span></div>
          </div>
        </div>
 
        <!-- MongoDB -->
        <div class="diag-card">
          <div class="diag-card-header">
            <div class="diag-card-title">🍃 MongoDB</div>
            <span class="status-pill pill-loading" id="pill-mongo">LOADING</span>
          </div>
          <div class="diag-body" id="body-mongo">
            <div class="diag-row"><span class="diag-key">Status</span><span class="diag-val">—</span></div>
          </div>
        </div>
 
        <!-- CR API -->
        <div class="diag-card">
          <div class="diag-card-header">
            <div class="diag-card-title">🃏 CR API</div>
            <span class="status-pill pill-loading" id="pill-crapi">LOADING</span>
          </div>
          <div class="diag-body" id="body-crapi">
            <div class="diag-row"><span class="diag-key">Status</span><span class="diag-val">—</span></div>
          </div>
        </div>
 
        <!-- Bot Process -->
        <div class="diag-card">
          <div class="diag-card-header">
            <div class="diag-card-title">🤖 Bot Process</div>
            <span class="status-pill pill-loading" id="pill-bot">LOADING</span>
          </div>
          <div class="diag-body" id="body-bot">
            <div class="diag-row"><span class="diag-key">Status</span><span class="diag-val">—</span></div>
          </div>
        </div>
 
      </div>
 
      <div class="section-label">Cache & Data</div>
      <div class="diag-grid">
 
        <!-- Cache Stats -->
        <div class="diag-card">
          <div class="diag-card-header">
            <div class="diag-card-title">📊 Cache Stats</div>
            <span class="status-pill pill-loading" id="pill-cache">LIVE</span>
          </div>
          <div class="diag-body" id="body-cache">
            <div class="diag-row"><span class="diag-key">Loading...</span><span class="diag-val">—</span></div>
          </div>
        </div>
 
        <!-- Harvest -->
        <div class="diag-card">
          <div class="diag-card-header">
            <div class="diag-card-title">📡 Harvest Status</div>
            <span class="status-pill pill-loading" id="pill-harvest">LOADING</span>
          </div>
          <div class="diag-body" id="body-harvest">
            <div class="diag-row"><span class="diag-key">Loading...</span><span class="diag-val">—</span></div>
          </div>
        </div>
 
        <!-- Active Tasks -->
        <div class="diag-card">
          <div class="diag-card-header">
            <div class="diag-card-title">⏳ Background Tasks</div>
            <span class="status-pill pill-loading" id="pill-tasks">LOADING</span>
          </div>
          <div class="diag-body" id="body-tasks">
            <div class="diag-row"><span class="diag-key">Loading...</span><span class="diag-val">—</span></div>
          </div>
        </div>
 
        <!-- Templates -->
        <div class="diag-card">
          <div class="diag-card-header">
            <div class="diag-card-title">🖼 HTML Templates</div>
            <span class="status-pill pill-loading" id="pill-templates">LOADING</span>
          </div>
          <div class="diag-body" id="body-templates">
            <div class="diag-row"><span class="diag-key">Loading...</span><span class="diag-val">—</span></div>
          </div>
        </div>
 
      </div>
 
      <div class="section-label">Event Log</div>
      <div class="log-box" id="diag-log">Waiting for data...\n</div>
    </div>
 
    <!-- ═══════════════════════ WAR TAB ═══════════════════════ -->
    <div class="tab-pane" id="tab-war">
      <div class="page-header">
        <div class="page-title">War Monitor</div>
        <div class="page-sub">Current River Race</div>
      </div>
      <div class="toolbar">
        <button class="btn-refresh" onclick="loadWar()">↻ Refresh</button>
        <span class="last-refresh" id="war-last-refresh"></span>
      </div>
      <div id="war-content">
        <div style="color:var(--dim); font-family:var(--font-mono); font-size:12px;">
          Click refresh to load war data.
        </div>
      </div>
    </div>
 
    <!-- ═══════════════════════ CACHE TAB ═══════════════════════ -->
    <div class="tab-pane" id="tab-cache">
      <div class="page-header">
        <div class="page-title">Cache Inspector</div>
        <div class="page-sub">Live Redis / Mongo fallback state</div>
      </div>
      <div class="toolbar">
        <button class="btn-refresh" onclick="loadDiagnostics()">↻ Refresh</button>
      </div>
      <div class="diag-grid" id="cache-detail-grid">
        <div style="color:var(--dim); font-family:var(--font-mono); font-size:12px;">Load diagnostics first.</div>
      </div>
    </div>
 
    <!-- ═══════════════════════ HARVEST LOG TAB ═══════════════════════ -->
    <div class="tab-pane" id="tab-harvest">
      <div class="page-header">
        <div class="page-title">Harvest Log</div>
        <div class="page-sub">daily_snapshot_loop history</div>
      </div>
      <div class="toolbar">
        <button class="btn-refresh" onclick="loadDiagnostics()">↻ Refresh</button>
      </div>
      <div id="harvest-detail">
        <div style="color:var(--dim); font-family:var(--font-mono); font-size:12px;">Load diagnostics first.</div>
      </div>
    </div>
 
    <!-- ═══════════════════════ ADMIN TOOLS TAB ═══════════════════════ -->
    <div class="tab-pane" id="tab-admin">
      <div class="page-header">
        <div class="page-title">Admin Tools</div>
        <div class="page-sub">Careful in here</div>
      </div>
      <div class="diag-grid">
 
        <div class="diag-card">
          <div class="diag-card-header">
            <div class="diag-card-title">🖼 Template Management</div>
          </div>
          <div class="diag-body" style="display:flex;flex-direction:column;gap:10px;">
            <p style="font-size:12px;color:var(--dim);font-family:var(--font-mono);">
              Resets all HTML templates in MongoDB and clears the in-memory cache. 
              Pages will fall back to Python defaults until re-populated.
            </p>
            <button class="btn-danger" onclick="confirmReset()">⚠ Reset All Templates</button>
          </div>
        </div>
 
        <div class="diag-card">
          <div class="diag-card-header">
            <div class="diag-card-title">🔄 Cache Flush</div>
          </div>
          <div class="diag-body" style="display:flex;flex-direction:column;gap:10px;">
            <p style="font-size:12px;color:var(--dim);font-family:var(--font-mono);">
              Flush all Redis cache keys for this clan. The next command will re-warm from the CR API.
              Use when data looks stale.
            </p>
            <button class="btn-danger" onclick="confirmFlushCache()">⚠ Flush CR Cache</button>
          </div>
        </div>
 
        <div class="diag-card">
          <div class="diag-card-header">
            <div class="diag-card-title">🩺 Health Check</div>
          </div>
          <div class="diag-body" style="display:flex;flex-direction:column;gap:10px;">
            <p style="font-size:12px;color:var(--dim);font-family:var(--font-mono);">
              Raw JSON from /admin/diagnostics. Useful when building new features.
            </p>
            <button class="btn-refresh" onclick="window.open('/admin/diagnostics','_blank')">Open Raw JSON ↗</button>
          </div>
        </div>
 
      </div>
    </div>
 
  </main>
</div>
 
<!-- TOAST CONTAINER -->
<div class="toast-wrap" id="toast-wrap"></div>
 
<script>
// ─── TAB SWITCHING ───────────────────────────────────────────────────────
function showTab(name, btn) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  const pane = document.getElementById('tab-' + name);
  if (pane) pane.classList.add('active');
  if (btn)  btn.classList.add('active');
}
 
// ─── TOAST ────────────────────────────────────────────────────────────────
function toast(msg, type='info', duration=3500) {
  const wrap = document.getElementById('toast-wrap');
  const t = document.createElement('div');
  t.className = `toast toast-${type}`;
  t.textContent = msg;
  t.onclick = () => t.remove();
  wrap.appendChild(t);
  setTimeout(() => t.remove(), duration);
}
 
// ─── LOG HELPER ───────────────────────────────────────────────────────────
const _logLines = [];
function appendLog(msg, level='info') {
  const ts = new Date().toLocaleTimeString();
  _logLines.push({ ts, msg, level });
  if (_logLines.length > 200) _logLines.shift();
  const box = document.getElementById('diag-log');
  box.innerHTML = _logLines.map(l =>
    `<span class="log-line-${l.level}">[${l.ts}] ${escHtml(l.msg)}</span>`
  ).join('\n');
  box.scrollTop = box.scrollHeight;
}
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
 
// ─── PILL HELPER ─────────────────────────────────────────────────────────
function setPill(id, text, type) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = `status-pill pill-${type}`;
  el.textContent = text;
}
 
// ─── STAT CARD HELPER ─────────────────────────────────────────────────────
function setStatCard(id, value, note, status) {
  const card = document.getElementById(id);
  if (!card) return;
  card.className = `stat-card ${status}`;
  card.querySelector('.stat-value').className = `stat-value ${status}`;
  card.querySelector('.stat-value').textContent = value;
  card.querySelector('.stat-note').textContent  = note;
}
 
// ─── DIAG BODY HELPER ─────────────────────────────────────────────────────
function renderRows(rows) {
  return rows.map(([k, v, cls='']) =>
    `<div class="diag-row">
      <span class="diag-key">${escHtml(k)}</span>
      <span class="diag-val ${cls}">${escHtml(String(v))}</span>
    </div>`
  ).join('');
}
 
// ─── LOAD DIAGNOSTICS ────────────────────────────────────────────────────
async function loadDiagnostics() {
  const btn = document.getElementById('btn-diag-refresh');
  const spin = document.getElementById('diag-spin');
  btn.disabled = true;
  spin.className = 'spin';
  appendLog('Fetching /admin/diagnostics...', 'info');
 
  try {
    const resp = await fetch('/admin/diagnostics');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const d = await resp.json();
    appendLog(`Response received (HTTP 200)`, 'ok');
    renderDiagnostics(d);
    document.getElementById('diag-last-refresh').textContent =
      'Last refresh: ' + new Date().toLocaleTimeString();
  } catch(e) {
    appendLog(`Error: ${e.message}`, 'err');
    toast('Failed to load diagnostics: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
    spin.className = '';
  }
}
 
function renderDiagnostics(d) {
  // ── ENV ──
  document.getElementById('diag-env').textContent =
    `v${d.version || '?'} · ${d.environment || 'unknown'} · ${d.hostname || ''}`;
 
  // ── REDIS ──
  const redis = d.redis || {};
  const redisOk = redis.status === 'ok';
  setPill('pill-redis', redisOk ? 'ONLINE' : 'OFFLINE', redisOk ? 'ok' : 'err');
  setStatCard('sc-redis', redisOk ? '✓' : '✗', redis.ping_ms != null ? `${redis.ping_ms}ms ping` : 'unreachable', redisOk ? 'ok' : 'err');
  document.getElementById('body-redis').innerHTML = renderRows([
    ['Status',      redis.status || 'unknown',  redisOk ? 'ok' : 'err'],
    ['Ping',        redis.ping_ms != null ? redis.ping_ms + ' ms' : 'N/A'],
    ['Used Memory', redis.used_memory || 'N/A'],
    ['Total Keys',  redis.total_keys ?? 'N/A'],
    ['Mode',        redis.mode || 'N/A'],
    ['Version',     redis.redis_version || 'N/A'],
  ]);
  appendLog(`Redis: ${redis.status} ${redis.ping_ms != null ? '(' + redis.ping_ms + 'ms)' : ''}`, redisOk ? 'ok' : 'err');
 
  // ── MONGO ──
  const mongo = d.mongo || {};
  const mongoOk = mongo.status === 'ok';
  setPill('pill-mongo', mongoOk ? 'ONLINE' : 'OFFLINE', mongoOk ? 'ok' : 'err');
  setStatCard('sc-mongo', mongoOk ? '✓' : '✗', mongo.ping_ms != null ? `${mongo.ping_ms}ms ping` : 'unreachable', mongoOk ? 'ok' : 'err');
  document.getElementById('body-mongo').innerHTML = renderRows([
    ['Status',        mongo.status || 'unknown',  mongoOk ? 'ok' : 'err'],
    ['Ping',          mongo.ping_ms != null ? mongo.ping_ms + ' ms' : 'N/A'],
    ['DB Name',       mongo.db_name || 'N/A'],
    ['Collections',   (mongo.collections || []).join(', ') || 'N/A'],
    ['Snapshots',     mongo.snapshot_count ?? 'N/A'],
    ['Battle Docs',   mongo.battle_count ?? 'N/A'],
    ['Profile Docs',  mongo.profile_count ?? 'N/A'],
  ]);
  appendLog(`MongoDB: ${mongo.status}`, mongoOk ? 'ok' : 'err');
 
  // ── CR API ──
  const api = d.cr_api || {};
  const apiOk = api.status === 'ok';
  const apiWarn = api.status === 'rate_limited';
  const apiCls = apiOk ? 'ok' : apiWarn ? 'warn' : 'err';
  setPill('pill-crapi', api.status?.toUpperCase() || 'UNKNOWN', apiCls);
  setStatCard('sc-crapi', api.status_code || '—', api.latency_ms != null ? `${api.latency_ms}ms` : 'unreachable', apiCls);
  document.getElementById('body-crapi').innerHTML = renderRows([
    ['Status',          api.status || 'unknown', apiCls],
    ['HTTP Code',       api.status_code ?? 'N/A'],
    ['Latency',         api.latency_ms != null ? api.latency_ms + ' ms' : 'N/A'],
    ['Rate Limit Left', api.rate_limit_remaining ?? 'N/A'],
    ['Endpoint Tested', api.endpoint_tested || 'N/A'],
  ]);
  appendLog(`CR API: ${api.status} (HTTP ${api.status_code})`, apiCls);
 
  // ── BOT ──
  const bot = d.bot || {};
  const botOk = bot.connected === true;
  setPill('pill-bot', botOk ? 'CONNECTED' : 'OFFLINE', botOk ? 'ok' : 'err');
  document.getElementById('body-bot').innerHTML = renderRows([
    ['Discord WS',    bot.connected ? 'Connected' : 'Disconnected', bot.connected ? 'ok' : 'err'],
    ['Latency',       bot.latency_ms != null ? bot.latency_ms + ' ms' : 'N/A'],
    ['Guilds',        bot.guild_count ?? 'N/A'],
    ['Uptime',        bot.uptime || 'N/A'],
    ['Active Prefix', bot.prefix || 'N/A'],
  ]);
 
  // ── CACHE STATS ──
  const cache = d.cache || {};
  const totalKeys = cache.total_keys ?? 0;
  setStatCard('sc-cache-keys', totalKeys, 'keys in store', totalKeys > 0 ? 'ok' : 'warn');
  setPill('pill-cache', 'LIVE', 'ok');
  document.getElementById('body-cache').innerHTML = renderRows([
    ['Backend',        cache.backend || 'unknown'],
    ['Total Keys',     cache.total_keys ?? 0],
    ['Player Keys',    cache.player_keys ?? 0],
    ['Clan Keys',      cache.clan_keys ?? 0],
    ['Battlelog Keys', cache.battlelog_keys ?? 0],
    ['War Keys',       cache.war_keys ?? 0],
    ['Card List Key',  cache.cards_cached ? 'present' : 'missing', cache.cards_cached ? 'ok' : 'warn'],
    ['HTML Cache',     cache.html_cache_entries ?? 0],
  ]);
  // Populate cache inspector tab
  document.getElementById('cache-detail-grid').innerHTML = `
    <div class="diag-card">
      <div class="diag-card-header"><div class="diag-card-title">📦 Key Breakdown</div></div>
      <div class="diag-body">${renderRows([
        ['Backend',        cache.backend || 'unknown'],
        ['Total Keys',     cache.total_keys ?? 0],
        ['Player Keys',    cache.player_keys ?? 0],
        ['Clan Keys',      cache.clan_keys ?? 0],
        ['Battlelog Keys', cache.battlelog_keys ?? 0],
        ['War Keys',       cache.war_keys ?? 0],
        ['Card Cache',     cache.cards_cached ? 'present ✓' : 'missing ✗', cache.cards_cached ? 'ok' : 'warn'],
        ['HTML Cache',     cache.html_cache_entries ?? 0],
      ])}</div>
    </div>`;
 
  // ── HARVEST ──
  const harv = d.harvest || {};
  const harvOk = !!harv.last_run;
  setPill('pill-harvest', harvOk ? 'HAS DATA' : 'NO DATA', harvOk ? 'ok' : 'warn');
  setStatCard('sc-harvest', harv.last_run || 'Never', harv.snapshots_saved ? `${harv.snapshots_saved} snaps` : 'no data', harvOk ? 'ok' : 'warn');
  document.getElementById('body-harvest').innerHTML = renderRows([
    ['Last Run',         harv.last_run || 'Never'],
    ['Snapshots Saved',  harv.snapshots_saved ?? 'N/A'],
    ['Profiles Updated', harv.profiles_saved ?? 'N/A'],
    ['Battles Saved',    harv.battles_saved ?? 'N/A'],
    ['Duration',         harv.duration_s != null ? harv.duration_s + 's' : 'N/A'],
    ['Status',           harv.status || 'unknown'],
  ]);
  // Populate harvest tab
  document.getElementById('harvest-detail').innerHTML = `
    <div class="diag-grid">
      <div class="diag-card">
        <div class="diag-card-header"><div class="diag-card-title">📡 Last Harvest Run</div></div>
        <div class="diag-body">${renderRows([
          ['Last Run',         harv.last_run || 'Never'],
          ['Status',           harv.status || 'unknown', harv.status === 'ok' ? 'ok' : 'warn'],
          ['Snapshots Saved',  harv.snapshots_saved ?? 'N/A'],
          ['Profiles Updated', harv.profiles_saved ?? 'N/A'],
          ['Battles Saved',    harv.battles_saved ?? 'N/A'],
          ['Duration',         harv.duration_s != null ? harv.duration_s + 's' : 'N/A'],
        ])}</div>
      </div>
    </div>`;
 
  // ── TASKS ──
  const tasks = d.tasks || {};
  const warmupActive = (tasks.active_warmups || []).length > 0;
  setStatCard('sc-warmup', warmupActive ? 'ACTIVE' : 'IDLE', `${(tasks.active_warmups||[]).length} active`, warmupActive ? 'warn' : 'ok');
  setPill('pill-tasks', warmupActive ? 'RUNNING' : 'IDLE', warmupActive ? 'warn' : 'ok');
  document.getElementById('body-tasks').innerHTML = renderRows([
    ['Active Warmups',       (tasks.active_warmups || []).join(', ') || 'none'],
    ['Reminder Loop',        tasks.reminder_loop || 'unknown'],
    ['Snapshot Loop',        tasks.snapshot_loop || 'unknown'],
    ['Next Snapshot',        tasks.next_snapshot || 'N/A'],
    ['Feature Auto Pings',   tasks.feature_auto_pings ? 'enabled' : 'disabled'],
  ]);
 
  // ── TEMPLATES ──
  const tmpl = d.templates || {};
  const allPresent = Object.values(tmpl).every(v => v === 'db');
  setPill('pill-templates', allPresent ? 'ALL DB' : 'PARTIAL', allPresent ? 'ok' : 'warn');
  document.getElementById('body-templates').innerHTML = renderRows(
    Object.entries(tmpl).map(([k, v]) => [k, v === 'db' ? '✓ MongoDB' : '⚠ Fallback', v === 'db' ? 'ok' : 'warn'])
  );
 
  appendLog('Diagnostics render complete.', 'ok');
  toast('Diagnostics loaded', 'ok', 2000);
}
 
// ─── LOAD WAR ─────────────────────────────────────────────────────────────
async function loadWar() {
  document.getElementById('war-content').innerHTML =
    `<div style="color:var(--dim);font-family:var(--font-mono);font-size:12px;">Loading...</div>`;
  try {
    const resp = await fetch('/admin/diagnostics');
    const d = await resp.json();
    const war = d.war || {};
    document.getElementById('war-last-refresh').textContent = 'Refreshed: ' + new Date().toLocaleTimeString();
 
    if (!war.state) {
      document.getElementById('war-content').innerHTML =
        `<div style="color:var(--dim);font-family:var(--font-mono);font-size:12px;">No war data available. Data is pulled from the diagnostics endpoint.</div>`;
      return;
    }
 
    const rows = (war.participants || []).map(p => {
      const pct = Math.min(100, Math.round((p.decksUsedToday / 4) * 100));
      return `<tr>
        <td>${escHtml(p.name)}</td>
        <td>${p.fame}</td>
        <td>${p.decksUsedToday}/4</td>
        <td><div class="bar-wrap"><div class="bar-fill" style="width:${pct}%"></div></div></td>
      </tr>`;
    }).join('');
 
    document.getElementById('war-content').innerHTML = `
      <div class="stat-row" style="margin-bottom:20px;">
        <div class="stat-card ok">
          <div class="stat-label">State</div>
          <div class="stat-value ok" style="font-size:16px;">${escHtml(war.state)}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Clan Fame</div>
          <div class="stat-value">${(war.clan_fame||0).toLocaleString()}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Participants</div>
          <div class="stat-value">${(war.participants||[]).length}</div>
        </div>
      </div>
      <table class="war-table">
        <thead><tr><th>Player</th><th>Fame</th><th>Decks Today</th><th>Progress</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  } catch(e) {
    document.getElementById('war-content').innerHTML =
      `<div style="color:var(--err);font-family:var(--font-mono);font-size:12px;">Error: ${escHtml(e.message)}</div>`;
  }
}
 
// ─── ADMIN ACTIONS ────────────────────────────────────────────────────────
function confirmReset() {
  if (!confirm('Reset ALL HTML templates? Pages will fall back to Python defaults.')) return;
  fetch('/admin/reset-html', { method: 'POST' })
    .then(r => r.json())
    .then(d => {
      toast(d.message || 'Templates reset', 'ok');
      appendLog('Templates reset via admin action.', 'warn');
    })
    .catch(e => toast('Error: ' + e.message, 'err'));
}
 
function confirmFlushCache() {
  if (!confirm('Flush all CR API cache keys? Next commands will be slower.')) return;
  fetch('/admin/flush-cache', { method: 'POST' })
    .then(r => r.json())
    .then(d => {
      toast(d.message || 'Cache flushed', 'ok');
      appendLog('Cache flushed via admin action.', 'warn');
      loadDiagnostics();
    })
    .catch(e => toast('Error: ' + e.message, 'err'));
}
 
// ─── BOOT ─────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  appendLog('Admin panel loaded.', 'info');
  loadDiagnostics();
});
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# INDEX ROUTE HELPERS & JOIN MONITOR TRAFFIC PIPELINE
# ---------------------------------------------------------------------------
def _build_war_participants(war_data: dict | None) -> dict:
    if not war_data or not isinstance(war_data, dict):
        return {}
    if "clan" in war_data and war_data["clan"] and "participants" in war_data["clan"]:
        return {clean_tag(p["tag"]): p for p in war_data["clan"]["participants"] if "tag" in p}
    for clan in war_data.get("clans", []):
        if clan and clean_tag(clan.get("tag", "")) == clean_tag(CLAN_TAG):
            return {clean_tag(p["tag"]): p for p in clan.get("participants", [])}
    return {}


def _enrich_members(raw_members: list, profile_map: dict, war_participants: dict) -> list:
    players = []
    for m in raw_members:
        if not m or "tag" not in m:
            continue
        tag = clean_tag(m["tag"])
        p_data = profile_map.get(tag, {})
        wp_data = war_participants.get(tag, {})
        
        m["current_streak"] = p_data.get("current_streak", 0)
        m["warDayWins"] = p_data.get("warDayWins", 0)
        m["fame"] = wp_data.get("fame", 0)
        m["clean_tag"] = tag
        players.append(m)
    return sorted(players, key=lambda x: x.get("trophies", 0), reverse=True)


def _process_roster_changes(current_member_tags: list, profile_map: dict, raw_members: list):
    """Audits the clan roster to detect New Joins, Returns, and Kicked Returns."""
    new_joins = []
    standard_returns = []
    kicked_returns = []

    name_map = {clean_tag(m["tag"]): m.get("name", "Unknown Member") for m in raw_members if "tag" in m}

    for tag in current_member_tags:
        player_name = name_map.get(tag, "Unknown Member")

        if tag not in profile_map:
            new_joins.append(player_name)
            continue

        profile = profile_map[tag]
        if profile.get("in_clan_last_seen") is False:
            if profile.get("last_departure_status") == "kicked":
                kicked_returns.append(player_name)
            else:
                standard_returns.append(player_name)

    if new_joins or standard_returns or kicked_returns:
        _try_redis_publish({
            "action": "ROSTER_JOIN_ALERTS",
            "new_joins": new_joins,
            "standard_returns": standard_returns,
            "kicked_returns": kicked_returns
        })


# ---------------------------------------------------------------------------
# SECTION 4 — Replace /admin route + add /admin/diagnostics, /admin/flush-cache
# ---------------------------------------------------------------------------
 # ---------------------------------------------------------------------------
# PUBLIC FRONTEND ROUTES
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    # 1. Fetch Clan Roster
    clan_data = fetch_cr_api(f"clans/%23{CLAN_TAG}")
    if not clan_data:
        return "Could not load clan data from API.", 500
        
    raw_members = clan_data.get("memberList", [])
    member_tags = [clean_tag(m["tag"]) for m in raw_members if "tag" in m]
    
    # 2. Grab Database Profiles for Streaks/Stats
    db_profiles = list(db_sync["player_profiles"].find({"_id": {"$in": member_tags}}))
    profiles_map = {p["_id"]: p for p in db_profiles}
    
    # 3. Fetch War Data
    war_data = fetch_cr_api(f"clans/%23{CLAN_TAG}/currentriverrace")
    war_participants = _build_war_participants(war_data)
    
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
        top_war=top_war
    )

@app.route("/favicon.ico")
def favicon():
    # Returns an empty 204 No Content response to stop the 404 console errors
    return "", 204
 
 
# ── /admin ────────────────────────────────────────────────────────────────
@app.route("/admin")
def admin_panel():
    if not is_admin():
        return "Unauthorized", 403
    # Just render the shell — all data loads via JS fetch to /admin/diagnostics
    return render_sandboxed(
        get_template("admin"),
        clan_tag=CLAN_TAG,
    )
 
 
# ── /admin/diagnostics ────────────────────────────────────────────────────
@app.route("/admin/diagnostics")
def admin_diagnostics():
    if not is_admin():
        return jsonify({"error": "unauthorized"}), 403
 
    result = {}
 
    # ── Redis ──────────────────────────────────────────────────────────────
    try:
        t0 = _time.monotonic()
        redis_sync_client.ping()
        ping_ms = round((_time.monotonic() - t0) * 1000, 1)
        info = redis_sync_client.info()
        total_keys = redis_sync_client.dbsize()
        result["redis"] = {
            "status": "ok",
            "ping_ms": ping_ms,
            "used_memory": info.get("used_memory_human", "N/A"),
            "total_keys": total_keys,
            "mode": info.get("redis_mode", "N/A"),
            "redis_version": info.get("redis_version", "N/A"),
        }
    except Exception as e:
        result["redis"] = {"status": "error", "error": str(e)}
 
    # ── MongoDB ────────────────────────────────────────────────────────────
    try:
        t0 = _time.monotonic()
        mongo_client_sync.admin.command("ping")
        ping_ms = round((_time.monotonic() - t0) * 1000, 1)
        collections = db_sync.list_collection_names()
        result["mongo"] = {
            "status": "ok",
            "ping_ms": ping_ms,
            "db_name": db_sync.name,
            "collections": collections,
            "snapshot_count": db_sync["historical_snapshots"].estimated_document_count(),
            "battle_count":   db_sync["battle_history"].estimated_document_count(),
            "profile_count":  db_sync["player_profiles"].estimated_document_count(),
        }
    except Exception as e:
        result["mongo"] = {"status": "error", "error": str(e)}
 
    # ── CR API ─────────────────────────────────────────────────────────────
    try:
        endpoint = f"https://proxy.royaleapi.dev/v1/clans/%23{CLAN_TAG}"
        t0 = _time.monotonic()
        
        # FIX: Add the missing authentication headers to the health check
        auth_headers = {
            "Authorization": f"Bearer {os.getenv('CR_TOKEN', '').strip()}",
            "Accept": "application/json"
        }
        
        resp = cr_api_session.get(endpoint, headers=auth_headers, timeout=5)
        latency_ms = round((_time.monotonic() - t0) * 1000, 1)
        if resp.status_code == 200:
            api_status = "ok"
        elif resp.status_code == 429:
            api_status = "rate_limited"
        elif resp.status_code == 403:
            api_status = "forbidden"
        else:
            api_status = "error"
        result["cr_api"] = {
            "status": api_status,
            "status_code": resp.status_code,
            "latency_ms": latency_ms,
            "rate_limit_remaining": resp.headers.get("X-RateLimit-Remaining", "N/A"),
            "endpoint_tested": endpoint,
        }
    except Exception as e:
        result["cr_api"] = {"status": "unreachable", "error": str(e)}
 
    # ── Bot process ────────────────────────────────────────────────────────
    # bot_instance is a module-level reference — set it in your Bot.__init__
    # e.g.  import mainbot; mainbot._bot_instance = self
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
 
    # ── Cache stats ────────────────────────────────────────────────────────
    try:
        backend = "redis" if result.get("redis", {}).get("status") == "ok" else "mongo_fallback"
        if backend == "redis":
            all_keys    = redis_sync_client.keys("*")
            str_keys    = [k.decode() if isinstance(k, bytes) else k for k in all_keys]
            player_keys = sum(1 for k in str_keys if k.startswith("player:"))
            clan_keys   = sum(1 for k in str_keys if k.startswith("clan:"))
            bl_keys     = sum(1 for k in str_keys if k.startswith("battlelog:"))
            war_keys    = sum(1 for k in str_keys if k.startswith("currentrace:") or k.startswith("racelog:"))
            cards_cached = any(k == "cards:all" for k in str_keys)
            total = len(str_keys)
        else:
            # Mongo fallback — count cache collection docs by key prefix
            total        = db_sync["api_cache"].estimated_document_count()
            player_keys  = db_sync["api_cache"].count_documents({"_id": {"$regex": "^player:"}})
            clan_keys    = db_sync["api_cache"].count_documents({"_id": {"$regex": "^clan:"}})
            bl_keys      = db_sync["api_cache"].count_documents({"_id": "battlelog:"})
            war_keys     = db_sync["api_cache"].count_documents({"_id": {"$regex": "^currentrace:|^racelog:"}})
            cards_cached = db_sync["api_cache"].count_documents({"_id": "cards:all"}) > 0
 
        with _cache_lock:
            html_entries = len(_HTML_CACHE)
 
        result["cache"] = {
            "backend": backend,
            "total_keys": total,
            "player_keys": player_keys,
            "clan_keys": clan_keys,
            "battlelog_keys": bl_keys,
            "war_keys": war_keys,
            "cards_cached": cards_cached,
            "html_cache_entries": html_entries,
        }
    except Exception as e:
        result["cache"] = {"error": str(e)}
 
    # ── Harvest metadata ───────────────────────────────────────────────────
    result["harvest"] = _harvest_meta.copy()
 
    # ── Background tasks ───────────────────────────────────────────────────
    bot_inst = globals().get("_bot_instance")
    cog = None
    if bot_inst:
        cog = bot_inst.cogs.get("ClashRoyale")
 
    if cog:
        rl = cog.reminder_loop
        sl = cog.daily_snapshot_loop
 
        def _loop_status(loop):
            if loop.is_running(): return "running"
            if loop.failed():     return "failed"
            return "stopped"
 
        next_snap = None
        if sl.next_iteration:
            next_snap = sl.next_iteration.strftime("%Y-%m-%d %H:%M:%S UTC")
 
        result["tasks"] = {
            "active_warmups":    list(cog.active_warmups),
            "reminder_loop":     _loop_status(rl),
            "snapshot_loop":     _loop_status(sl),
            "next_snapshot":     next_snap,
            "feature_auto_pings": getattr(bot_inst, "feature_auto_pings", False),
        }
    else:
        result["tasks"] = {
            "active_warmups": [],
            "reminder_loop": "cog not loaded",
            "snapshot_loop": "cog not loaded",
            "next_snapshot": None,
            "feature_auto_pings": False,
        }
 
    # ── Templates ─────────────────────────────────────────────────────────
    tmpl_doc = db_sync["config"].find_one({"_id": "html_templates"}) or {}
    result["templates"] = {
        name: ("db" if tmpl_doc.get(name) else "fallback")
        for name in ["roster", "player", "admin"]
    }
 
    # ── War data (lightweight) ─────────────────────────────────────────────
    try:
        war_data = fetch_cr_api(f"clans/%23{CLAN_TAG}/currentriverrace")
        if war_data:
            clan_info = war_data.get("clan", {})
            participants = clan_info.get("participants", [])
            if not participants and "clans" in war_data:
                for c in war_data.get("clans", []):
                    if c.get("tag","").replace("#","").upper() == CLAN_TAG:
                        participants = c.get("participants", [])
                        break
            result["war"] = {
                "state": war_data.get("state", "unknown"),
                "clan_fame": clan_info.get("fame", 0),
                "participants": [
                    {
                        "name": p.get("name", "Unknown"),
                        "fame": p.get("fame", 0),
                        "decksUsedToday": p.get("decksUsedToday", 0),
                    }
                    for p in sorted(participants, key=lambda x: x.get("fame", 0), reverse=True)
                ],
            }
        else:
            result["war"] = {"state": None}
    except Exception as e:
        result["war"] = {"state": None, "error": str(e)}
 
    # ── Meta ───────────────────────────────────────────────────────────────
    result["version"] = "1.0.0"
    result["environment"] = os.getenv("ENVIRONMENT", "production")
    result["hostname"] = platform.node()
 
    return jsonify(result)
 
 
# ── /admin/reset-html ─────────────────────────────────────────────────────
@app.route("/admin/reset-html", methods=["POST"])
def admin_reset_html():
    if not is_admin():
        return jsonify({"error": "unauthorized"}), 403
    db_sync["config"].delete_one({"_id": "html_templates"})
    invalidate_template_cache()
    return jsonify({"message": "All templates reset. Pages now use Python fallbacks."})
 
 
# ── /admin/flush-cache ────────────────────────────────────────────────────
@app.route("/admin/flush-cache", methods=["POST"])
def admin_flush_cache():
    if not is_admin():
        return jsonify({"error": "unauthorized"}), 403
    flushed = 0
    try:
        keys = redis_sync_client.keys("*")
        cr_keys = [k for k in keys if any(
            (k.decode() if isinstance(k, bytes) else k).startswith(p)
            for p in ("player:", "clan:", "battlelog:", "currentrace:", "racelog:", "cards:", "chests:", "warmed_today:")
        )]
        if cr_keys:
            flushed = redis_sync_client.delete(*cr_keys)
    except Exception:
        # Redis not available — flush mongo cache collection
        result = db_sync["api_cache"].delete_many({})
        flushed = result.deleted_count
    invalidate_template_cache()
    return jsonify({"message": f"Flushed {flushed} cache keys."})


# ---------------------------------------------------------------------------
# ADMIN — TEMPLATE MANAGEMENT
# ---------------------------------------------------------------------------
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


@app.route("/admin/reset-html")
def reset_html():
    if not is_admin():
        return "Unauthorized", 403
    db_sync["config"].delete_one({"_id": "html_templates"})
    invalidate_template_cache()
    return redirect("/admin?success=All+UI+Templates+Reset+to+Factory+Defaults!")


@app.route("/admin/validate-template", methods=["POST"])
def validate_template():
    if not is_admin():
        return {"error": "Unauthorized"}, 403
    html = request.json.get("html", "")
    ok, message, line = validate_jinja_syntax(html)
    return {"ok": ok, "message": message, "line": line}


@app.route("/admin/save-template-safe", methods=["POST"])
def save_template_safe():
    if not is_admin():
        return {"error": "Unauthorized"}, 403
    html = request.json.get("html", "")
    template_name = request.json.get("template_name", "")
    if template_name not in ["roster", "player", "link", "admin"]:
        return {"ok": False, "message": "Invalid template name."}, 400
    ok, message, line = validate_jinja_syntax(html)
    if not ok:
        return {"ok": False, "message": f"Validation failed — template NOT saved: {message}", "line": line}
    db_sync["config"].update_one(
        {"_id": "html_templates"},
        {"$set": {template_name: html}},
        upsert=True,
    )
    invalidate_template_cache()
    return {"ok": True, "message": f"'{template_name}' validated and deployed successfully."}


# ---------------------------------------------------------------------------
# ADMIN — CUSTOM PAGES
# ---------------------------------------------------------------------------
@app.route("/admin/pages/save", methods=["POST"])
def admin_save_page():
    if not is_admin():
        return {"error": "Unauthorized"}, 403
    data = request.json or {}
    slug = data.get("slug", "").strip().lower().replace(" ", "-")
    html = data.get("html", "")
    if not slug:
        return {"ok": False, "message": "Slug is required."}
    ok, message, line = validate_jinja_syntax(html)
    if not ok:
        return {"ok": False, "message": f"Jinja error on line {line}: {message}"}
    
    now_str = datetime.now(zoneinfo.ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M")
    db_sync["pages"].update_one(
        {"_id": slug},
        {"$set": {"html": html, "updated": now_str}},
        upsert=True,
    )
    return {"ok": True, "message": f"Page deployed at /p/{slug}"}


@app.route("/admin/pages/get/<slug>")
def admin_get_page(slug):
    if not is_admin():
        return {"error": "Unauthorized"}, 403
    doc = db_sync["pages"].find_one({"_id": slug})
    if not doc:
        return {"ok": False, "message": "Page not found."}, 404
    return {"ok": True, "html": doc.get("html", ""), "slug": slug}


@app.route("/admin/pages/delete/<slug>")
def admin_delete_page(slug):
    if not is_admin():
        return "Unauthorized", 403
    db_sync["pages"].delete_one({"_id": slug})
    return redirect("/admin?success=Page+deleted!#pages")


# ---------------------------------------------------------------------------
# ADMIN — DATA VIEWER
# ---------------------------------------------------------------------------
@app.route("/admin/data/<collection>")
def admin_view_collection(collection):
    if not is_admin():
        return {"error": "Unauthorized"}, 403
    allowed = ["users", "player_profiles", "custom_commands", "config"]
    if collection not in allowed:
        return {"error": "Forbidden"}, 403
    docs = list(db_sync[collection].find().limit(100))
    for d in docs:
        d["_id"] = str(d["_id"])
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = v.isoformat()
    return jsonify(docs)


@app.route("/admin/snapshot/<name>")
def admin_view_snapshot(name):
    if not is_admin():
        return {"error": "Unauthorized"}, 403
    allowed = ["clan", "riverrace"]
    if name not in allowed:
        return {"error": "Forbidden"}, 403
    doc = db_sync["snapshots"].find_one({"_id": name})
    if not doc:
        return jsonify({"error": "No snapshot yet. Hit the fetch button first."})
    doc["_id"] = str(doc["_id"])
    if "fetched_at" in doc and isinstance(doc["fetched_at"], datetime):
        doc["fetched_at"] = doc["fetched_at"].isoformat()
    return jsonify(doc)


# ---------------------------------------------------------------------------
# ADMIN — MANUAL API FETCH TRIGGERS
# ---------------------------------------------------------------------------
@app.route("/admin/fetch/clan")
def admin_fetch_clan():
    if not is_admin():
        return "Unauthorized", 403
    data = fetch_cr_api(f"clans/%23{CLAN_TAG}")
    if not data:
        return redirect("/admin?error=Clan+API+call+failed#data")
    db_sync["snapshots"].update_one(
        {"_id": "clan"},
        {"$set": {"data": data, "fetched_at": datetime.now(zoneinfo.ZoneInfo("UTC"))}},
        upsert=True,
    )
    return redirect("/admin?success=Clan+data+refreshed+and+stored!#data")


@app.route("/admin/fetch/riverrace")
def admin_fetch_riverrace():
    if not is_admin():
        return "Unauthorized", 403
    data = fetch_cr_api(f"clans/%23{CLAN_TAG}/currentriverrace")
    if not data:
        return redirect("/admin?error=River+race+API+call+failed#data")
    db_sync["snapshots"].update_one(
        {"_id": "riverrace"},
        {"$set": {"data": data, "fetched_at": datetime.now(zoneinfo.ZoneInfo("UTC"))}},
        upsert=True,
    )
    return redirect("/admin?success=River+race+data+refreshed+and+stored!#data")


@app.route("/admin/fetch/player/<tag>")
def admin_fetch_player(tag):
    if not is_admin():
        return "Unauthorized", 403
    tag = clean_tag(tag)
    data = fetch_cr_api(f"players/%23{tag}")
    if not data:
        return redirect("/admin?error=Player+not+found#data")
    db_sync["player_profiles"].update_one(
        {"_id": tag},
        {"$set": data},
        upsert=True,
    )
    return redirect(f"/admin?success=Profile+for+{tag}+updated!#data")


# ---------------------------------------------------------------------------
# ADMIN — ASYNC PROFILE SCRAPER ENGINE (Addresses Waitress thread freezing)
# ---------------------------------------------------------------------------
async def _bg_scrape_task(member_tags: list):
    """Background async wrapper that runs safely outside active web server thread pool blocks."""
    log.info(f"Starting background async scraper loop for {len(member_tags)} entries.")
    global bot
    updated, failed = 0, 0
    for tag in member_tags:
        profile = await bot.async_fetch_cr_api(f"players/%23{tag}")
        if profile:
            await bot.db["player_profiles"].update_one({"_id": tag}, {"$set": profile}, upsert=True)
            updated += 1
        else:
            failed += 1
        await asyncio.sleep(0.5)
    log.info(f"🎯 Background profile scraping task finished: {updated} success, {failed} failure entries.")


@app.route("/admin/fetch/all-profiles")
def admin_fetch_all_profiles():
    if not is_admin():
        return "Unauthorized", 403
    clan_data = fetch_cr_api(f"clans/%23{CLAN_TAG}")
    if not clan_data:
        return redirect("/admin?error=Could+not+contact+Clash+Royale+API#data")
    members = clan_data.get("memberList", [])
    member_tags = [clean_tag(m["tag"]) for m in members if "tag" in m]
    
    global bot
    asyncio.run_coroutine_threadsafe(_bg_scrape_task(member_tags), bot.loop)
    return redirect("/admin?success=Profile+scraping+task+started+safely+in+the+background!#data")


# ---------------------------------------------------------------------------
# ADMIN — CACHE & COG CONTROLS
# ---------------------------------------------------------------------------
@app.route("/admin/clear-cache")
def admin_clear_cache():
    if not is_admin():
        return "Unauthorized", 403
    invalidate_template_cache()
    return redirect("/admin?success=All+template+caches+cleared!#data")


@app.route("/admin/reload-cog")
def admin_reload_cog():
    if not is_admin():
        return "Unauthorized", 403
    _try_redis_publish({"action": "RELOAD_COG", "cog": "cogs.clash_cog"})
    return redirect("/admin?success=Bot+command+cog+reload+signal+sent!#data")


# ---------------------------------------------------------------------------
# ADMIN — ACCESS CONTROLS & MANAGEMENT
# ---------------------------------------------------------------------------
@app.route("/admin/grant-role/<player_tag>")
def admin_grant_role(player_tag):
    if not is_admin():
        return "Unauthorized", 403
    linked_user = users_sync.find_one({"player_id": clean_tag(player_tag)})
    if not linked_user:
        return redirect("/admin?error=This+player+has+not+linked+their+Discord+account+yet.")
    db_sync["config"].update_one(
        {"_id": "system_config_file"},
        {"$addToSet": {"admin_user_ids": str(linked_user["_id"])}},
        upsert=True,
    )
    _try_redis_publish({"action": "RELOAD"})
    return redirect("/admin?success=Granted+dashboard+admin+privileges!")


@app.route("/admin/manual-link", methods=["POST"])
def admin_manual_link():
    if not is_admin():
        return "Unauthorized", 403
    player_tag = clean_tag(request.form.get("player_tag", ""))
    discord_id = request.form.get("discord_id", "").strip()
    if not player_tag or not discord_id:
        return redirect("/admin?error=Missing+tag+or+Discord+ID")
    if not discord_id.isdigit() or len(discord_id) < 17:
        return redirect("/admin?error=Invalid+Discord+ID+Format.+Must+be+17%2B+digits.")
    users_sync.update_one(
        {"_id": discord_id},
        {"$set": {"player_id": player_tag}},
        upsert=True,
    )
    return redirect("/admin?success=Successfully+Linked+Tag+to+Discord+ID!")


@app.route("/admin/add-command", methods=["POST"])
def admin_add_command():
    if not is_admin():
        return "Unauthorized", 403
    trigger = request.form.get("trigger", "").strip().lower().lstrip("!?")
    response_text = request.form.get("response", "").strip()
    if trigger and response_text:
        custom_cmds_sync.update_one(
            {"_id": trigger},
            {"$set": {"response": response_text}},
            upsert=True,
        )
        return redirect("/admin?success=Custom+Command+Added!")
    return redirect("/admin?error=Trigger+and+Response+Required")


@app.route("/admin/delete-command/<cmd_id>")
def admin_delete_command(cmd_id):
    if not is_admin():
        return "Unauthorized", 403
    custom_cmds_sync.delete_one({"_id": cmd_id})
    return redirect("/admin?success=Command+Deleted!")


@app.route("/admin/update-system-config", methods=["POST"])
def update_system_config():
    if not is_admin():
        return "Unauthorized", 403

    def split_ids(field: str) -> list[str]:
        return [v.strip() for v in request.form.get(field, "").split(",") if v.strip()]

    db_sync["config"].update_one(
        {"_id": "system_config_file"},
        {
            "$set": {
                "command_prefix": request.form.get("command_prefix", "!"),
                "maintenance_mode": bool(request.form.get("maintenance_mode")),
                "feature_auto_pings": bool(request.form.get("feature_auto_pings")),
                "war_channel_id": request.form.get("war_channel_id", "").strip(),
                "ignored_channels": split_ids("ignored_channels"),
                "admin_role_ids": split_ids("admin_role_ids"),
                "admin_user_ids": split_ids("admin_user_ids"),
            }
        },
        upsert=True,
    )
    global _CONFIG_CACHE_EXPIRE
    _CONFIG_CACHE_EXPIRE = 0.0  # Reset caching pipeline triggers immediately
    _try_redis_publish({"action": "RELOAD"})
    return redirect("/admin?success=System+configuration+updated+instantly!")


@app.route("/admin/ping/<player_name>/<int:decks_left>")
def admin_ping_player(player_name, decks_left):
    if not is_admin():
        return "Unauthorized", 403
    ok = _try_redis_publish({"action": "SINGLE_PING", "player_name": player_name, "decks_left": decks_left})
    if ok:
        return redirect("/admin?success=Sent+nudge+alert!")
    return redirect("/admin?error=Redis+offline.+Instant+pings+currently+unavailable.")


@app.route("/admin/mass-ping")
def admin_mass_ping():
    if not is_admin():
        return "Unauthorized", 403
    ok = _try_redis_publish({"action": "MASS_PING"})
    if ok:
        return redirect("/admin?success=Mass+War+Alert+Broadcasted!")
    return redirect("/admin?error=Redis+offline.+Mass+pings+unavailable.")


# ---------------------------------------------------------------------------
# OVERHAULED NATIVE EXPORT ENGINE (Addresses bad Jinja CSV formatting)
# ---------------------------------------------------------------------------
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
    war_participants = _build_war_participants(war_data)

    export_format = request.form.get("export_format", "csv")
    selected_fields = request.form.getlist("fields")

    if export_format == "json":
        export_data = _build_export_rows(members, profiles_map, war_participants, selected_fields)
        return jsonify(export_data)

    si = io.StringIO()
    cw = csv.writer(si)
    
    if selected_fields:
        cw.writerow(selected_fields)
        rows = _build_export_rows(members, profiles_map, war_participants, selected_fields)
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
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=Graveyard_Custom_Export.csv"},
    )


def _build_export_rows(members, profiles_map, war_participants, selected_fields):
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
    return rows


@app.route("/health")
def health():
    return {"status": "ok"}, 200


# ---------------------------------------------------------------------------
# REDIS SYNCHRONOUS ROUTE PUBLISHER HELPER
# ---------------------------------------------------------------------------
def _try_redis_publish(payload: dict) -> bool:
    try:
        redis_sync_client.publish("graveyard_bot_signals", json.dumps(payload))
        return True
    except Exception as e:
        log.warning(f"Redis publish failed: {e}")
        return False


# ---------------------------------------------------------------------------
# 4. FLASK SERVER MANAGER RUNNER
# ---------------------------------------------------------------------------
def run_flask():
    port = int(os.getenv("PORT", 5000))
    try:
        test = fetch_cr_api(f"clans/%23{CLAN_TAG}")
        if test and "memberList" in test:
            log.info(f"✅ CR API OK — {len(test['memberList'])} members found")
        else:
            log.warning(f"⚠️ CR API returned unexpected data on startup: {test}")
    except Exception as e:
        log.error(f"❌ CR API startup test failed: {e}")

    log.info(f"🌐 Flask dashboard running on port {port}")
    serve(app, host="0.0.0.0", port=port)


# ---------------------------------------------------------------------------
# 5. DYNAMIC PREFIX CALLABLE LINK
# ---------------------------------------------------------------------------
def get_dynamic_prefix(bot_instance, message):
    return bot_instance.active_prefix


# ---------------------------------------------------------------------------
# 6. DISCORD BOT ENGINE SETUP
# ---------------------------------------------------------------------------
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
        return {
            "Authorization": f"Bearer {os.getenv('CR_TOKEN', '').strip()}",
            "Accept": "application/json",
        }

    async def async_fetch_cr_api(self, endpoint: str, retries: int = 3) -> dict | None:
        url = f"https://proxy.royaleapi.dev/v1/{endpoint}"
        timeout = aiohttp.ClientTimeout(total=10)

        for attempt in range(retries):
            try:
                async with self.http_session.get(url, headers=self._cr_headers(), timeout=timeout) as response:
                    if response.status == 200:
                        data = await response.json()
                        if isinstance(data, dict):
                            _normalize_card_levels(data)
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
                log.warning(f"⚠️ Redis unavailable at startup: {e}")
                self.redis_available = False
        else:
            self.redis_available = False

        await self.load_extension("cogs.clash_cog")

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if not self.redis_available:
            now = time.time()
            if now - self._last_config_load > 30:
                try:
                    await self.load_system_config()
                    self._last_config_load = now
                except Exception as e:
                    log.error(f"Fallback config reload failed: {e}")

        prefix = self.active_prefix

        if self.maintenance_mode and message.content.startswith(prefix):
            await message.channel.send("⚠️ GraveyardBot is down for maintenance. Try again shortly.")
            return

        if str(message.channel.id) in self.ignored_channels:
            return

        if message.content.startswith(prefix):
            cmd_name = message.content[len(prefix):].split()[0].lower()
            custom_cmd = await self.custom_cmds.find_one({"_id": cmd_name})
            if custom_cmd:
                await message.channel.send(custom_cmd["response"])
                return

        await self.process_commands(message)

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        log.error(f"Global command error: {error}")

    async def load_system_config(self):
        config_doc = await self.db["config"].find_one({"_id": "system_config_file"})
        if config_doc:
            self.active_prefix = config_doc.get("command_prefix", "!")
            self.maintenance_mode = config_doc.get("maintenance_mode", False)
            self.feature_auto_pings = config_doc.get("feature_auto_pings", False)
            self.ignored_channels = config_doc.get("ignored_channels", [])
            self.war_channel_id = int(config_doc.get("war_channel_id") or 0)
        else:
            self.active_prefix = "!"
            self.maintenance_mode = False
            self.feature_auto_pings = False
            self.ignored_channels = []
            self.war_channel_id = 0

    async def listen_to_web_ui(self):
        backoff = 5
        max_backoff = 60

        while not self.is_closed():
            pubsub = None
            try:
                pubsub = self.redis.pubsub()
                await pubsub.subscribe("graveyard_bot_signals")
                log.info("📡 Redis PubSub listener active.")
                backoff = 5

                async for msg in pubsub.listen():
                    if msg["type"] != "message":
                        continue
                    try:
                        payload = json.loads(msg["data"])
                        await self._handle_redis_action(payload)
                    except json.JSONDecodeError:
                        log.warning("Malformed Redis payload — ignoring.")

            except Exception as e:
                log.error(f"Redis listener dropped: {e}. Reconnecting in {backoff}s…")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
            finally:
                if pubsub:
                    try:
                        await pubsub.close()
                    except Exception:
                        pass

    async def _handle_redis_action(self, payload: dict):
        action = payload.get("action")

        if action == "RELOAD":
            await self.load_system_config()

        elif action == "RELOAD_COG":
            cog = payload.get("cog", "cogs.clash_cog")
            try:
                await self.reload_extension(cog)
                log.info(f"✅ Reloaded cog: {cog}")
            except Exception as e:
                log.error(f"❌ Cog reload failed: {e}")

        elif action == "SINGLE_PING":
            channel = self.get_channel(self.war_channel_id)
            if not channel:
                return
            player_name = payload.get("player_name")
            decks_left = payload.get("decks_left")
            matched_user = await self.db_users.find_one({"clan_name_cache": player_name})
            mention = f"<@{matched_user['_id']}>" if matched_user else f"**{player_name}**"
            embed = discord.Embed(
                title="⚔️ River Race Nudge Alert!",
                description=f"Yo {mention}, you still have **{decks_left} war deck(s)** left! Lock it in.",
                color=0xE74C3C,
            )
            await channel.send(embed=embed)

        elif action == "MASS_PING":
            channel = self.get_channel(self.war_channel_id)
            if channel:
                await channel.send("🚨 **SQUAD ATTENTION!** 🚨 Complete remaining battles immediately!")

        elif action == "ROSTER_JOIN_ALERTS":
            channel = self.get_channel(self.war_channel_id)
            if not channel:
                return

            # A. Process Brand New Recruits
            new_joins = payload.get("new_joins", [])
            for name in new_joins:
                embed = discord.Embed(
                    title="✨ Welcome New Recruit!",
                    description=f"**{name}** has just joined **Graveyard Squad** for the very first time! Raise your shields! 🛡️",
                    color=0x3498DB,
                    timestamp=discord.utils.utcnow()
                )
                await channel.send(embed=embed)

            # B. Process Standard Returning Veterans
            standard_returns = payload.get("standard_returns", [])
            for name in standard_returns:
                embed = discord.Embed(
                    title="💀 Welcome Back!",
                    description=f"**{name}** has returned home to **Graveyard Squad**! Good to see you back in the trenches. ⚔️",
                    color=0x2ECC71,
                    timestamp=discord.utils.utcnow()
                )
                await channel.send(embed=embed)

            # C. Process Probationary Returning Kicked Players
            kicked_returns = payload.get("kicked_returns", [])
            for name in kicked_returns:
                embed = discord.Embed(
                    title="⚠️ Probationary Return Status",
                    description=f"**{name}** has rejoined the clan after previously being **kicked**. Leadership attention requested. 👁️",
                    color=0xE67E22,
                    timestamp=discord.utils.utcnow()
                )
                embed.set_footer(text="Historical Status: Prior Kicked State Reset Clean")
                await channel.send(embed=embed)
                
                matched_user = await self.db["player_profiles"].find_one({"name": name})
                if matched_user:
                    await self.db["player_profiles"].update_one(
                        {"_id": matched_user["_id"]},
                        {"$set": {"last_departure_status": "none"}}
                    )

        else:
            log.warning(f"Unknown Redis action received: {action!r}")

    async def close(self):
        if self.http_session:
            await self.http_session.close()
        if self.redis_available:
            await self.redis.aclose()
        await super().close()


# ---------------------------------------------------------------------------
# ENTRYPOINT RUNNER ENGINE
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.config["RAW_CSV_TEMPLATE_FALLBACK"] = "Native field selector extraction logic active."
    bot = GraveyardBot()
    _bot_instance = bot  # <-- ADD THIS LINE
    threading.Thread(target=run_flask, daemon=True).start()
    bot.run(os.getenv("DISCORD_TOKEN"))
