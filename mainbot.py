import os
import sys
import subprocess
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
from datetime import datetime, timedelta, time as dt_time
import importlib.metadata

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from flask import Flask, render_template_string, request, redirect, session, jsonify
from waitress import serve
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient, UpdateOne
import redis.asyncio as redis
import redis as sync_redis
from jinja2.sandbox import SandboxedEnvironment

# ---------------------------------------------------------------------------
# 1. AUTOMATED ENVIRONMENT SETUP
# ---------------------------------------------------------------------------
def sync_environment():
    if os.name != "nt":
        return

    req_file = "requirements.txt"
    venv_dir = "venv"
    is_venv = sys.prefix != sys.base_prefix or os.path.exists(venv_dir)

    if not os.path.exists(req_file):
        with open(req_file, "w") as f:
            f.write(
                "discord.py\naiohttp\nmotor\npymongo\nredis\nflask\n"
                "python-dotenv\nthefuzz\nwaitress\nopenpyxl\ntzdata\nrequests\n"
            )
        print(f"✅ Created default {req_file}")

    if not is_venv:
        subprocess.check_call([sys.executable, "-m", "venv", venv_dir])
        python_exe = os.path.join(venv_dir, "Scripts", "python.exe")
        subprocess.check_call([python_exe, "-m", "pip", "install", "-r", req_file])
        os.execv(python_exe, [python_exe] + sys.argv)
    else:
        try:
            with open(req_file, "r") as f:
                required = [l.strip().split("==")[0] for l in f if l.strip() and not l.startswith("#")]
            installed = {pkg.metadata["Name"].lower() for pkg in importlib.metadata.distributions()}
            if not all(r.lower() in installed for r in required):
                raise Exception("Missing packages")
        except Exception:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req_file])


sync_environment()

# ---------------------------------------------------------------------------
# 2. STANDARD IMPORTS & SETUP
# ---------------------------------------------------------------------------
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)-10s %(message)s",
)
log = logging.getLogger("mainbot")

# ---------------------------------------------------------------------------
# 3. UNIFIED FLASK WEB INFRASTRUCTURE & SHARED CONFIGS
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET"]
app.permanent_session_lifetime = timedelta(days=30)

CLAN_TAG = "9LVY89UP"
MAX_CARD_LEVEL = 16

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = "https://graveyardbot.onrender.com/callback"
GUILD_ID = os.getenv("DISCORD_GUILD_ID")

mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
mongo_client_sync = MongoClient(mongo_url)
db_sync = mongo_client_sync["graveyardbot"]
users_sync = db_sync["users"]
custom_cmds_sync = db_sync["custom_commands"]

redis_sync_client = sync_redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))

cr_api_session = requests.Session()

# Threading lock and secure sandboxing configurations for global safety updates
_cache_lock = threading.Lock()
_HTML_CACHE: dict[str, str] = {}

# 🔴 FIX: Enable autoescape so bracketed names don't render as invisible HTML tags
sandbox_env = SandboxedEnvironment(autoescape=True)
def _enrich_members(raw_members: list, profile_map: dict, war_participants: dict) -> list:
    players = []
    seen_tags = set()
    
    for m in raw_members:
        if not m or "tag" not in m:
            continue
            
        tag = clean_tag(m["tag"])
        
        # Prevent the API from returning ghost duplicate cards
        if tag in seen_tags:
            continue
        seen_tags.add(tag)
        
        # Strip Clash Royale color codes (<c2>, </c>) safely on the backend
        raw_name = m.get("name", "Unknown")
        m["name"] = re.sub(r"<c\d?>|</c>", "", raw_name, flags=re.IGNORECASE)

        p_data = profile_map.get(tag, {})
        wp_data = war_participants.get(tag, {})
        
        m["current_streak"] = p_data.get("current_streak", 0)
        m["warDayWins"] = p_data.get("warDayWins", 0)
        m["fame"] = wp_data.get("fame", 0)
        m["clean_tag"] = tag
        players.append(m)
        
    return sorted(players, key=lambda x: x.get("trophies", 0), reverse=True)
_CONFIG_CACHE = {}
_CONFIG_CACHE_EXPIRE = 0.0

def _get_cached_system_config():
    global _CONFIG_CACHE, _CONFIG_CACHE_EXPIRE
    now = time.time()
    if now > _CONFIG_CACHE_EXPIRE:
        try:
            doc = db_sync["config"].find_one({"_id": "system_config_file"}) or {}
            _CONFIG_CACHE = doc
            _CONFIG_CACHE_EXPIRE = now + 60.0  # 60 Second TTL Cache
        except Exception as e:
            log.error(f"Error fetching system config for cache: {e}")
            return _CONFIG_CACHE
    return _CONFIG_CACHE

# ---------------------------------------------------------------------------
# UTILITY HELPERS
# ---------------------------------------------------------------------------
def clean_tag(tag: str) -> str:
    """Normalises Clash Royale tags to uppercase, stripping whitespace and '#'."""
    return tag.strip().upper().replace("#", "")


def _normalize_card_levels(data: dict) -> dict:
    for key in ("cards", "currentDeck"):
        for card in data.get(key, []):
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
        "Authorization": f"Bearer {os.getenv('CR_TOKEN')}",
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
    import re
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
    
    # 🔴 FIX: Inject Flask's session object into the sandbox context 
    # so the HTML can check login and admin states without crashing.
    context["session"] = session
    
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

