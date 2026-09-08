"""
Strava Activity Merger — Webhook Server
When a Garmin strength activity lands on Strava, fetches the matching workout
from Hevy's API and applies its title + exercise description.
"""

import os
import time
import asyncio
import logging
import httpx
from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import json

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("strava-merger")

# ── Config ──────────────────────────────────────────────────────────────────
STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
STRAVA_VERIFY_TOKEN = os.getenv("STRAVA_VERIFY_TOKEN", "merger-verify-token")
HEVY_API_KEY = os.getenv("HEVY_API_KEY")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
TOKEN_FILE = os.getenv("TOKEN_FILE", "tokens.json")
# Shared secret for /merge and /debug. Unset = open (fine for localhost only).
MERGE_TOKEN = os.getenv("MERGE_TOKEN")

OVERLAP_WINDOW = 60 * 90  # 90 minutes (Hevy timestamps can be offset)

STRAVA_API = "https://www.strava.com/api/v3"
HEVY_API = "https://api.hevyapp.com/v1"

# ── Token Management ───────────────────────────────────────────────────────
# Serverless hosts (Vercel) have a read-only, ephemeral filesystem, so when
# DATABASE_URL is set the OAuth tokens live in Postgres instead of a JSON file.
# Without it, behaviour is unchanged: a local tokens.json.

DATABASE_URL = os.getenv("DATABASE_URL")


def _db_conn():
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def _ensure_token_table():
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS strava_tokens (
                    id INTEGER PRIMARY KEY,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    expires_at BIGINT NOT NULL
                )
                """
            )
        conn.commit()


def load_tokens() -> dict:
    if DATABASE_URL:
        _ensure_token_table()
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT access_token, refresh_token, expires_at "
                    "FROM strava_tokens WHERE id = 1"
                )
                row = cur.fetchone()
        if not row:
            return {}
        return {"access_token": row[0], "refresh_token": row[1], "expires_at": row[2]}

    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return json.load(f)
    return {}


def save_tokens(tokens: dict):
    if DATABASE_URL:
        _ensure_token_table()
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO strava_tokens (id, access_token, refresh_token, expires_at)
                    VALUES (1, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        access_token = EXCLUDED.access_token,
                        refresh_token = EXCLUDED.refresh_token,
                        expires_at = EXCLUDED.expires_at
                    """,
                    (tokens["access_token"], tokens["refresh_token"], int(tokens["expires_at"])),
                )
            conn.commit()
        return

    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f, indent=2)

def get_access_token() -> str:
    tokens = load_tokens()
    if not tokens:
        raise RuntimeError("No tokens found. Run /auth/start first.")

    if tokens.get("expires_at", 0) < time.time() + 60:
        log.info("Refreshing expired access token…")
        resp = httpx.post("https://www.strava.com/oauth/token", data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        })
        resp.raise_for_status()
        new = resp.json()
        tokens.update({
            "access_token": new["access_token"],
            "refresh_token": new["refresh_token"],
            "expires_at": new["expires_at"],
        })
        save_tokens(tokens)

    return tokens["access_token"]

def strava_headers() -> dict:
    return {"Authorization": f"Bearer {get_access_token()}"}

# ── Strava API Helpers ─────────────────────────────────────────────────────

def fetch_recent_activities(per_page: int = 10) -> list[dict]:
    resp = httpx.get(f"{STRAVA_API}/activities", headers=strava_headers(),
                     params={"per_page": per_page})
    resp.raise_for_status()
    return resp.json()

def get_activity(activity_id: int) -> dict:
    resp = httpx.get(f"{STRAVA_API}/activities/{activity_id}", headers=strava_headers())
    resp.raise_for_status()
    return resp.json()

def update_activity(activity_id: int, **fields) -> dict:
    resp = httpx.put(f"{STRAVA_API}/activities/{activity_id}",
                     headers=strava_headers(), json=fields)
    resp.raise_for_status()
    return resp.json()

# ── Hevy API Helpers ───────────────────────────────────────────────────────

def hevy_headers() -> dict:
    return {"api-key": HEVY_API_KEY}

def fetch_hevy_workouts(page: int = 1, page_size: int = 5) -> list[dict]:
    resp = httpx.get(f"{HEVY_API}/workouts",
                     headers=hevy_headers(),
                     params={"page": page, "pageSize": page_size})
    resp.raise_for_status()
    return resp.json().get("workouts", [])

