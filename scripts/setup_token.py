#!/usr/bin/env python3
"""
Smart Vision Alert — Xiaomi Cloud Token Setup
══════════════════════════════════════════════

One-time interactive script to extract your Xiaomi Cloud device token.
Run this LOCALLY (not on the server) to get your camera's credentials.

This wraps the approach from:
  https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor

Usage:
    python setup_token.py
"""

import hashlib
import json
import sys

import requests


REGIONS = {
    "cn": "China",
    "de": "Germany",
    "us": "USA",
    "ru": "Russia",
    "tw": "Taiwan",
    "sg": "Singapore",
    "in": "India",
    "i2": "India 2",
}


def login_xiaomi(username: str, password: str) -> requests.Session | None:
    """Login to Xiaomi account and return authenticated session."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Android-7.1.1-1.0.0-ONEPLUS A3010-136-"
            "ABPeerAppMi498-release/2.3.1.092300-"
            "SDK-22-WiFi-1"
        )
    })

    print("\n🔐 Logging in to Xiaomi Cloud...")

    # Step 1: Get sign
    sign_url = "https://account.xiaomi.com/pass/serviceLogin"
    params = {"sid": "xiaomiio", "_json": "true"}
    resp = session.get(sign_url, params=params)
    data_text = resp.text.replace("&&&START&&&", "")
    sign_data = json.loads(data_text)
    _sign = sign_data.get("_sign", "")

    # Step 2: Login
    login_url = "https://account.xiaomi.com/pass/serviceLoginAuth2"
    login_data = {
        "sid": "xiaomiio",
        "hash": hashlib.md5(password.encode()).hexdigest().upper(),
        "callback": "https://sts.api.io.mi.com/sts",
        "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
        "user": username,
        "_sign": _sign,
        "_json": "true",
    }

    resp = session.post(login_url, data=login_data)
    login_text = resp.text.replace("&&&START&&&", "")
    login_result = json.loads(login_text)

    if "location" not in login_result:
        error = login_result.get("desc", "Unknown error")
        print(f"❌ Login failed: {error}")

        if "notificationUrl" in login_result:
            print("⚠️  2FA verification required!")
            print(f"   Check your email/phone for the verification code.")
            print(f"   URL: {login_result.get('notificationUrl', '')}")
        return None

    # Step 3: Get service token
    location = login_result["location"]
    resp = session.get(location)

    service_token = session.cookies.get("serviceToken")
    if not service_token:
        print("❌ Failed to get service token")
        return None

    print("✅ Login successful!")
    return session


def get_devices(session: requests.Session, region: str) -> list:
    """Get all devices for a specific region."""
    if region == "cn":
        base_url = "https://api.io.mi.com/app"
    else:
        base_url = f"https://{region}.api.io.mi.com/app"

    url = f"{base_url}/home/device_list"
    data = {"getVirtualModel": False, "getHuamiDevices": 0}

    resp = session.post(url, data={"data": json.dumps(data)})
    result = resp.json()

    return result.get("result", {}).get("list", [])


def main():
    print("=" * 60)
    print("  Xiaomi Cloud Token Extractor")
    print("  Smart Vision Alert — Setup Tool")
    print("=" * 60)

    # Get credentials
    username = input("\n📧 Xiaomi account (email/phone): ").strip()
    password = input("🔑 Password: ").strip()

    if not username or not password:
        print("❌ Username and password are required")
        sys.exit(1)

    # Login
    session = login_xiaomi(username, password)
    if session is None:
        sys.exit(1)

    # Select region
    print("\n📍 Available regions:")
    for code, name in REGIONS.items():
        print(f"   {code} — {name}")

    region = input("\nSelect region (or press Enter for all): ").strip().lower()

    regions_to_check = [region] if region in REGIONS else list(REGIONS.keys())

    # Get devices
    all_devices = []
    for r in regions_to_check:
        print(f"\n🔍 Checking region: {r} ({REGIONS.get(r, r)})...")
        devices = get_devices(session, r)
        if devices:
            print(f"   Found {len(devices)} device(s)")
            for d in devices:
                d["_region"] = r
            all_devices.extend(devices)

    if not all_devices:
        print("\n❌ No devices found in any region")
        sys.exit(1)

    # Display devices
    print("\n" + "=" * 60)
    print("  DEVICES FOUND")
    print("=" * 60)

    for i, device in enumerate(all_devices, 1):
        print(f"\n  [{i}] {device.get('name', 'Unknown')}")
        print(f"      Model:   {device.get('model', 'N/A')}")
        print(f"      DID:     {device.get('did', 'N/A')}")
        print(f"      Token:   {device.get('token', 'N/A')}")
        print(f"      IP:      {device.get('localip', 'N/A')}")
        print(f"      MAC:     {device.get('mac', 'N/A')}")
        print(f"      Region:  {device.get('_region', 'N/A')}")

    print("\n" + "=" * 60)

    # Save to file
    save = input("\n💾 Save device info to file? (y/n): ").strip().lower()
    if save == "y":
        output_file = "xiaomi_devices.json"
        with open(output_file, "w") as f:
            # Sanitize: remove sensitive fields for safety
            safe_devices = []
            for d in all_devices:
                safe_devices.append({
                    "name": d.get("name"),
                    "model": d.get("model"),
                    "did": d.get("did"),
                    "token": d.get("token"),
                    "localip": d.get("localip"),
                    "mac": d.get("mac"),
                    "region": d.get("_region"),
                })
            json.dump(safe_devices, f, indent=2)

        print(f"✅ Saved to {output_file}")
        print(f"⚠️  Keep this file private! It contains device tokens.")

    # Show .env config hint
    print("\n" + "=" * 60)
    print("  NEXT STEPS")
    print("=" * 60)
    print("\n  1. Copy config/.env.example → config/.env")
    print("  2. Fill in your credentials:")
    print(f"     XIAOMI_USERNAME={username}")
    print(f"     XIAOMI_PASSWORD=<your_password>")
    if all_devices:
        print(f"     XIAOMI_SERVER_REGION={all_devices[0].get('_region', 'sg')}")
    print("  3. Set up Gemini API key and Telegram bot")
    print("  4. Run: python main.py --test")


if __name__ == "__main__":
    main()