DEFAULT_ADMIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Graveyard HQ</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/theme/dracula.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/xml/xml.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/javascript/javascript.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/css/css.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/htmlmixed/htmlmixed.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/edit/matchbrackets.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/edit/closetag.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/js-beautify/1.14.9/beautify-html.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:        #0b0c10;
    --surface:   #161b22;
    --surface2:  #1f2833;
    --border:    #2a3545;
    --accent:    #45a29e;
    --accent2:   #66fcf1;
    --text:      #c5c6c7;
    --text-dim:  #6b7785;
    --danger:    #e74c3c;
    --success:   #2ecc71;
    --warn:      #f1c40f;
    --info:      #5dade2;
    --sidebar-w: 220px;
  }

  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; display: flex; min-height: 100vh; overflow-x: hidden; }

  .sidebar { width: var(--sidebar-w); background: var(--surface); border-right: 1px solid var(--border); display: flex; flex-direction: column; position: fixed; top: 0; left: 0; bottom: 0; z-index: 100; overflow-y: auto; transition: transform 0.3s ease; }
  .sidebar-brand { padding: 20px 18px 16px; border-bottom: 1px solid var(--border); }
  .sidebar-brand .skull { font-size: 22px; margin-bottom: 4px; }
  .sidebar-brand h2 { font-size: 13px; font-weight: 700; color: var(--accent2); letter-spacing: 1.5px; text-transform: uppercase; }
  .sidebar-brand p  { font-size: 11px; color: var(--text-dim); margin-top: 2px; }
  .nav-section { padding: 12px 10px 4px; }
  .nav-label { font-size: 10px; font-weight: 700; color: var(--text-dim); letter-spacing: 1.5px; text-transform: uppercase; padding: 0 8px; margin-bottom: 4px; }
  .nav-item { display: flex; align-items: center; gap: 9px; padding: 9px 10px; border-radius: 7px; cursor: pointer; font-size: 13px; font-weight: 500; color: var(--text-dim); border: none; background: none; width: 100%; text-align: left; transition: background 0.15s, color 0.15s; text-decoration: none; }
  .nav-item:hover  { background: rgba(69,162,158,0.12); color: var(--text); }
  .nav-item.active { background: rgba(69,162,158,0.18); color: var(--accent2); }
  .nav-item .icon  { font-size: 16px; flex-shrink: 0; }
  .nav-item .badge { margin-left: auto; background: var(--danger); color: #fff; font-size: 10px; font-weight: 700; border-radius: 10px; padding: 1px 6px; min-width: 18px; text-align: center; }
  .sidebar-footer { margin-top: auto; padding: 14px 18px; border-top: 1px solid var(--border); font-size: 11px; color: var(--text-dim); }
  .sidebar-footer strong { color: var(--accent); }
  .sidebar-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 90; }

  .main { margin-left: var(--sidebar-w); flex: 1; padding: 28px 32px; min-height: 100vh; width: 100%; }
  .mobile-header { display: none; background: var(--surface); border-bottom: 1px solid var(--border); padding: 15px 20px; align-items: center; justify-content: space-between; margin: -28px -32px 20px -32px; }
  .mobile-header h2 { font-size: 16px; color: var(--accent2); margin: 0; }
  .hamburger { background: none; border: none; color: var(--accent2); font-size: 24px; cursor: pointer; }

  .tab-pane { display: none; }
  .tab-pane.active { display: block; }

  .page-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 26px; padding-bottom: 20px; border-bottom: 1px solid var(--border); flex-wrap: wrap; gap: 12px; }
  .page-header h1 { font-size: 20px; color: var(--accent2); font-weight: 700; }
  .page-header p  { font-size: 12px; color: var(--text-dim); margin-top: 3px; }

  .flash { padding: 12px 18px; border-radius: 8px; font-weight: 600; font-size: 13px; margin-bottom: 22px; }
  .flash.ok  { background: rgba(46,204,113,0.15); border: 1px solid var(--success); color: var(--success); }
  .flash.err { background: rgba(231,76,60,0.15);  border: 1px solid var(--danger);  color: var(--danger); }

  .stat-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; margin-bottom: 28px; }
  .stat-card { background: var(--surface2); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; }
  .stat-card .label { font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 8px; }
  .stat-card .value { font-size: 26px; font-weight: 700; }
  .stat-card .value.green { color: var(--success); }
  .stat-card .value.red   { color: var(--danger); }
  .stat-card .value.teal  { color: var(--accent2); }

  .panel { background: var(--surface2); border: 1px solid var(--border); border-radius: 10px; padding: 22px 24px; margin-bottom: 24px; }
  .panel h3 { font-size: 13px; font-weight: 700; color: var(--accent2); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 18px; padding-bottom: 12px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 8px; }

  .table-responsive { overflow-x: auto; -webkit-overflow-scrolling: touch; width: 100%; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; min-width: 600px; }
  th, td { padding: 10px 12px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: middle; }
  th { color: var(--accent); font-size: 10px; text-transform: uppercase; letter-spacing: 0.8px; font-weight: 700; background: rgba(0,0,0,0.2); }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(69,162,158,0.05); }

  .btn { display: inline-flex; align-items: center; gap: 5px; padding: 7px 14px; border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer; border: none; transition: opacity 0.15s, transform 0.1s; text-decoration: none; line-height: 1; white-space: nowrap; }
  .btn:hover  { opacity: 0.85; }
  .btn:active { transform: scale(0.97); }
  .btn-teal  { background: var(--accent);  color: #0b0c10; }
  .btn-cyan  { background: var(--accent2); color: #0b0c10; }
  .btn-warn  { background: var(--danger);  color: #fff; }
  .btn-green { background: var(--success); color: #0b0c10; }
  .btn-gold  { background: var(--warn);    color: #0b0c10; }
  .btn-blue  { background: var(--info);    color: #0b0c10; }
  .btn-ghost { background: transparent; border: 1px solid var(--border); color: var(--text); }
  .btn-ghost:hover { border-color: var(--accent); color: var(--accent2); }
  .btn-sm { padding: 4px 10px; font-size: 11px; }

  .form-group { margin-bottom: 16px; }
  .form-group label { display: block; font-size: 11px; font-weight: 700; color: var(--accent); text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 6px; }
  .form-group .hint { font-size: 11px; color: var(--text-dim); margin-top: 4px; }
  input[type="text"], select, textarea { background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 9px 12px; border-radius: 6px; font-size: 13px; width: 100%; font-family: inherit; transition: border-color 0.15s; }
  input[type="text"]:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent); }
  input[type="checkbox"] { accent-color: var(--accent2); width: 15px; height: 15px; cursor: pointer; }
  .checkbox-row { display: flex; align-items: center; gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--border); }
  .checkbox-row:last-of-type { border-bottom: none; }
  .checkbox-row label { font-size: 13px; color: var(--text); font-weight: 400; text-transform: none; letter-spacing: 0; cursor: pointer; }
  .checkbox-row .sub { font-size: 11px; color: var(--text-dim); }

  .check-grid { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 18px; }
  .check-grid label { display: flex; align-items: center; gap: 7px; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 7px 12px; cursor: pointer; font-size: 12px; transition: border-color 0.15s; }
  .check-grid label:hover { border-color: var(--accent); }
  .check-grid input[type="checkbox"]:checked + span { color: var(--accent2); }

  .decks-pill { display: inline-block; padding: 3px 9px; border-radius: 20px; font-size: 11px; font-weight: 700; }
  .decks-pill.full    { background: rgba(46,204,113,0.15); color: var(--success); }
  .decks-pill.partial { background: rgba(241,196,15,0.15); color: var(--warn); }
  .decks-pill.empty   { background: rgba(231,76,60,0.15);  color: var(--danger); }

  .CodeMirror { height: 480px; border-radius: 0 0 8px 8px; font-size: 13px; font-family: 'Consolas', 'JetBrains Mono', monospace; border: 1px solid var(--border); border-top: none; max-width: 100%; }
  .editor-toolbar { display: flex; align-items: center; justify-content: space-between; background: #1a1f2e; border: 1px solid var(--border); border-bottom: none; border-radius: 8px 8px 0 0; padding: 8px 14px; gap: 10px; flex-wrap: wrap; }
  .editor-toolbar select { width: auto; padding: 5px 10px; font-size: 12px; }
  .editor-toolbar .actions { display: flex; gap: 8px; align-items: center; }

  .modal-backdrop { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.75); z-index: 500; align-items: center; justify-content: center; }
  .modal-backdrop.open { display: flex; }
  .modal { background: var(--surface2); border: 1px solid var(--border); border-radius: 12px; padding: 26px; width: 420px; max-width: 90vw; }
  .modal h3 { color: var(--accent2); font-size: 18px; margin-bottom: 4px; }
  .modal .tag { color: var(--text-dim); font-size: 13px; margin-bottom: 20px; }
  .modal-actions { display: flex; flex-direction: column; gap: 10px; }
  .modal-actions .btn { justify-content: center; padding: 11px; font-size: 13px; }
  .modal hr { border: none; border-top: 1px solid var(--border); margin: 10px 0; }
  .modal .manual-link-label { font-size: 12px; color: var(--accent); font-weight: 700; text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 8px; }
  .modal form { display: flex; flex-direction: column; gap: 8px; }

  .cmd-add-row { display: flex; gap: 8px; margin-top: 16px; }
  .cmd-add-row input { flex: 1; }
  .cmd-add-row .cmd-trigger { max-width: 160px; }

  #data-output { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 16px; font-size: 11px; max-height: 500px; overflow: auto; color: #66fcf1; font-family: 'Consolas', monospace; white-space: pre; }
  .btn-group { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }

  .page-item { display: flex; align-items: center; justify-content: space-between; padding: 10px; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; margin-bottom: 8px; }
  .page-item-info strong { color: var(--accent2); }
  .page-item-info span { font-size: 11px; color: var(--text-dim); margin-left: 10px; }
  .page-item-actions { display: flex; gap: 8px; }

  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  @media (max-width: 768px) {
    .sidebar { transform: translateX(-100%); }
    .sidebar.open { transform: translateX(0); }
    .sidebar-overlay.open { display: block; }
    .main { margin-left: 0; padding: 20px 15px; }
    .mobile-header { display: flex; }
    .cmd-add-row { flex-direction: column; }
    .cmd-add-row .cmd-trigger { max-width: 100%; }
    .page-header { flex-direction: column; align-items: stretch; }
    .page-header .btn { justify-content: center; width: 100%; }
    .editor-toolbar { flex-direction: column; align-items: stretch; }
    .editor-toolbar select { width: 100%; }
    .editor-toolbar .actions { justify-content: space-between; margin-top: 5px; }
    .btn-group { flex-direction: column; }
    .page-item { flex-direction: column; align-items: flex-start; gap: 10px; }
  }
</style>
</head>
<body>

<div id="actionModal" class="modal-backdrop">
  <div class="modal">
    <h3 id="modalName">Player Name</h3>
    <p class="tag" id="modalTag">#TAG</p>
    <div class="modal-actions">
      <a id="btnProfile" href="#" class="btn btn-blue">📊 View Player Analytics</a>
      <a id="btnAdmin"   href="#" class="btn btn-gold">🔑 Grant HQ Dashboard Access</a>
      <hr>
      <p class="manual-link-label">🔗 Manually Map Discord Account</p>
      <form method="POST" action="/admin/manual-link">
        <input type="hidden" name="player_tag" id="modalInputTag">
        <input type="text" name="discord_id" placeholder="Paste 18-digit Discord ID..." required>
        <button type="submit" class="btn btn-green" style="width:100%; justify-content:center; padding:11px;">Link Account</button>
      </form>
      <button onclick="closeModal()" class="btn btn-ghost" style="justify-content:center;">Cancel</button>
    </div>
  </div>
</div>

<div id="sidebarOverlay" class="sidebar-overlay" onclick="toggleSidebar()"></div>
<aside class="sidebar" id="sidebar">
  <div class="sidebar-brand">
    <div class="skull">💀</div>
    <h2>Graveyard HQ</h2>
    <p>Control Panel</p>
  </div>

  <div class="nav-section">
    <p class="nav-label">Dashboard</p>
    <button class="nav-item active" onclick="showTab('war')" id="nav-war">
      <span class="icon">⚔️</span> War Monitor
      <span class="badge" id="badge-decks">{{ total_decks_left }}</span>
    </button>
    <button class="nav-item" onclick="showTab('commands')" id="nav-commands">
      <span class="icon">💬</span> Custom Commands
    </button>
  </div>

  <div class="nav-section">
    <p class="nav-label">Tools</p>
    <button class="nav-item" onclick="showTab('data')" id="nav-data">
      <span class="icon">🗄️</span> Data & API
    </button>
    <button class="nav-item" onclick="showTab('export')" id="nav-export">
      <span class="icon">📥</span> CSV Export
    </button>
    <button class="nav-item" onclick="showTab('editor')" id="nav-editor">
      <span class="icon">🎨</span> UI Editor
    </button>
    <button class="nav-item" onclick="showTab('pages')" id="nav-pages">
      <span class="icon">📄</span> Custom Pages
    </button>
    <button class="nav-item" onclick="showTab('config')" id="nav-config">
      <span class="icon">🛠️</span> System Config
    </button>
  </div>

  <div class="nav-section">
    <p class="nav-label">Site</p>
    <a class="nav-item" href="/">
      <span class="icon">👥</span> Clan Roster
    </a>
  </div>

  <div class="sidebar-footer">
    Logged in as<br><strong>@{{ session['discord_name'] }}</strong>
  </div>
</aside>

<main class="main">

  <div class="mobile-header">
    <h2>💀 Graveyard HQ</h2>
    <button class="hamburger" onclick="toggleSidebar()">☰</button>
  </div>

  {% if success or error %}
  <div class="flash {{ 'ok' if success else 'err' }}">
    {{ '✅ ' + success if success else '❌ ' + error }}
  </div>
  {% endif %}

  <div class="tab-pane active" id="tab-war">
    <div class="page-header">
      <div>
        <h1>⚔️ War Monitor</h1>
        <p>Live river race deck tracking for {{ war_players | length }} participants</p>
      </div>
      <a href="/admin/mass-ping" class="btn btn-warn">🚨 Broadcast Mass War Alarm</a>
    </div>
    <div class="stat-row">
      <div class="stat-card"><div class="label">Active Members</div><div class="value teal">{{ war_players | length }}</div></div>
      <div class="stat-card"><div class="label">Unused Decks Today</div><div class="value {{ 'red' if total_decks_left > 0 else 'green' }}">{{ total_decks_left }}</div></div>
      <div class="stat-card"><div class="label">Discord Mappings</div><div class="value green">{{ linked_count }}</div></div>
    </div>
    <div class="panel">
      <h3>Operational Deck Monitor</h3>
      <div class="table-responsive">
        <table>
          <thead><tr><th>Member</th><th>Role</th><th>Fame</th><th>Decks Used</th><th>Decks Left</th><th>Action</th></tr></thead>
          <tbody>
            {% for p in war_players %}
            <tr>
              <td>
                <a href="#" onclick="openModal('{{ p.name | e }}', '{{ p.tag }}'); return false;" style="color: var(--accent2); font-weight: 600; text-decoration: none; border-bottom: 1px dashed var(--accent);">{{ p.name }}</a>
                <div style="font-size: 11px; color: var(--text-dim);">#{{ p.tag }}</div>
              </td>
              <td style="color: var(--text-dim);">{{ p.role }}</td>
              <td style="color: var(--success); font-weight: 600;">⚡ {{ p.fame }}</td>
              <td>{{ p.decksUsedToday }} / 4</td>
              <td>
                {% if p.decksRemaining == 0 %}<span class="decks-pill full">✓ Done</span>
                {% elif p.decksRemaining <= 2 %}<span class="decks-pill partial">{{ p.decksRemaining }} left</span>
                {% else %}<span class="decks-pill empty">{{ p.decksRemaining }} left</span>{% endif %}
              </td>
              <td>
                {% if p.decksRemaining > 0 %}<a href="/admin/ping/{{ p.name }}/{{ p.decksRemaining }}" class="btn btn-teal btn-sm">Nudge</a>
                {% else %}<span style="color: var(--text-dim); font-size: 11px;">—</span>{% endif %}
              </td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <div class="tab-pane" id="tab-commands">
    <div class="page-header"><div><h1>💬 Custom Commands</h1><p>Auto-responder rules for the Discord bot</p></div></div>
    <div class="panel">
      <h3>Active Commands</h3>
      <div class="table-responsive">
        <table>
          <thead><tr><th style="width:200px;">Trigger</th><th>Response</th><th style="width:80px;">Action</th></tr></thead>
          <tbody>
            {% for cmd in custom_commands %}
            <tr>
              <td style="font-weight: 700; color: var(--accent2);">{{ sys_config.command_prefix or '!' }}{{ cmd._id }}</td>
              <td style="color: var(--text);">{{ cmd.response }}</td>
              <td><a href="/admin/delete-command/{{ cmd._id }}" class="btn btn-warn btn-sm" onclick="return confirm('Delete {{ cmd._id }}?')">Delete</a></td>
            </tr>
            {% else %}
            <tr><td colspan="3" style="color: var(--text-dim); text-align: center; padding: 24px;">No custom commands yet.</td></tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
      <form method="POST" action="/admin/add-command" class="cmd-add-row">
        <input type="text" name="trigger" placeholder="Trigger (e.g. rules)" class="cmd-trigger" required>
        <input type="text" name="response" placeholder="Bot response text..." required>
        <button type="submit" class="btn btn-green">+ Add</button>
      </form>
    </div>
  </div>

  <div class="tab-pane" id="tab-data">
    <div class="page-header">
      <div><h1>🗄️ Data & API Control</h1><p>Manual fetch triggers, cache control, and raw data viewer</p></div>
    </div>

    <div class="panel">
      <h3>⚡ Manual API Triggers</h3>
      <div class="btn-group">
        <a href="/admin/fetch/clan" class="btn btn-teal">🏰 Refresh Clan Data</a>
        <a href="/admin/fetch/riverrace" class="btn btn-teal">⚔️ Refresh River Race</a>
        <a href="/admin/fetch/all-profiles" class="btn btn-gold" onclick="return confirm('This will fetch all 50 player profiles one by one in the background loop. Continue?')">👥 Scrape All Profiles</a>
        <a href="/admin/clear-cache" class="btn btn-ghost">🗑️ Clear All Caches</a>
        <a href="/admin/reload-cog" class="btn btn-blue">🔄 Reload Bot Commands</a>
      </div>
    </div>

    <div class="panel">
      <h3>🔍 Raw Data Viewer</h3>
      <div class="btn-group">
        <button onclick="loadData('users')" class="btn btn-blue">👤 Users</button>
        <button onclick="loadData('player_profiles')" class="btn btn-blue">🎮 Player Profiles</button>
        <button onclick="loadData('custom_commands')" class="btn btn-blue">💬 Commands</button>
        <button onclick="loadSnapshot('clan')" class="btn btn-ghost">🏰 Clan Snapshot</button>
        <button onclick="loadSnapshot('riverrace')" class="btn btn-ghost">⚔️ War Snapshot</button>
      </div>
      <div id="data-output">Select a collection above to view raw data.</div>
    </div>
  </div>

  <div class="tab-pane" id="tab-export">
    <div class="page-header"><div><h1>📥 Advanced Export Builder</h1><p>Build custom reports and filter data for external tracking</p></div></div>
    <div class="panel">
      <h3>Build Your Report</h3>
      <form method="POST" action="/admin/export/custom" id="exportForm" onsubmit="handleExportSubmit()">
        <div class="form-group" style="max-width: 250px; margin-bottom: 24px;">
          <label>Export Format</label>
          <select name="export_format">
            <option value="csv">Raw Comma Separated (.csv)</option>
            <option value="xlsx">Excel Spreadsheet (.xlsx)</option>
            <option value="json">JSON Data (.json)</option>
          </select>
        </div>
        <p style="font-size: 12px; color: var(--text-dim); margin-bottom: 16px;">Select the data points to include:</p>

        <div style="display:flex; align-items:center; justify-content:space-between; max-width:500px; margin-bottom:10px;">
          <p style="font-size:11px; font-weight:700; color:var(--accent); text-transform:uppercase; letter-spacing:0.8px; margin:0;">Player Identity</p>
          <label style="font-size:11px; color:var(--text-dim); cursor:pointer;"><input type="checkbox" onchange="toggleGroup(this,'grp-identity')" checked> Select All</label>
        </div>
        <div class="check-grid grp-identity">
          <label><input type="checkbox" name="fields" value="name" checked><span>Player Name</span></label>
          <label><input type="checkbox" name="fields" value="tag"  checked><span>Player Tag</span></label>
          <label><input type="checkbox" name="fields" value="role" checked><span>Clan Role</span></label>
          <label><input type="checkbox" name="fields" value="expLevel"><span>XP Level</span></label>
          <label><input type="checkbox" name="fields" value="favoriteCard"><span>Favorite Card</span></label>
        </div>

        <div style="display:flex; align-items:center; justify-content:space-between; max-width:500px; margin-top:18px; margin-bottom:10px;">
          <p style="font-size:11px; font-weight:700; color:var(--accent); text-transform:uppercase; letter-spacing:0.8px; margin:0;">Progression & Social</p>
          <label style="font-size:11px; color:var(--text-dim); cursor:pointer;"><input type="checkbox" onchange="toggleGroup(this,'grp-social')"> Select All</label>
        </div>
        <div class="check-grid grp-social">
          <label><input type="checkbox" name="fields" value="trophies"><span>Current Trophies</span></label>
          <label><input type="checkbox" name="fields" value="donations"><span>Donations Given</span></label>
          <label><input type="checkbox" name="fields" value="donationsReceived"><span>Donations Received</span></label>
        </div>

        <div style="display:flex; align-items:center; justify-content:space-between; max-width:500px; margin-top:18px; margin-bottom:10px;">
          <p style="font-size:11px; font-weight:700; color:var(--accent); text-transform:uppercase; letter-spacing:0.8px; margin:0;">Combat Analytics</p>
          <label style="font-size:11px; color:var(--text-dim); cursor:pointer;"><input type="checkbox" onchange="toggleGroup(this,'grp-combat')"> Select All</label>
        </div>
        <div class="check-grid grp-combat">
          <label><input type="checkbox" name="fields" value="totalWins"><span>Total Wins</span></label>
          <label><input type="checkbox" name="fields" value="totalLosses"><span>Total Losses</span></label>
          <label><input type="checkbox" name="fields" value="current_streak"><span>Current Win Streak</span></label>
        </div>

        <div style="display:flex; align-items:center; justify-content:space-between; max-width:500px; margin-top:18px; margin-bottom:10px;">
          <p style="font-size:11px; font-weight:700; color:var(--accent); text-transform:uppercase; letter-spacing:0.8px; margin:0;">Live River Race Data</p>
          <label style="font-size:11px; color:var(--text-dim); cursor:pointer;"><input type="checkbox" onchange="toggleGroup(this,'grp-war')" checked> Select All</label>
        </div>
        <div class="check-grid grp-war">
          <label><input type="checkbox" name="fields" value="fame"           checked><span>War Fame</span></label>
          <label><input type="checkbox" name="fields" value="decksUsedToday" checked><span>Decks Used</span></label>
          <label><input type="checkbox" name="fields" value="decksRemaining" checked><span>Decks Remaining</span></label>
          <label><input type="checkbox" name="fields" value="warDayWins"><span>Lifetime War Wins</span></label>
        </div>

        <button type="submit" id="exportBtn" class="btn btn-gold" style="margin-top:24px; padding:10px 20px; font-size:14px;">⬇️ Generate File</button>
      </form>
    </div>
  </div>

  <div class="tab-pane" id="tab-editor">
    <div class="page-header">
      <div><h1>🎨 Live UI Editor</h1><p>Edit page templates with live HTML/CSS. Changes deploy after validation.</p></div>
      <a href="/admin/reset-html" class="btn btn-warn btn-sm" onclick="return confirm('Factory reset ALL templates? This cannot be undone.')">⚠️ Reset All Templates</a>
    </div>
    <div id="deploy-status" style="display:none;" class="flash"></div>
    <div class="panel">
      <h3>Code Editor</h3>
      <p style="font-size:12px; color:var(--text-dim); margin-bottom:16px;">Templates are <strong>validated before saving</strong> — broken Jinja syntax will be caught and rejected.</p>
      <div class="editor-toolbar">
        <select id="template_selector" onchange="switchTemplate()">
          <option value="roster">Public Roster Page</option>
          <option value="player">Player Profile Page</option>
          <option value="link">Link Account Page</option>
          <option value="admin">Control Panel (this page)</option>
        </select>
        <div class="actions">
          <button type="button" onclick="formatCode()" class="btn btn-ghost btn-sm">✨ Beautify</button>
          <button type="button" onclick="validateOnly()" class="btn btn-blue btn-sm">🔍 Validate Only</button>
          <button type="button" onclick="safeDeployTemplate()" class="btn btn-gold">💾 Deploy Live</button>
        </div>
      </div>
      <div id="codemirror_container"></div>
    </div>
  </div>

  <div class="tab-pane" id="tab-pages">
    <div class="page-header">
      <div><h1>📄 Custom Pages</h1><p>Create pages available at <strong>/p/your-slug</strong>. Full Jinja2 and HTML/CSS supported.</p></div>
    </div>

    <div class="panel">
      <h3>Existing Pages</h3>
      {% for page in custom_pages %}
      <div class="page-item">
        <div class="page-item-info">
          <strong>/p/{{ page._id }}</strong>
          <span>Updated: {{ page.updated }}</span>
        </div>
        <div class="page-item-actions">
          <a href="/p/{{ page._id }}" target="_blank" class="btn btn-ghost btn-sm">View</a>
          <button onclick="loadPageForEdit('{{ page._id }}')" class="btn btn-blue btn-sm">Edit</button>
          <a href="/admin/pages/delete/{{ page._id }}" class="btn btn-warn btn-sm" onclick="return confirm('Delete /p/{{ page._id }}?')">Delete</a>
        </div>
      </div>
      {% else %}
      <p style="color:var(--text-dim); font-size:13px; margin-bottom:16px;">No custom pages yet. Create one below.</p>
      {% endfor %}
    </div>

    <div class="panel">
      <h3>Create / Edit Page</h3>
      <div id="page-deploy-status" style="display:none;" class="flash"></div>
      <div class="form-group" style="max-width:300px;">
        <label>Page Slug</label>
        <input type="text" id="page-slug-input" placeholder="e.g. rules → available at /p/rules">
        <div class="hint">Lowercase, no spaces. Use hyphens.</div>
      </div>
      <div class="editor-toolbar">
        <span style="font-size:12px; color:var(--text-dim);">Page HTML Editor</span>
        <div class="actions">
          <button type="button" onclick="formatPageCode()" class="btn btn-ghost btn-sm">✨ Beautify</button>
          <button type="button" onclick="deployPage()" class="btn btn-gold">🚀 Deploy Page</button>
        </div>
      </div>
      <div id="page-editor-container"></div>
    </div>
  </div>

  <div class="tab-pane" id="tab-config">
    <div class="page-header"><div><h1>🛠️ System Configuration</h1><p>Bot settings, feature flags, and access control</p></div></div>
    <form method="POST" action="/admin/update-system-config">
      <div class="panel">
        <h3>Bot Settings</h3>
        <div class="form-group" style="max-width:200px;">
          <label>Command Prefix</label>
          <input type="text" name="command_prefix" value="{{ sys_config.command_prefix or '!' }}" maxlength="3">
        </div>
        <div class="form-group" style="max-width:400px;">
          <label>War Nudge Channel ID</label>
          <input type="text" name="war_channel_id" value="{{ sys_config.war_channel_id or '' }}" placeholder="Discord channel snowflake ID">
        </div>
        <div class="form-group" style="max-width:700px;">
          <label>Ignored / Muted Channels</label>
          <input type="text" name="ignored_channels" value="{{ sys_config.ignored_channels | join(', ') if sys_config.ignored_channels else '' }}" placeholder="Channel IDs, comma-separated">
        </div>
      </div>
      <div class="panel">
        <h3>Feature Flags</h3>
        <div class="checkbox-row">
          <input type="checkbox" name="maintenance_mode" id="maintenance_mode" {% if sys_config.maintenance_mode %}checked{% endif %}>
          <div><label for="maintenance_mode">Enable Global Maintenance Mode</label><div class="sub">Disables all bot commands.</div></div>
        </div>
        <div class="checkbox-row">
          <input type="checkbox" name="feature_auto_pings" id="feature_auto_pings" {% if sys_config.feature_auto_pings %}checked{% endif %}>
          <div><label for="feature_auto_pings">Enable Automated War Pings</label><div class="sub">Sends scheduled reminders.</div></div>
        </div>
      </div>
      <div class="panel">
        <h3>Access Control</h3>
        <div class="form-group">
          <label>Authorized Admin Role IDs</label>
          <input type="text" name="admin_role_ids" value="{{ sys_config.admin_role_ids | join(', ') if sys_config.admin_role_ids else '' }}" placeholder="Role IDs, comma-separated">
        </div>
        <div class="form-group">
          <label>Authorized Admin User IDs</label>
          <input type="text" name="admin_user_ids" value="{{ sys_config.admin_user_ids | join(', ') if sys_config.admin_user_ids else '' }}" placeholder="User IDs, comma-separated">
        </div>
      </div>
      <button type="submit" class="btn btn-cyan">💾 Save All Settings</button>
    </form>
  </div>

</main>

<div style="display:none;">
  <textarea id="raw_roster">{{ raw_roster }}</textarea>
  <textarea id="raw_player">{{ raw_player }}</textarea>
  <textarea id="raw_link">{{ raw_link }}</textarea>
  <textarea id="raw_admin">{{ raw_admin }}</textarea>
</div>

<script>
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('sidebarOverlay').classList.toggle('open');
}
function showTab(name) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  document.getElementById('nav-' + name).classList.add('active');
  if (name === 'editor' && !window._cmReady) initCodeMirror();
  if (name === 'pages' && !window._pageCmReady) initPageEditor();
  if (window.innerWidth <= 768) {
    document.getElementById('sidebar').classList.remove('open');
    document.getElementById('sidebarOverlay').classList.remove('open');
  }
}
function openModal(name, tag) {
  document.getElementById('modalName').innerText = name;
  document.getElementById('modalTag').innerText = '#' + tag;
  document.getElementById('btnProfile').href = '/player/' + tag;
  document.getElementById('btnAdmin').href = '/admin/grant-role/' + tag;
  document.getElementById('modalInputTag').value = tag;
  document.getElementById('actionModal').classList.add('open');
}
function closeModal() { document.getElementById('actionModal').classList.remove('open'); }
document.getElementById('actionModal').addEventListener('click', function(e) { if (e.target === this) closeModal(); });

function toggleGroup(masterCheckbox, groupClass) {
  document.querySelectorAll('.' + groupClass + ' input[type="checkbox"]').forEach(cb => cb.checked = masterCheckbox.checked);
}
function handleExportSubmit() {
  const btn = document.getElementById('exportBtn');
  btn.disabled = true; btn.style.opacity = '0.7'; btn.innerHTML = '⏳ Compiling Data...';
  setTimeout(() => { btn.disabled = false; btn.style.opacity = '1'; btn.innerHTML = '⬇️ Generate File'; }, 3000);
}

// ── TEMPLATE EDITOR ──
var _cm = null;
window._cmReady = false;
function initCodeMirror() {
  _cm = CodeMirror(document.getElementById('codemirror_container'), {
    mode: 'htmlmixed', theme: 'dracula', lineNumbers: true, matchBrackets: true,
    autoCloseTags: true, lineWrapping: true, tabSize: 2, indentWithTabs: false,
    value: document.getElementById('raw_roster').value
  });
  window._cmReady = true;
}
function switchTemplate() {
  var sel = document.getElementById('template_selector').value;
  if (_cm) { _cm.setValue(document.getElementById('raw_' + sel).value); _cm.refresh(); }
}
function formatCode() {
  if (!_cm) return;
  if (typeof html_beautify !== 'undefined') { _cm.setValue(html_beautify(_cm.getValue(), { indent_size: 2, wrap_line_length: 0, preserve_newlines: true })); }
  else { alert('Beautifier not loaded yet.'); }
}
function setStatus(ok, message) {
  var el = document.getElementById('deploy-status');
  el.style.display = 'block'; el.className = 'flash ' + (ok ? 'ok' : 'err');
  el.textContent = (ok ? '✅ ' : '❌ ') + message;
  el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
function jumpToLine(line) { if (_cm && line) { _cm.setCursor({ line: line - 1, ch: 0 }); _cm.focus(); } }
async function validateOnly() {
  if (!_cm) return;
  setStatus(true, 'Validating...');
  try {
    var res = await fetch('/admin/validate-template', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ template_name: document.getElementById('template_selector').value, html: _cm.getValue() }) });
    var data = await res.json();
    setStatus(data.ok, data.message);
    if (!data.ok) jumpToLine(data.line);
  } catch(e) { setStatus(false, 'Network error: ' + e.message); }
}
async function safeDeployTemplate() {
  if (!_cm) return;
  var templateName = document.getElementById('template_selector').value;
  setStatus(true, 'Validating before save...');
  try {
    var res = await fetch('/admin/save-template-safe', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ template_name: templateName, html: _cm.getValue() }) });
    var data = await res.json();
    setStatus(data.ok, data.message);
    if (!data.ok) { jumpToLine(data.line); }
    else if (templateName === 'admin') { document.getElementById('deploy-status').textContent += ' Reloading in 2s...'; setTimeout(() => location.reload(), 2000); }
  } catch(e) { setStatus(false, 'Network error: ' + e.message); }
}

