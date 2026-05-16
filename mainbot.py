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
import zoneinfo
from datetime import datetime, timedelta, time as dt_time
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from flask import Flask, render_template_string, request, redirect, session
from waitress import serve
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient, UpdateOne
import redis.asyncio as redis

# --- 1. AUTOMATED ENVIRONMENT SETUP ---
def sync_environment():
    if os.name != 'nt':
        return  
        
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

import redis as sync_redis
redis_sync_client = sync_redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))

cr_api_session = requests.Session()

def fetch_cr_api(endpoint: str) -> dict | None:
    url = f"https://proxy.royaleapi.dev/v1/{endpoint}"
    headers = {"Authorization": f"Bearer {os.getenv('CR_TOKEN')}", "Accept": "application/json"}
    try:
        response = cr_api_session.get(url, headers=headers, timeout=10)
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
    if "discord_id" not in session: return False
    discord_id = str(session.get("discord_id"))
    
    if discord_id == "751975709643112569": return True
    
    sys_config_db = db_sync["config"].find_one({"_id": "system_config_file"}) or {}
    allowed_roles = sys_config_db.get("admin_role_ids", [])
    allowed_users = sys_config_db.get("admin_user_ids", []) 
    
    if discord_id in allowed_users: return True
    
    user_roles = session.get("user_roles", [])
    return any(str(role_id) in allowed_roles for role_id in user_roles)

# ── IN-MEMORY HTML CACHE ────────────────────────────────────────────────── #
_HTML_CACHE = {}

def get_template(template_name):
    if template_name in _HTML_CACHE:
        return _HTML_CACHE[template_name]
        
    doc = db_sync["config"].find_one({"_id": "html_templates"})
    if doc and template_name in doc:
        _HTML_CACHE[template_name] = doc[template_name]
        return doc[template_name]
        
    fallback = globals().get(f"DEFAULT_{template_name.upper()}_HTML", "")
    _HTML_CACHE[template_name] = fallback
    return fallback

