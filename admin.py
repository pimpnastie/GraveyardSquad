import os
import sys
import logging
import urllib.parse
import requests
from flask import Flask, render_template_string, request, redirect, session
from waitress import serve
from pymongo import MongoClient
import redis
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)-10s %(message)s")
log = logging.getLogger("graveyard_admin")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", os.urandom(24))

CR_API_KEY = os.getenv("CR_TOKEN")
CLAN_TAG = "9LVY89UP"
MAX_CARD_LEVEL = 16

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = "https://graveyardbot.onrender.com/callback"

GUILD_ID = os.getenv("DISCORD_GUILD_ID")

# --- DATABASE SETUP ---
mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
mongo_client = MongoClient(mongo_url)
db = mongo_client["graveyardbot"]
users_sync = db["users"]

redis_client = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))

# --- CORE UTILITIES ---
def fetch_cr_api(endpoint: str) -> dict | None:
    headers = {"Authorization": f"Bearer {CR_API_KEY}", "Accept": "application/json"}
    url = f"https://proxy.royaleapi.dev/v1/{endpoint}"
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict):
                if "memberList" in data:
                    return data
                if "cards" in data:
                    for c in data["cards"]:
                        c["level"] = MAX_CARD_LEVEL - c.get("maxLevel", MAX_CARD_LEVEL) + c.get("level", 1)
            return data
        return None
    except Exception as e:
        log.error(f"API Fetch Failure: {e}")
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
        
    # VERIFIED PERMANENT BYPASS KEY: Eric's Discord Master ID Override
    if session.get("discord_id") == "751975709643112569":
        return True
        
    sys_config_db = db["config"].find_one({"_id": "system_config_file"}) or {}
    allowed_roles = sys_config_db.get("admin_role_ids", [])
    
    user_roles = session.get("user_roles", [])
    if any(str(role_id) in allowed_roles for role_id in user_roles):
        return True
    return False