// ── PAGE EDITOR ──
var _pageCm = null;
window._pageCmReady = false;
function initPageEditor() {
  _pageCm = CodeMirror(document.getElementById('page-editor-container'), {
    mode: 'htmlmixed', theme: 'dracula', lineNumbers: true, matchBrackets: true,
    autoCloseTags: true, lineWrapping: true, tabSize: 2, indentWithTabs: false,
    value: '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="UTF-8">\n<title>My Page</title>\n</head>\n<body>\n\n</body>\n</html>'
  });
  window._pageCmReady = true;
}
function formatPageCode() {
  if (_pageCm && typeof html_beautify !== 'undefined') { _pageCm.setValue(html_beautify(_pageCm.getValue(), { indent_size: 2 })); }
}
function setPageStatus(ok, message) {
  var el = document.getElementById('page-deploy-status');
  el.style.display = 'block'; el.className = 'flash ' + (ok ? 'ok' : 'err');
  el.textContent = (ok ? '✅ ' : '❌ ') + message;
}
async function deployPage() {
  if (!_pageCm) return;
  var slug = document.getElementById('page-slug-input').value.trim();
  if (!slug) { setPageStatus(false, 'Please enter a slug.'); return; }
  try {
    var res = await fetch('/admin/pages/save', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ slug: slug, html: _pageCm.getValue() }) });
    var data = await res.json();
    setPageStatus(data.ok, data.message);
  } catch(e) { setPageStatus(false, 'Network error: ' + e.message); }
}
async function loadPageForEdit(slug) {
  showTab('pages');
  document.getElementById('page-slug-input').value = slug;
  try {
    var res = await fetch('/admin/pages/get/' + slug);
    var data = await res.json();
    if (_pageCm && data.html) { _pageCm.setValue(data.html); }
  } catch(e) { setPageStatus(false, 'Could not load page: ' + e.message); }
}