def format_hevy_description(workout: dict) -> str:
    lines = []
    for exercise in workout.get("exercises", []):
        title = exercise.get("title", "Unknown")
        sets = exercise.get("sets", [])
        set_lines = []
        for s in sets:
            weight = s.get("weight_kg")
            reps = s.get("reps")
            set_type = s.get("type", "normal")
            prefix = ""
            if set_type == "warmup":
                prefix = "(W) "
            elif set_type == "dropset":
                prefix = "(D) "
            elif set_type == "failure":
                prefix = "(F) "

            if weight is not None and reps is not None:
                set_lines.append(f"{prefix}{weight}kg × {reps}")
            elif reps is not None:
                set_lines.append(f"{prefix}{reps} reps")
            elif weight is not None:
                set_lines.append(f"{prefix}{weight}kg")

        lines.append(f"{title}")
        for sl in set_lines:
            lines.append(f"  {sl}")
        lines.append("")

    return "\n".join(lines).strip()

# ── Merge Logic ────────────────────────────────────────────────────────────

def is_garmin_strength(activity: dict) -> bool:
    strength_types = {"WeightTraining", "Workout", "Crossfit"}
    if activity.get("type") not in strength_types and activity.get("sport_type") not in strength_types:
        return False
    ext_id = activity.get("external_id", "") or ""
    device = activity.get("device_name", "") or ""
    return "garmin" in ext_id.lower() or "garmin" in device.lower()

def find_and_merge():
    """Scan recent Garmin strength activities and apply matching Hevy workout data."""
    activities = fetch_recent_activities(per_page=10)
    garmin_strength = [a for a in activities if is_garmin_strength(a)]

    if not garmin_strength:
        log.info("No Garmin strength activities found.")
        return {"merged": 0}

    hevy_workouts = fetch_hevy_workouts(page=1, page_size=10)
    if not hevy_workouts:
        log.info("No Hevy workouts found.")
        return {"merged": 0}

    from datetime import datetime

    merged_count = 0
    used_workout_ids = set()
    for garmin in garmin_strength:
        garmin_start = datetime.fromisoformat(garmin["start_date"].replace("Z", "+00:00"))

        # Pick the CLOSEST Hevy workout inside the window, not the first one found.
        best, best_diff = None, None
        for workout in hevy_workouts:
            if workout.get("id") in used_workout_ids:
                continue

            hevy_start = datetime.fromisoformat(workout["start_time"].replace("Z", "+00:00"))
            diff = abs((garmin_start - hevy_start).total_seconds())

            if diff > OVERLAP_WINDOW:
                continue

            if best_diff is None or diff < best_diff:
                best, best_diff = workout, diff

        if best is None:
            continue

        used_workout_ids.add(best.get("id"))

        # Check if already merged (title already matches Hevy's)
        if garmin.get("name") == best.get("title"):
            continue

        hevy_title = best.get("title", "")
        hevy_desc = format_hevy_description(best)

        log.info(f"Merging: Garmin #{garmin['id']} ← Hevy workout '{hevy_title}' ({int(best_diff // 60)} min apart)")
        update_activity(garmin["id"], name=hevy_title, description=hevy_desc)
        merged_count += 1
        log.info(f"✓ Applied Hevy title + description to Garmin #{garmin['id']}")

    if merged_count == 0:
        log.info("No matching Garmin+Hevy pairs found.")

    return {"merged": merged_count}

# ── Single-Activity Merge (webhook path) ───────────────────────────────

def merge_one(activity_id: int) -> dict:
    """Merge one Strava activity by id. Used by the webhook, which knows
    exactly which activity just landed, so there is no need to scan."""
    from datetime import datetime

    activity = get_activity(activity_id)

    if not is_garmin_strength(activity):
        log.info(f"#{activity_id} is not a Garmin strength activity; skipping.")
        return {"merged": 0, "reason": "not a garmin strength activity"}

    hevy_workouts = fetch_hevy_workouts(page=1, page_size=10)
    if not hevy_workouts:
        log.info("No Hevy workouts found.")
        return {"merged": 0, "reason": "no hevy workouts"}

    start = datetime.fromisoformat(activity["start_date"].replace("Z", "+00:00"))

    best, best_diff = None, None
    for workout in hevy_workouts:
        hevy_start = datetime.fromisoformat(workout["start_time"].replace("Z", "+00:00"))
        diff = abs((start - hevy_start).total_seconds())

        if diff > OVERLAP_WINDOW:
            continue

        if best_diff is None or diff < best_diff:
            best, best_diff = workout, diff

    if best is None:
        log.info(f"No Hevy workout within the window for #{activity_id}.")
        return {"merged": 0, "reason": "no hevy workout in window"}

    if activity.get("name") == best.get("title"):
        log.info(f"#{activity_id} already merged.")
        return {"merged": 0, "reason": "already merged"}

    hevy_title = best.get("title", "")
    log.info(f"Merging: Garmin #{activity_id} ← Hevy workout '{hevy_title}' ({int(best_diff // 60)} min apart)")
    update_activity(activity_id, name=hevy_title, description=format_hevy_description(best))
    log.info(f"✓ Applied Hevy title + description to Garmin #{activity_id}")
    return {"merged": 1, "title": hevy_title}