# ── HTML UI DEFAULT PAGE STRINGS ────────────────────────────────────────── #
DEFAULT_ROSTER_HTML = """
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
        {% if session.get('is_admin_user') %}
            <a href="/admin" class="btn-discord" style="background: #2ecc71; margin-right: 10px;">💀 Go to HQ Control Panel</a>
        {% endif %}
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
<html>
<head>
    <title>Graveyard Squad - HQ Control Center</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: #0b0c10; color: #c5c6c7; font-family: 'Segoe UI', sans-serif; display: flex; min-height: 100vh; }
        .sidebar { width: 260px; background: #1f2833; padding: 30px 20px; display: flex; flex-direction: column; gap: 20px; border-right: 1px solid #45a29e; }
        .sidebar h2 { color: #66fcf1; font-size: 1.2rem; text-transform: uppercase; letter-spacing: 1px; }
        .sidebar a { color: #c5c6c7; text-decoration: none; padding: 12px; border-radius: 6px; transition: 0.2s; }
        .sidebar a:hover, .sidebar a.active { background: #45a29e; color: #0b0c10; font-weight: bold; }
        .main-content { flex: 1; padding: 30px; overflow-y: auto; }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #1f2833; padding-bottom: 20px; margin-bottom: 30px; }
        .header h1 { color: #66fcf1; }
        .panel-section { background: #1f2833; border-radius: 8px; padding: 20px; margin-bottom: 30px; border: 1px solid #252e3a; }
        .panel-section h3 { margin-bottom: 20px; color: #66fcf1; border-left: 4px solid #45a29e; padding-left: 10px; }
        table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.9rem; }
        th, td { padding: 10px 12px; border-bottom: 1px solid #0b0c10; vertical-align: middle; }
        th { background: #0b0c10; color: #45a29e; text-transform: uppercase; font-size: 0.75rem; letter-spacing: 0.5px; }
        tr:hover { background: #252e3a; }
        .btn { background: #45a29e; color: #0b0c10; border: none; padding: 6px 12px; border-radius: 4px; font-weight: bold; cursor: pointer; transition: 0.2s; text-decoration: none; display: inline-block; font-size: 0.8rem; }
        .btn:hover { background: #66fcf1; }
        .btn-warn { background: #e74c3c; color: white; border: none; padding: 10px 20px; text-decoration: none; border-radius: 5px; font-weight: bold; cursor: pointer; display: inline-block; }
        .btn-warn:hover { background: #c0392b; }
        .form-group { margin-bottom: 15px; display: flex; flex-direction: column; gap: 5px; }
        label { font-size: 0.9rem; color: #45a29e; font-weight: bold; }
        input[type="text"], input[type="checkbox"] { background: #0b0c10; border: 1px solid #45a29e; color: white; padding: 8px; border-radius: 4px; }
        .checkbox-group { display: flex; align-items: center; gap: 10px; margin: 15px 0; }
        
        .modal-overlay { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.8); z-index:1000; align-items:center; justify-content:center; }
        .modal-content { background:#1f2833; padding:25px; border-radius:8px; border:1px solid #45a29e; width:450px; max-width:90%; position:relative; }
    </style>
</head>
<body>
    <div id="actionModal" class="modal-overlay">
        <div class="modal-content">
            <h3 id="modalName" style="color:#66fcf1; margin-bottom:5px; font-size:1.4rem;">Player Name</h3>
            <p id="modalTag" style="color:#888; font-size:0.9rem; margin-bottom:20px;">#TAG</p>
            
            <div style="display:flex; flex-direction:column; gap:12px;">
                <a id="btnProfile" href="#" class="btn" style="text-align:center; background:#5dade2; padding:10px; font-size:0.9rem;">📊 View Player Analytics</a>
                <a id="btnAdmin" href="#" class="btn" style="text-align:center; background:#f1c40f; color:#121212; padding:10px; font-size:0.9rem;">🔑 Grant HQ Dashboard Access</a>
                
                <hr style="border:0; border-top:1px solid #333; margin:10px 0;">
                
                <h4 style="color:#45a29e; font-size:0.95rem; margin-bottom:5px;">🔗 Manually Map Discord Account</h4>
                <form method="POST" action="/admin/manual-link" style="display:flex; flex-direction:column; gap:10px;">
                    <input type="hidden" name="player_tag" id="modalInputTag">
                    <input type="text" name="discord_id" placeholder="Paste 18-digit Discord ID..." required style="background:#0b0c10; border:1px solid #45a29e; color:white; padding:10px; border-radius:4px;">
                    <button type="submit" class="btn" style="background:#2ecc71; color:#0b0c10; padding:10px;">Link Account Database</button>
                </form>
                
                <button onclick="closeModal()" class="btn-warn" style="margin-top:10px; width:100%; padding:10px;">Cancel & Close</button>
            </div>
        </div>
    </div>

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
            <div class="metric-card" style="background: #1f2833; border: 1px solid #45a29e; border-radius: 8px; padding: 20px; display:inline-block; margin-right:15px;">
                <h5>Active War Logs</h5><p style="color: #86c232; font-size: 1.8rem; font-weight: bold;">{{ war_players | length }} Members</p>
            </div>
            <div class="metric-card" style="background: #1f2833; border: 1px solid #45a29e; border-radius: 8px; padding: 20px; display:inline-block; margin-right:15px;">
                <h5>Unused Decks (Today)</h5><p style="color:#e74c3c; font-size: 1.8rem; font-weight: bold;">{{ total_decks_left }}</p>
            </div>
            <div class="metric-card" style="background: #1f2833; border: 1px solid #45a29e; border-radius: 8px; padding: 20px; display:inline-block;">
                <h5>Database Mappings</h5><p style="color: #86c232; font-size: 1.8rem; font-weight: bold;">{{ linked_count }}</p>
            </div>
        </div>

        <div class="panel-section">
            <h3>⚔️ Operational War Deck Monitor</h3>
            <table style="margin-bottom: 20px;">
                <thead>
                    <tr>
                        <th>Member Name</th>
                        <th>Role</th>
                        <th>War Fame</th>
                        <th>Decks Used</th>
                        <th>Decks Left</th>
                        <th>Action Trigger</th>
                    </tr>
                </thead>
                <tbody>
                    {% for p in war_players %}
                    <tr>
                        <td>
                            <a href="#" onclick="openModal('{{ p.name | escape }}', '{{ p.tag }}'); return false;" style="color: #66fcf1; font-weight: bold; text-decoration: none; border-bottom: 1px dashed #45a29e;" title="Click for action menu">
                                {{ p.name }}
                            </a>
                            <div style="font-size: 0.7rem; color: #888;">#{{ p.tag }}</div>
                        </td>
                        <td>{{ p.role }}</td>
                        <td style="color: #2ecc71; font-weight: bold;">⚡ {{ p.fame }}</td>
                        <td>{{ p.decksUsedToday }} / 4</td>
                        <td style="font-weight: bold; color: {{ '#e74c3c' if p.decksRemaining > 0 else '#2ecc71' }};">
                            {{ p.decksRemaining }}
                        </td>
                        <td>
                            {% if p.decksRemaining > 0 %}
                                <a href="/admin/ping/{{ p.name }}/{{ p.decksRemaining }}" class="btn">Nudge</a>
                            {% else %}
                                <span style="color:#2ecc71; font-size:0.8rem;">✓ Clear</span>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <div class="panel-section">
            <h3>📥 Custom Report Builder (CSV/Excel)</h3>
            <p style="font-size:0.85rem; color:#888; margin-bottom:15px;">Select the exact data points you want to export. The engine will dynamically build your spreadsheet.</p>
            <form method="POST" action="/admin/export/custom" style="background:#0b0c10; padding:15px; border-radius:5px; border:1px solid #45a29e;">
                <h4 style="color:#66fcf1; margin-bottom:10px;">Player Identity</h4>
                <div style="display:flex; gap:15px; margin-bottom:15px;">
                    <label><input type="checkbox" name="fields" value="name" checked> Player Name</label>
                    <label><input type="checkbox" name="fields" value="tag" checked> Player Tag</label>
                    <label><input type="checkbox" name="fields" value="role"> Clan Role</label>
                    <label><input type="checkbox" name="fields" value="expLevel"> XP Level</label>
                </div>
                <h4 style="color:#66fcf1; margin-bottom:10px;">Progression & Social</h4>
                <div style="display:flex; gap:15px; margin-bottom:15px;">
                    <label><input type="checkbox" name="fields" value="trophies"> Current Trophies</label>
                    <label><input type="checkbox" name="fields" value="donations"> Donations Given</label>
                    <label><input type="checkbox" name="fields" value="donationsReceived"> Donations Received</label>
                </div>
                <h4 style="color:#66fcf1; margin-bottom:10px;">Live River Race Data</h4>
                <div style="display:flex; gap:15px; margin-bottom:20px;">
                    <label><input type="checkbox" name="fields" value="fame" checked> War Fame</label>
                    <label><input type="checkbox" name="fields" value="decksUsedToday"> Decks Used</label>
                    <label><input type="checkbox" name="fields" value="decksRemaining" checked> Decks Remaining</label>
                </div>
                <button type="submit" class="btn" style="background:#f1c40f; color:#0b0c10;">Generate Custom Excel File</button>
            </form>
        </div>
        
        <div class="panel-section">
            <h3>💬 Dynamic Custom Commands (Auto-Responder)</h3>
            <table style="margin-bottom: 20px;">
                <thead>
                    <tr>
                        <th>Command Trigger</th>
                        <th>Bot Response</th>
                        <th>Action</th>
                    </tr>
                </thead>
                <tbody>
                    {% for cmd in custom_commands %}
                    <tr>
                        <td style="font-weight: bold; color: #66fcf1;">{{ sys_config.command_prefix or '!' }}{{ cmd._id }}</td>
                        <td>{{ cmd.response }}</td>
                        <td>
                            <a href="/admin/delete-command/{{ cmd._id }}" class="btn-warn" style="padding: 4px 8px; font-size: 0.75rem;">Delete</a>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            
            <form method="POST" action="/admin/add-command" style="display: flex; gap: 10px;">
                <input type="text" name="trigger" placeholder="Trigger (e.g. rules)" required style="width: 20%; background: #0b0c10; border: 1px solid #45a29e; color: white; padding: 10px; border-radius: 4px;">
                <input type="text" name="response" placeholder="Bot Response..." required style="width: 60%; background: #0b0c10; border: 1px solid #45a29e; color: white; padding: 10px; border-radius: 4px;">
                <button type="submit" class="btn" style="background: #2ecc71; color: #0b0c10;">+ Add Command</button>
            </form>
        </div>
        
        <div class="panel-section">
            <h3>🎨 Live Web UI Studio (HTML/CSS Code Editor)</h3>
            <p style="font-size:0.85rem; margin-bottom:15px; color:#aaa;">Select a page below to live-edit its source code. <br>⚠️ <strong>Warning:</strong> Be careful with <code>{% raw %}{{{% endraw %} ... {% raw %}}}{% endraw %}</code> brackets—breaking Jinja syntax will crash the page! If you ever get locked out by a crash, navigate to <strong>/admin/reset-html</strong> to factory reset.</p>
            
            <form method="POST" action="/admin/update-html">
                <div class="form-group" style="display:flex; justify-content:space-between; align-items:center;">
                    <select id="template_selector" name="template_name" onchange="switchTemplate()" style="max-width: 300px; padding:8px; font-weight:bold; cursor:pointer;">
                        <option value="roster">Public Roster Page (ROSTER_HTML)</option>
                        <option value="player">Player Profile Page (PLAYER_HTML)</option>
                        <option value="link">Link Account Page (LINK_HTML)</option>
                        <option value="admin">Control Panel (ADMIN_HTML)</option>
                    </select>
                    <a href="/admin/reset-html" class="btn-warn" style="font-size:0.75rem; background:#c0392b; padding:6px 10px;" onclick="return confirm('Are you sure? This permanently deletes all your custom UI code and restores the factory templates.')">Factory Reset All Templates</a>
                </div>
                
                <div class="form-group">
                    <textarea id="html_editor" name="html_content" rows="25" style="font-family: 'Courier New', monospace; background:#1e1e1e; color:#a6e22e; width:100%; padding:15px; border:1px solid #45a29e; border-radius:5px; line-height:1.4; resize:vertical;"></textarea>
                </div>
                <button type="submit" class="btn" style="background:#f1c40f; color:#0b0c10; font-size:1rem; padding:10px 20px;">💾 Deploy Code Live</button>
            </form>
        </div>

        <div class="panel-section">
            <h3>🛠️ Live System File Configurations</h3>
            <form method="POST" action="/admin/update-system-config">
                <div class="form-group">
                    <label>Bot Command Prefix</label>
                    <input type="text" name="command_prefix" value="{{ sys_config.command_prefix or '!' }}" max_length="3" style="max-width: 400px;">
                </div>
                <div class="checkbox-group">
                    <input type="checkbox" name="maintenance_mode" id="maintenance_mode" {% if sys_config.maintenance_mode %}checked{% endif %}>
                    <label for="maintenance_mode">Enable Global Maintenance Mode (Disable Bot Commands)</label>
                </div>
                <div class="checkbox-group">
                    <input type="checkbox" name="feature_auto_pings" id="feature_auto_pings" {% if sys_config.feature_auto_pings %}checked{% endif %}>
                    <label for="feature_auto_pings">Enable Automated War Pings (Feature Flag)</label>
                </div>
                <div class="form-group">
                    <label>Discord War Nudge Channel ID</label>
                    <input type="text" name="war_channel_id" value="{{ sys_config.war_channel_id or '' }}" style="max-width: 400px;">
                </div>
                <div class="form-group">
                    <label>Authorized Admin Role IDs (Comma-separated)</label>
                    <input type="text" name="admin_role_ids" value="{{ sys_config.admin_role_ids | join(', ') if sys_config.admin_role_ids else '' }}" style="max-width: 600px;">
                </div>
                <div class="form-group">
                    <label>Authorized Admin User IDs (Comma-separated)</label>
                    <div style="font-size: 0.8rem; color: #888; margin-top:-5px; margin-bottom:5px;">Discord IDs granted via the roster table. Delete an ID here to revoke access.</div>
                    <input type="text" name="admin_user_ids" value="{{ sys_config.admin_user_ids | join(', ') if sys_config.admin_user_ids else '' }}" style="max-width: 600px;">
                </div>
                <div class="form-group">
                    <label>Ignored/Muted Discord Channels (Comma-separated)</label>
                    <input type="text" name="ignored_channels" value="{{ sys_config.ignored_channels | join(', ') if sys_config.ignored_channels else '' }}" style="max-width: 600px;">
                </div>
                <button type="submit" class="btn" style="background:#66fcf1; color:#0b0c10; margin-top: 10px;">Save System Variables</button>
            </form>
        </div>
    </div>
    
    <div style="display:none;">
        <textarea id="raw_roster">{{ raw_roster }}</textarea>
        <textarea id="raw_player">{{ raw_player }}</textarea>
        <textarea id="raw_link">{{ raw_link }}</textarea>
        <textarea id="raw_admin">{{ raw_admin }}</textarea>
    </div>

    <script>
        function switchTemplate() {
            var sel = document.getElementById("template_selector").value;
            document.getElementById("html_editor").value = document.getElementById("raw_" + sel).value;
        }
        function openModal(name, tag) {
            document.getElementById('modalName').innerText = name;
            document.getElementById('modalTag').innerText = '#' + tag;
            document.getElementById('btnProfile').href = '/player/' + tag;
            document.getElementById('btnAdmin').href = '/admin/grant-role/' + tag;
            document.getElementById('modalInputTag').value = tag;
            document.getElementById('actionModal').style.display = 'flex';
        }
        function closeModal() {
            document.getElementById('actionModal').style.display = 'none';
        }
        window.onload = switchTemplate;
    </script>
</body>
</html>
"""

