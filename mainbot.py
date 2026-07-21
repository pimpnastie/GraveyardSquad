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

# Idea #148: session cookie hardening. SECURE requires the app to actually be
# served over HTTPS (true for the Render deployment this project runs on) —
# if this is ever run locally over plain HTTP, browsers will silently refuse
# to set the cookie at all, so keep that in mind when testing locally.
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

# Register all web routes from web_routes.py
app.register_blueprint(web_bp)

# Idea #188: custom 404/500 error pages matching the site's dark visual
# identity instead of Flask's default plain-text error output. Kept as a
# small inline template (rather than a Mongo-backed get_template() entry)
# since it has to work even when Mongo itself is the thing that's down.
_ERROR_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} | Graveyard Squad</title>
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/static/theme.css">
<style>
  body {{ background: var(--gy-bg, #0b0c10); color: var(--gy-text, #c5c6c7); font-family: 'Segoe UI', system-ui, sans-serif;
          min-height: 100vh; display: flex; align-items: center; justify-content: center; margin: 0; text-align: center; padding: 24px; }}
  .err-code {{ font-size: 72px; font-weight: 700; color: var(--gy-accent, #00e5ff); text-shadow: 0 0 24px rgba(0,229,255,0.35); margin-bottom: 8px; }}
  .err-skull {{ font-size: 40px; margin-bottom: 12px; }}
  .err-msg {{ font-size: 16px; color: var(--gy-dim, #888); margin-bottom: 24px; }}
  .err-link {{ color: var(--gy-accent, #00e5ff); text-decoration: none; font-weight: 700; }}
  .err-link:hover {{ opacity: 0.8; }}
</style>
</head>
<body>
  <div>
    <div class="err-skull">☠</div>
    <div class="err-code">{code}</div>
    <div class="err-msg">{message}</div>
    <a class="err-link" href="/">← Back to the Roster</a>
  </div>
</body>
</html>"""

@app.errorhandler(404)
def _handle_404(e):
    return _ERROR_PAGE.format(title="Not Found", code="404", message="That page doesn't exist — maybe it wandered into the graveyard."), 404

@app.errorhandler(500)
def _handle_500(e):
    log.error(f"Unhandled 500 error: {e}")
    return _ERROR_PAGE.format(title="Server Error", code="500", message="Something broke on our end. It's been logged — try again shortly."), 500

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

        # 250-ideas pass (block 5): clash_cog.py now also registers slash
        # commands (app_commands) — /mystats, /scout, /leaderboard, /link,
        # /deck, /botstatus, /recruit, /syncbeta, plus a right-click context
        # menu command. Sync scoped to GUILD_ID (near-instant) rather than a
        # global sync (which can take up to an hour to propagate).
        try:
            guild_id = os.getenv("GUILD_ID")
            if guild_id:
                guild_obj = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=guild_obj)
                synced = await self.tree.sync(guild=guild_obj)
            else:
                synced = await self.tree.sync()
            log.info(f"✅ Synced {len(synced)} slash command(s).")
        except Exception as e:
            log.error(f"❌ Slash command sync failed: {e}")

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