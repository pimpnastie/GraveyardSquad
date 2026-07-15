import os
import time
import logging
import threading
import requests
from datetime import datetime, timezone
from pymongo import MongoClient, UpdateOne
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("harvester")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [HARVESTER] %(message)s")

class DataHarvester:
    def __init__(self):
        self.mongo_client = MongoClient(os.getenv("MONGO_URL", "mongodb://localhost:27017"))
        self.db = self.mongo_client["graveyardbot"]
        
        self.col_battles = self.db["battle_history"]
        self.col_profiles = self.db["player_profiles"]
        self.col_war = self.db["war_tracking"]
        self.col_war_history = self.db["war_history"]
        self.col_snapshots = self.db["clan_snapshots"]
        self.col_config = self.db["config"]

        self.col_battles.create_index([("player_tag", 1), ("battle_time", -1)])
        self.col_battles.create_index("unique_battle_id", unique=True)

        self.cr_token = os.getenv("CR_TOKEN", "").strip()
        self.clan_tag = os.getenv("CLAN_TAG", "9LVY89UP").strip().upper().replace("#", "")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.cr_token}",
            "Accept": "application/json"
        })

        # Restoring your original metadata tracker logic
        self._harvest_meta = {
            "last_run": None,
            "snapshots_saved": 0,
            "profiles_saved": 0,
            "battles_saved": 0,
            "duration_s": None,
            "status": "never_run",
        }
        self._restore_harvest_meta()

    def _restore_harvest_meta(self):
        try:
            doc = self.col_config.find_one({"_id": "harvest_meta"})
            if doc:
                doc.pop("_id", None)
                self._harvest_meta.update(doc)
                log.info(f"✅ Restored harvest metadata. Last run: {self._harvest_meta['last_run']}")
        except Exception as e:
            log.warning(f"Could not restore harvest meta: {e}")

    def _save_harvest_meta(self):
        self.col_config.update_one(
            {"_id": "harvest_meta"},
            {"$set": self._harvest_meta},
            upsert=True
        )

    def fetch_api(self, endpoint: str, retries=4):
        url = f"https://proxy.royaleapi.dev/v1/{endpoint}"
        for attempt in range(retries):
            try:
                resp = self.session.get(url, timeout=10)
                if resp.status_code == 200: return resp.json()
                elif resp.status_code == 429:
                    time.sleep(2 ** attempt)
            except Exception as e:
                time.sleep(2 ** attempt)
        return None

    def backfill_missed_wars(self):
        """Checks the historical riverracelog to fill gaps if the bot missed a war end."""
        log.info("Checking for missed historical wars...")
        log_data = self.fetch_api(f"clans/%23{self.clan_tag}/riverracelog")
        if log_data and "items" in log_data:
            for race in log_data["items"]:
                season_id = race.get("seasonId")
                section_index = race.get("sectionIndex")
                unique_war_id = f"season_{season_id}_section_{section_index}"
                
                # Upsert into war history so we don't save duplicates
                self.col_war_history.update_one(
                    {"unique_war_id": unique_war_id},
                    {"$set": {"unique_war_id": unique_war_id, "data": race}},
                    upsert=True
                )

    def harvest_clan_and_profiles(self):
        clan_data = self.fetch_api(f"clans/%23{self.clan_tag}")
        if not clan_data: return []

        self.col_snapshots.insert_one({
            "timestamp": datetime.now(timezone.utc),
            "tag": clan_data.get("tag"),
            "name": clan_data.get("name"),
            "clanScore": clan_data.get("clanScore"),
            "memberCount": clan_data.get("members")
        })

        member_tags = [m["tag"].replace("#", "") for m in clan_data.get("memberList", [])]
        profile_operations = []
        for tag in member_tags:
            profile_data = self.fetch_api(f"players/%23{tag}")
            if profile_data:
                profile_operations.append(UpdateOne(
                    {"tag": profile_data["tag"]},
                    {"$set": {**profile_data, "last_updated": datetime.now(timezone.utc)}},
                    upsert=True
                ))
            time.sleep(0.05)

        if profile_operations:
            self.col_profiles.bulk_write(profile_operations)
            self._harvest_meta["profiles_saved"] += len(profile_operations)

        return member_tags

    def harvest_battles(self, member_tags):
        battle_operations = []
        for tag in member_tags:
            battles = self.fetch_api(f"players/%23{tag}/battlelog")
            if not battles: continue

            for b in battles:
                unique_id = f"{tag}_{b.get('battleTime', '')}"
                b["player_tag"] = tag
                b["unique_battle_id"] = unique_id
                
                battle_operations.append(UpdateOne(
                    {"unique_battle_id": unique_id},
                    {"$set": b},
                    upsert=True
                ))
            time.sleep(0.05)

        if battle_operations:
            result = self.col_battles.bulk_write(battle_operations)
            self._harvest_meta["battles_saved"] += result.upserted_count
            log.info(f"Battle Catch-Up: {result.upserted_count} new battles found.")

    def run_full_cycle(self, is_startup=False):
        start_time = time.time()
        log.info("Starting Harvester Cycle...")
        
        if is_startup:
            self.backfill_missed_wars()

        tags = self.harvest_clan_and_profiles()
        if tags:
            self.harvest_battles(tags)
            
            # Live war tracking
            war_data = self.fetch_api(f"clans/%23{self.clan_tag}/currentriverrace")
            if war_data:
                war_data["harvest_time"] = datetime.now(timezone.utc)
                self.col_war.insert_one(war_data)

        self._harvest_meta["last_run"] = datetime.now(timezone.utc).isoformat()
        self._harvest_meta["duration_s"] = round(time.time() - start_time, 2)
        self._save_harvest_meta()
        log.info(f"✅ Cycle Complete in {self._harvest_meta['duration_s']}s.")

# ---------------------------------------------------------------------------
# BACKGROUND WORKER LOOP (For mainbot.py integration)
# ---------------------------------------------------------------------------
def start_harvester_loop(interval_minutes=30):
    harvester = DataHarvester()
    
    # Run immediate catch-up on boot
    log.info("Executing initial boot Catch-Up cycle...")
    try:
        harvester.run_full_cycle(is_startup=True)
    except Exception as e:
        log.error(f"Error during boot catch-up: {e}")

    # Enter normal looping state
    log.info(f"Harvester entering standard {interval_minutes}-minute loop.")
    while True:
        time.sleep(interval_minutes * 60)
        try:
            harvester.run_full_cycle(is_startup=False)
        except Exception as e:
            log.error(f"Critical error in Harvester Loop: {e}")