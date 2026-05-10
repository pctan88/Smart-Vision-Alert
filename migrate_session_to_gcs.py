#!/usr/bin/env python3
"""
Migrate Xiaomi session from local pickle file → Google Cloud Storage JSON.
Run once after setup_gcloud.sh completes.

Usage:
    python3 migrate_session_to_gcs.py
"""

import os
import sys
import json
import pickle

sys.path.insert(0, os.path.dirname(__file__))
from config.settings import settings


def main():
    session_file = settings.STUDIO_SESSION_FILE
    bucket_name  = settings.GCS_BUCKET
    blob_name    = settings.GCS_SESSION_BLOB

    if not bucket_name:
        print("ERROR: GCS_BUCKET not set in config/.env")
        sys.exit(1)

    if not os.path.exists(session_file):
        print(f"ERROR: Session file not found: {session_file}")
        print("Run login_qr.py first to create a session.")
        sys.exit(1)

    # Load pickle session
    with open(session_file, "rb") as f:
        state = pickle.load(f)

    # Only keep JSON-serialisable fields
    session_json = {
        "user_id":       str(state.get("user_id", "")),
        "pass_token":    state.get("pass_token", ""),
        "service_token": state.get("service_token", ""),
        "ssecurity":     state.get("ssecurity", ""),
        "locale":        state.get("locale", "en_US"),
        "timezone":      state.get("timezone", "GMT+08:00"),
    }

    print(f"Session loaded from: {session_file}")
    print(f"  user_id : {session_json['user_id']}")
    print(f"  Fields  : {list(session_json.keys())}")

    # Upload to GCS
    from google.cloud import storage
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob   = bucket.blob(blob_name)
    blob.upload_from_string(
        json.dumps(session_json, indent=2),
        content_type="application/json",
    )

    print(f"\n✅ Session uploaded to gs://{bucket_name}/{blob_name}")
    print("Cloud Run will load this on each pipeline run.")


if __name__ == "__main__":
    main()