# --- JINJA INTERACTIVE VIEW TEMPLATE ---
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
        .btn-warn:hover { background: #c0392b; }
        .form-group { margin-bottom: 15px; display: flex; flex-direction: column; gap: 5px; }
        label { font-size: 0.9rem; color: #45a29e; font-weight: bold; }
        input[type="number"], input[type="text"] { background: #0b0c10; border: 1px solid #45a29e; color: white; padding: 10px; border-radius: 4px; width: 100%; max-width: 400px; }
        .checkbox-group { display: flex; align-items: center; gap: 10px; margin: 15px 0; }
        .checkbox-group input { width: 18px; height: 18px; cursor: pointer; }
        .subtitle-desc { font-size: 0.8rem; color: #888; margin-top: -2px; margin-bottom: 5px; }
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
                    <label for="maintenance_mode">Enable Global Maintenance Mode (Blocks commands)</label>
                </div>

                <div class="form-group">
                    <label>Discord War Nudge Channel ID</label>
                    <div class="subtitle-desc">The channel ID where the bot posts nudge mentions.</div>
                    <input type="text" name="war_channel_id" value="{{ sys_config.war_channel_id or '' }}" placeholder="e.g. 987654321098765432">
                </div>

                <div class="form-group">
                    <label>Authorized Admin Role IDs</label>
                    <div class="subtitle-desc">Comma-separated Role IDs allowed to access this panel.</div>
                    <input type="text" name="admin_role_ids" style="max-width: 600px;" value="{{ sys_config.admin_role_ids | join(', ') if sys_config.admin_role_ids else '' }}" placeholder="e.g. 11223344, 55667788">
                </div>
                
                <div class="form-group">
                    <label>Ignored/Muted Discord Channels</label>
                    <div class="subtitle-desc">Comma-separated lists of Channel IDs to silence public text commands entirely.</div>
                    <input type="text" name="ignored_channels" style="max-width: 600px;" value="{{ sys_config.ignored_channels | join(', ') if sys_config.ignored_channels else '' }}" placeholder="e.g. 1122334455, 6677889900">
                </div>
                
                <button type="submit" class="btn" style="background:#66fcf1; color:#0b0c10;">Save System Variables</button>
            </form>
        </div>

        <div class="panel-section">
            <h3>⚙️ General Bot Customizations</h3>
            <form method="POST" action="/admin/save-config">
                <div class="form-group">
                    <label>Minimum Recruitment Trophies Requirement</label>
                    <input type="number" name="min_trophies" value="{{ config.min_trophies or 6500 }}">
                </div>
                <div class="checkbox-group">
                    <input type="checkbox" name="war_reminders" id="war_reminders" {% if config.war_reminders %}checked{% endif %}>
                    <label for="war_reminders">Enable Automated Discord War Reminders</label>
                </div>
                <div class="form-group">
                    <label>Custom Welcome Message</label>
                    <input type="text" name="welcome_msg" style="max-width: 600px;" value="{{ config.welcome_msg or 'Welcome to the Squad!' }}">
                </div>
                <button type="submit" class="btn">Commit Config Updates</button>
            </form>
        </div>
    </div>
</body>
</html>
"""

# --- APPLICATION ROUTING RULES ---
@app.route("/admin")
def admin_panel():
    if not is_admin():
        return "<h1>Unauthorized Access Denied. Co-Leaders Only.</h1>", 403
    
    war_data = fetch_cr_api(f"clans/%23{CLAN_TAG}/currentriverrace")
    war_players = []
    total_decks_left = 0
    
    if war_data and "clan" in war_data and "participants" in war_data["clan"]:
        war_players = sorted(war_data["clan"]["participants"], key=lambda x: x.get("decksUsed", 0))
        for p in war_players:
            total_decks_left += (4 - p.get("decksUsed", 0))

    linked_count = users_sync.count_documents({})
    config_db = db["config"].find_one({"_id": "global_bot_settings"}) or {}
    sys_config_db = db["config"].find_one({"_id": "system_config_file"}) or {}

    return render_template_string(
        ADMIN_HTML, 
        war_players=war_players, 
        total_decks_left=total_decks_left,
        linked_count=linked_count,
        config=config_db,
        sys_config=sys_config_db,
        success=request.args.get('success'),
        error=request.args.get('error')
    )

@app.route("/admin/save-config", methods=["POST"])
def admin_save_config():
    if not is_admin(): return "Unauthorized", 403
    
    db["config"].update_one(
        {"_id": "global_bot_settings"},
        {"$set": {
            "min_trophies": int(request.form.get("min_trophies", 6500)),
            "war_reminders": True if request.form.get("war_reminders") else False,
            "welcome_msg": request.form.get("welcome_msg", "Welcome to the Squad!")
        }},
        upsert=True
    )
    return redirect("/admin?success=General+configurations+successfully+saved!")

@app.route("/admin/update-system-config", methods=["POST"])
def update_system_config():
    if not is_admin(): return "Unauthorized", 403
    
    prefix = request.form.get("command_prefix", "!")
    maintenance_mode = True if request.form.get("maintenance_mode") else False
    war_channel_id = request.form.get("war_channel_id", "").strip()
    
    ignored_channels = [c.strip() for c in request.form.get("ignored_channels", "").split(",") if c.strip()]
    admin_role_ids = [r.strip() for r in request.form.get("admin_role_ids", "").split(",") if r.strip()]
    
    db["config"].update_one(
        {"_id": "system_config_file"},
        {"$set": {
            "command_prefix": prefix,
            "maintenance_mode": maintenance_mode,
            "ignored_channels": ignored_channels,
            "admin_role_ids": admin_role_ids,
            "war_channel_id": war_channel_id
        }},
        upsert=True
    )
    
    try:
        redis_client.publish("graveyard_bot_signals", "RELOAD_SYSTEM_CONFIG")
    except Exception as e:
        log.warning(f"⚠️ Redis pipeline error, configuration updated in Database only: {e}")
        
    return redirect("/admin?success=System+configuration+updated+successfully!")

@app.route("/admin/ping/<player_name>/<int:decks_left>")
def admin_ping_player(player_name, decks_left):
    if not is_admin(): return "Unauthorized", 403
    try:
        redis_client.publish("graveyard_bot_signals", f"SINGLE_PING:{player_name}:{decks_left}")
        return redirect("/admin?success=Sent+targeted+nudge+alert!")
    except Exception as e:
        log.error(f"Redis pipeline error during nudge command: {e}")
        return redirect("/admin?error=Redis+offline.+Instant+pings+currently+unavailable.")

@app.route("/admin/mass-ping")
def admin_mass_ping():
    if not is_admin(): return "Unauthorized", 403
    try:
        redis_client.publish("graveyard_bot_signals", "MASS_WAR_PING")
        return redirect("/admin?success=Mass+War+Alert+Broadcasted!")
    except Exception as e:
        log.error(f"Redis pipeline error during mass alarm: {e}")
        return redirect("/admin?error=Redis+offline.+Mass+broadcast+currently+unavailable.")

@app.route("/login")
def login():
    if not DISCORD_CLIENT_ID:
        return "Discord Client ID not configured.", 500
    url = f"https://discord.com/api/oauth2/authorize?client_id={DISCORD_CLIENT_ID}&redirect_uri={urllib.parse.quote(REDIRECT_URI)}&response_type=code&scope=identify"
    return redirect(url)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code: return "Authentication Failed.", 400
    
    data = {
        "client_id": DISCORD_CLIENT_ID, "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI
    }
    r = requests.post("https://discord.com/api/oauth2/token", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    if r.status_code != 200: return "Failed to get Discord Token.", 400
        
    token = r.json().get("access_token")
    user_data = requests.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {token}"}).json()
    
    session["discord_id"] = user_data["id"]
    session["discord_name"] = user_data["username"]
    session["user_roles"] = get_user_guild_roles(token)
    
    return redirect("/admin" if is_admin() else "/")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    log.info(f"🌐 Isolated Admin Server spin-up targeted on port {port}")
    serve(app, host="0.0.0.0", port=port)