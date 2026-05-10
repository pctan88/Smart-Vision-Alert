import os
import sys
import subprocess
import requests
from flask import Flask, request, jsonify

# Add the project root to path
sys.path.insert(0, os.path.dirname(__file__))

from config.settings import settings
from core.notifier import TelegramNotifier
from core.database import EventDB

app = Flask(__name__)
notifier = TelegramNotifier(settings)

def register_webhook():
    """Register the webhook with Telegram if WEBHOOK_URL is set."""
    if not settings.WEBHOOK_URL or not settings.TELEGRAM_BOT_TOKEN:
        print("WEBHOOK_URL or TELEGRAM_BOT_TOKEN not set. Webhook registration skipped.")
        return

    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/setWebhook"
    payload = {
        "url": settings.WEBHOOK_URL,
        "secret_token": settings.WEBHOOK_SECRET,
        "drop_pending_updates": True
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200 and response.json().get("ok"):
            print(f"Successfully registered webhook: {settings.WEBHOOK_URL}")
        else:
            print(f"Failed to register webhook: {response.text}")
    except Exception as e:
        print(f"Error registering webhook: {e}")

# Register webhook on startup
register_webhook()

@app.route('/webhook', methods=['POST'])
@app.route('/', methods=['POST'])
def telegram_webhook():
    """Handle incoming Telegram webhook requests."""
    # Verify the secret token
    secret_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret_token != settings.WEBHOOK_SECRET:
        return jsonify({"status": "unauthorized"}), 401

    try:
        update = request.get_json()
        if update and "message" in update and "text" in update["message"]:
            text = update["message"]["text"]
            
            if text.startswith("/check"):
                from_user = update["message"].get("from", {})
                username = from_user.get("username", "")
                first_name = from_user.get("first_name", "User")
                
                print(f"Manual check triggered via webhook by {first_name} (@{username})")
                
                # Log to database
                try:
                    db = EventDB(settings)
                    db.log_manual_trigger(username, first_name, text)
                    db.close()
                except Exception as db_err:
                    print(f"Failed to log trigger to DB: {db_err}")

                # Send immediate response
                display_name = f"@{username}" if username else first_name
                notifier.send_text(f"🔍 Manual check triggered by {display_name}. Fetching latest CCTV status...")
                
                # Fire and forget the monitor script
                # We use subprocess.Popen to run it completely in the background
                # so we can return 200 OK to Telegram immediately.
                python_exe = sys.executable
                script_path = os.path.join(os.path.dirname(__file__), "monitor_studio.py")
                subprocess.Popen([python_exe, script_path, "--manual-check"])
                
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500

if __name__ == '__main__':
    # Only for local testing; use passenger_wsgi.py on A2 Hosting
    app.run(host='0.0.0.0', port=5000)
