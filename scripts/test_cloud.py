import os
import json
import time
import pickle
import base64
import hashlib
import logging
import locale
import datetime
import threading
import http.server
import urllib.parse
import requests
import tzlocal
from dotenv import load_dotenv

from micloud.miutils import get_session, gen_nonce, signed_nonce, generate_enc_params, decrypt_rc4

load_dotenv('config/.env')

USERNAME   = os.getenv('XIAOMI_USERNAME')
PASSWORD   = os.getenv('XIAOMI_PASSWORD')
DEVICE_ID  = "294183200"
COUNTRY    = os.getenv('XIAOMI_SERVER_REGION', 'cn')
SESSION_FILE = "config/.micloud_session"

CAPTCHA_PORT = 31415
AUTH_BASE    = "https://account.xiaomi.com"


# ── session persistence ───────────────────────────────────────────────────────

def save_session(state: dict):
    with open(SESSION_FILE, 'wb') as f:
        pickle.dump(state, f)
    print(f"Session saved → {SESSION_FILE}")


def load_session() -> dict | None:
    if not os.path.exists(SESSION_FILE):
        return None
    try:
        with open(SESSION_FILE, 'rb') as f:
            return pickle.load(f)
    except Exception:
        return None


# ── captcha helper ────────────────────────────────────────────────────────────

def show_captcha(session: requests.Session, captcha_url: str) -> str:
    """Serve captcha image on localhost and prompt user to enter it."""
    img_data = session.get(captcha_url).content

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(img_data)
        def log_message(self, *a):
            pass

    try:
        srv = http.server.HTTPServer(("127.0.0.1", CAPTCHA_PORT), Handler)
        t = threading.Thread(target=srv.serve_forever)
        t.daemon = True
        t.start()
        print(f"\nCaptcha image: http://127.0.0.1:{CAPTCHA_PORT}")
        code = input("Enter captcha (case-sensitive): ").strip()
        srv.shutdown()
    except Exception:
        # fallback: save to temp file
        import tempfile, subprocess
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.write(img_data)
        tmp.close()
        print(f"\nCaptcha saved to: {tmp.name}")
        try:
            subprocess.Popen(["open", tmp.name])
        except Exception:
            pass
        code = input("Enter captcha (case-sensitive): ").strip()
    return code


# ── 2FA / notification flow ───────────────────────────────────────────────────

