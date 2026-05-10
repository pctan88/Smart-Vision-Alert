import os
import sys
import subprocess
import requests
from pathlib import Path
from flask import Flask, request, jsonify

sys.path.insert(0, os.path.dirname(__file__))

from config.settings import settings
from core.notifier import TelegramNotifier
from core.database import EventDB
from core.models import AnalysisResult

app = Flask(__name__)
notifier = TelegramNotifier(settings)

PROJECT_ROOT = Path(__file__).resolve().parent


def register_webhook():
    """Register the Telegram webhook on startup."""
    if not settings.WEBHOOK_URL or not settings.TELEGRAM_BOT_TOKEN:
        print("WEBHOOK_URL or TELEGRAM_BOT_TOKEN not set. Webhook registration skipped.")
        return

    url     = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/setWebhook"
    payload = {
        "url":                  settings.WEBHOOK_URL,
        "secret_token":         settings.WEBHOOK_SECRET,
        "drop_pending_updates": True,
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200 and response.json().get("ok"):
            print(f"Webhook registered: {settings.WEBHOOK_URL}")
        else:
            print(f"Webhook registration failed: {response.text}")
    except Exception as e:
        print(f"Error registering webhook: {e}")


register_webhook()


# ── Telegram webhook ───────────────────────────────────────────────────────────

@app.route('/webhook', methods=['POST'])
@app.route('/', methods=['POST'])
def telegram_webhook():
    """Handle incoming Telegram commands."""
    secret_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret_token != settings.WEBHOOK_SECRET:
        return jsonify({"status": "unauthorized"}), 401

    try:
        update = request.get_json()
        if update and "message" in update and "text" in update["message"]:
            text      = update["message"]["text"]
            from_user = update["message"].get("from", {})
            username   = from_user.get("username", "")
            first_name = from_user.get("first_name", "User")

            if text.startswith("/check"):
                print(f"Manual check triggered by {first_name} (@{username})")

                try:
                    db = EventDB(settings)
                    db.log_manual_trigger(username, first_name, text)
                    db.close()
                except Exception as db_err:
                    print(f"Failed to log trigger to DB: {db_err}")

                display_name = f"@{username}" if username else first_name
                notifier.send_text(
                    f"🔍 Manual check triggered by {display_name}\\. "
                    f"Fetching latest CCTV status\\.\\.\\."
                )

                # Delegate to monitor_studio.py which triggers Cloud Run
                python_exe  = sys.executable
                script_path = str(PROJECT_ROOT / "monitor_studio.py")
                subprocess.Popen([python_exe, script_path, "--manual-check"])

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500


# ── Internal API (called by Cloud Run) ────────────────────────────────────────

def _verify_internal(req) -> bool:
    """Validate X-Internal-Secret header for Cloud Run callbacks."""
    return req.headers.get("X-Internal-Secret") == settings.INTERNAL_SECRET


@app.route('/api/is-processed', methods=['POST'])
def api_is_processed():
    """Check if an event has already been processed."""
    if not _verify_internal(request):
        return jsonify({"error": "unauthorized"}), 401

    data    = request.get_json(silent=True) or {}
    file_id = data.get("file_id", "")
    if not file_id:
        return jsonify({"error": "file_id required"}), 400

    try:
        db        = EventDB(settings)
        processed = db.is_processed(file_id)
        db.close()
        return jsonify({"processed": processed}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/save-result', methods=['POST'])
def api_save_result():
    """
    Receive event result from Cloud Run.
    Saves image to captures/studio/<did>/<fid>/first/,
    writes processed_events + analysis_results + alert_history to MySQL.
    """
    if not _verify_internal(request):
        return jsonify({"error": "unauthorized"}), 401

    # Payload arrives as JSON body OR as multipart form field when image is attached
    if request.content_type and "multipart" in request.content_type:
        payload = request.form.get("payload")
        if not payload:
            return jsonify({"error": "payload field missing"}), 400
        data = json_loads(payload)
    else:
        data = request.get_json(silent=True) or {}

    if not data.get("file_id"):
        return jsonify({"error": "file_id required"}), 400

    # ── save image ─────────────────────────────────────────────────────────
    capture_dir = data.get("capture_dir", "")  # e.g. captures/studio/<did>/<fid>
    if capture_dir and "image" in request.files:
        img_file   = request.files["image"]
        save_dir   = PROJECT_ROOT / capture_dir / "first"
        save_dir.mkdir(parents=True, exist_ok=True)
        img_path   = save_dir / img_file.filename
        img_file.save(str(img_path))

    # ── write to MySQL ─────────────────────────────────────────────────────
    try:
        import datetime as _dt

        db         = EventDB(settings)
        file_id    = data["file_id"]
        camera_did = data.get("camera_did", "")
        event_time = _dt.datetime.fromisoformat(data["event_time"])

        db.mark_processed(
            file_id      = file_id,
            camera_did   = camera_did,
            camera_name  = data.get("camera_name", ""),
            event_type   = data.get("event_type", ""),
            event_time   = event_time,
            duration_sec = data.get("duration_sec", 0),
            frames_saved = data.get("frames_saved", 0),
            capture_dir  = capture_dir,
        )

        analysis = data.get("analysis")
        if analysis:
            result = AnalysisResult.from_dict({
                "is_safe":             analysis.get("is_safe", True),
                "risk_level":          analysis.get("risk_level", "safe"),
                "description":         analysis.get("description", ""),
                "detected_hazards":    analysis.get("hazards", []),
                "confidence":          analysis.get("confidence", 0.0),
                "motion_detected":     analysis.get("motion_detected", True),
                "stillness_warning":   analysis.get("stillness_warn", False),
                "temporal_description": "",
            })
            db.save_analysis(
                file_id,
                camera_did,
                result,
                segment_label=analysis.get("segment_label", "first"),
            )

            if data.get("alert_sent"):
                db.save_alert(file_id, camera_did, result,
                              telegram_ok=data.get("telegram_ok", False))

        db.close()
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def json_loads(s):
    import json
    return json.loads(s)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