# ── FastAPI App ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Strava Merger started.")
    yield

app = FastAPI(title="Strava Activity Merger", lifespan=lifespan)

# ── OAuth Flow ─────────────────────────────────────────────────────────────

@app.get("/auth/start")
def auth_start():
    url = (
        f"https://www.strava.com/oauth/authorize"
        f"?client_id={STRAVA_CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={BASE_URL}/auth/callback"
        f"&scope=activity:read_all,activity:write"
        f"&approval_prompt=auto"
    )
    return {"message": "Open this URL in your browser", "url": url}

@app.get("/auth/callback")
def auth_callback(code: str = Query(...)):
    resp = httpx.post("https://www.strava.com/oauth/token", data={
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
    })
    resp.raise_for_status()
    data = resp.json()
    save_tokens({
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": data["expires_at"],
    })
    return {"message": "Authenticated! Tokens saved. You can close this window."}

# ── Strava Webhook ────────────────────────────────────────────────────────

@app.get("/webhook")
def webhook_verify(
    mode: str = Query(None, alias="hub.mode"),
    token: str = Query(None, alias="hub.verify_token"),
    challenge: str = Query(None, alias="hub.challenge"),
):
    if mode == "subscribe" and token == STRAVA_VERIFY_TOKEN:
        log.info("Webhook subscription verified.")
        return JSONResponse({"hub.challenge": challenge})
    return JSONResponse({"error": "Forbidden"}, status_code=403)

@app.post("/webhook")
async def webhook_event(request: Request):
    body = await request.json()
    log.info(f"Webhook event received: {body}")

    obj_type = body.get("object_type")
    aspect_type = body.get("aspect_type")
    object_id = body.get("object_id")

    if obj_type == "activity" and aspect_type == "create" and object_id:
        # On serverless nothing survives the response, so the merge runs inline.
        # It touches one activity, so it is quick; if Strava times out and
        # retries, merging again is a no-op.
        try:
            result = await asyncio.to_thread(merge_one, int(object_id))
            log.info(f"Webhook merge result: {result}")
        except Exception as e:
            log.error(f"Merge failed for #{object_id}: {e}")

    # Always 200 — Strava disables subscriptions that keep erroring.
    return JSONResponse({"status": "ok"})

# ── Manual Trigger ─────────────────────────────────────────────────────────

@app.get("/merge")
def manual_merge(key: str = Query(None)):
    if MERGE_TOKEN and key != MERGE_TOKEN:
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    try:
        result = find_and_merge()
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ── Debug ─────────────────────────────────────────────────────────────────

@app.get("/debug")
def debug_activities(key: str = Query(None)):
    if MERGE_TOKEN and key != MERGE_TOKEN:
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    activities = fetch_recent_activities(per_page=10)
    hevy_workouts = fetch_hevy_workouts(page=1, page_size=5)
    return {
        "strava_activities": [
            {
                "id": a["id"],
                "name": a.get("name"),
                "type": a.get("type"),
                "start_date": a.get("start_date"),
                "device_name": a.get("device_name"),
                "is_garmin_strength": is_garmin_strength(a),
            }
            for a in activities
        ],
        "hevy_workouts": [
            {
                "id": w.get("id"),
                "title": w.get("title"),
                "start_time": w.get("start_time"),
                "exercises": len(w.get("exercises", [])),
            }
            for w in hevy_workouts
        ],
    }

# ── Health ─────────────────────────────────────────────────────────────────

@app.get("/")
def health():
    return {"status": "running", "service": "strava-merger"}
