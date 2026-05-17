# Strava Activity Merger

Automatically merges overlapping Garmin + Hevy strength activities on Strava via webhooks.

**What it does:** When you log a strength workout on both Garmin and Hevy, both sync to Strava as separate activities. This server detects the overlap, keeps the Garmin activity (with HR/calories/duration), applies Hevy's title and exercise description to it, then deletes the Hevy duplicate.

## Setup

### 1. Create a Strava API App

1. Go to [strava.com/settings/api](https://www.strava.com/settings/api)
2. Create an application (name it anything, e.g. "Activity Merger")
3. Set the **Authorization Callback Domain** to your server's domain (or `localhost` for testing)
4. Note your **Client ID** and **Client Secret**

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env with your Client ID and Secret
```

### 3. Install & Run

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

### 4. Authenticate with Strava

1. Open `http://your-server:8000/auth/start`
2. Click the URL it returns → authorize on Strava
3. Tokens are saved automatically

### 5. Register the Webhook

Once your server is publicly accessible, register the webhook with Strava:

```bash
curl -X POST https://www.strava.com/api/v3/push_subscriptions \
  -d client_id=YOUR_CLIENT_ID \
  -d client_secret=YOUR_CLIENT_SECRET \
  -d callback_url=https://your-server.com/webhook \
  -d verify_token=merger-verify-token
```

## Deployment Options

The server needs to be publicly reachable for Strava webhooks. Some options:

- **Railway** (railway.app) — easiest, free tier available
- **Fly.io** — generous free tier
- **VPS** (Hetzner, DigitalOcean) — cheapest long-term
- **Home server + Cloudflare Tunnel** — free, runs on your own machine

### Railway (quickest)

```bash
# Install Railway CLI, then:
railway login
railway init
railway up
# Set env vars in Railway dashboard
```

## Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Health check |
| `/auth/start` | GET | Begin Strava OAuth flow |
| `/auth/callback` | GET | OAuth callback (automatic) |
| `/webhook` | GET | Strava webhook verification |
| `/webhook` | POST | Incoming webhook events |
| `/merge` | GET | Manually trigger a merge scan |

## How Matching Works

1. Fetches 15 most recent activities
2. Filters to strength/weight-training types
3. Identifies source (Garmin vs Hevy) via `external_id`, `device_name`, and description content
4. Pairs activities that start within 30 minutes of each other
5. Applies Hevy's title + description → Garmin activity, deletes Hevy's

## Notes

- **Photos:** Strava's API doesn't support photo transfer, so the Hevy photo is lost on merge. This is a Strava API limitation.
- **Overlap window:** Default 30 minutes. Adjust `OVERLAP_WINDOW` in `app.py` if needed.
- **Safety:** The `/merge` endpoint lets you trigger manually if a webhook is missed.