# ── FLASK ROUTE CONTROLLERS ──────────────────────────────────────────────── #
@app.route("/")
def index():
    data = fetch_cr_api(f"clans/%23{CLAN_TAG}")
    if not data:
        return "<h1>Clan not found or API down.</h1>", 500
    return render_template_string(get_template("roster"), members=data.get("memberList", []))

@app.route("/player/<tag>")
def web_profile(tag):
    data = fetch_cr_api(f"players/%23{tag}")
    if not data:
        return "<h1>Player data not found.</h1>", 404
    return render_template_string(get_template("player"), data=data, max_lvl=MAX_CARD_LEVEL)

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
            return redirect(f"/player/{tag}")
        else:
            error_msg = "Could not find a Clash Royale account with that tag."
    return render_template_string(get_template("link"), name=session.get("discord_name", "Unknown"), error=error_msg)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code: return "Authentication Failed.", 400

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
    user_data = requests.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {token}"}).json()

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
            if c.get("tag", "").replace("#", "").upper() == CLAN_TAG.upper():
                raw_participants = c.get("participants", [])
                break

    sys_config_db = db_sync["config"].find_one({"_id": "system_config_file"}) or {}
    war_players = []
    total_decks_left = 0

    for p in raw_participants:
        p_tag = p.get("tag", "").replace("#", "").upper()
        fame = p.get("fame", 0)
        decks_used_today = p.get("decksUsedToday", 0)
        decks_remaining = max(0, 4 - decks_used_today)

        player_clean_meta = {
            "tag": p_tag,
            "name": p.get("name", "Unknown"),
            "role": p.get("role", "Member").replace("_", " ").title(),
            "fame": fame,
            "decksUsedToday": decks_used_today,
            "decksRemaining": decks_remaining,
        }
        total_decks_left += decks_remaining
        war_players.append(player_clean_meta)

    war_players = sorted(war_players, key=lambda x: -x["decksRemaining"])
    linked_count = users_sync.count_documents({})
    all_custom_cmds = list(custom_cmds_sync.find())

    return render_template_string(
        get_template("admin"), 
        war_players=war_players, 
        total_decks_left=total_decks_left,
        linked_count=linked_count, 
        sys_config=sys_config_db,
        custom_commands=all_custom_cmds,
        raw_roster=get_template("roster"),
        raw_player=get_template("player"),
        raw_link=get_template("link"),
        raw_admin=get_template("admin"),
        success=request.args.get('success'), 
        error=request.args.get('error')
    )

