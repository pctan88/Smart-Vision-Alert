# Deploying Smart Vision Alert on A2Hosting

## Prerequisites

- A2Hosting shared hosting account with **SSH access**
- cPanel access for cron job setup
- Python 3.8+ available on the server

---

## Step 1: Upload Project Files

Upload the entire `Smart-Vision-Alert/` folder to your A2Hosting home directory.
You can use:
- **File Manager** in cPanel
- **SFTP** (e.g., FileZilla)
- **SSH + Git**: `git clone <your-repo-url> ~/smart-vision-alert`

---

## Step 2: Create Python Virtual Environment

SSH into your server:

```bash
ssh username@your-server.a2hosting.com
```

Set up the virtual environment:

```bash
cd ~/smart-vision-alert

# Create virtual environment
python3 -m venv venv

# Activate it
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Verify installation
python -c "import google.generativeai; print('✅ google-generativeai OK')"
python -c "import requests; print('✅ requests OK')"
python -c "from PIL import Image; print('✅ Pillow OK')"
python -c "from dotenv import load_dotenv; print('✅ python-dotenv OK')"
```

---

## Step 3: Configure Environment

```bash
# Copy the example config
cp config/.env.example config/.env

# Edit with your credentials
nano config/.env
```

Fill in:
- `GEMINI_API_KEY` — Get from https://aistudio.google.com/apikey
- `TELEGRAM_BOT_TOKEN` — Create via @BotFather on Telegram
- `TELEGRAM_CHAT_ID` — Your group's chat ID
- `CAMERA_SOURCE` — Start with `local_folder` for testing

---

## Step 4: Test the Setup

```bash
# Activate venv
source ~/smart-vision-alert/venv/bin/activate

# Check system status
python ~/smart-vision-alert/main.py --status

# Send test Telegram message
python ~/smart-vision-alert/main.py --test

# Place a test image in captures/manual/ and run analysis
python ~/smart-vision-alert/main.py --analyze

# Run full pipeline
python ~/smart-vision-alert/main.py
```

---

## Step 5: Set Up Cron Job

### Via cPanel:

1. Login to cPanel → **Cron Jobs**
2. Set interval: **Every 5 minutes** (`*/5 * * * *`)
3. Command:

```
/home/YOUR_USERNAME/smart-vision-alert/venv/bin/python /home/YOUR_USERNAME/smart-vision-alert/main.py >/dev/null 2>&1
```

> ⚠️ Replace `YOUR_USERNAME` with your actual A2Hosting username.

### Via SSH (alternative):

```bash
crontab -e
```

Add this line:

```
*/5 * * * * /home/YOUR_USERNAME/smart-vision-alert/venv/bin/python /home/YOUR_USERNAME/smart-vision-alert/main.py >/dev/null 2>&1
```

---

## Step 6: Monitor

Check the logs to verify it's running:

```bash
# View recent logs
tail -50 ~/smart-vision-alert/logs/app.log

# View errors only
tail -20 ~/smart-vision-alert/logs/errors.log

# Watch logs in real-time
tail -f ~/smart-vision-alert/logs/app.log
```

---

## Troubleshooting

### "ModuleNotFoundError"
Make sure the cron command uses the full path to the **venv Python**:
```
/home/YOUR_USERNAME/smart-vision-alert/venv/bin/python
```

### "Permission denied"
```bash
chmod +x ~/smart-vision-alert/main.py
```

### Telegram bot not sending messages
1. Make sure the bot is added to the group
2. Send `/start` to the bot first
3. Get group chat ID by adding `@userinfobot` or `@getidsbot` to the group
4. Group chat IDs are usually negative numbers (e.g., `-1001234567890`)

### Gemini API errors
- Check your API key is valid at https://aistudio.google.com/
- Free tier: 15 requests/minute, 1,500 requests/day
- At 5-min intervals = 288 requests/day (well within limits)

### Image not being captured
- For `local_folder` mode: place images in `captures/manual/`
- For `xiaomi_cloud` mode: run `setup_token.py` locally first, check logs for auth errors
