import os
import sys
import subprocess
import logging
import threading
import traceback
import asyncio

# --- 1. AUTOMATED ENVIRONMENT SETUP ---
def sync_environment():
    """Ensures a virtual environment exists and requirements are installed."""
    req_file = "requirements.txt"
    venv_dir = "venv"
    
    # Check if we are already running inside a virtual environment
    is_venv = sys.prefix != sys.base_prefix or os.path.exists(venv_dir)

    if not os.path.exists(req_file):
        # Create a basic requirements file if it doesn't exist to prevent errors
        with open(req_file, "w") as f:
            f.write("discord.py\naiohttp\nmotor\nredis\nflask\npython-dotenv\nthefuzz\nwaitress\nopenpyxl\n")
        print(f"✅ Created default {req_file}")

    # If running locally and no venv exists, create it
    if not is_venv and os.name == 'nt':  # Windows check
        print("🛠️ Local environment detected. Creating virtual environment...")
        subprocess.check_call([sys.executable, "-m", "venv", venv_dir])
        
        # Path to python inside the new venv
        python_exe = os.path.join(venv_dir, "Scripts", "python.exe")
        
        print("📦 Installing requirements...")
        subprocess.check_call([python_exe, "-m", "pip", "install", "-r", req_file])
        
        print("🚀 Environment ready. Restarting in Virtual Environment...")
        os.execv(python_exe, [python_exe] + sys.argv)

    # If already in a venv, dynamically check requirements.txt
    else:
        try:
            import pkg_resources
            
            # Read all packages listed in requirements.txt (ignoring empty lines)
            with open(req_file, "r") as f:
                required_packages = [line.strip() for line in f if line.strip() and not line.startswith("#")]
            
            # This asks the environment: "Are all of these packages installed?"
            # It will intentionally throw an error if anything is missing or outdated.
            pkg_resources.require(required_packages)
            
        except Exception as e:
            # If ANY package from the text file is missing, we trigger the installer!
            print("🛠️ Missing or outdated dependencies detected. Installing...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req_file])

# Run environment sync before starting the bot
sync_environment()

# --- 2. STANDARD IMPORTS ---

# --- 2. STANDARD IMPORTS ---
import discord
import aiohttp
import redis.asyncio as redis
from flask import Flask, render_template_string
from discord.ext import commands
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from waitress import serve 

load_dotenv()

# --- 3. LOGGING & CONFIG ---
discord.utils.setup_logging(level=logging.INFO)
log = logging.getLogger("clashbot")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CR_TOKEN      = os.getenv("CR_TOKEN")
MONGO_URL      = os.getenv("MONGO_URL")
REDIS_URL      = os.getenv("REDIS_URL")

# Validate environment
_REQUIRED_ENV = {"DISCORD_TOKEN": DISCORD_TOKEN, "CR_TOKEN": CR_TOKEN, "MONGO_URL": MONGO_URL}
for _name, _val in _REQUIRED_ENV.items():
    if not _val:
        raise RuntimeError(f"Required environment variable '{_name}' is missing")

# --- 4. FLASK DASHBOARD ---
app = Flask(__name__)
_dashboard_lock = threading.Lock()
dashboard_data: list = []

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Graveyard Bot Dashboard</title>
    <style>
        body { font-family: sans-serif; padding: 2rem; }
        table { border-collapse: collapse; width: 100%; }
        th, td { border: 1px solid #ccc; padding: 0.5rem 1rem; text-align: left; }
        th { background: #f0f0f0; }
    </style>
</head>
<body>
    <h1>🏆 Graveyard Bot Dashboard</h1>
    <p>{{ users | length }} linked player(s)</p>
    <table>
        <tr><th>Discord ID</th><th>Player Tag</th></tr>
        {% for user in users %}
        <tr>
            <td>{{ user['_id'] }}</td>
            <td>#{{ user.get('player_id', '???') }}</td>
        </tr>
        {% endfor %}
    </table>
</body>
</html>
"""

@app.route("/")
def home():
    with _dashboard_lock:
        data = list(dashboard_data)
    return render_template_string(HTML_TEMPLATE, users=data)

@app.route("/health")
def health():
    return {"status": "ok"}, 200

def run_flask():
    port = int(os.getenv("PORT", 5000))
    def _start():
        try:
            log.info(f"Flask dashboard starting on port {port} (Waitress)")
            serve(app, host="0.0.0.0", port=port)
        except Exception as e:
            log.error(f"Flask error: {e}")
    threading.Thread(target=_start, daemon=True).start()

# --- 5. DISCORD BOT ---
class ClashBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)

        self.mongo: AsyncIOMotorClient | None = None
        self.db = None
        self.db_users = None
        self.redis: redis.Redis | None = None
        self.redis_available: bool = False
        self.http_session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession()

        self.mongo = AsyncIOMotorClient(MONGO_URL)
        self.db = self.mongo["ClashBotDB"]
        self.db_users = self.db["users"]
        log.info("✅ MongoDB client initialised")

        if REDIS_URL:
            try:
                self.redis = redis.from_url(REDIS_URL, decode_responses=True)
                await self.redis.ping()
                self.redis_available = True
                log.info("✅ Redis connected")
            except Exception as e:
                log.warning(f"⚠️  Redis unavailable, caching disabled: {e}")

        # Loading the combined cog we created
        extensions = ["cogs.clash_cog"]
        for ext in extensions:
            try:
                await self.load_extension(ext)
                log.info(f"✅ Loaded extension: {ext}")
            except Exception as e:
                log.error(f"❌ Failed to load extension '{ext}':\n{traceback.format_exc()}")

        await self.tree.sync()

        # Background task for dashboard
        task = asyncio.create_task(self.update_dashboard_cache())
        task.add_done_callback(self._on_background_task_done)

    def _on_background_task_done(self, task: asyncio.Task):
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.error(f"Background task crashed:\n{traceback.format_exc()}")

    def _cr_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {CR_TOKEN}",
            "Accept": "application/json",
        }

    async def update_dashboard_cache(self):
        global dashboard_data
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                cursor = self.db_users.find({})
                docs = await cursor.to_list(length=100)
                new_data = [{**doc, "_id": str(doc["_id"])} for doc in docs]
                with _dashboard_lock:
                    dashboard_data = new_data
                log.debug(f"Dashboard cache updated: {len(new_data)} users")
            except Exception as e:
                log.error(f"Dashboard cache update failed: {e}")
            await asyncio.sleep(60)

    async def on_ready(self):
        log.info(f"✅ Logged in as {self.user} (ID: {self.user.id})")

    async def close(self):
        if self.http_session:
            await self.http_session.close()
        if self.redis and self.redis_available:
            await self.redis.aclose()
        await super().close()
        if self.mongo:
            self.mongo.close()

# --- 6. MAIN RUNNER ---
async def main():
    bot = ClashBot()
    run_flask()
    try:
        async with bot:
            await bot.start(DISCORD_TOKEN)
    except Exception as e:
        log.critical(f"Bot crashed: {e}")
        traceback.print_exc()
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down (KeyboardInterrupt)")