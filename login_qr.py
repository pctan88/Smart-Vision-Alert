"""
Xiaomi QR Code Login for a new/shared account.
─────────────────────────────────────────────────
Run this script. A QR code will appear — scan it with the Mi Home app.
Session saved to config/.micloud_session_new (separate from main account).

Usage:
  python3 login_qr.py
  python3 login_qr.py --out config/.my_session --country cn
"""

import argparse
import io
import json
import locale as _locale
import os
import pickle
import sys
import time
import datetime

import requests
import qrcode

from micloud.miutils import get_session, gen_nonce, signed_nonce, generate_enc_params, decrypt_rc4

# ── helpers ────────────────────────────────────────────────────────────────────

def print_qr_terminal(data: str):
    """Render QR code as ASCII art in the terminal."""
    qr = qrcode.QRCode(border=1)
    qr.add_data(data)
    qr.make(fit=True)
    f = io.StringIO()
    qr.print_ascii(out=f, invert=True)
    f.seek(0)
    print(f.read())


def serve_image_once(image_bytes: bytes, port: int = 31415):
    """Serve a PNG image once on localhost so user can open it in a browser."""
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(image_bytes)
        def log_message(self, *a):
            pass

    httpd = HTTPServer(("", port), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


# ── QR login flow (mirrors token extractor implementation) ─────────────────────

def qr_login(session: requests.Session) -> dict | None:
    """
    4-step QR login:
      1. GET longPolling/loginUrl  → qr image URL + long-poll URL
      2. Download QR image, display in terminal + serve via HTTP
      3. Long-poll until user scans & confirms in Mi Home app
      4. Follow location redirect → serviceToken
    """

    # ── step 1: get QR URLs ────────────────────────────────────────────────
    print("Requesting QR code from Xiaomi...")
    r1 = session.get("https://account.xiaomi.com/longPolling/loginUrl", params={
        "_qrsize":    "480",
        "qs":         "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
        "callback":   "https://sts.api.io.mi.com/sts",
        "_hasLogo":   "false",
        "sid":        "xiaomiio",
        "serviceParam": "",
        "_locale":    "en_GB",
        "_dc":        str(int(time.time() * 1000)),
    })

    if r1.status_code != 200:
        print(f"  ✗ Failed to get QR URL (HTTP {r1.status_code})")
        print(f"  Response: {r1.text[:200]}")
        return None

    data1 = json.loads(r1.text.replace("&&&START&&&", ""))
    qr_image_url   = data1.get("qr", "")
    login_url      = data1.get("loginUrl", "")
    long_poll_url  = data1.get("lp", "")
    timeout_secs   = data1.get("timeout", 180)

    if not qr_image_url or not long_poll_url:
        print(f"  ✗ Unexpected response: {json.dumps(data1)[:300]}")
        return None

    print(f"  ✓ Got QR image URL and long-poll URL")

    # ── step 2: download QR image and display ─────────────────────────────
    r2 = session.get(qr_image_url, timeout=15)
    if r2.status_code != 200:
        print(f"  ✗ Could not download QR image (HTTP {r2.status_code})")
        return None

    # Try to render as ASCII in terminal
    try:
        from PIL import Image
        import qrcode.image.base
        img = Image.open(io.BytesIO(r2.content))
        # Read QR data from image using pyzbar if available
        try:
            from pyzbar.pyzbar import decode
            qr_data = decode(img)
            if qr_data:
                print_qr_terminal(qr_data[0].data.decode())
        except ImportError:
            # Can't decode, just serve the image
            pass
    except Exception:
        pass

    # Always serve the original image via HTTP so you can open it
    httpd = serve_image_once(r2.content, port=31415)

    print()
    print("=" * 58)
    print("  Scan the QR code with Mi Home app to log in")
    print("=" * 58)
    print(f"  Open in browser: http://127.0.0.1:31415")
    print(f"  Or visit:        {login_url}")
    print("=" * 58)
    print()
    print(f"  Waiting for scan (up to {timeout_secs}s) ...")

    # ── step 3: long-poll until confirmed ─────────────────────────────────
    data3     = {}
    start     = time.time()
    confirmed = False

    while time.time() - start < timeout_secs:
        try:
            r3 = session.get(long_poll_url, timeout=12)
        except requests.exceptions.Timeout:
            print("  . (poll timeout, retrying...)", end="\r")
            continue
        except Exception as e:
            print(f"  Poll error: {e}")
            break

        if r3.status_code == 200:
            try:
                data3 = json.loads(r3.text.replace("&&&START&&&", ""))
            except Exception:
                data3 = {}
            confirmed = True
            break
        else:
            # Non-200 means still waiting or error
            print(f"  . waiting... (poll status {r3.status_code})", end="\r")

    httpd.shutdown()

    if not confirmed:
        print("\n  ✗ Timed out or scan failed.")
        return None

    user_id    = str(data3.get("userId", ""))
    ssecurity  = data3.get("ssecurity", "")
    cuser_id   = data3.get("cUserId", "")
    pass_token = data3.get("passToken", "")
    location   = data3.get("location", "")

    if not user_id or not ssecurity or not location:
        print(f"\n  ✗ Incomplete login data: {json.dumps(data3)[:300]}")
        return None

    print(f"\n  ✓ Scanned! userId={user_id}")

    # ── step 4: follow location → serviceToken ─────────────────────────────
    print("  Fetching service token...")
    r4 = session.get(location, headers={"content-type": "application/x-www-form-urlencoded"})
    if r4.status_code != 200:
        print(f"  ✗ STS redirect failed (HTTP {r4.status_code})")
        return None

    service_token = r4.cookies.get("serviceToken") or session.cookies.get("serviceToken", "")
    if not service_token:
        print("  ✗ No serviceToken in cookies after redirect.")
        return None

    print(f"  ✓ serviceToken obtained")

    import tzlocal
    tz = datetime.datetime.now(tzlocal.get_localzone()).strftime("%z")
    tz_str = f"GMT{tz[:-2]}:{tz[-2:]}"

    return {
        "user_id":       user_id,
        "ssecurity":     ssecurity,
        "service_token": service_token,
        "cuser_id":      cuser_id,
        "pass_token":    pass_token,
        "locale":        (_locale.getlocale()[0] or "en_US"),
        "timezone":      tz_str,
    }


# ── validate + list devices ────────────────────────────────────────────────────

def validate_and_list(state: dict, country: str = "cn"):
    """Call device_list to confirm session works and show all devices."""
    try:
        prefix = "" if country.lower() == "cn" else country.lower() + "."
        url    = f"https://{prefix}api.io.mi.com/app/home/device_list"
        http   = get_session()
        http.headers.update({
            "Accept-Encoding":            "identity",
            "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
            "content-type":               "application/x-www-form-urlencoded",
            "MIOT-ENCRYPT-ALGORITHM":     "ENCRYPT-RC4",
        })
        http.cookies.update({
            "userId":                 str(state["user_id"]),
            "yetAnotherServiceToken": state["service_token"],
            "serviceToken":           state["service_token"],
            "locale":                 state.get("locale", "en_US"),
            "timezone":               state.get("timezone", "GMT+08:00"),
            "channel":                "MI_APP_STORE",
        })
        nonce  = gen_nonce()
        sn     = signed_nonce(state["ssecurity"], nonce)
        params = {"data": '{"getVirtualModel":true,"getHuamiDevices":1,"get_split_device":true,"support_smart_home":true}'}
        pdata  = generate_enc_params(url, "POST", sn, nonce, params, state["ssecurity"])
        resp   = http.post(url, data=pdata)
        raw    = decrypt_rc4(signed_nonce(state["ssecurity"], pdata["_nonce"]), resp.text)
        j      = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        devices = (j.get("result") or {}).get("list") or []
        print(f"\n  ✓ Session valid — {len(devices)} device(s) on account:")
        for d in devices:
            owner = "owned" if d.get("admin") else "shared"
            print(f"    • [{owner}] {d.get('name','?')}")
            print(f"             did={d.get('did')}  model={d.get('model')}")
    except Exception as e:
        print(f"  ✗ Validation failed: {e}")


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out",     default="config/.micloud_session_new",
                   help="Session file to write (default: config/.micloud_session_new)")
    p.add_argument("--country", default="sg", help="API country/server (default: sg)")
    args = p.parse_args()

    os.makedirs("config", exist_ok=True)

    session = get_session()
    state   = qr_login(session)

    if not state:
        print("\nLogin failed.")
        sys.exit(1)

    print("\nLogin successful!")
    validate_and_list(state, country=args.country)

    with open(args.out, "wb") as f:
        pickle.dump(state, f)
    print(f"\n✓ Session saved → {args.out}")


if __name__ == "__main__":
    main()
