import os
import logging
import threading
import discord
from discord.ext import commands
from dotenv import load_dotenv
from flask import Flask
from waitress import serve
from motor.motor_asyncio import AsyncIOMotorClient

# Import the new Blueprint containing all web routes
from web_routes import web_bp

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
        """Loads cogs automatically when the bot boots up."""
        try:
            # Assuming your cog is in a folder named 'cogs' and the file is 'clash_cog.py'
            await self.load_extension("cogs.clash_cog")
            log.info("✅ clash_cog loaded successfully.")
        except Exception as e:
            log.error(f"❌ Failed to load cogs: {e}")

# ---------------------------------------------------------------------------
# 4. EXECUTION
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.config["RAW_CSV_TEMPLATE_FALLBACK"] = "Native field selector extraction logic active."
    
    # Start Flask Web Server in a separate thread
    threading.Thread(target=run_flask, daemon=True).start()
    
    # Start Discord Bot
    bot = GraveyardBot()
    bot.run(os.getenv("DISCORD_TOKEN"))