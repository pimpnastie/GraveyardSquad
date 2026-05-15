import os
import sys
import subprocess
import logging
import threading
import asyncio
import urllib.parse
import aiohttp
import requests
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
    req_file = "requirements.txt"
    venv_dir = "venv"
    is_venv = sys.prefix != sys.base_prefix or os.path.exists(venv_dir)

    if not os.path.exists(req_file):
        with open(req_file, "w") as f:
            f.write("discord.py\naiohttp\nmotor\npymongo\nredis\nflask\npython-dotenv\nthefuzz\nwaitress\nopenpyxl\ntzdata\nrequests\n")
        print(f"✅ Created default {req_file}")

    if not is_venv and os.name == 'nt':
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

# --- 3. FLASK WEB DASHBOARD & OAUTH ---
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", os.urandom(24))

CR_API_KEY = os.getenv("CR_TOKEN")
CLAN_TAG = "9LVY89UP"

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = "https://graveyardbot.onrender.com/callback"

mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
mongo_client_sync = MongoClient(mongo_url)
db_sync = mongo_client_sync["graveyardbot"]
users_sync = db_sync["users"]

def fetch_cr_api(endpoint: str) -> dict | None:
    headers = {"Authorization": f"Bearer {CR_API_KEY}", "Accept": "application/json"}
    url = f"https://proxy.royaleapi.dev/v1/{endpoint}"
    try:
        response = requests.get(url, headers=headers, timeout=10)
        return response.json() if response.status_code == 200 else None
    except Exception as e:
        log.error(f"Flask API Request failed: {e}")
        return None

# ── HTML TEMPLATES ────────────────────────────────────────────────────────── #

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
        <a href="/login" class="btn-discord">Log in with Discord to Link Account</a>
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
        
        {% if error %}
            <div class="error">{{ error }}</div>
        {% endif %}

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

    {% if data.leagueStatistics %}
    <h2>🏆 Path of Legends</h2>
    <div class="grid">
        <div class="stat-box"><div class="label">Current Season</div><div class="value blue">{% if data.leagueStatistics.currentSeason %}Rating: {{ data.leagueStatistics.currentSeason.rank if data.leagueStatistics.currentSeason.rank else data.leagueStatistics.currentSeason.trophies }}{% else %}—{% endif %}</div></div>
        <div class="stat-box"><div class="label">Best Season</div><div class="value white">{% if data.leagueStatistics.bestSeason %}Rating: {{ data.leagueStatistics.bestSeason.rank if data.leagueStatistics.bestSeason.rank else data.leagueStatistics.bestSeason.trophies }}{% else %}—{% endif %}</div></div>
    </div>
    {% endif %}

    <h2>📚 Collection Breakdown</h2>
    <div class="grid">
        {% set counts = namespace(lvl16=0, lvl15=0, lvl14=0, total=data.cards|length) %}
        {% for c in data.cards %}
            {% if c.level == 16 %} {% set counts.lvl16 = counts.lvl16 + 1 %}
            {% elif c.level == 15 %} {% set counts.lvl15 = counts.lvl15 + 1 %}
            {% elif c.level == 14 %} {% set counts.lvl14 = counts.lvl14 + 1 %}
            {% endif %}
        {% endfor %}
        <div class="stat-box"><div class="label">Total Cards Found</div><div class="value white">{{ counts.total }} / 121</div></div>
        <div class="stat-box"><div class="label">Maxed (Lvl 16)</div><div class="value green">{{ counts.lvl16 }}</div></div>
        <div class="stat-box"><div class="label">Elite (Lvl 15)</div><div class="value blue">{{ counts.lvl15 }}</div></div>
        <div class="stat-box"><div class="label">Lvl 14</div><div class="value white">{{ counts.lvl14 }}</div></div>
    </div>

    <h2>⚔️ Battle Stats</h2>
    <div class="grid">
        <div class="stat-box"><div class="label">Total Wins</div><div class="value green">{{ data.wins }}</div></div>
        <div class="stat-box"><div class="label">Losses</div><div class="value red">{{ data.losses }}</div></div>
        <div class="stat-box"><div class="label">3-Crown Wins</div><div class="value green">👑 {{ data.threeCrownWins }}</div></div>
        <div class="stat-box"><div class="label">Total Battles</div><div class="value white">{{ data.battleCount }}</div></div>
        <div class="stat-box"><div class="label">Win Rate</div><div class="value {% if data.battleCount > 0 and (data.wins / data.battleCount * 100) >= 50 %}green{% else %}red{% endif %}">{% if data.battleCount > 0 %}{{ "%.1f" | format(data.wins / data.battleCount * 100) }}%{% else %}—{% endif %}</div></div>
    </div>

    <h2>🎁 Social & Misc</h2>
    <div class="grid">
        <div class="stat-box"><div class="label">Total Donations</div><div class="value white">{{ data.totalDonations }}</div></div>
        <div class="stat-box"><div class="label">War Day Wins</div><div class="value white">{{ data.warDayWins }}</div></div>
        <div class="stat-box">
            <div class="label">Account Age</div>
            <div class="value blue">
                {% set age = namespace(found=false, years=0) %}
                {% for badge in data.badges %}{% if 'Played' in badge.name and 'Year' in badge.name %}{% set age.found = true %}{% set age.years = badge.progress %}{% endif %}{% endfor %}
                {% if age.found %}{{ age.years }} Years{% else %}< 1 Year{% endif %}
            </div>
        </div>
    </div>

    <h2>🃏 Current Battle Deck</h2>
    <div class="deck-grid">
        {% for card in data.currentDeck %}
            <div class="card-box {% if card.level == 16 %}maxed{% endif %}">
                <div class="card-name">{{ card.name }}</div>
                <span class="card-level">Lvl {{ card.level }}{% if card.level == 16 %} ✓{% endif %}</span>
            </div>
        {% endfor %}
    </div>