@app.route("/admin/update-html", methods=["POST"])
def update_html():
    if not is_admin(): return "Unauthorized", 403
    template_name = request.form.get("template_name")
    html_content = request.form.get("html_content")
    
    if template_name in ["roster", "player", "link", "admin"]:
        db_sync["config"].update_one(
            {"_id": "html_templates"},
            {"$set": {template_name: html_content}},
            upsert=True
        )
        _HTML_CACHE.clear() 
        return redirect("/admin?success=UI+Code+Deployed+Live!")
    return redirect("/admin?error=Invalid+Template+Name")

@app.route("/admin/reset-html")
def reset_html():
    if not is_admin(): return "Unauthorized", 403
    db_sync["config"].delete_one({"_id": "html_templates"})
    _HTML_CACHE.clear() 
    return redirect("/admin?success=All+UI+Templates+Reset+to+Factory+Defaults!")

@app.route("/admin/grant-role/<player_tag>")
def admin_grant_role(player_tag):
    if not is_admin(): return "Unauthorized", 403
    linked_user = users_sync.find_one({"player_id": player_tag.upper()})
    if not linked_user:
        return redirect("/admin?error=This+player+has+not+linked+their+Discord+account+yet.")

    db_sync["config"].update_one(
        {"_id": "system_config_file"},
        {"$addToSet": {"admin_user_ids": str(linked_user["_id"])}},
        upsert=True
    )
    try: redis_sync_client.publish("graveyard_bot_signals", json.dumps({"action": "RELOAD"}))
    except Exception: pass
    return redirect("/admin?success=Granted+dashboard+admin+privileges!")

