import os
import json
import time
import secrets
import threading
import logging
import requests
from datetime import datetime, timezone
from flask import Blueprint, request, redirect, session, jsonify, url_for
from pymongo import MongoClient
import redis as sync_redis
from jinja2.sandbox import SandboxedEnvironment

from data_harvester import get_harvester

# ---------------------------------------------------------------------------
# 1. WEB SETUP & DATABASE SYNC CONNECTIONS
# ---------------------------------------------------------------------------
web_bp = Blueprint("web", __name__)
log = logging.getLogger("web_routes")

CLAN_TAG = os.getenv("CLAN_TAG", "9LVY89UP").strip().upper().replace("#", "")
MAX_CARD_LEVEL = int(os.getenv("MAX_CARD_LEVEL", 15))

# Discord OAuth2 (Authorization Code flow)
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "")
DISCORD_API = "https://discord.com/api"
DISCORD_OAUTH_SCOPES = "identify guilds.members.read"
GUILD_ID = os.getenv("GUILD_ID", "")

# Database connections
mongo_client_sync = MongoClient(os.getenv("MONGO_URL", "mongodb://localhost:27017"))
db_sync = mongo_client_sync["graveyardbot"]
redis_sync_client = sync_redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))

# API session for efficiency
cr_api_session = requests.Session()
cr_api_session.headers.update({
    "Authorization": f"Bearer {os.getenv('CR_TOKEN', '').strip()}",
    "User-Agent": "GraveyardBot/1.2 (Web Blueprint)",
    "Cache-Control": "max-age=300"
})

sandbox_env = SandboxedEnvironment(autoescape=True)
_cache_lock = threading.Lock()
_HTML_CACHE = {}

# ---------------------------------------------------------------------------
# 2. LEGACY HELPERS & DATA BRIDGES
# ---------------------------------------------------------------------------
def clean_tag(tag: str) -> str:
    return tag.strip().upper().replace("#", "")

def fetch_cr_api(endpoint: str, retries: int = 3) -> dict | None:
    url = f"https://proxy.royaleapi.dev/v1/{endpoint}"
    for attempt in range(retries):
        try:
            response = cr_api_session.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                return data
        except Exception as e:
            log.error(f"Flask API exception: {e}")
            time.sleep(2 ** attempt)
    return None

def get_user_guild_roles(access_token: str) -> list:
    """Fetch the caller's role IDs in GUILD_ID using their OAuth access token.
    Ported from admin.py — needed so admin_role_ids-based access (role-granted,
    not just explicit user ID) works the same way the old standalone app did.
    """
    if not GUILD_ID:
        return []
    try:
        r = requests.get(
            f"{DISCORD_API}/users/@me/guilds/{GUILD_ID}/member",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=5,
        )
        if r.status_code == 200:
            return r.json().get("roles", [])
    except Exception as e:
        log.error(f"Failed to fetch guild roles: {e}")
    return []

def is_admin() -> bool:
    if "discord_id" not in session: return False
    discord_id = str(session.get("discord_id"))
    master_admin = os.getenv("MASTER_ADMIN_ID", "")
    if master_admin and discord_id == master_admin: return True
    # Check Mongo for authorized admins
    config = db_sync["config"].find_one({"_id": "system_config"}) or {}
    if discord_id in config.get("admin_user_ids", []):
        return True
    # Role-based access — ported from admin.py so members granted admin via a
    # Discord role (not just an explicit user ID) aren't locked out.
    allowed_roles = set(str(r) for r in config.get("admin_role_ids", []))
    if allowed_roles:
        user_roles = session.get("user_roles", [])
        if any(str(r) in allowed_roles for r in user_roles):
            return True
    return False

def get_template(template_name: str) -> str:
    with _cache_lock:
        if template_name in _HTML_CACHE: return _HTML_CACHE[template_name]
        doc = db_sync["config"].find_one({"_id": "html_templates"})
        content = doc.get(template_name) if doc else None
        if not content:
            # Nothing deployed to Mongo yet for this template — fall back to the
            # canonical .html file on disk so pages never show "Template Missing"
            # just because nobody has hit Deploy in the UI editor.
            import pathlib
            disk_path = pathlib.Path(__file__).parent / f"{template_name}.html"
            if disk_path.exists():
                content = disk_path.read_text(encoding="utf-8")
                log.warning(f"'{template_name}' not found in Mongo html_templates — served from disk fallback.")
            else:
                content = "<h2>Template Missing</h2>"
                log.error(f"'{template_name}' missing from both Mongo and disk ({disk_path}).")
        _HTML_CACHE[template_name] = content
        return content

def get_csrf_token() -> str:
    """One token per browser session, generated lazily and reused for every
    admin page render so the frontend can echo it back on state-changing POSTs."""
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token

@web_bp.before_request
def _csrf_protect():
    # Only state-changing admin requests need a token — GETs and everything
    # outside /admin/ (public roster/player/link pages, /api/lfg, etc.) are untouched.
    if request.method == "POST" and request.path.startswith("/admin/"):
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not expected or not secrets.compare_digest(str(sent), str(expected)):
            return jsonify({"error": "CSRF token missing or invalid. Refresh the page and try again."}), 403

def render_sandboxed(template_str: str, **context) -> str:
    template = sandbox_env.from_string(template_str)
    if "session" not in context: context["session"] = session
    return template.render(**context)

# Analytical Bridges (Syncing logic with ClashCog)
def get_player_analytical_data(tag):
    clean = clean_tag(tag)
    player = fetch_cr_api(f"players/%23{clean}")
    if not player: return None
    history = list(db_sync["battle_history"].find({"player_tag": clean}).sort("battle_time", -1).limit(10))
    player["recent_battles"] = history

    # Collection completion — how much of their full card collection is maxed.
    cards = player.get("cards") or []
    player["collection_total_count"] = len(cards)
    player["collection_maxed_count"] = sum(
        1 for c in cards if c.get("level", 0) >= c.get("maxLevel", 999)
    )

    # 7-day trophy trend — same baseline logic as the clan-wide "climbers" leaderboard,
    # just scoped to one player so their own page can show a personal "most improved" stat.
    import datetime as _dt
    week_ago = (_dt.datetime.now(timezone.utc) - _dt.timedelta(days=7)).strftime("%Y-%m-%d")
    old_snap = db_sync["player_snapshots"].find_one(
        {"tag": f"#{clean}", "date": {"$gte": week_ago}},
        sort=[("date", 1)],
    )
    player["trophy_trend_7d"] = (player.get("trophies", 0) - old_snap.get("trophies", 0)) if old_snap else None

    return player