def handle_2fa(session: requests.Session, notification_url: str, user_id: str = "") -> tuple[str, str, str]:
    """Complete email 2FA. Returns (ssecurity, service_token, user_id)."""
    headers = {
        "User-Agent":   "Android-7.1.1-1.0.0-ONEPLUS A3010-136-" + hashlib.md5(USERNAME.encode()).hexdigest()[:13].upper(),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    context = urllib.parse.parse_qs(urllib.parse.urlparse(notification_url).query).get("context", [None])[0]

    # Step 1: open notificationUrl
    session.get(notification_url, headers=headers)

    # Step 2: fetch identity options (required before sending ticket)
    session.get(f"{AUTH_BASE}/identity/list", headers=headers, params={
        "sid": "xiaomiio", "context": context, "_locale": "en_US"
    })

    # Step 3: request OTP email — ick cookie must be passed in body
    ick = session.cookies.get("ick", "")
    session.post(f"{AUTH_BASE}/identity/auth/sendEmailTicket", headers=headers,
        params={"_dc": str(int(time.time() * 1000)), "sid": "xiaomiio",
                "context": context, "mask": "0", "_locale": "en_US"},
        data={"retry": "0", "icode": "", "_json": "true", "ick": ick}
    )

    print("\nTwo-factor auth required – check your email for the OTP code.")
    code = input("Enter 2FA code: ").strip()

    # Step 4: verify OTP (code goes in 'ticket' field)
    ick = session.cookies.get("ick", "")
    r_verify = session.post(f"{AUTH_BASE}/identity/auth/verifyEmail", headers=headers,
        params={"_flag": "8", "_json": "true", "sid": "xiaomiio",
                "context": context, "mask": "0", "_locale": "en_US"},
        data={"_flag": "8", "ticket": code, "trust": "false", "_json": "true", "ick": ick},
        allow_redirects=False,
    )
    # Step 5: follow redirect chain manually — handle relative and absolute URLs
    def abs_url(base, url):
        if not url:
            return ""
        return url if url.startswith("http") else base + url

    finish_loc = abs_url(AUTH_BASE, r_verify.headers.get("Location", ""))

    # verifyEmail may return JSON with a 'location' field instead of a redirect
    if not finish_loc:
        try:
            body = json.loads(r_verify.text.replace("&&&START&&&", ""))
            finish_loc = abs_url(AUTH_BASE, body.get("location", body.get("redirect", "")))
        except Exception:
            pass

    r_check = session.get(finish_loc, headers=headers, allow_redirects=False) if finish_loc else r_verify

    # Step 6: follow to serviceLoginAuth2/end — ssecurity is in extension-pragma header
    end_url = abs_url(AUTH_BASE, r_check.headers.get("Location", ""))
    r_end   = session.get(end_url, headers=headers, allow_redirects=False) if end_url else r_check
    pragma_raw = r_end.headers.get("extension-pragma", "{}")
    try:
        ssecurity = json.loads(pragma_raw).get("ssecurity", "")
    except Exception:
        ssecurity = ""

    # Step 7: follow STS redirect → serviceToken cookie
    sts_url       = abs_url("https://sts.api.io.mi.com", r_end.headers.get("Location", "/sts"))
    if not sts_url or sts_url == "https://sts.api.io.mi.com":
        sts_url = "https://sts.api.io.mi.com/sts"
    r_sts         = session.get(sts_url, headers=headers, allow_redirects=True)
    service_token = r_sts.cookies.get("serviceToken") or session.cookies.get("serviceToken", "")

    if not user_id:
        user_id = next(
            (c.value for c in session.cookies if c.name in ("userId", "user_id")), ""
        )

    return ssecurity, service_token, user_id


# ── main login ────────────────────────────────────────────────────────────────

def full_login() -> dict | None:
    """Interactive login with captcha + 2FA support. Returns session state dict."""
    session = get_session()
    session.cookies.set("userId", USERNAME)

    tz     = datetime.datetime.now(tzlocal.get_localzone()).strftime('%z')
    tz_str = f"GMT{tz[:-2]}:{tz[-2:]}"
    loc    = locale.getlocale()[0] or "en_US"

    # ── step 1: get _sign ──────────────────────────────────────────────────
    r1 = session.get(f"{AUTH_BASE}/pass/serviceLogin?sid=xiaomiio&_json=true")
    j1 = json.loads(r1.text.replace("&&&START&&&", ""))
    sign = j1.get("_sign", "")

    # ── step 2: send credentials (retry loop for captcha) ──────────────────
    post_data = {
        "sid":      "xiaomiio",
        "hash":     hashlib.md5(PASSWORD.encode()).hexdigest().upper(),
        "callback": "https://sts.api.io.mi.com/sts",
        "qs":       "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
        "user":     USERNAME,
        "_json":    "true",
    }
    if sign:
        post_data["_sign"] = sign

    j2 = {}
    for attempt in range(3):
        r2 = session.post(f"{AUTH_BASE}/pass/serviceLoginAuth2", data=post_data)
        j2 = json.loads(r2.text.replace("&&&START&&&", ""))

        # captcha required — show image, collect code, retry
        if j2.get("captchaUrl"):
            raw_url     = j2["captchaUrl"]
            captcha_url = raw_url if raw_url.startswith("http") else AUTH_BASE + raw_url
            capt_code   = show_captcha(session, captcha_url)
            post_data["captCode"] = capt_code
            post_data["captcId"]  = urllib.parse.urlparse(captcha_url).query
            continue

        # 2FA required — handle immediately (contains notificationUrl, may lack userId)
        if j2.get("notificationUrl"):
            break

        if j2.get("result") != "ok":
            print(f"Login rejected by Xiaomi: {j2.get('description', j2)}")
            return None

        break
    else:
        print("Failed after captcha retries.")
        return None

    # extract what's available from step-2 response
    user_id    = str(j2.get("userId", j2.get("user_id", "")))
    ssecurity  = j2.get("ssecurity", "")
    cuser_id   = j2.get("cUserId", "")
    pass_token = j2.get("passToken", "")
    location   = j2.get("location", "")

    # ── step 3: 2FA if notificationUrl present ─────────────────────────────
    notification_url = j2.get("notificationUrl")
    if notification_url:
        ssecurity, service_token, user_id = handle_2fa(session, notification_url, user_id)
    else:
        # no 2FA – follow location → STS
        r3 = session.get(location)
        if r3.status_code == 403:
            print("Access denied at step 3.")
            return None
        service_token = r3.cookies.get("serviceToken", "")
        if not service_token:
            service_token = session.cookies.get("serviceToken", "")

    if not service_token:
        print("Could not obtain service token.")
        return None

    print("Login successful.")
    return {
        "user_id":       user_id,
        "ssecurity":     ssecurity,
        "service_token": service_token,
        "cuser_id":      cuser_id,
        "pass_token":    pass_token,
        "locale":        loc,
        "timezone":      tz_str,
    }


# ── cloud API callers ─────────────────────────────────────────────────────────

def api_url(country: str) -> str:
    prefix = "" if country.strip().lower() == "cn" else country.strip().lower() + "."
    return f"https://{prefix}api.io.mi.com/app"


def _make_session(state: dict) -> requests.Session:
    session = get_session()
    session.headers.update({
        "Accept-Encoding":            "identity",
        "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
        "content-type":               "application/x-www-form-urlencoded",
        "MIOT-ENCRYPT-ALGORITHM":     "ENCRYPT-RC4",
    })
    session.cookies.update({
        "userId":                 str(state["user_id"]),
        "yetAnotherServiceToken": state["service_token"],
        "serviceToken":           state["service_token"],
        "locale":                 state["locale"],
        "timezone":               state["timezone"],
        "is_daylight":            str(time.daylight),
        "dst_offset":             str(time.localtime().tm_isdst * 60 * 60 * 1000),
        "channel":                "MI_APP_STORE",
    })
    return session


def call_api(state: dict, endpoint: str, params: dict, country: str):
    """Standard encrypted POST to api.io.mi.com."""
    url      = api_url(country) + endpoint
    session  = _make_session(state)
    nonce    = gen_nonce()
    snonce   = signed_nonce(state["ssecurity"], nonce)
    pdata    = generate_enc_params(url, "POST", snonce, nonce, params, state["ssecurity"])
    response = session.post(url, data=pdata)
    try:
        return decrypt_rc4(signed_nonce(state["ssecurity"], pdata["_nonce"]), response.text)
    except Exception:
        return response.text


def _gen_camera_signature(method: str, path: str, signed_nonce: str, params: dict) -> str:
    """Like gen_enc_signature but uses urlparse path directly (avoids split('com') bug)."""
    import hmac as _hmac
    parts = [method.upper(), path]
    for k, v in params.items():
        parts.append(f"{k}={v}")
    parts.append(signed_nonce)
    sig_str = "&".join(parts)
    return base64.b64encode(hashlib.sha1(sig_str.encode()).digest()).decode()


def call_camera_api(state: dict, host: str, path: str, params: dict) -> dict:
    """Encrypted GET to Xiaomi smart-camera API hosts."""
    from Crypto.Cipher import ARC4

    url    = f"https://{host}/{path}"
    url_path = f"/{path}"  # used for signing — avoids split("com") bug
    session = _make_session(state)
    nonce   = gen_nonce()
    snonce  = signed_nonce(state["ssecurity"], nonce)

    # Encrypt params (same RC4 method as generate_enc_params but with correct path)
    enc_params = {"data": json.dumps(params)}
    enc_params["rc4_hash__"] = _gen_camera_signature("GET", url_path, snonce, enc_params)
    for k, v in enc_params.items():
        r = ARC4.new(base64.b64decode(snonce))
        r.encrypt(bytes(1024))
        enc_params[k] = base64.b64encode(r.encrypt(v.encode())).decode()
    enc_params["signature"] = _gen_camera_signature("GET", url_path, snonce, enc_params)
    enc_params["ssecurity"] = state["ssecurity"]
    enc_params["_nonce"]    = nonce
    enc_params["yetAnotherServiceToken"] = state["service_token"]

    response = session.get(url, params=enc_params)
    try:
        raw = decrypt_rc4(signed_nonce(state["ssecurity"], nonce), response.text)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        return json.loads(raw)
    except Exception:
        raw = response.text
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except Exception:
            return {"_raw": raw}


# ── silent token refresh (no captcha/2FA needed) ─────────────────────────────

def silent_refresh(state: dict) -> dict | None:
    """Use saved passToken to get a new serviceToken without captcha or 2FA."""
    session = get_session()
    # inject existing pass/user cookies so Xiaomi skips re-authentication
    session.cookies.update({
        "userId":    str(state["user_id"]),
        "passToken": state["pass_token"],
    })

    # step 1 — Xiaomi returns location directly when passToken is valid
    r1 = session.get(f"{AUTH_BASE}/pass/serviceLogin?sid=xiaomiio&_json=true")
    j1 = json.loads(r1.text.replace("&&&START&&&", ""))

    location = j1.get("location", "")
    ssecurity = j1.get("ssecurity", state.get("ssecurity", ""))

    if not location:
        # passToken stale — need full re-login
        return None

    # step 2 — follow location → get new serviceToken
    r2 = session.get(location)
    if r2.status_code == 403:
        return None
    service_token = r2.cookies.get("serviceToken") or session.cookies.get("serviceToken", "")
    if not service_token:
        return None

    return {**state, "service_token": service_token, "ssecurity": ssecurity}


# ── main test ─────────────────────────────────────────────────────────────────

def test_cloud_access():
    if not USERNAME or not PASSWORD:
        print("Error: set XIAOMI_USERNAME and XIAOMI_PASSWORD in config/.env")
        return

    # try saved session first
    state = load_session()
    if state:
        print("Loaded saved session – validating...")
        try:
            r = call_api(state, "/home/device_list",
                         {"data": '{"getVirtualModel":true,"getHuamiDevices":1}'}, COUNTRY)
            parsed = json.loads(r) if r else {}
            if parsed.get("result") is not None or parsed.get("code") == 0:
                print("Session valid.\n")
            else:
                raise ValueError(f"bad response: {r[:100]}")
        except Exception:
            print("Session expired – attempting silent refresh...")
            state = silent_refresh(state)
            if state:
                save_session(state)
                print("Token refreshed silently.\n")
            else:
                print("Silent refresh failed – full re-login required.\n")

    if not state:
        print(f"Logging in as {USERNAME}...")
        state = full_login()
        if not state:
            return
        save_session(state)

    # ── fetch event list via smartcamera API ──────────────────────────────
    print("Fetching surveillance events (last 2 h)...")
    end_ms   = int(time.time() * 1000)
    begin_ms = end_ms - 2 * 3600 * 1000

    # sg.business.smartcamera.api.io.mi.com + region CN works for Malaysia/CN accounts
    resp = call_camera_api(state,
        host="sg.business.smartcamera.api.io.mi.com",
        path="common/app/get/eventlist",
        params={
            "did":       DEVICE_ID,
            "model":     "isa.camera.hlc6",
            "doorBell":  0,
            "eventType": "Default",
            "needMerge": True,
            "sortType":  "DESC",
            "region":    "CN",
            "language":  locale.getlocale()[0] or "en_US",
            "beginTime": begin_ms,
            "endTime":   end_ms,
            "limit":     20,
        }
    )
    events = (resp.get("data") or {}).get("thirdPartPlayUnits") or []

    if not events:
        print(f"No events found. Response: {json.dumps(resp)}")
        return

    # Deduplicate by fileId (same video can appear under multiple event types)
    seen, unique_events = set(), []
    for ev in events:
        fid = ev.get("fileId")
        if fid not in seen:
            seen.add(fid)
            unique_events.append(ev)

    print(f"\nFound {len(events)} events ({len(unique_events)} unique videos):\n")

    # Fetch m3u8 URL for the most recent event as a test
    latest = unique_events[0]
    m3u8_resp = call_camera_api(state,
        host="business.smartcamera.api.io.mi.com",
        path="common/app/m3u8",
        params={
            "did":        DEVICE_ID,
            "model":      "isa.camera.hlc6",
            "fileId":     latest.get("fileId"),
            "isAlarm":    latest.get("isAlarm", False),
            "videoCodec": "H264",
            "region":     "CN",
        }
    )
    m3u8_url = (m3u8_resp.get("data") or {}).get("url") or m3u8_resp.get("url") or ""
    print(f"Latest video m3u8: {m3u8_url or json.dumps(m3u8_resp)[:200]}\n")

    for ev in unique_events:
        ts_ms  = ev.get("createTime")
        ts_str = datetime.datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M:%S UTC") if ts_ms else "N/A"
        print(f"  File ID:   {ev.get('fileId')}")
        print(f"  Type:      {ev.get('eventType')}")
        print(f"  Time:      {ts_str}")
        print(f"  Expires:   {datetime.datetime.utcfromtimestamp(ev['expireTime']/1000).strftime('%Y-%m-%d') if ev.get('expireTime') else 'N/A'}")
        print(f"  Has image: {ev.get('isShowImg')} | Storage: {ev.get('location')}")
        print("  " + "-" * 38)


if __name__ == "__main__":
    test_cloud_access()
