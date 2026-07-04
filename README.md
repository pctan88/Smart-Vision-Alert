# 🎯 Smart Vision Alert

**AI-Powered Aerial Dance Studio CCTV Safety Monitoring System**

Monitors your studio's CCTV camera using Google Gemini AI to detect accidents and safety hazards in real-time. Sends instant alerts to your Telegram group when danger is detected.

---

## ✨ Features

- 📷 **Camera Integration** — Xiaomi Cloud camera support + local folder/URL modes
- 🤖 **AI Safety Analysis** — Gemini 1.5 Flash vision AI tuned for aerial dance hazards
- 📱 **Telegram Alerts** — Instant photo + description alerts to your group
- ⏰ **Cron Scheduled** — Runs every 5 min on A2Hosting shared server
- 🛡️ **Smart Cooldown** — Prevents alert spam with configurable cooldown
- 📊 **Structured Logging** — Rotating file-based logs for monitoring

## 🎪 What It Detects

| Risk Level | Examples |
|-----------|---------|
| 🚨 Critical | Person unconscious/hanging limp, fire, equipment failure |
| 🔴 High | Person fallen in unnatural position, uncontrolled descent |
| ⚠️ Medium | Person fallen but conscious, unsafe practice |
| 📝 Low | Floor hazard, mats not positioned |
| ✅ Safe | Normal practice, empty studio, warm-up |

> **Smart Context:** The AI understands that people being upside down or wrapped in aerial silks is **normal practice**, not an accident.

---

## 🚀 Quick Start

### 1. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp config/.env.example config/.env
# Edit config/.env with your API keys
```

You'll need:
- **Gemini API Key** → [Google AI Studio](https://aistudio.google.com/apikey)
- **Telegram Bot Token** → Create via [@BotFather](https://t.me/BotFather)
- **Telegram Chat ID** → Add [@getidsbot](https://t.me/getidsbot) to your group

### 3. Test

```bash
# Check system status
python main.py --status

# Test Telegram connection
python main.py --test

# Place a test image in captures/manual/ and analyze
python main.py --analyze
```

### 4. Run

```bash
# One-time check
python main.py

# Set up cron (every 5 min) — see cron_setup.md
```

---

## 🖥️ Run the Portal Locally

The web portal is served by the Flask app in `webhook.py`.

### Start the portal

```bash
source venv/bin/activate
WEBHOOK_URL= TELEGRAM_BOT_TOKEN= python webhook.py
```

The local portal runs at:

```text
http://127.0.0.1:5001/login
```

The empty `WEBHOOK_URL` and `TELEGRAM_BOT_TOKEN` values prevent local startup from trying to register a Telegram webhook.

### Default local login

```text
Username: admin
Password: admin123
```

These can be overridden with environment variables:

```bash
PORTAL_ADMIN_USERNAME=your_admin
PORTAL_ADMIN_PASSWORD=your_password
```

### Portal requirements

The portal reads and writes data from MySQL using the database settings in `config/.env`:

```text
DB_HOST
DB_PORT
DB_USER
DB_PASSWORD
DB_NAME
```

If login or dashboard data fails, first confirm MySQL is running and those values are correct.

---

## ☁️ Deploy Cloud Run

Cloud Run runs `cloud_run_main.py` and receives environment variables from the
Cloud Run service configuration, not from local `config/.env`.

### Production target

```text
Project: itc-monitoring-495910
Region: asia-southeast1
Service: smart-vision-alert
Artifact image: asia-southeast1-docker.pkg.dev/itc-monitoring-495910/smart-vision-alert/monitor
```

### Push code

```bash
git status --short
git push origin main
```

Only committed tracked files are deployed. Local untracked files such as cookies
or agent scratch folders should not be committed.

### Manual build and deploy

`cloudbuild.yaml` uses `$COMMIT_SHA` as the Docker image tag. GitHub/Cloud Build
triggers provide this automatically, but a manual `gcloud builds submit` does
not. For manual deploys, pass the current commit SHA explicitly:

```bash
COMMIT_SHA=$(git rev-parse HEAD)