// ── DATA VIEWER ──
async function loadData(collection) {
  document.getElementById('data-output').textContent = 'Loading ' + collection + '...';
  try {
    var res = await fetch('/admin/data/' + collection);
    var data = await res.json();
    document.getElementById('data-output').textContent = JSON.stringify(data, null, 2);
  } catch(e) { document.getElementById('data-output').textContent = 'Error: ' + e.message; }
}
async function loadSnapshot(name) {
  document.getElementById('data-output').textContent = 'Loading ' + name + ' snapshot...';
  try {
    var res = await fetch('/admin/snapshot/' + name);
    var data = await res.json();
    document.getElementById('data-output').textContent = JSON.stringify(data, null, 2);
  } catch(e) { document.getElementById('data-output').textContent = 'Error: ' + e.message; }
}

// ── HASH ROUTING ──
(function() {
  var hash = window.location.hash.replace('#', '');
  var valid = ['war', 'commands', 'data', 'export', 'editor', 'pages', 'config'];
  if (valid.indexOf(hash) > -1) showTab(hash);
})();
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
# FLASK ROUTE CONTROLLERS
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    if "discord_id" in session:
        session["is_admin_user"] = is_admin()

    data = fetch_cr_api(f"clans/%23{clean_tag(CLAN_TAG)}")
    if not data or "memberList" not in data:
        log.error(f"❌ Roster load failed! API response context: {data}")
        return "<h1>Clan data missing from API response. Check your deployment logs.</h1>", 500

    raw_members = data.get("memberList", [])
    member_tags = [clean_tag(m["tag"]) for m in raw_members if "tag" in m]

    try:
        profiles = list(db_sync["player_profiles"].find({"_id": {"$in": member_tags}}))
        profile_map = {p["_id"]: p for p in profiles if "_id" in p}
    except Exception as e:
        log.error(f"Database tracking profiles failure: {e}")
        profile_map = {}

    # Run the change auditor tracking checks before updating current session flags
    _process_roster_changes(member_tags, profile_map, raw_members)

    db_sync["player_profiles"].update_many(
        {"_id": {"$in": member_tags}},
        {"$set": {"in_clan_last_seen": True}}
    )

    left_members = list(db_sync["player_profiles"].find({
        "_id": {"$nin": member_tags, "$in": list(profile_map.keys())},
        "in_clan_last_seen": True
    }))
    
    if left_members:
        left_tags = [p["_id"] for p in left_members]
        db_sync["player_profiles"].update_many(
            {"_id": {"$in": left_tags}},
            {"$set": {"in_clan_last_seen": False}}
        )
        for p in left_members:
            if p.get("last_departure_status") != "kicked":
                db_sync["player_profiles"].update_one(
                    {"_id": p["_id"]},
                    {"$set": {"last_departure_status": "left"}}
                )

    war_data = fetch_cr_api(f"clans/%23{clean_tag(CLAN_TAG)}/currentriverrace")
    war_participants = _build_war_participants(war_data)

    players = _enrich_members(raw_members, profile_map, war_participants)

    top_pusher = max(players, key=lambda x: x.get("trophies", 0), default=None)
    top_streak = max(players, key=lambda x: x.get("current_streak", 0), default=None)
    top_war    = max(players, key=lambda x: x.get("warDayWins", 0), default=None)

    return render_sandboxed(
        get_template("roster"),
        players=players,
        top_pusher=top_pusher,
        top_streak=top_streak,
        top_war=top_war,
    )


