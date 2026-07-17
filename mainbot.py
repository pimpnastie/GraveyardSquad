import os
import logging
import threading
import asyncio
import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv
from flask import Flask
from waitress import serve
from motor.motor_asyncio import AsyncIOMotorClient

# Import the new Blueprint containing all web routes
from web_routes import web_bp
from data_harvester import start_harvester_loop

# ---------------------------------------------------------------------------
# 1. SETUP & ENV VARS
# ---------------------------------------------------------------------------
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mainbot")

REQUIRED_ENV_VARS = [
    "CR_TOKEN", "DISCORD_TOKEN", "FLASK_SECRET", 
    "MONGO_URL", "REDIS_URL", "GUILD_ID"
]
missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
if missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

# ---------------------------------------------------------------------------
# 2. FLASK APP INITIALIZATION
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET")

# Register all web routes from web_routes.py
app.register_blueprint(web_bp)

def run_flask():
    port = int(os.getenv("PORT", 5000))
    log.info(f"🌐 Flask dashboard running on port {port}")
    serve(app, host="0.0.0.0", port=port)

# ---------------------------------------------------------------------------
# 3. DISCORD BOT CLASS
# ---------------------------------------------------------------------------
def get_dynamic_prefix(bot_instance, message):
    return bot_instance.active_prefix

class GraveyardBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix=get_dynamic_prefix, intents=intents)
        
        # Async Discord Engine Variables
        self.http_session = None
        self.cr_token = os.getenv("CR_TOKEN", "").strip()
        self.redis_available = False
        self.active_prefix = "!"
        
        # Configuration & State Variables (Restored)
        self.maintenance_mode = False
        self.feature_auto_pings = False
        self.ignored_channels = []
        self.war_channel_id = 0
        self._last_config_load = 0.0
        
        # Async MongoDB for Bot logic (Restored)
        self.mongo_client = AsyncIOMotorClient(os.getenv("MONGO_URL", "mongodb://localhost:27017"))
        self.db = self.mongo_client["graveyardbot"]
        self.db_users = self.db["users"]
        self.custom_cmds = self.db["custom_commands"]

    async def setup_hook(self):
        """Creates the shared HTTP session, then loads cogs when the bot boots up."""
        self.http_session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.cr_token}", "Accept": "application/json"}
        )
        try:
            # Assuming your cog is in a folder named 'cogs' and the file is 'clash_cog.py'
            await self.load_extension("cogs.clash_cog")
            log.info("✅ clash_cog loaded successfully.")
        except Exception as e:
            log.error(f"❌ Failed to load cogs: {e}")

    async def async_fetch_cr_api(self, endpoint: str, retries: int = 4):
        """Async counterpart to DataHarvester.fetch_api, for use inside cog commands/tasks."""
        if self.http_session is None:
            log.error("async_fetch_cr_api called before http_session was initialized.")
            return None
        url = f"https://proxy.royaleapi.dev/v1/{endpoint}"
        for attempt in range(retries):
            try:
                async with self.http_session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 429:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        log.warning(f"CR API {endpoint} returned {resp.status}")
            except Exception as e:
                log.warning(f"CR API fetch error on {endpoint} (attempt {attempt+1}/{retries}): {e}")
                await asyncio.sleep(2 ** attempt)
        return None

    async def close(self):
        if self.http_session:
            await self.http_session.close()
        await super().close()

# ---------------------------------------------------------------------------
# 4. EXECUTION
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.config["RAW_CSV_TEMPLATE_FALLBACK"] = "Native field selector extraction logic active."
    
    # Start Flask Web Server in a separate thread
    threading.Thread(target=run_flask, daemon=True).start()

    # Start the data harvester loop (boot catch-up + 30-min refresh cycle) so the
    # roster/player pages and Discord commands actually have data to show.
    threading.Thread(target=start_harvester_loop, daemon=True).start()

    # Start Discord Bot
    bot = GraveyardBot()
    bot.run(os.getenv("DISCORD_TOKEN"))