def get_clan_war_summary():
    latest_war = db_sync["war_tracking"].find_one({}, sort=[("harvest_time", -1)])
    if not latest_war: return {"status": "No War Data"}
    participants = latest_war.get("participants", [])
    slackers = [m for m in participants if m.get("decksUsed", 0) == 0]
    return {"participants_count": len(participants), "slackers": slackers}

# ---------------------------------------------------------------------------
# 3. DISCORD OAUTH (login / callback / logout)
# ---------------------------------------------------------------------------
@web_bp.route("/login")
def login():
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    session["post_login_redirect"] = request.args.get("next", "/")
    params = (
        f"client_id={DISCORD_CLIENT_ID}&redirect_uri={requests.utils.quote(DISCORD_REDIRECT_URI, safe='')}"
        f"&response_type=code&scope={DISCORD_OAUTH_SCOPES}&state={state}"
    )
    return redirect(f"{DISCORD_API}/oauth2/authorize?{params}")

@web_bp.route("/callback")
def oauth_callback():
    error = request.args.get("error")
    if error:
        return f"Discord login was cancelled or failed: {error}", 400

    code = request.args.get("code")
    state = request.args.get("state")
    if not code or not state or state != session.get("oauth_state"):
        return "Invalid or expired login attempt. Please try again.", 400

    token_resp = requests.post(
        f"{DISCORD_API}/oauth2/token",
        data={
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DISCORD_REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    if token_resp.status_code != 200:
        log.error(f"Discord token exchange failed: {token_resp.text}")
        return "Login failed while exchanging the authorization code.", 400
    access_token = token_resp.json().get("access_token")

    user_resp = requests.get(
        f"{DISCORD_API}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if user_resp.status_code != 200:
        return "Login failed while fetching your Discord profile.", 400
    user_data = user_resp.json()

    session["discord_id"] = user_data["id"]
    session["discord_name"] = f"{user_data['username']}"
    session["discord_avatar"] = user_data.get("avatar")
    session["user_roles"] = get_user_guild_roles(access_token)
    session.pop("oauth_state", None)

    dest = session.pop("post_login_redirect", "/")
    return redirect(dest)

@web_bp.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ---------------------------------------------------------------------------
# 4. ACCOUNT LINKING (Clash Royale tag <-> Discord ID)
# ---------------------------------------------------------------------------
@web_bp.route("/link", methods=["GET", "POST"])
def link_account():
    if "discord_id" not in session:
        return redirect(url_for("web.login", next="/link"))

    if request.method == "GET":
        return render_sandboxed(get_template("link"), name=session.get("discord_name", "Warrior"))

    tag = clean_tag(request.form.get("tag", ""))
    if not tag:
        return render_sandboxed(get_template("link"), name=session.get("discord_name", "Warrior"), error="Please enter a player tag.")

    player = fetch_cr_api(f"players/%23{tag}")
    if not player:
        return render_sandboxed(get_template("link"), name=session.get("discord_name", "Warrior"), error="Couldn't find that player tag. Double-check it and try again.")

    db_sync["users"].update_one(
        {"discord_id": session["discord_id"]},
        {"$set": {
            "discord_id": session["discord_id"],
            "discord_name": session.get("discord_name"),
            "cr_tag": f"#{tag}",
            "linked_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    return redirect(f"/player/{tag}")

# ---------------------------------------------------------------------------
# 5. PUBLIC FRONTEND ROUTES
# ---------------------------------------------------------------------------
@web_bp.route("/")
def index():
    clan_data = fetch_cr_api(f"clans/%23{CLAN_TAG}")
    if not clan_data:
        log.error("Live clan fetch failed on index() — check CR_TOKEN / IP whitelist on this host.")
        clan_data = {"memberList": [], "memberCount": 0}
    return render_sandboxed(get_template("roster"), clan_data=clan_data)

@web_bp.route("/player/<tag>")
def player_profile(tag):
    player_data = get_player_analytical_data(tag)
    db_player = db_sync["player_profiles"].find_one({"tag": f"#{clean_tag(tag)}"})
    return render_sandboxed(get_template("player"), player=player_data, db_player=db_player, is_admin=is_admin(), csrf_token=get_csrf_token())

# ---------------------------------------------------------------------------
# 4. ADMIN & MANAGEMENT ROUTES
# ---------------------------------------------------------------------------
@web_bp.route("/admin")
def admin_panel():
    if not is_admin(): return "Unauthorized", 403
    clan_data = fetch_cr_api(f"clans/%23{CLAN_TAG}")
    db_players = {
        p["tag"].replace("#", ""): p
        for p in db_sync["player_profiles"].find({}, {"tag": 1, "admin_notes": 1, "strikes": 1})
    }
    bot_settings = db_sync["config"].find_one({"_id": "bot_settings"}) or {}
    system_config = db_sync["config"].find_one({"_id": "system_config"}) or {}
    return render_sandboxed(
        get_template("admin"),
        clan_data=clan_data,
        db_players=db_players,
        bot_settings=bot_settings,
        system_config=system_config,
        clan_tag=CLAN_TAG,
        csrf_token=get_csrf_token(),
    )

@web_bp.route("/admin/export/custom", methods=["POST"])
def admin_export_csv():
    """Returns JSON when export_format=json (for the JS CSV builder), raw CSV otherwise."""
    if not is_admin(): return "Unauthorized", 403
    from flask import Response
    export_format = request.form.get("export_format", "csv")
    requested_fields = request.form.getlist("fields") or ["name", "tag", "role", "trophies", "donations"]

    # Pull latest war participant data to enrich the export
    latest_war = db_sync["war_tracking"].find_one({}, sort=[("harvest_time", -1)]) or {}
    war_participants = {
        p.get("tag", "").replace("#", "").upper(): p
        for p in latest_war.get("clan", {}).get("participants", [])
    }

    clan_data = fetch_cr_api(f"clans/%23{CLAN_TAG}") or {}
    records = []
    for m in clan_data.get("memberList", []):
        tag = m.get("tag", "").replace("#", "").upper()
        wp = war_participants.get(tag, {})
        db_profile = db_sync["player_profiles"].find_one({"tag": f"#{tag}"}) or {}
        row = {
            "name": m.get("name", ""),
            "tag": m.get("tag", ""),
            "role": m.get("role", ""),
            "trophies": m.get("trophies", 0),
            "donations": m.get("donations", 0),
            "fame": wp.get("fame", 0),
            "decksUsedToday": wp.get("decksUsedToday", 0),
            "decksRemaining": 4 - wp.get("decksUsedToday", 0),
            "warDayWins": db_profile.get("warDayWins", 0),
            "totalWins": db_profile.get("wins", 0),
            "totalLosses": db_profile.get("losses", 0),
            "current_streak": db_profile.get("current_streak", 0),
        }
        # Only keep requested fields
        records.append({k: row[k] for k in requested_fields if k in row})

    if export_format == "json":
        return jsonify(records)

    import csv, io
    buf = io.StringIO()
    if records:
        writer = csv.DictWriter(buf, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=clan_roster.csv"},
    )

@web_bp.route("/admin/api/player/update", methods=["POST"])
def admin_player_update():
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Expected a JSON body."}), 400
    db_sync["player_profiles"].update_one({"tag": f"#{clean_tag(data.get('tag'))}"}, {"$set": {"admin_notes": data.get("notes")}}, upsert=True)
    return jsonify({"success": True})

@web_bp.route("/admin/api/player/strike", methods=["POST"])
def admin_add_strike():
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Expected a JSON body."}), 400
    db_sync["player_profiles"].update_one({"tag": f"#{clean_tag(data.get('tag'))}"}, {"$inc": {"strikes": 1}}, upsert=True)
    return jsonify({"success": True})

@web_bp.route("/admin/api/player/admin_toggle", methods=["POST"])
def admin_toggle_privilege():
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    data = request.json
    tag = clean_tag(data.get("tag"))
    # is_admin() checks discord_id against admin_user_ids, so we need this player's
    # linked Discord ID, not their in-game tag, for the grant to actually take effect.
    user = db_sync["users"].find_one({"cr_tag": f"#{tag}"})
    if not user or not user.get("discord_id"):
        return jsonify({"error": "This player hasn't linked a Discord account yet — link them first."}), 400
    if data.get("is_admin"):
        db_sync["config"].update_one({"_id": "system_config"}, {"$addToSet": {"admin_user_ids": user["discord_id"]}}, upsert=True)
    else:
        db_sync["config"].update_one({"_id": "system_config"}, {"$pull": {"admin_user_ids": user["discord_id"]}})
    return jsonify({"success": True})

@web_bp.route("/admin/flush-cache", methods=["POST"])
def admin_flush_cache():
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    redis_sync_client.flushall()
    _HTML_CACHE.clear()
    return jsonify({"message": "Cache flushed."})

@web_bp.route("/admin/api/player/link", methods=["POST"])
def admin_manual_link():
    """Manually associate a Discord ID with a player tag (admin.html's manualLinkDiscord)."""
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    data = request.json or {}
    tag = clean_tag(data.get("tag", ""))
    discord_id = str(data.get("discord_id", "")).strip()
    if not tag or not discord_id.isdigit():
        return jsonify({"error": "A valid tag and numeric Discord ID are required."}), 400
    db_sync["users"].update_one(
        {"discord_id": discord_id},
        {"$set": {"discord_id": discord_id, "cr_tag": f"#{tag}", "linked_at": datetime.now(timezone.utc), "linked_by": "admin"}},
        upsert=True,
    )
    return jsonify({"success": True})

@web_bp.route("/admin/api/player/dm-warning", methods=["POST"])
def admin_dm_warning():
    """Queue a single-player DM warning; clash_cog.py's pending-actions loop delivers it."""
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    data = request.json or {}
    tag = clean_tag(data.get("tag", ""))
    user = db_sync["users"].find_one({"cr_tag": f"#{tag}"})
    if not user:
        return jsonify({"error": "That player hasn't linked a Discord account yet."}), 400
    db_sync["pending_actions"].insert_one({
        "kind": "dm_warning",
        "discord_id": user["discord_id"],
        "message": data.get("message", "You're missing war participation — please use your decks!"),
        "created_at": datetime.now(timezone.utc),
        "processed": False,
    })
    return jsonify({"success": True, "message": "DM queued for delivery."})

@web_bp.route("/admin/war/nudges", methods=["POST"])
def admin_war_nudges():
    """Queue DM reminders to every linked member who still has war decks left today."""
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    summary = get_clan_war_summary()
    slacker_tags = {s.get("tag") for s in summary.get("slackers", []) if s.get("tag")}
    if not slacker_tags:
        return jsonify({"message": "No slackers found — nothing to send."})

    linked_users = list(db_sync["users"].find({"cr_tag": {"$in": list(slacker_tags)}}))
    discord_ids = [u["discord_id"] for u in linked_users if u.get("discord_id")]
    if not discord_ids:
        return jsonify({"message": "Slackers found, but none have linked a Discord account yet."})

    db_sync["pending_actions"].insert_one({
        "kind": "war_nudge",
        "discord_ids": discord_ids,
        "created_at": datetime.now(timezone.utc),
        "processed": False,
    })
    return jsonify({"success": True, "message": f"Nudges queued for {len(discord_ids)} member(s)."})

@web_bp.route("/admin/api/settings/save", methods=["POST"])
def admin_save_settings():
    """Persist bot settings (admin.html's Settings tab); the bot reloads these every 30s.
    Covers everything admin.py used to save across its two forms (General Bot
    Customizations + Live System File Configurations), consolidated into one endpoint
    and one Settings tab instead of two disconnected config docs.
    """
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    data = request.json or {}

    # -- bot_settings doc: runtime bot behavior --------------------------------
    update = {
        "maintenance_mode": bool(data.get("maintenance_mode", False)),
        "feature_auto_pings": bool(data.get("feature_auto_pings", False)),
        "war_channel_id": data.get("war_channel_id", 0),
    }
    if "command_prefix" in data:
        prefix = str(data.get("command_prefix") or "!").strip()[:3]
        update["command_prefix"] = prefix or "!"
    if "ignored_channels" in data:
        raw = data.get("ignored_channels", [])
        if isinstance(raw, str):
            raw = [c.strip() for c in raw.split(",") if c.strip()]
        update["ignored_channels"] = [str(c).strip() for c in raw if str(c).strip()]
    # Ported from admin.py's old /admin/save-config ("General Bot Customizations") —
    # these three lived in a separate, disconnected doc before; folded in here so
    # there's one Settings tab and one save button instead of two.
    if "min_trophies" in data:
        try:
            update["min_trophies"] = int(data.get("min_trophies") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "min_trophies must be a number"}), 400
    if "war_reminders" in data:
        update["war_reminders"] = bool(data.get("war_reminders", False))
    if "welcome_msg" in data:
        update["welcome_msg"] = str(data.get("welcome_msg") or "Welcome to the Squad!").strip()[:500]

    db_sync["config"].update_one(
        {"_id": "bot_settings"},
        {"$set": update},
        upsert=True,
    )

    # -- system_config doc: access control -------------------------------------
    # admin_role_ids lives in the same doc is_admin() already reads for admin_user_ids,
    # so role-based access changes take effect immediately without a separate lookup.
    if "admin_role_ids" in data:
        raw = data.get("admin_role_ids", [])
        if isinstance(raw, str):
            raw = [r.strip() for r in raw.split(",") if r.strip()]
        role_ids = [str(r).strip() for r in raw if str(r).strip()]
        db_sync["config"].update_one(
            {"_id": "system_config"},
            {"$set": {"admin_role_ids": role_ids}},
            upsert=True,
        )

    return jsonify({"success": True})

@web_bp.route("/admin/api/harvest/trigger", methods=["POST"])
def admin_trigger_harvest():
    """Manually kick a full harvest cycle (admin.html's triggerHarvester) without blocking the request."""
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    harvester = get_harvester()
    threading.Thread(target=harvester.run_full_cycle, daemon=True).start()
    return jsonify({"success": True, "message": "Harvest started in the background."})

# ---------------------------------------------------------------------------
# 5. NEW ADMIN API ROUTES (required by the new admin dashboard)
# ---------------------------------------------------------------------------

@web_bp.route("/admin/diagnostics")
def admin_diagnostics():
    """Full health-check JSON — powers the Diagnostics tab and raw JSON viewer."""
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    import socket, time as _time

    result = {
        "version": "1.3",
        "environment": os.getenv("FLASK_ENV", "production"),
        "hostname": socket.gethostname(),
    }

    # Redis
    t0 = _time.monotonic()
    try:
        info = redis_sync_client.info("memory")
        ping_ms = round((_time.monotonic() - t0) * 1000, 1)
        result["redis"] = {
            "status": "ok",
            "ping_ms": ping_ms,
            "used_memory": info.get("used_memory_human", "N/A"),
            "total_keys": redis_sync_client.dbsize(),
        }
    except Exception as e:
        result["redis"] = {"status": "error", "error": str(e)}

    # MongoDB
    t0 = _time.monotonic()
    try:
        mongo_client_sync.admin.command("ping")
        ping_ms = round((_time.monotonic() - t0) * 1000, 1)
        result["mongo"] = {
            "status": "ok",
            "ping_ms": ping_ms,
            "snapshot_count": db_sync["player_snapshots"].estimated_document_count(),
            "battle_count": db_sync["battle_history"].estimated_document_count(),
        }
    except Exception as e:
        result["mongo"] = {"status": "error", "error": str(e)}

    # CR API
    t0 = _time.monotonic()
    try:
        test_url = f"https://proxy.royaleapi.dev/v1/clans/%23{CLAN_TAG}"
        resp = cr_api_session.get(test_url, timeout=8)
        latency_ms = round((_time.monotonic() - t0) * 1000, 1)
        result["cr_api"] = {
            "status": "ok" if resp.status_code == 200 else ("rate_limited" if resp.status_code == 429 else "error"),
            "status_code": resp.status_code,
            "latency_ms": latency_ms,
            "endpoint_tested": f"/clans/%23{CLAN_TAG}",
        }
    except Exception as e:
        result["cr_api"] = {"status": "error", "error": str(e)}

    # Bot (written by the bot process into Mongo every 30s via reload_config_loop)
    bot_heartbeat = db_sync["config"].find_one({"_id": "bot_heartbeat"}) or {}
    result["bot"] = {
        "connected": bot_heartbeat.get("connected", False),
        "latency_ms": bot_heartbeat.get("latency_ms"),
        "uptime": bot_heartbeat.get("uptime"),
    }

    # Cache
    result["cache"] = {
        "backend": "redis" if result.get("redis", {}).get("status") == "ok" else "mongo",
        "total_keys": result.get("redis", {}).get("total_keys", 0),
        "html_cache_entries": len(_HTML_CACHE),
    }

    # Harvest metadata
    harv = get_harvester()._harvest_meta.copy()
    # Snapshot history: distinct dates from player_snapshots
    dates = sorted(db_sync["player_snapshots"].distinct("date"), reverse=True)[:14]
    harv["history_dates"] = dates
    harv["member_count"] = db_sync["player_profiles"].estimated_document_count()
    harv["war_participants_found"] = db_sync["war_tracking"].find_one(
        {}, sort=[("harvest_time", -1)]
    ) or {}
    harv["war_participants_found"] = len(
        (harv["war_participants_found"].get("clan") or {}).get("participants", [])
    )
    result["harvest"] = harv

    result["tasks"] = {
        "snapshot_loop": f"every {os.getenv('HARVEST_INTERVAL_MINUTES', 30)} min",
        "next_snapshot": "scheduled",
    }

    return jsonify(result)


@web_bp.route("/admin/api/roster")
def admin_api_roster():
    """Enriched member list for the admin Roster tab.
    Merges: live clan API → player_profiles (strikes, notes, war stats)
            → war_tracking (decks used today, fame)
            → users (discord link, discord_name)
            → config (who is a site admin)
    """
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403

    clan_data = fetch_cr_api(f"clans/%23{CLAN_TAG}") or {}
    members = clan_data.get("memberList", [])

    # Current war participant data keyed by clean tag
    latest_war = db_sync["war_tracking"].find_one({}, sort=[("harvest_time", -1)]) or {}
    war_participants = {
        p.get("tag", "").replace("#", "").upper(): p
        for p in (latest_war.get("clan") or {}).get("participants", [])
    }

    # DB profiles keyed by clean tag
    profiles = {
        p.get("tag", "").replace("#", "").upper(): p
        for p in db_sync["player_profiles"].find(
            {}, {"tag": 1, "strikes": 1, "admin_notes": 1, "warDayWins": 1,
                 "bestTrophies": 1, "wins": 1, "losses": 1}
        )
    }

    # Users (Discord links) keyed by clean cr_tag
    users = {
        u.get("cr_tag", "").replace("#", "").upper(): u
        for u in db_sync["users"].find({}, {"cr_tag": 1, "discord_id": 1, "discord_name": 1})
    }

    # Site admin discord IDs
    config = db_sync["config"].find_one({"_id": "system_config"}) or {}
    admin_ids = set(config.get("admin_user_ids", []))
    master_admin = os.getenv("MASTER_ADMIN_ID", "")

    result = []
    for m in members:
        tag = m.get("tag", "").replace("#", "").upper()
        wp  = war_participants.get(tag, {})
        db  = profiles.get(tag, {})
        usr = users.get(tag, {})
        discord_id = usr.get("discord_id", "")
        result.append({
            "tag":           m.get("tag", ""),
            "name":          m.get("name", ""),
            "role":          m.get("role", "member"),
            "trophies":      m.get("trophies", 0),
            "bestTrophies":  db.get("bestTrophies") or m.get("bestTrophies", 0),
            "donations":     m.get("donations", 0),
            "fame":          wp.get("fame", 0),
            "decksUsedToday": wp.get("decksUsedToday"),   # None = not in war
            "warDayWins":    wp.get("warDayWins") or db.get("warDayWins", 0),
            "in_war":        tag in war_participants,
            "strikes":       db.get("strikes", 0),
            "admin_notes":   db.get("admin_notes", ""),
            "discord_id":    discord_id,
            "discord_name":  usr.get("discord_name", ""),
            "is_site_admin": bool(discord_id and (discord_id in admin_ids or discord_id == master_admin)),
        })

    return jsonify(result)


@web_bp.route("/admin/api/war")
def admin_api_war():
    """Current River Race data."""
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    data = fetch_cr_api(f"clans/%23{CLAN_TAG}/currentriverrace")
    if not data:
        return jsonify({"error": "Could not fetch current war data from CR API."})
    return jsonify(data)


@web_bp.route("/admin/api/war/previous")
def admin_api_war_previous():
    """Most recent completed River Race from the log."""
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    data = fetch_cr_api(f"clans/%23{CLAN_TAG}/riverracelog?limit=1")
    if not data:
        return jsonify({"standings": []})
    items = data.get("items", [])
    return jsonify(items[0] if items else {"standings": []})


@web_bp.route("/admin/api/war/aggregate")
def admin_api_war_aggregate():
    """Aggregate war stats from war_history collection."""
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    pipeline = [
        {"$unwind": "$data.clan.participants"},
        {"$group": {"_id": None, "total_fame": {"$sum": "$data.clan.fame"}}},
    ]
    rows = list(db_sync["war_history"].aggregate(pipeline))
    total_fame = rows[0]["total_fame"] if rows else 0

    # Also pull from live war_tracking collection as a fallback
    if total_fame == 0:
        for doc in db_sync["war_tracking"].find({}, {"_id": 0, "clan.fame": 1}):
            total_fame += (doc.get("clan") or {}).get("fame", 0)

    return jsonify({"total_fame": total_fame})


@web_bp.route("/admin/api/battles")
def admin_api_battles():
    """Last 100 battle records across all clan members."""
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    battles = list(
        db_sync["battle_history"]
        .find({}, {"_id": 0})
        .sort("battle_time", -1)
        .limit(100)
    )
    # Ensure card lists are name strings for the JS renderer
    for b in battles:
        b["team_cards"] = [
            c if isinstance(c, str) else c.get("name", str(c))
            for c in (b.get("team_cards") or [])
        ]
        b["opponent_cards"] = [
            c if isinstance(c, str) else c.get("name", str(c))
            for c in (b.get("opponent_cards") or [])
        ]
    return jsonify(battles)


@web_bp.route("/api/player/<tag>/battles")
def api_player_battles(tag):
    """Per-player recent battles — used by the player profile page's async loader."""
    battles = list(
        db_sync["battle_history"]
        .find({"player_tag": clean_tag(tag)}, {"_id": 0})
        .sort("battle_time", -1)
        .limit(20)
    )
    for b in battles:
        b["team_cards"] = [
            c if isinstance(c, str) else c.get("name", str(c))
            for c in (b.get("team_cards") or [])
        ]
        b["opponent_cards"] = [
            c if isinstance(c, str) else c.get("name", str(c))
            for c in (b.get("opponent_cards") or [])
        ]
    return jsonify(battles)


@web_bp.route("/api/player/<tag>/insights")
def api_player_insights(tag):
    """Per-player battle intelligence — most-used cards, toughest/most-defeated
    matchups, win streaks, rival opponent, busiest battle day. Computed entirely
    from this player's own logged battle_history — no new Clash Royale API calls,
    same pattern as /api/player/<tag>/battles above.
    """
    clean = clean_tag(tag)
    battles = list(
        db_sync["battle_history"]
        .find(
            {"player_tag": clean},
            {"_id": 0, "team_cards": 1, "opponent_cards": 1, "result": 1,
             "battle_time": 1, "opponent_name": 1, "opponent_tag": 1},
        )
        .sort("battle_time", 1)  # chronological — needed for streak math
    )
    if not battles:
        return jsonify({"total_battles": 0})

    def card_name(c):
        return c if isinstance(c, str) else (c or {}).get("name")

    card_usage = {}       # my card -> {games, wins}
    opp_card_faced = {}   # opponent card -> {encounters, wins, losses}
    rival_record = {}     # (opponent name, tag) -> {w, l, d}
    day_counts = {}
    running_streak = 0
    longest_streak = 0

    for b in battles:
        result = b.get("result")

        for raw in (b.get("team_cards") or []):
            c = card_name(raw)
            if not c: continue
            e = card_usage.setdefault(c, {"games": 0, "wins": 0})
            e["games"] += 1
            if result == "win": e["wins"] += 1

        for raw in (b.get("opponent_cards") or []):
            c = card_name(raw)
            if not c: continue
            e = opp_card_faced.setdefault(c, {"encounters": 0, "wins": 0, "losses": 0})
            e["encounters"] += 1
            if result == "win": e["wins"] += 1
            elif result == "loss": e["losses"] += 1

        opp_key = (b.get("opponent_name") or "Unknown", b.get("opponent_tag") or "")
        rec = rival_record.setdefault(opp_key, {"w": 0, "l": 0, "d": 0})
        if result == "win": rec["w"] += 1
        elif result == "loss": rec["l"] += 1
        else: rec["d"] += 1

        # CR API battle_time looks like "20240312T183045.000Z" — first 8 chars = YYYYMMDD
        day = (b.get("battle_time") or "")[:8]
        if len(day) == 8:
            day_counts[day] = day_counts.get(day, 0) + 1

        if result == "win":
            running_streak += 1
            longest_streak = max(longest_streak, running_streak)
        else:
            running_streak = 0

    most_used_cards = sorted(
        [
            {"card": c, "games": v["games"], "win_rate": round(v["wins"] / v["games"] * 100, 1)}
            for c, v in card_usage.items()
        ],
        key=lambda x: -x["games"],
    )[:5]

    MIN_ENCOUNTERS = 3
    qualifying = [
        {
            "card": c, "encounters": v["encounters"], "wins": v["wins"], "losses": v["losses"],
            "win_rate": round(v["wins"] / v["encounters"] * 100, 1),
        }
        for c, v in opp_card_faced.items() if v["encounters"] >= MIN_ENCOUNTERS
    ]
    most_defeated = sorted(qualifying, key=lambda x: (-x["wins"], -x["win_rate"]))[:1]
    toughest_matchup = sorted(qualifying, key=lambda x: (x["win_rate"], -x["encounters"]))[:1]

    rival = None
    if rival_record:
        (r_name, r_tag), rec = max(rival_record.items(), key=lambda kv: kv[1]["w"] + kv[1]["l"] + kv[1]["d"])
        rival = {"name": r_name, "tag": r_tag, "wins": rec["w"], "losses": rec["l"], "draws": rec["d"]}

    busiest_day = None
    if day_counts:
        d, count = max(day_counts.items(), key=lambda kv: kv[1])
        busiest_day = {"date": f"{d[0:4]}-{d[4:6]}-{d[6:8]}", "battles": count}

    return jsonify({
        "total_battles": len(battles),
        "most_used_cards": most_used_cards,
        "most_defeated_card": most_defeated[0] if most_defeated else None,
        "toughest_matchup": toughest_matchup[0] if toughest_matchup else None,
        "current_streak": running_streak,
        "longest_win_streak": longest_streak,
        "rival": rival,
        "busiest_day": busiest_day,
    })


@web_bp.route("/admin/api/template/<name>")
def admin_get_template(name):
    """Serve a template to the UI editor — ?source=current pulls live DB, ?source=default pulls the DEFAULT_ constant."""
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    allowed = {"roster", "player", "admin", "link"}
    if name not in allowed:
        return jsonify({"error": f"Unknown template '{name}'"}), 400

    source = request.args.get("source", "current")
    if source == "current":
        doc = db_sync["config"].find_one({"_id": "html_templates"}) or {}
        html = doc.get(name, "")
        if not html:
            # Fall back to default if nothing is deployed yet
            source = "default"

    if source == "default":
        # Read the canonical file from disk next to web_routes.py
        import pathlib
        candidates = [
            pathlib.Path(__file__).parent / f"{name}.html",
        ]
        html = ""
        for path in candidates:
            if path.exists():
                html = path.read_text(encoding="utf-8")
                break
        if not html:
            return jsonify({"error": f"Default file for '{name}' not found on disk."}), 404

    return jsonify({"html": html, "template": name, "source": source})


@web_bp.route("/admin/update-html", methods=["POST"])
def admin_update_html():
    """Deploy an HTML template from the UI editor into the DB."""
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    template_name = request.form.get("template_name", "").strip()
    html_content = request.form.get("html_content", "").strip()
    allowed = {"roster", "player", "admin", "link"}
    if template_name not in allowed:
        return jsonify({"error": f"Unknown template '{template_name}'"}), 400
    if not html_content:
        return jsonify({"error": "html_content is empty"}), 400

    db_sync["config"].update_one(
        {"_id": "html_templates"},
        {"$set": {template_name: html_content}},
        upsert=True,
    )
    # Invalidate the in-process HTML cache so the next page load picks up the change
    with _cache_lock:
        _HTML_CACHE.pop(template_name, None)

    return jsonify({"success": True, "template": template_name})


@web_bp.route("/admin/preview", methods=["POST"])
def admin_preview():
    """Render a template with dummy context so the editor's preview button works."""
    if not is_admin(): return "Unauthorized", 403
    template_name = request.form.get("template_name", "")
    html_content = request.form.get("html_content", "")
    if not html_content:
        return "Empty template", 400

    # Provide enough dummy context for the template to render without crashing
    dummy_clan = fetch_cr_api(f"clans/%23{CLAN_TAG}") or {"memberList": [], "memberCount": 0}
    dummy_player = {"name": "Preview Player", "tag": "#PREVIEW", "trophies": 9999,
                    "bestTrophies": 9999, "wins": 100, "losses": 50, "threeCrownWins": 30,
                    "battleCount": 150, "donations": 200, "donationsReceived": 180,
                    "warDayWins": 12, "expLevel": 14, "role": "member",
                    "arena": {"name": "Ultimate Champion"}, "currentDeck": [],
                    "current_streak": 3, "currentFavouriteCard": {"name": "Goblin Barrel"}}
    try:
        rendered = render_sandboxed(
            html_content,
            clan_data=dummy_clan,
            player=dummy_player,
            data=dummy_player,
            db_player={},
            is_admin=True,
            clan_tag=CLAN_TAG,
            max_lvl=MAX_CARD_LEVEL,
        )
        return rendered
    except Exception as e:
        return f"<pre>Template render error:\n{e}</pre>", 400


@web_bp.route("/admin/harvest/manual", methods=["POST"])
def admin_harvest_manual():
    """Alias for /admin/api/harvest/trigger — the new admin JS calls this path."""
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    harvester = get_harvester()
    threading.Thread(target=harvester.run_full_cycle, daemon=True).start()
    return jsonify({"success": True, "message": "Harvest started in the background."})


@web_bp.route("/admin/api/snapshot/<date>")
def admin_api_snapshot(date):
    """Return all player snapshots for a given date (YYYY-MM-DD) as JSON."""
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    docs = list(db_sync["player_snapshots"].find({"date": date}, {"_id": 0}))
    return jsonify(docs)


@web_bp.route("/admin/api/users")
def admin_api_users():
    """User table for the User Access tab."""
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403

    config = db_sync["config"].find_one({"_id": "system_config"}) or {}
    admin_ids = set(config.get("admin_user_ids", []))
    master_admin = os.getenv("MASTER_ADMIN_ID", "")

    # Build a tag -> profile map for clan rank
    profiles = {
        p.get("tag", ""): p
        for p in db_sync["player_profiles"].find({}, {"tag": 1, "role": 1, "name": 1})
    }

    users = list(db_sync["users"].find({}, {"_id": 0}))
    result = []
    for u in users:
        cr_tag = u.get("cr_tag", "")
        profile = profiles.get(cr_tag, {})
        discord_id = u.get("discord_id", "")
        result.append({
            "discord_id": discord_id,
            "name": profile.get("name") or u.get("discord_name", "Unknown"),
            "cr_tag": cr_tag.replace("#", "") if cr_tag else "",
            "is_linked": bool(cr_tag),
            "rank": profile.get("role", "—"),
            "status": "Admin" if (discord_id in admin_ids or discord_id == master_admin) else "Member",
        })

    return jsonify(result)


@web_bp.route("/admin/users/update", methods=["POST"])
def admin_users_update():
    """Form submit from the User Access tab — promote or demote by Discord ID."""
    if not is_admin(): return "Unauthorized", 403
    discord_id = request.form.get("discord_id", "").strip()
    status = request.form.get("status", "member")
    if not discord_id.isdigit():
        return redirect("/admin")
    if status == "admin":
        db_sync["config"].update_one(
            {"_id": "system_config"},
            {"$addToSet": {"admin_user_ids": discord_id}},
            upsert=True,
        )
    else:
        db_sync["config"].update_one(
            {"_id": "system_config"},
            {"$pull": {"admin_user_ids": discord_id}},
        )
    return redirect("/admin")


# ---------------------------------------------------------------------------
# 5b. ANALYTICS (data-driven member/clan dashboard)
# ---------------------------------------------------------------------------
@web_bp.route("/admin/api/analytics/overview")
def admin_analytics_overview():
    """Clan-wide headline numbers for the Analytics tab's stat row."""
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403

    clan_data = fetch_cr_api(f"clans/%23{CLAN_TAG}") or {}
    members = clan_data.get("memberList", [])
    member_count = len(members)

    total_trophies = sum(m.get("trophies", 0) for m in members)
    total_donations = sum(m.get("donations", 0) for m in members)
    inactive = sum(1 for m in members if m.get("donations", 0) == 0)

    latest_war = db_sync["war_tracking"].find_one({}, sort=[("harvest_time", -1)]) or {}
    participants = (latest_war.get("clan") or {}).get("participants", [])
    decks_used = sum(p.get("decksUsedToday", 0) for p in participants)
    max_decks = len(participants) * 4
    war_participation_pct = round((decks_used / max_decks) * 100, 1) if max_decks else 0

    battle_count = db_sync["battle_history"].estimated_document_count()

    total_w = db_sync["battle_history"].count_documents({"result": "win"})
    total_l = db_sync["battle_history"].count_documents({"result": "loss"})
    overall_win_rate = round((total_w / (total_w + total_l)) * 100, 1) if (total_w + total_l) else 0

    return jsonify({
        "member_count": member_count,
        "avg_trophies": round(total_trophies / member_count) if member_count else 0,
        "total_donations_live": total_donations,
        "inactive_members": inactive,
        "war_participation_pct": war_participation_pct,
        "battles_logged": battle_count,
        "overall_win_rate": overall_win_rate,
    })


@web_bp.route("/admin/api/analytics/leaderboards")
def admin_analytics_leaderboards():
    """Top-N leaderboards: donators, trophy climbers (7d delta), win rate, war fame, streaks."""
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403

    clan_data = fetch_cr_api(f"clans/%23{CLAN_TAG}") or {}
    members = clan_data.get("memberList", [])

    top_donators = sorted(members, key=lambda m: m.get("donations", 0), reverse=True)[:10]
    top_donators = [{"name": m.get("name"), "tag": m.get("tag", "").replace("#", ""), "value": m.get("donations", 0)} for m in top_donators]

    # Trophy climbers: compare current trophies against the earliest snapshot in the last 7 days
    import datetime as _dt
    week_ago = (_dt.datetime.now(timezone.utc) - _dt.timedelta(days=7)).strftime("%Y-%m-%d")
    # Ascending sort, but keep only the FIRST (oldest) snapshot seen per tag — the
    # baseline to climb from. A plain dict comprehension here would keep the last
    # value instead, comparing current trophies against yesterday's snapshot rather
    # than a week ago.
    old_snaps = {}
    for s in db_sync["player_snapshots"].find({"date": {"$gte": week_ago}}).sort("date", 1):
        old_snaps.setdefault(s["tag"], s.get("trophies", 0))
    climbers = []
    for m in members:
        tag = m.get("tag", "")
        old = old_snaps.get(tag)
        if old is not None:
            climbers.append({"name": m.get("name"), "tag": tag.replace("#", ""), "value": m.get("trophies", 0) - old})
    climbers.sort(key=lambda x: x["value"], reverse=True)
    top_climbers = climbers[:10]

    # Win rate from logged battles (min 10 battles to qualify, avoids noisy small samples)
    win_pipeline = [
        {"$group": {
            "_id": "$player_tag",
            "wins": {"$sum": {"$cond": [{"$eq": ["$result", "win"]}, 1, 0]}},
            "total": {"$sum": 1},
        }},
        {"$match": {"total": {"$gte": 10}}},
    ]
    win_rows = list(db_sync["battle_history"].aggregate(win_pipeline))
    name_by_tag = {m.get("tag", "").replace("#", ""): m.get("name") for m in members}
    win_rates = [
        {
            "name": name_by_tag.get(r["_id"], r["_id"]),
            "tag": r["_id"],
            "value": round((r["wins"] / r["total"]) * 100, 1),
            "battles": r["total"],
        }
        for r in win_rows
    ]
    win_rates.sort(key=lambda x: x["value"], reverse=True)
    top_win_rate = win_rates[:10]

    latest_war = db_sync["war_tracking"].find_one({}, sort=[("harvest_time", -1)]) or {}
    war_participants = (latest_war.get("clan") or {}).get("participants", [])
    top_fame = sorted(war_participants, key=lambda p: p.get("fame", 0), reverse=True)[:10]
    top_fame = [{"name": p.get("name"), "tag": p.get("tag", "").replace("#", ""), "value": p.get("fame", 0)} for p in top_fame]

    return jsonify({
        "top_donators": top_donators,
        "top_climbers": top_climbers,
        "top_win_rate": top_win_rate,
        "top_war_fame": top_fame,
    })


@web_bp.route("/admin/api/analytics/clan-trend")
def admin_analytics_clan_trend():
    """Clan-wide trophy/member-count time series for the trend chart (from clan_snapshots)."""
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    days = min(int(request.args.get("days", 30)), 180)
    import datetime as _dt
    cutoff = _dt.datetime.now(timezone.utc) - _dt.timedelta(days=days)
    docs = list(
        db_sync["clan_snapshots"]
        .find({"timestamp": {"$gte": cutoff}}, {"_id": 0, "timestamp": 1, "clanScore": 1, "memberCount": 1})
        .sort("timestamp", 1)
    )
    for d in docs:
        d["timestamp"] = d["timestamp"].isoformat() if hasattr(d["timestamp"], "isoformat") else d["timestamp"]
    return jsonify(docs)


@web_bp.route("/admin/api/analytics/member/<tag>/trend")
def admin_analytics_member_trend(tag):
    """Per-member trophy/donation time series for the member drill-down chart."""
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    days = min(int(request.args.get("days", 30)), 180)
    import datetime as _dt
    cutoff_date = (_dt.datetime.now(timezone.utc) - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
    docs = list(
        db_sync["player_snapshots"]
        .find({"tag": f"#{clean_tag(tag)}", "date": {"$gte": cutoff_date}}, {"_id": 0})
        .sort("date", 1)
    )
    return jsonify(docs)


@web_bp.route("/admin/api/analytics/battles")
def admin_analytics_battles():
    """Clan-wide deck/card win-rate analysis from logged battles."""
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403

    recent = list(
        db_sync["battle_history"]
        .find({}, {"_id": 0, "team_cards": 1, "result": 1, "battle_type": 1})
        .sort("battle_time", -1)
        .limit(2000)
    )
    card_stats = {}
    type_counts = {}
    total_wins = total_games = 0
    for b in recent:
        result = b.get("result")
        if result in ("win", "loss"):
            total_games += 1
            if result == "win": total_wins += 1
        btype = b.get("battle_type", "unknown")
        type_counts[btype] = type_counts.get(btype, 0) + 1
        if result not in ("win", "loss"):
            continue
        for card in (b.get("team_cards") or []):
            if not card: continue
            entry = card_stats.setdefault(card, {"wins": 0, "games": 0})
            entry["games"] += 1
            if result == "win": entry["wins"] += 1

    card_leaderboard = [
        {"card": c, "games": v["games"], "win_rate": round((v["wins"] / v["games"]) * 100, 1)}
        for c, v in card_stats.items() if v["games"] >= 5
    ]
    card_leaderboard.sort(key=lambda x: x["win_rate"], reverse=True)

    return jsonify({
        "overall_win_rate": round((total_wins / total_games) * 100, 1) if total_games else 0,
        "sample_size": total_games,
        "battle_type_breakdown": type_counts,
        "top_cards": card_leaderboard[:15],
        "worst_cards": card_leaderboard[-15:][::-1] if len(card_leaderboard) > 15 else [],
    })


@web_bp.route("/admin/api/analytics/archetypes")
def admin_analytics_archetypes():
    """Deck archetype win-rate analysis — the 'battle algorithm' view. Card-level
    win rate (see admin_analytics_battles above) answers 'is this card good';
    this answers 'which whole decks are actually winning', by grouping battles
    on the sorted 8-card signature actually played rather than individual cards.
    No new Clash Royale API calls — reads battle_history the harvester already
    collected, same as the card win-rate endpoint above.
    """
    if not is_admin(): return jsonify({"error": "unauthorized"}), 403
    min_games = max(int(request.args.get("min_games", 3)), 1)

    recent = list(
        db_sync["battle_history"]
        .find({}, {"_id": 0, "team_cards": 1, "result": 1, "player_tag": 1, "battle_time": 1})
        .sort("battle_time", -1)
        .limit(3000)
    )

    archetypes = {}
    for b in recent:
        result = b.get("result")
        if result not in ("win", "loss"):
            continue
        cards = [c for c in (b.get("team_cards") or []) if c]
        if len(cards) < 8:
            continue  # incomplete deck record — skip rather than mis-group
        signature = tuple(sorted(cards[:8]))
        entry = archetypes.setdefault(signature, {"wins": 0, "games": 0, "players": set(), "last_seen": None})
        entry["games"] += 1
        if result == "win":
            entry["wins"] += 1
        if b.get("player_tag"):
            entry["players"].add(b["player_tag"])
        bt = b.get("battle_time")
        if bt and (entry["last_seen"] is None or bt > entry["last_seen"]):
            entry["last_seen"] = bt

    scored = []
    for sig, v in archetypes.items():
        if v["games"] < min_games:
            continue
        scored.append({
            "cards": list(sig),
            "games": v["games"],
            "wins": v["wins"],
            "win_rate": round((v["wins"] / v["games"]) * 100, 1),
            "unique_players": len(v["players"]),
            "last_seen": v["last_seen"],
        })

    return jsonify({
        "sample_size": len(recent),
        "distinct_archetypes": len(archetypes),
        "qualifying_archetypes": len(scored),
        "top_by_win_rate": sorted(scored, key=lambda x: (-x["win_rate"], -x["games"]))[:15],
        "top_by_usage": sorted(scored, key=lambda x: -x["games"])[:15],
    })


# ---------------------------------------------------------------------------
# 6. FUTURE STUBS
# ---------------------------------------------------------------------------
@web_bp.route("/api/lfg", methods=["POST"])
def look_for_group():
    if "discord_id" not in session: return jsonify({"error": "Unauthorized"}), 403
    db_sync["pending_actions"].insert_one({
        "kind": "lfg_ping",
        "user": session.get("discord_name", "A clan member"),
        "created_at": datetime.now(timezone.utc),
        "processed": False,
    })
    return jsonify({"message": "Ping queued."})

@web_bp.route("/api/predict/matchmaking/<tag>")
def predict_matchmaking(tag): return jsonify({"message": "Analysis pending."})