@app.route("/admin/manual-link", methods=["POST"])
def admin_manual_link():
    if not is_admin(): return "Unauthorized", 403
    player_tag = request.form.get("player_tag", "").strip().upper().replace("#", "")
    discord_id = request.form.get("discord_id", "").strip()
    
    if not player_tag or not discord_id:
        return redirect("/admin?error=Missing+tag+or+Discord+ID")
        
    if not discord_id.isdigit() or len(discord_id) < 17:
        return redirect("/admin?error=Invalid+Discord+ID+Format.+Must+be+17%2B+digits.")
        
    users_sync.update_one(
        {"_id": discord_id}, 
        {"$set": {"player_id": player_tag}},
        upsert=True
    )
    return redirect(f"/admin?success=Successfully+Linked+Tag+to+Discord+ID!")

@app.route("/admin/add-command", methods=["POST"])
def admin_add_command():
    if not is_admin(): return "Unauthorized", 403
    trigger = request.form.get("trigger", "").strip().lower()
    response_text = request.form.get("response", "").strip()
    
    if trigger.startswith("!") or trigger.startswith("?"):
        trigger = trigger[1:] 
        
    if trigger and response_text:
        custom_cmds_sync.update_one({"_id": trigger}, {"$set": {"response": response_text}}, upsert=True)
        return redirect("/admin?success=Custom+Command+Added!")
    return redirect("/admin?error=Trigger+and+Response+Required")

