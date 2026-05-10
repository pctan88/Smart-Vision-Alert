from config.settings import settings
from core.notifier import TelegramNotifier

def main():
    print("Testing Telegram Integration...")
    
    if settings.TELEGRAM_BOT_TOKEN == "your_bot_token" or not settings.TELEGRAM_BOT_TOKEN:
        print("❌ ERROR: TELEGRAM_BOT_TOKEN is not configured in config/.env")
        return
        
    if settings.TELEGRAM_CHAT_ID == "your_group_chat_id" or not settings.TELEGRAM_CHAT_ID:
        print("❌ ERROR: TELEGRAM_CHAT_ID is not configured in config/.env")
        return

    notifier = TelegramNotifier(settings)
    
    print(f"Sending test message to Chat ID: {settings.TELEGRAM_CHAT_ID}...")
    success = notifier.send_test_message()
    
    if success:
        print("✅ Success! Check your Telegram group for the test message.")
    else:
        print("❌ Failed to send message. Please check your Bot Token and Chat ID.")

if __name__ == "__main__":
    main()