@app.route("/player/<tag>")
def web_profile(tag):
    tag = clean_tag(tag)
    data = fetch_cr_api(f"players/%23{tag}")
    if not data:
        return "<h1>Player data not found.</h1>", 404
    return render_sandboxed(get_template("player"), data=data, max_lvl=MAX_CARD_LEVEL)


@app.route("/p/<slug>")
def custom_page(slug):
    doc = db_sync["pages"].find_one({"_id": slug})
    if not doc:
        return "<h1>Page not found.</h1>", 404
    return render_sandboxed(doc["html"], max_lvl=MAX_CARD_LEVEL)


@app.route("/login")
def login():
    if not DISCORD_CLIENT_ID:
        return "Discord Client ID not configured.", 500
    scope = "identify guilds.members.read" if GUILD_ID else "identify"
    url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        f"&response_type=code"
        f"&scope={urllib.parse.quote(scope)}"
    )
    return redirect(url)


@app.route("/link", methods=["GET", "POST"])
def web_link():
    if "discord_id" not in session:
        return redirect("/login")
    error_msg = None
    if request.method == "POST":
        tag = clean_tag(request.form.get("tag", ""))
        cr_data = fetch_cr_api(f"players/%23{tag}")
        if cr_data and "name" in cr_data:
            users_sync.update_one(
                {"_id": session["discord_id"]},
                {"$set": {"player_id": tag}},
                upsert=True,
            )
            return redirect(f"/player/{tag}")
        else:
            error_msg = "Could not find a Clash Royale account with that tag."
    return render_sandboxed(
        get_template("link"),
        name=session.get("discord_name", "Unknown"),
        error=error_msg,
    )