@app.route("/admin/delete-command/<cmd_id>")
def admin_delete_command(cmd_id):
    if not is_admin(): return "Unauthorized", 403
    custom_cmds_sync.delete_one({"_id": cmd_id})
    return redirect("/admin?success=Command+Deleted!")

@app.route("/admin/update-system-config", methods=["POST"])
def update_system_config():
    if not is_admin(): return "Unauthorized", 403
    db_sync["config"].update_one(
        {"_id": "system_config_file"},
        {"$set": {
            "command_prefix": request.form.get("command_prefix", "!"),
            "maintenance_mode": bool(request.form.get("maintenance_mode")),
            "feature_auto_pings": bool(request.form.get("feature_auto_pings")),
            "war_channel_id": request.form.get("war_channel_id", "").strip(),
            "ignored_channels": [c.strip() for c in request.form.get("ignored_channels", "").split(",") if c.strip()],
            "admin_role_ids": [r.strip() for r in request.form.get("admin_role_ids", "").split(",") if r.strip()],
            "admin_user_ids": [u.strip() for u in request.form.get("admin_user_ids", "").split(",") if u.strip()]
        }},
        upsert=True
    )
    try: redis_sync_client.publish("graveyard_bot_signals", json.dumps({"action": "RELOAD"}))
    except Exception as e: log.warning(f"⚠️ Redis offline: {e}")
    return redirect("/admin?success=System+configuration+updated+instantly!")

@app.route("/admin/ping/<player_name>/<int:decks_left>")
def admin_ping_player(player_name, decks_left):
    if not is_admin(): return "Unauthorized", 403
    try:
        payload = json.dumps({"action": "SINGLE_PING", "player_name": player_name, "decks_left": decks_left})
        redis_sync_client.publish("graveyard_bot_signals", payload)
        return redirect("/admin?success=Sent+nudge+alert!")
    except Exception:
        return redirect("/admin?error=Redis+offline.+Instant+pings+currently+unavailable.")

@app.route("/admin/mass-ping")
def admin_mass_ping():
    if not is_admin(): return "Unauthorized", 403
    try:
        redis_sync_client.publish("graveyard_bot_signals", json.dumps({"action": "MASS_PING"}))
        return redirect("/admin?success=Mass+War+Alert+Broadcasted!")
    except Exception:
        return redirect("/admin?error=Redis+offline.+Mass+pings+unavailable.")

@app.route("/admin/export/custom", methods=["POST"])
def export_custom_csv():
    if not is_admin(): return "Unauthorized", 403
    
    selected_fields = request.form.getlist("fields")
    if not selected_fields:
        return redirect("/admin?error=No+fields+selected+for+export.")

    clan_data = fetch_cr_api(f"clans/%23{CLAN_TAG}")
    war_data = fetch_cr_api(f"clans/%23{CLAN_TAG}/currentriverrace")
    
    members = clan_data.get("memberList", []) if clan_data else []
    
    raw_participants = []
    if war_data and "clan" in war_data and "participants" in war_data["clan"]:
        raw_participants = war_data["clan"]["participants"]
    elif war_data and "clans" in war_data:
        for c in war_data["clans"]:
            if c.get("tag", "").replace("#", "").upper() == CLAN_TAG.upper():
                raw_participants = c.get("participants", [])
                break
                
    war_participants = {p["tag"]: p for p in raw_participants}

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(selected_fields)

    for m in members:
        tag = m["tag"]
        flat_data = {
            "name": m.get("name", ""),
            "tag": tag,
            "role": m.get("role", ""),
            "expLevel": m.get("expLevel", 0),
            "trophies": m.get("trophies", 0),
            "donations": m.get("donations", 0),
            "donationsReceived": m.get("donationsReceived", 0)
        }
        
        if tag in war_participants:
            wp = war_participants[tag]
            flat_data["fame"] = wp.get("fame", 0)
            flat_data["decksUsedToday"] = wp.get("decksUsedToday", 0)
            flat_data["decksRemaining"] = max(0, 4 - wp.get("decksUsedToday", 0))

        row = [flat_data.get(field, "N/A") for field in selected_fields]
        writer.writerow(row)

    return app.response_class(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=Graveyard_Custom_Export.csv"}
    )

