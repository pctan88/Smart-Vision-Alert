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
