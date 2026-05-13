# Smart Vision Alert Frontend Dashboard Notes

## Project Structure And Features

This project is a compact Python application for AI-powered CCTV safety monitoring in an aerial dance studio.

Main files:

- `main.py`: Local CLI safety check runner.
- `cloud_run_main.py`: Cloud Run pipeline. Fetches Xiaomi events, captures frames, runs Gemini, sends Telegram, posts result back.
- `webhook.py`: A2/shared-host Flask app. Handles Telegram `/check` and internal API callbacks.
- `core/database.py`: MySQL/MariaDB database layer.
- `core/analyzer.py`: Gemini vision analysis.
- `core/notifier.py`: Telegram alert sender.
- `config/settings.py`: `.env` config loader.

Current project has no frontend app yet.

## Runtime Flow

The live production flow is:

```text
Telegram /check or cron
    -> webhook.py / monitor_studio.py
    -> Cloud Run /run
    -> Xiaomi camera event fetch
    -> frame extraction from camera event video
    -> Gemini multi-frame safety analysis
    -> Telegram alert if needed
    -> POST result back to A2
    -> webhook.py saves MySQL records + image files
```

## Database Connection

Configured database target:

```text
DB_HOST: localhost
DB_PORT: 33069
DB_NAME: muarholi_itc
DB_USER: configured
DB_PASSWORD: configured
Server: MariaDB 10.5.25
```

Secrets were not printed.

## Database Tables

There are 4 main tables:

```text
processed_events
- Stores one row per Xiaomi camera event.
- Important fields: file_id, camera_did, camera_name, event_type, event_time,
  duration_sec, frames_saved, capture_dir, processed_at.

analysis_results
- Stores Gemini result for each event.
- Important fields: file_id, camera_did, is_safe, risk_level, description,
  hazards, confidence, motion_detected, stillness_warn, model_used, analyzed_at.

alert_history
- Stores Telegram alert attempts.
- Important fields: file_id, camera_did, risk_level, description,
  telegram_ok, alerted_at.

manual_triggers
- Stores Telegram /check command usage.
- Important fields: username, first_name, command, triggered_at.
```

The key relationship for a dashboard is:

```sql
processed_events.file_id = analysis_results.file_id
processed_events.file_id = alert_history.file_id
```

## Current Data

Live counts:

```text
processed_events: 154
analysis_results: 150
alert_history: 15
manual_triggers: 16
```

Camera summary:

```text
C300-1 / 1066815174: 85 events, avg duration 98.2s, last event 2026-05-12 20:50:47
C300-2 / 1066840805: 69 events, avg duration 139.3s, last event 2026-05-12 20:51:14
```

Risk distribution:

```text
critical: 1
high: 1
medium: 4
safe: 144
```

Events by day:

```text
2026-05-12: 68
2026-05-11: 66
2026-05-10: 20
```

Important dashboard warning: several latest rows have `description = "Could not parse AI response"` and `confidence = 0.0`, while still being stored as `safe`. The dashboard should show this as an AI health issue, not just a normal safe event.

## Frontend Dashboard Recommendation

Do not connect the frontend directly to MySQL. Add backend JSON APIs in Flask, then build the portal against those APIs.

Useful dashboard views:

```text
1. Overview cards
- Events today
- Alerts today
- Unsafe events today
- AI parse failures
- Telegram success rate

2. Camera status
- Camera name
- Last event time
- Event count
- Average event duration
- Last risk level

3. Event feed
- Time
- Camera
- Risk level
- Confidence
- Description
- Thumbnail/image link
- Alert sent status

4. Risk analytics
- Risk count chart
- Events by day
- Alerts by day
- AI failures by day

5. Event detail page
- Captured frames
- Gemini description
- Hazards JSON
- Telegram result
```

Example backend query for event feed:

```sql
SELECT
  pe.file_id,
  pe.camera_name,
  pe.camera_did,
  pe.event_time,
  pe.duration_sec,
  pe.frames_saved,
  pe.capture_dir,
  ar.risk_level,
  ar.is_safe,
  ar.confidence,
  ar.description,
  ar.hazards,
  ar.motion_detected,
  ar.stillness_warn,
  ar.model_used,
  ar.analyzed_at
FROM processed_events pe
LEFT JOIN analysis_results ar ON ar.file_id = pe.file_id
ORDER BY pe.event_time DESC
LIMIT 50;
```

Example API response shape for frontend:

```json
{
  "summary": {
    "events_today": 68,
    "alerts_today": 4,
    "unsafe_total": 6,
    "ai_parse_failures": 8
  },
  "cameras": [
    {
      "camera_did": "1066815174",
      "camera_name": "C300-1",
      "events": 85,
      "last_event_time": "2026-05-12 20:50:47"
    }
  ],
  "recent_events": [
    {
      "file_id": "562090990982923264",
      "camera_name": "C300-2",
      "risk_level": "safe",
      "confidence": 0,
      "description": "Could not parse AI response",
      "capture_dir": "captures/studio/1066840805/562090990982923264"
    }
  ]
}
```

Best next implementation step: add `/api/dashboard/summary`, `/api/events`, `/api/events/<file_id>`, and `/api/cameras` to `webhook.py`, then create a frontend portal that reads those endpoints.

## Can This Python Project Serve A Frontend?

Yes, this Python project can serve a frontend. You do not need another repo unless you want a larger separate frontend app.

Recommended options:

```text
1. Simple dashboard inside this repo
- Use Flask in webhook.py.
- Add HTML templates in templates/.
- Add CSS/JS in static/.
- Add API routes like /api/dashboard and /api/events.
- Good for the current project.

2. Modern frontend inside this repo
- Add a React/Vite app in a folder like frontend/.
- Python Flask serves API.
- React calls the Flask API.
- Good if you want charts, filters, and a nicer UI.

3. Separate frontend repo
- Only needed for independent deployment, more complex users/auth, or a dedicated frontend team.
- More setup and deployment work.
```

For this project, the recommended path is same repo, Flask plus templates first.

Example structure:

```text
Smart-Vision-Alert/
├── webhook.py
├── core/
├── config/
├── templates/
│   ├── dashboard.html
│   └── event_detail.html
├── static/
│   ├── dashboard.css
│   └── dashboard.js
```

Then `webhook.py` can have:

```python
@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")
```

And API routes:

```python
@app.route("/api/dashboard")
def api_dashboard():
    # query MySQL
    return jsonify({
        "events_today": 68,
        "alerts_today": 4,
        "risk_counts": {
            "safe": 144,
            "medium": 4,
            "high": 1,
            "critical": 1
        }
    })
```

Conclusion: the existing Python code can host the frontend dashboard directly. Keep it in this repo and build the portal on top of the existing Flask app.
