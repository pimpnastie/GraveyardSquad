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
import discord
from discord.ext import commands
from dotenv import load_dotenv
from flask import Flask, render_template_string, request, redirect, session
from waitress import serve
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient
import redis.asyncio as redis

# --- 1. AUTOMATED ENVIRONMENT SETUP ---
def sync_environment():
    if os.name != 'nt':
        return  # Render handles dependencies at build time. Skip to prevent loops on Linux.
        
    req_file = "requirements.txt"
    venv_dir = "venv"
    is_venv = sys.prefix != sys.base_prefix or os.path.exists(venv_dir)

    if not os.path.exists(req_file):
        with open(req_file, "w") as f:
            f.write("discord.py\naiohttp\nmotor\npymongo\nredis\nflask\npython-dotenv\nthefuzz\nwaitress\nopenpyxl\ntzdata\nrequests\n")
        print(f"✅ Created default {req_file}")

    if not is_venv:
        subprocess.check_call([sys.executable, "-m", "venv", venv_dir])
        python_exe = os.path.join(venv_dir, "Scripts", "python.exe")
        subprocess.check_call([python_exe, "-m", "pip", "install", "-r", req_file])
        os.execv(python_exe, [python_exe] + sys.argv)
    else:
        try:
            import pkg_resources
            with open(req_file, "r") as f:
                required_packages = [line.strip() for line in f if line.strip() and not line.startswith("#")]
            pkg_resources.require(required_packages)
        except Exception:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req_file])

sync_environment()

# --- 2. STANDARD IMPORTS & SETUP ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)-10s %(message)s")
log = logging.getLogger("mainbot")

# --- 3. UNIFIED FLASK WEB INFRASTRUCTURE ---
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "graveyard_squad_permanent_secret_key_1993")

CR_API_KEY = os.getenv("CR_TOKEN")
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

import redis as sync_redis
redis_sync_client = sync_redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))

def fetch_cr_api(endpoint: str) -> dict | None:
    headers = {"Authorization": f"Bearer {CR_API_KEY}", "Accept": "application/json"}
    url = f"https://proxy.royaleapi.dev/v1/{endpoint}"
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict):
                if "cards" in data:
                    for c in data["cards"]:
                        c["level"] = MAX_CARD_LEVEL - c.get("maxLevel", MAX_CARD_LEVEL) + c.get("level", 1)
                if "currentDeck" in data:
                    for c in data["currentDeck"]:
                        c["level"] = MAX_CARD_LEVEL - c.get("maxLevel", MAX_CARD_LEVEL) + c.get("level", 1)
            return data
        return None
    except Exception as e:
        log.error(f"Flask API Request failed: {e}")
        return None

def get_user_guild_roles(token: str) -> list:
    url = f"https://discord.com/api/users/@me/guilds/{GUILD_ID}/member"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200:
            return r.json().get("roles", [])
    except Exception as e:
        log.error(f"Failed to fetch guild roles: {e}")
    return []

def is_admin():
    if "discord_id" not in session:
        return False
    if session.get("discord_id") == os.getenv("ADMIN_OWNER_ID", "751975709643112569"):
        return True
    sys_config_db = db_sync["config"].find_one({"_id": "system_config_file"}) or {}
    allowed_roles = sys_config_db.get("admin_role_ids", [])
    user_roles = session.get("user_roles", [])
    return any(str(role_id) in allowed_roles for role_id in user_roles)