</body>
</html>
"""

# ── Flask Routes ─────────────────────────────────────────────────────────── #

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
    return render_template_string(PLAYER_HTML, data=data)

@app.route("/login")
def login():
    if not DISCORD_CLIENT_ID:
        return "Discord Client ID not configured.", 500
    url = f"https://discord.com/api/oauth2/authorize?client_id={DISCORD_CLIENT_ID}&redirect_uri={urllib.parse.quote(REDIRECT_URI)}&response_type=code&scope=identify"
    return redirect(url)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "Authentication Failed.", 400
    
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post("https://discord.com/api/oauth2/token", data=data, headers=headers)
    
    if r.status_code != 200:
        return "Failed to get Discord Token.", 400
        
    token = r.json().get("access_token")
    user_r = requests.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {token}"})
    user_data = user_r.json()
    
    session["discord_id"] = user_data["id"]
    session["discord_name"] = user_data["username"]
    
    return redirect("/link")

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

    return render_template_string(LINK_HTML, name=session["discord_name"], error=error_msg)

@app.route("/health")
def health():
    return {"status": "ok"}, 200

def run_flask():
    port = int(os.getenv("PORT", 5000))
    log.info(f"🌐 Flask dashboard running on port {port}")
    serve(app, host="0.0.0.0", port=port)

# --- 4. DISCORD BOT SETUP ---
class GraveyardBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.http_session = None
        self.redis_available = False
        
        self.mongo_client = AsyncIOMotorClient(mongo_url)
        self.db = self.mongo_client["graveyardbot"]
        self.db_users = self.db["users"]

    def _cr_headers(self):
        return {"Authorization": f"Bearer {CR_API_KEY}", "Accept": "application/json"}

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession()

        redis_url = os.getenv("REDIS_URL")
        if redis_url:
            try:
                self.redis = redis.from_url(redis_url, decode_responses=True)
                await self.redis.ping()
                self.redis_available = True
            except Exception as e:
                log.warning(f"⚠️ Redis unavailable, falling back to MongoDB. Error: {e}")
                
        await self.load_extension("cogs.clash_cog")

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