gcloud builds submit \
  --config cloudbuild.yaml \
  --substitutions=COMMIT_SHA=${COMMIT_SHA} \
  --project itc-monitoring-495910 \
  .
```

If `COMMIT_SHA` is omitted during manual deploy, Docker fails with an empty image
tag like:

```text
invalid argument ".../monitor:" for "-t, --tag" flag
```

### Update Cloud Run environment variables

Cloud Run env vars are updated separately from local `.env`. For example, to
send Telegram alerts only for `high` and `critical` risk:

```bash
gcloud run services update smart-vision-alert \
  --region=asia-southeast1 \
  --project=itc-monitoring-495910 \
  --update-env-vars=ALERT_THRESHOLD=high
```

Useful env updates follow the same pattern:

```bash
gcloud run services update smart-vision-alert \
  --region=asia-southeast1 \
  --project=itc-monitoring-495910 \
  --update-env-vars=KEY=value,OTHER_KEY=other_value
```

Do not use `--set-env-vars` for a small update unless you intend to replace the
whole env var set. Prefer `--update-env-vars`.

### Verify deployment

Check the active revision, traffic, and image:

```bash
gcloud run services describe smart-vision-alert \
  --region=asia-southeast1 \
  --project=itc-monitoring-495910 \
  --format='value(status.latestReadyRevisionName,status.traffic[0].revisionName,status.traffic[0].percent,spec.template.spec.containers[0].image)'
```

Verify one env var without printing secrets:

```bash
gcloud run services describe smart-vision-alert \
  --region=asia-southeast1 \
  --project=itc-monitoring-495910 \
  --format='json(spec.template.spec.containers[0].env)' \
  | venv/bin/python -c 'import json,sys; data=json.load(sys.stdin); env=data["spec"]["template"]["spec"]["containers"][0]["env"]; print(next((e.get("value") for e in env if e.get("name")=="ALERT_THRESHOLD"), "MISSING"))'
```

Expected Telegram alert threshold:

```text
ALERT_THRESHOLD=high
```

---

## 🔎 Debug Cloud Run + A2

Use these checks when the portal appears stale, Telegram is quiet, or cron seems
not to process new events.

### 1. Check A2 code and cron log

On A2 Hosting:

```bash
cd /home/muarholi/itc/Smart-Vision-Alert
git status --short
git log -1 --oneline
tail -120 /home/muarholi/itc/logs/cron.log
```

Healthy cron output looks like:

```text
Triggering Cloud Run (manual_check=False)...
Cloud Run response: {"result": {"cameras": [], "total_new": 0, "total_skip": 0}, "status": "ok"}
```

`total_new=0` and `total_skip=0` means Cloud Run ran successfully but did not
find new Xiaomi events in its lookback window. It does not necessarily mean the
system is broken.

### 2. Check Cloud Run revision and image

From a machine with `gcloud` access:

```bash
gcloud run services describe smart-vision-alert \
  --region=asia-southeast1 \
  --project=itc-monitoring-495910 \
  --format='value(status.latestReadyRevisionName,status.url,spec.template.spec.containers[0].image)'
```

### 3. Check Cloud Run request logs

This confirms whether A2 cron is reaching Cloud Run and what HTTP status Cloud
Run returned:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="smart-vision-alert"' \
  --project=itc-monitoring-495910 \
  --limit=80 \
  --format='value(timestamp,severity,httpRequest.status,httpRequest.latency,httpRequest.requestUrl)' \
  --freshness=12h
```

Healthy cron requests normally show:

```text
POST /run
status 200
userAgent python-requests
```

Very short latencies, for example under 1 second, usually mean Cloud Run did not
find any events to analyze. Longer requests usually mean it captured frames and
called Gemini.

### 4. Check one Cloud Run env var safely

Avoid printing all environment variables because some are secrets. To check only
`ALERT_THRESHOLD`:

```bash
gcloud run services describe smart-vision-alert \
  --region=asia-southeast1 \
  --project=itc-monitoring-495910 \
  --format='json(spec.template.spec.containers[0].env)' \
  | venv/bin/python -c 'import json,sys; data=json.load(sys.stdin); env=data["spec"]["template"]["spec"]["containers"][0]["env"]; print(next((e.get("value") for e in env if e.get("name")=="ALERT_THRESHOLD"), "MISSING"))'
```

