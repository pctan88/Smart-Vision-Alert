import os
import sys
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config.settings import settings

def main():
    if not settings.WEBHOOK_URL or not settings.TELEGRAM_BOT_TOKEN:
        print("Error: WEBHOOK_URL or TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)

    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/setWebhook"
    payload = {
        "url": settings.WEBHOOK_URL,
        "secret_token": settings.WEBHOOK_SECRET,
        "drop_pending_updates": True
    }
    
    print(f"Registering webhook: {settings.WEBHOOK_URL}")
    response = requests.post(url, json=payload, timeout=10)
    
    if response.status_code == 200 and response.json().get("ok"):
        print("✅ Successfully registered webhook!")
        print(response.json())
    else:
        print("❌ Failed to register webhook.")
        print(f"Status: {response.status_code}")
        print(response.text)

if __name__ == "__main__":
    main()
