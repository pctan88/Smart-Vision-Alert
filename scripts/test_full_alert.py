from config.settings import settings
from core.notifier import TelegramNotifier
from core.models import AnalysisResult
import os

def test_full_alert():
    print("Testing Full Telegram Alert with Image...")
    
    notifier = TelegramNotifier(settings)
    
    # Create a dummy analysis result
    result = AnalysisResult(
        is_safe=False,
        risk_level="high",
        description="Test alert: A simulated hazard has been detected in the studio.",
        detected_hazards=["Simulated Fall", "Stillness Detected"],
        confidence=0.95,
        motion_detected=False,
        stillness_warning=True,
        temporal_description="Subject appeared to fall and has remained motionless for 60 seconds.",
        analysis_mode="multi_frame",
        frames_analyzed=4
    )
    
    # Path to a real image from the capture session
    image_path = "captures/20260509_2100_2200/561559273962491392/t0060s_01.jpg"
    
    if not os.path.exists(image_path):
        print(f"❌ ERROR: Image not found at {image_path}. Please check the path.")
        return

    print(f"Sending full alert with image to Chat ID: {settings.TELEGRAM_CHAT_ID}...")
    success = notifier.send_alert(result, image_path)
    
    if success:
        print("✅ Success! Check your Telegram group for the rich alert.")
    else:
        print("❌ Failed to send alert. Check logs for details.")

if __name__ == "__main__":
    test_full_alert()