### 5. Check database recency

From a local checkout with DB access configured:

```bash
venv/bin/python - <<'PY'
from config.settings import settings
from core.database import EventDB

db = EventDB(settings)
conn = db._get_conn()
try:
    with conn.cursor() as cur:
        for table, time_col in [
            ("processed_events", "processed_at"),
            ("analysis_results", "analyzed_at"),
            ("alert_history", "alerted_at"),
        ]:
            cur.execute(f"SELECT COUNT(*) AS total, MAX({time_col}) AS latest FROM {table}")
            print(table, cur.fetchone())

        cur.execute("""
            SELECT file_id, camera_name, event_type, event_time, processed_at
            FROM processed_events
            ORDER BY processed_at DESC
            LIMIT 8
        """)
        print("recent_processed")
        for row in cur.fetchall():
            print(row)
finally:
    db.close()
PY
```

To summarize inserts by hour:

```bash
venv/bin/python - <<'PY'
from config.settings import settings
from core.database import EventDB

db = EventDB(settings)
conn = db._get_conn()
try:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DATE(processed_at) AS day, HOUR(processed_at) AS hour,
                   COUNT(*) AS events,
                   MIN(event_time) AS first_event,
                   MAX(event_time) AS last_event,
                   MIN(processed_at) AS first_processed,
                   MAX(processed_at) AS last_processed
            FROM processed_events
            WHERE processed_at >= CURDATE()
            GROUP BY DATE(processed_at), HOUR(processed_at)
            ORDER BY day DESC, hour DESC
        """)
        for row in cur.fetchall():
            print(row)
finally:
    db.close()
PY
```

### 6. Interpret common results

```text
A2 cron log has regular status=ok responses
Cloud Run request logs show HTTP 200
DB has no newer rows
Xiaomi app has no newer events
```

This is normal. There are no events to process.

```text
A2 cron log has regular status=ok responses
Cloud Run request logs show HTTP 200
Xiaomi app has newer events
DB has no newer rows
```

Likely causes:

```text
EVENT_LOOKBACK is too short
Xiaomi event feed is delayed
Cloud Run is failing per camera but app logs are not visible
```

Try increasing the Cloud Run lookback window:

```bash
gcloud run services update smart-vision-alert \
  --region=asia-southeast1 \
  --project=itc-monitoring-495910 \
  --update-env-vars=EVENT_LOOKBACK=3600
```

Use `--update-env-vars`, not `--set-env-vars`, for single-setting changes.

---

## 📁 Project Structure

```
Smart-Vision-Alert/
├── config/
│   ├── .env.example      # Config template
│   └── settings.py       # Config loader
├── core/
│   ├── camera.py         # Image capture (Xiaomi Cloud / local / URL)
│   ├── analyzer.py       # Gemini AI safety analysis
│   ├── notifier.py       # Telegram alert sender
│   └── models.py         # Data models
├── utils/
│   ├── logger.py         # Rotating file logger
│   └── image_utils.py    # Image helpers
├── captures/             # Captured images (auto-cleaned)
│   └── manual/           # Drop test images here
├── logs/                 # Application logs
├── main.py               # 🚀 Main entry point
├── setup_token.py        # Xiaomi token extraction (one-time)
├── requirements.txt      # Python dependencies
└── cron_setup.md         # A2Hosting deployment guide
```

---

## 📸 Camera Source Modes

| Mode | Config Value | Description |
|------|-------------|-------------|
| Local Folder | `local_folder` | Reads latest image from `captures/manual/` — great for testing |
| Direct URL | `url` | Downloads from a snapshot URL |
| Xiaomi Cloud | `xiaomi_cloud` | Pulls from Xiaomi Cloud API (run `setup_token.py` first) |

---

## 🛠️ CLI Commands

```bash
python main.py              # Run full safety check pipeline
python main.py --test       # Send test Telegram message
python main.py --analyze    # Analyze image only (no alert)
python main.py --status     # Show system config status
```

---

## 📄 License

MIT — Use for your studio's safety. Stay safe! 🎪