@app.route("/health")
def health():
    return {"status": "ok"}, 200

# --- 4. FLASK SERVER MANAGER RUNNER ---
def run_flask():
    port = int(os.getenv("PORT", 5000))
    log.info(f"🌐 Flask dashboard running on port {port}")
    serve(app, host="0.0.0.0", port=port)

# --- 5. DYNAMIC PREFIX CALLABLE LINK ---
def get_dynamic_prefix(bot_instance, message):
    return bot_instance.active_prefix

# --- 6. DISCORD BOT ENGINE SETUP ---
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
        self._last_config_load = 0

        self.mongo_client = AsyncIOMotorClient(mongo_url)
        self.db = self.mongo_client["graveyardbot"]
        self.db_users = self.db["users"]
        self.custom_cmds = self.db["custom_commands"]

    # ── ASYNC API HOOK TO PREVENT EVENT LOOP FREEZING ──
    async def async_fetch_cr_api(self, endpoint: str) -> dict | None:
        url = f"https://proxy.royaleapi.dev/v1/{endpoint}"
        headers = {"Authorization": f"Bearer {os.getenv('CR_TOKEN')}", "Accept": "application/json"}
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with self.http_session.get(url, headers=headers, timeout=timeout) as response:
                if response.status == 200:
                    data = await response.json()
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
            log.error(f"Async API Request failed: {e}")
            return None

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
            except Exception as e:
                log.warning(f"⚠️ Redis down: {e}")
                self.redis_available = False
        else:
            self.redis_available = False

        await self.load_extension("cogs.clash_cog")
        self.daily_snapshot_loop.start()

    async def on_message(self, message):
        if message.author.bot: return

        if not self.redis_available:
            now = time.time()
            if now - self._last_config_load > 30:
                try:
                    await self.load_system_config()
                    self._last_config_load = now
                except Exception as e:
                    log.error(f"Failed fallback config load: {e}")

        prefix = self.active_prefix
        if self.maintenance_mode and message.content.startswith(prefix):
            await message.channel.send("⚠️ GraveyardBot is down for web configuration maintenance. Try again shortly.")
            return

        if str(message.channel.id) in self.ignored_channels: return

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
        while not self.is_closed():
            pubsub = None
            try:
                pubsub = self.redis.pubsub()
                await pubsub.subscribe("graveyard_bot_signals")
                log.info("📡 Redis PubSub listener active.")
                
                async for message in pubsub.listen():
                    if message['type'] == 'message':
                        try:
                            payload = json.loads(message['data'])
                            action = payload.get("action")
                            
                            if action == "RELOAD":
                                await self.load_system_config()
                            elif action == "SINGLE_PING":
                                channel = self.get_channel(self.war_channel_id)
                                if channel:
                                    player_name = payload.get("player_name")
                                    decks_left = payload.get("decks_left")
                                    matched_user = await self.db_users.find_one({"clan_name_cache": player_name})
                                    mention_str = f"<@{matched_user['_id']}>" if matched_user else f"**{player_name}**"
                                    embed = discord.Embed(title="⚔️ River Race Nudge Alert!", description=f"Yo {mention_str}, you still have **{decks_left} war decks** left! Lock it in.", color=0xe74c3c)
                                    await channel.send(embed=embed)
                            elif action == "MASS_PING":
                                channel = self.get_channel(self.war_channel_id)
                                if channel:
                                    await channel.send("🚨 **SQUAD ATTENTION!** 🚨 Complete remaining battles immediately!")
                        except json.JSONDecodeError:
                            log.warning("Received malformed payload in Redis. Ignoring.")

            except Exception as e:
                log.error(f"Redis listener dropped: {e}. Reconnecting in 5 seconds...")
                await asyncio.sleep(5)
            finally:
                if pubsub:
                    await pubsub.close()

    # ── THE MIDNIGHT MASTER SNAPSHOT ──
    @tasks.loop(time=dt_time(hour=23, minute=55, tzinfo=zoneinfo.ZoneInfo("America/New_York")))
    async def daily_snapshot_loop(self):
        clan_data = await self.async_fetch_cr_api(f"clans/%23{CLAN_TAG}")
        war_data = await self.async_fetch_cr_api(f"clans/%23{CLAN_TAG}/currentriverrace")
        if not clan_data: 
            return
            
        snapshot_date = datetime.now(zoneinfo.ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        members = clan_data.get("memberList", [])
        
        # Parse War Participants
        raw_participants = []
        if war_data and "clan" in war_data and "participants" in war_data["clan"]:
            raw_participants = war_data["clan"]["participants"]
        elif war_data and "clans" in war_data:
            for c in war_data["clans"]:
                if c.get("tag", "").replace("#", "").upper() == CLAN_TAG.upper():
                    raw_participants = c.get("participants", [])
                    break
                    
        war_participants = {p["tag"]: p for p in raw_participants}
        
        # 1. Prepare Bulk Ops Arrays
        snapshot_ops = []
        profile_ops = []
        battle_ops = []
        
        # 2. Concurrency limit (Semaphore) to protect the API from rate limits
        sem = asyncio.Semaphore(5)

        async def harvest_member(member):
            tag = member["tag"].replace("#", "")
            async with sem:
                # Optimized: Execute both API calls in parallel
                profile, blog = await asyncio.gather(
                    self.async_fetch_cr_api(f"players/%23{tag}"),
                    self.async_fetch_cr_api(f"players/%23{tag}/battlelog")
                )
            return tag, member, profile, blog

        # Fetch all 50 members concurrently but safely
        log.info("📡 Initiating Master Data Harvest for 50 clan members...")
        results = await asyncio.gather(*(harvest_member(m) for m in members))
        
        for tag, m, profile, blog in results:
            
            # --- A. SNAPSHOT DATA (Daily CSV metrics) ---
            flat_data = {
                "date": snapshot_date,
                "name": m.get("name", ""),
                "tag": tag,
                "role": m.get("role", ""),
                "expLevel": m.get("expLevel", 0),
                "trophies": m.get("trophies", 0),
                "donations": m.get("donations", 0),
                "donationsReceived": m.get("donationsReceived", 0)
            }
            if tag in war_participants:
                wp = war_participants[tag]
                flat_data["fame"] = wp.get("fame", 0)
                flat_data["decksUsedToday"] = wp.get("decksUsedToday", 0)
                
            if profile:
                flat_data["totalWins"] = profile.get("wins", 0)
                flat_data["totalLosses"] = profile.get("losses", 0)
                flat_data["warDayWins"] = profile.get("warDayWins", 0)
                fav_card = profile.get("currentFavouriteCard", {})
                flat_data["favoriteCard"] = fav_card.get("name", "Unknown") if isinstance(fav_card, dict) else "Unknown"
                
                profile_ops.append(UpdateOne({"_id": tag}, {"$set": profile}, upsert=True))

            snapshot_ops.append(UpdateOne(
                {"tag": tag, "date": snapshot_date},
                {"$set": flat_data},
                upsert=True
            ))

            # --- B. BATTLE LOG DATA (Card vs Card tracking) ---
            if blog and isinstance(blog, list):
                for battle in blog:
                    battle_time = battle.get("battleTime")
                    if not battle_time: continue
                    
                    battle_id = f"{tag}_{battle_time}"
                    
                    team_cards = [c["name"] for c in battle.get("team", [{}])[0].get("cards", [])]
                    opp_cards = [c["name"] for c in battle.get("opponent", [{}])[0].get("cards", [])]
                    
                    battle_doc = {
                        "player_tag": tag,
                        "battle_time": battle_time,
                        "type": battle.get("type", ""),
                        "gameMode": battle.get("gameMode", {}).get("name", ""),
                        "team_cards": team_cards,
                        "opponent_cards": opp_cards,
                        "team_crowns": battle.get("team", [{}])[0].get("crowns", 0),
                        "opponent_crowns": battle.get("opponent", [{}])[0].get("crowns", 0)
                    }
                    battle_ops.append(UpdateOne({"_id": battle_id}, {"$set": battle_doc}, upsert=True))
            
        # 3. Execute all database writes in lightning-fast bulk payloads
        if snapshot_ops: await self.db["historical_snapshots"].bulk_write(snapshot_ops)
        if profile_ops: await self.db["player_profiles"].bulk_write(profile_ops)
        if battle_ops: await self.db["battle_history"].bulk_write(battle_ops)
            
        log.info(f"✅ Harvest Complete! Saved {len(snapshot_ops)} snapshots, {len(profile_ops)} profiles, and {len(battle_ops)} battles to MongoDB.")

    @daily_snapshot_loop.before_loop
    async def before_daily_snapshot_loop(self):
        await self.wait_until_ready()

    async def close(self):
        if self.http_session: await self.http_session.close()
        if self.redis_available: await self.redis.aclose()
        await super().close()

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    bot = GraveyardBot()
    bot.run(os.getenv("DISCORD_TOKEN"))