@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "Authentication Failed.", 400

    token_response = requests.post(
        "https://discord.com/api/oauth2/token",
        data={
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    token_data = token_response.json()
    if "access_token" not in token_data:
        return "OAuth token extraction failed. Please log in again.", 400

    token = token_data["access_token"]
    user_data = requests.get(
        "https://discord.com/api/users/@me",
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    if "id" not in user_data:
        return "Failed to fetch Discord user info.", 400

    session.permanent = True
    session["discord_id"] = user_data["id"]
    session["discord_name"] = user_data["username"]
    session["user_roles"] = get_user_guild_roles(token)
    session["is_admin_user"] = is_admin()

    return redirect("/admin" if session["is_admin_user"] else "/link")


@app.route("/admin")
def admin_panel():
    if not is_admin():
        return "<h1>Unauthorized Access Denied.</h1>", 403

    war_data = fetch_cr_api(f"clans/%23{CLAN_TAG}/currentriverrace")
    raw_participants = []

    if war_data and "clan" in war_data and "participants" in war_data["clan"]:
        raw_participants = war_data["clan"]["participants"]
    elif war_data and "clans" in war_data:
        for c in war_data["clans"]:
            if clean_tag(c.get("tag", "")) == CLAN_TAG:
                raw_participants = c.get("participants", [])
                break

    sys_config_db = _get_cached_system_config()
    war_players = []
    total_decks_left = 0

    for p in raw_participants:
        p_tag = clean_tag(p.get("tag", ""))
        decks_used_today = p.get("decksUsedToday", 0)
        decks_remaining = max(0, 4 - decks_used_today)
        war_players.append({
            "tag": p_tag,
            "name": p.get("name", "Unknown"),
            "role": p.get("role", "Member").replace("_", " ").title(),
            "fame": p.get("fame", 0),
            "decksUsedToday": decks_used_today,
            "decksRemaining": decks_remaining,
        })
        total_decks_left += decks_remaining

    war_players.sort(key=lambda x: -x["decksRemaining"])
    linked_count = users_sync.count_documents({})
    all_custom_cmds = list(custom_cmds_sync.find())
    custom_pages = list(db_sync["pages"].find())

    return render_sandboxed(
        get_template("admin"),
        war_players=war_players,
        total_decks_left=total_decks_left,
        linked_count=linked_count,
        sys_config=sys_config_db,
        custom_commands=all_custom_cmds,
        custom_pages=custom_pages,
        raw_roster=get_template("roster"),
        raw_player=get_template("player"),
        raw_link=get_template("link"),
        raw_admin=get_template("admin"),
        raw_custom_csv=app.config.get("RAW_CSV_TEMPLATE_FALLBACK", ""),
        success=request.args.get("success"),
        error=request.args.get("error"),
    )


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

        self.mongo_client = AsyncIOMotorClient(mongo_url)
        self.db = self.mongo_client["graveyardbot"]
        self.db_users = self.db["users"]
        self.custom_cmds = self.db["custom_commands"]

    def _cr_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {os.getenv('CR_TOKEN')}",
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
    threading.Thread(target=run_flask, daemon=True).start()
    bot.run(os.getenv("DISCORD_TOKEN"))