# ── HTML UI PAGE STRINGS ────────────────────────────────────────────────── #
ROSTER_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Graveyard Clan Dashboard</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: #121212; color: white; font-family: 'Segoe UI', sans-serif; text-align: center; padding: 40px 20px; }
        h1 { color: #f1c40f; margin-bottom: 8px; font-size: 2rem; }
        .subtitle { color: #888; margin-bottom: 30px; font-size: 0.9rem; }
        .container { max-width: 800px; margin: auto; }
        .member-card { background: #1e1e1e; padding: 15px 20px; margin: 8px 0; border-radius: 8px; display: flex; justify-content: space-between; align-items: center; text-decoration: none; color: white; border: 1px solid #333; transition: 0.2s; }
        .member-card:hover { border-color: #f1c40f; background: #252525; }
        .member-left { display: flex; flex-direction: column; align-items: flex-start; }
        .member-name { font-weight: bold; font-size: 1rem; }
        .member-role { color: #888; font-size: 0.78rem; margin-top: 2px; text-transform: capitalize; }
        .trophies { color: #5dade2; font-weight: bold; font-size: 1rem; }
        .btn-discord { background: #5865F2; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; font-weight: bold; display: inline-block; margin-bottom: 20px; transition: 0.2s;}
        .btn-discord:hover { background: #4752C4; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🛡️ Graveyard Clan Roster</h1>
        <a href="/login" class="btn-discord">Log in with Discord</a>
        <p class="subtitle">{{ members | length }} members · Click a name to view their profile</p>
        {% for m in members %}
            <a href="/player/{{ m.tag[1:] }}" class="member-card">
                <div class="member-left">
                    <span class="member-name">{{ m.name }}</span>
                    <span class="member-role">{{ m.role }}</span>
                </div>
                <span class="trophies">🏆 {{ m.trophies }}</span>
            </a>
        {% endfor %}
    </div>
</body>
</html>
"""

LINK_HTML = """
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

PLAYER_HTML = """
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

ADMIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Graveyard Squad - HQ Panel</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: #0b0c10; color: #c5c6c7; font-family: 'Segoe UI', sans-serif; display: flex; min-height: 100vh; }
        .sidebar { width: 260px; background: #1f2833; padding: 30px 20px; display: flex; flex-direction: column; gap: 20px; border-right: 1px solid #45a29e; }
        .sidebar h2 { color: #66fcf1; font-size: 1.2rem; text-transform: uppercase; letter-spacing: 1px; }
        .sidebar a { color: #c5c6c7; text-decoration: none; padding: 12px; border-radius: 6px; transition: 0.2s; }
        .sidebar a:hover, .sidebar a.active { background: #45a29e; color: #0b0c10; font-weight: bold; }
        .main-content { flex: 1; padding: 40px; overflow-y: auto; }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #1f2833; padding-bottom: 20px; margin-bottom: 30px; }
        .header h1 { color: #66fcf1; }
        .metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 30px; }
        .metric-card { background: #1f2833; border: 1px solid #45a29e; border-radius: 8px; padding: 20px; }
        .metric-card p { color: #86c232; font-size: 1.8rem; font-weight: bold; margin-top: 5px; }
        .panel-section { background: #1f2833; border-radius: 8px; padding: 25px; margin-bottom: 30px; }
        .panel-section h3 { margin-bottom: 20px; color: #66fcf1; border-left: 4px solid #45a29e; padding-left: 10px; }
        table { width: 100%; border-collapse: collapse; text-align: left; }
        th, td { padding: 12px 15px; border-bottom: 1px solid #0b0c10; }
        th { background: #0b0c10; color: #45a29e; text-transform: uppercase; font-size: 0.8rem; }
        tr:hover { background: #252e3a; }
        .btn { background: #45a29e; color: #0b0c10; border: none; padding: 8px 16px; border-radius: 4px; font-weight: bold; cursor: pointer; transition: 0.2s; text-decoration: none; display: inline-block; }
        .btn:hover { background: #66fcf1; }
        .btn-warn { background: #e74c3c; color: white; border: none; padding: 10px 20px; text-decoration: none; border-radius: 5px; font-weight: bold; cursor: pointer; display: inline-block; }
        .form-group { margin-bottom: 15px; display: flex; flex-direction: column; gap: 5px; }
        label { font-size: 0.9rem; color: #45a29e; font-weight: bold; }
        input[type="number"], input[type="text"] { background: #0b0c10; border: 1px solid #45a29e; color: white; padding: 10px; border-radius: 4px; width: 100%; max-width: 400px; }
        .checkbox-group { display: flex; align-items: center; gap: 10px; margin: 15px 0; }
        .subtitle-desc { font-size: 0.8rem; color: #888; }
    </style>
</head>
<body>
    <div class="sidebar">
        <h2>💀 GY HQ</h2>
        <a href="/">Roster Monitor</a>
        <a href="/admin" class="active">War Dashboard</a>
        <p style="margin-top:auto; font-size:0.75rem; color:#45a29e;">User: @{{ session['discord_name'] }}</p>
    </div>

    <div class="main-content">
        <div class="header">
            <h1>Graveyard Squad Control Rig</h1>
            <a href="/admin/mass-ping" class="btn btn-warn">🚨 Broadcast Mass War Alarm</a>
        </div>

        {% if success or error %}
            <div style="padding: 15px; margin-bottom: 20px; border-radius: 5px; font-weight: bold; background: {{ '#27ae60' if success else '#c0392b' }}; color: white;">
                {{ success if success else error }}
            </div>
        {% endif %}

        <div class="metrics">
            <div class="metric-card"><h5>Active War Logs</h5><p>{{ war_players | length }} Members</p></div>
            <div class="metric-card"><h5>Unused Decks (Today)</h5><p style="color:#e74c3c;">{{ total_decks_left }}</p></div>
            <div class="metric-card"><h5>Database Mappings</h5><p>{{ linked_count }}</p></div>
        </div>

        <div class="panel-section">
            <h3>⚔️ Operational War Deck Monitor</h3>
            <table>
                <thead>
                    <tr>
                        <th>Player Name</th>
                        <th>Role</th>
                        <th>Fame Metric</th>
                        <th>Decks Used</th>
                        <th>Decks Remaining</th>
                        <th>Action Trigger</th>
                    </tr>
                </thead>
                <tbody>
                    {% for p in war_players %}
                    <tr>
                        <td style="font-weight: bold; color:white;">{{ p.name }}</td>
                        <td>{{ p.role }}</td>
                        <td style="color: #2ecc71;">⚡ {{ p.fame }}</td>
                        <td>{{ p.decksUsed }} / 4</td>
                        <td style="font-weight:bold; color: {{ '#e74c3c' if (4 - p.decksUsed) > 0 else '#2ecc71' }};">{{ 4 - p.decksUsed }}</td>
                        <td>
                            {% if (4 - p.decksUsed) > 0 %}
                                <a href="/admin/ping/{{ p.name }}/{{ 4 - p.decksUsed }}" class="btn">Nudge Player</a>
                            {% else %}
                                <span style="color:#2ecc71; font-size:0.85rem;">✓ Complete</span>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <div class="panel-section">
            <h3>🛠️ Live System File Configurations (Zero Restart)</h3>
            <form method="POST" action="/admin/update-system-config">
                <div class="form-group">
                    <label>Bot Command Prefix</label>
                    <input type="text" name="command_prefix" value="{{ sys_config.command_prefix or '!' }}" max_length="3">
                </div>
                <div class="checkbox-group">
                    <input type="checkbox" name="maintenance_mode" id="maintenance_mode" {% if sys_config.maintenance_mode %}checked{% endif %}>
                    <label for="maintenance_mode">Enable Global Maintenance Mode</label>
                </div>
                <div class="form-group">
                    <label>Discord War Nudge Channel ID</label>
                    <input type="text" name="war_channel_id" value="{{ sys_config.war_channel_id or '' }}">
                </div>
                <div class="form-group">
                    <label>Authorized Admin Role IDs</label>
                    <input type="text" name="admin_role_ids" value="{{ sys_config.admin_role_ids | join(', ') if sys_config.admin_role_ids else '' }}">
                </div>
                <div class="form-group">
                    <label>Ignored/Muted Discord Channels</label>
                    <input type="text" name="ignored_channels" value="{{ sys_config.ignored_channels | join(', ') if sys_config.ignored_channels else '' }}">
                </div>
                <button type="submit" class="btn" style="background:#66fcf1; color:#0b0c10;">Save System Variables</button>
            </form>
        </div>
    </div>
</body>
</html>
"""

# ── FLASK ROUTES ─────────────────────────────────────────────────────────── #

@app.route("/")
def index():
    data = fetch_cr_api(f"clans/%23{CLAN_TAG}")
    if not data:
        return "<h1>Clan not found or API down.</h1>", 500
    return render_template_string(ROSTER_HTML, members=data.get("memberList", []))

@app.route("/player/<tag>")
def web_profile(tag):
    data = fetch_cr_api(f"players/%23{tag}")
    if not data:
        return "<h1>Player data not found.</h1>", 404
    return render_template_string(PLAYER_HTML, data=data, max_lvl=MAX_CARD_LEVEL)

@app.route("/login")
def login():
    if not DISCORD_CLIENT_ID:
        return "Discord Client ID not configured.", 500
    scope = "identify"
    if GUILD_ID:
        scope = "identify guilds.members.read"
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
        tag = request.form.get("tag", "").upper().replace("#", "")
        cr_data = fetch_cr_api(f"players/%23{tag}")
        if cr_data and "name" in cr_data:
            users_sync.update_one(
                {"_id": session["discord_id"]},
                {"$set": {"player_id": tag}},
                upsert=True
            )
            session.clear()
            return redirect(f"/player/{tag}")
        else:
            error_msg = "Could not find a Clash Royale account with that tag."
    return render_template_string(LINK_HTML, name=session.get("discord_name", "Unknown"), error=error_msg)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "Authentication Failed.", 400

    data = {
        "client_id": DISCORD_CLIENT_ID, "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI
    }
    r = requests.post(
        "https://discord.com/api/oauth2/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )

    token_data = r.json()
    if "access_token" not in token_data:
        return "OAuth token extraction failed. Please log in again.", 400

    token = token_data["access_token"]
    user_data = requests.get(
        "https://discord.com/api/users/@me",
        headers={"Authorization": f"Bearer {token}"}
    ).json()

    if "id" not in user_data:
        return "Failed to fetch Discord user info.", 400

    session["discord_id"] = user_data["id"]
    session["discord_name"] = user_data["username"]
    session["user_roles"] = get_user_guild_roles(token)

    return redirect("/admin" if is_admin() else "/link")

@app.route("/admin")
def admin_panel():
    if not is_admin():
        return "<h1>Unauthorized Access Denied.</h1>", 403

    war_data = fetch_cr_api(f"clans/%23{CLAN_TAG}/currentriverrace")
    war_players = []
    total_decks_left = 0

    if war_data and "clan" in war_data and "participants" in war_data["clan"]:
        war_players = war_data["clan"]["participants"]
    elif war_data and "clans" in war_data:
        for c in war_data["clans"]:
            if c.get("tag", "").replace("#", "").upper() == CLAN_TAG.upper():
                war_players = c.get("participants", [])
                break

    if war_players:
        war_players = sorted(war_players, key=lambda x: x.get("decksUsed", 0))
        for p in war_players:
            total_decks_left += max(0, 4 - p.get("decksUsed", 0))

    linked_count = users_sync.count_documents({})
    config_db = db_sync["config"].find_one({"_id": "global_bot_settings"}) or {}
    sys_config_db = db_sync["config"].find_one({"_id": "system_config_file"}) or {}

    return render_template_string(
        ADMIN_HTML, war_players=war_players, total_decks_left=total_decks_left,
        linked_count=linked_count, config=config_db, sys_config=sys_config_db,
        success=request.args.get('success'), error=request.args.get('error')
    )

@app.route("/admin/update-system-config", methods=["POST"])
def update_system_config():
    if not is_admin():
        return "Unauthorized", 403
    db_sync["config"].update_one(
        {"_id": "system_config_file"},
        {"$set": {
            "command_prefix": request.form.get("command_prefix", "!"),
            "maintenance_mode": bool(request.form.get("maintenance_mode")),
            "war_channel_id": request.form.get("war_channel_id", "").strip(),
            "ignored_channels": [c.strip() for c in request.form.get("ignored_channels", "").split(",") if c.strip()],
            "admin_role_ids": [r.strip() for r in request.form.get("admin_role_ids", "").split(",") if r.strip()]
        }},
        upsert=True
    )
    try:
        redis_sync_client.publish("graveyard_bot_signals", "RELOAD_SYSTEM_CONFIG")
    except Exception as e:
        log.warning(f"⚠️ Redis offline, saved to DB only: {e}")
    return redirect("/admin?success=System+configuration+updated+instantly!")

@app.route("/admin/ping/<player_name>/<int:decks_left>")
def admin_ping_player(player_name, decks_left):
    if not is_admin():
        return "Unauthorized", 403
    try:
        redis_sync_client.publish("graveyard_bot_signals", f"SINGLE_PING:{player_name}:{decks_left}")
        return redirect("/admin?success=Sent+nudge+alert!")
    except Exception as e:
        log.error(f"Redis error during nudge: {e}")
        return redirect("/admin?error=Redis+offline.+Instant+pings+currently+unavailable.")

@app.route("/admin/mass-ping")
def admin_mass_ping():
    if not is_admin():
        return "Unauthorized", 403
    try:
        redis_sync_client.publish("graveyard_bot_signals", "MASS_WAR_PING")
        return redirect("/admin?success=Mass+War+Alert+Broadcasted!")
    except Exception as e:
        log.error(f"Redis error during mass ping: {e}")
        return redirect("/admin?error=Redis+offline.+Mass+pings+unavailable.")

@app.route("/health")
def health():
    return {"status": "ok"}, 200

# --- 4. FLASK RUNNER (must be top-level, called from __main__) ---
def run_flask():
    port = int(os.getenv("PORT", 5000))
    log.info(f"🌐 Flask dashboard running on port {port}")
    serve(app, host="0.0.0.0", port=port)

# --- 5. DYNAMIC PREFIX CALLABLE ---
def get_dynamic_prefix(bot_instance, message):
    return bot_instance.active_prefix

# --- 6. DISCORD BOT ENGINE SETUP ---
class GraveyardBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True  # Privileged intent — must be enabled in Discord Developer Portal

        super().__init__(command_prefix=get_dynamic_prefix, intents=intents)
        self.http_session = None
        self.redis_available = False

        self.active_prefix = "!"
        self.maintenance_mode = False
        self.ignored_channels = []
        self.war_channel_id = 0
        self._last_config_load = 0

        self.mongo_client = AsyncIOMotorClient(mongo_url)
        self.db = self.mongo_client["graveyardbot"]
        self.db_users = self.db["users"]

    def _cr_headers(self):
        return {"Authorization": f"Bearer {CR_API_KEY}", "Accept": "application/json"}

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession()
        redis_url = os.getenv("REDIS_URL")
        await self.load_system_config()

        if redis_url:
            try:
                self.redis = redis.from_url(redis_url, decode_responses=True)
                await self.redis.ping()
                self.redis_available = True
                self.loop.create_task(self.listen_to_web_ui())
                log.info("📡 Redis connected. Instant web-hooks active.")
            except Exception as e:
                log.warning(f"⚠️ Redis down: {e}")
                self.redis_available = False
        else:
            self.redis_available = False

        await self.load_extension("cogs.clash_cog")

    async def on_message(self, message):
        if message.author.bot:
            return

        # Debounced config reload fallback when Redis is unavailable
        if not self.redis_available:
            now = time.time()
            if now - self._last_config_load > 30:
                try:
                    await self.load_system_config()
                    self._last_config_load = now
                except Exception as e:
                    log.error(f"Failed fallback config load: {e}")

        if self.maintenance_mode:
            ctx = await self.get_context(message)
            if ctx.valid:
                return await message.channel.send(
                    "⚠️ GraveyardBot is down for configuration edits via the web panel."
                )

        if str(message.channel.id) in self.ignored_channels:
            return

        await self.process_commands(message)

    async def load_system_config(self):
        config_doc = await self.db["config"].find_one({"_id": "system_config_file"})
        if config_doc:
            self.active_prefix = config_doc.get("command_prefix", "!")
            self.maintenance_mode = config_doc.get("maintenance_mode", False)
            self.ignored_channels = config_doc.get("ignored_channels", [])
            self.war_channel_id = int(config_doc.get("war_channel_id") or 0)
        else:
            self.active_prefix = "!"
            self.maintenance_mode = False
            self.ignored_channels = []
            self.war_channel_id = 0

    async def listen_to_web_ui(self):
        pubsub = self.redis.pubsub()
        await pubsub.subscribe("graveyard_bot_signals")

        async for message in pubsub.listen():
            if message['type'] == 'message':
                data = message['data']
                if data == "RELOAD_SYSTEM_CONFIG":
                    await self.load_system_config()
                    log.info("🔄 System config reloaded via Redis signal.")
                elif data.startswith("SINGLE_PING:"):
                    channel = self.get_channel(self.war_channel_id)
                    if channel:
                        # Split with maxsplit=2 to handle player names containing colons
                        parts = data.split(":", 2)
                        if len(parts) == 3:
                            _, player_name, decks_left = parts
                            matched_user = await self.db_users.find_one({"clan_name_cache": player_name})
                            mention_str = f"<@{matched_user['_id']}>" if matched_user else f"**{player_name}**"
                            embed = discord.Embed(
                                title="⚔️ River Race Nudge Alert!",
                                description=f"Yo {mention_str}, you still have **{decks_left} war decks** left! Lock it in.",
                                color=0xe74c3c
                            )
                            await channel.send(embed=embed)
                elif data == "MASS_WAR_PING":
                    channel = self.get_channel(self.war_channel_id)
                    if channel:
                        await channel.send("🚨 **SQUAD ATTENTION!** 🚨 Complete remaining battles immediately!")

    async def close(self):
        if self.http_session:
            await self.http_session.close()
        if self.redis_available:
            await self.redis.aclose()
        await super().close()


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    bot = GraveyardBot()
    bot.run(os.getenv("DISCORD_TOKEN"))