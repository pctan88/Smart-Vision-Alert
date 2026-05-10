"""
Run capture for a specific time range and store frames for AI processing.
Usage:
  python3 run_capture.py                     # today 9pm–10pm (default)
  python3 run_capture.py --start 20:00 --end 22:00
  python3 run_capture.py --date 2026-05-09 --start 21:00 --end 22:00
"""

import argparse
import datetime
import sys
from zoneinfo import ZoneInfo

from xiaomi_capture import (
    get_session_state, capture_time_range, local_time_range_ms, LOCAL_TZ
)

# Only needed if session is completely missing / expired
def _interactive_login():
    from test_cloud import full_login, save_session
    print("No valid session found. Starting interactive login...")
    state = full_login()
    if state:
        save_session(state)
    return state


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--date",  default=None, help="Date YYYY-MM-DD (default: today)")
    p.add_argument("--start", default="21:00", help="Start time HH:MM local (default: 21:00)")
    p.add_argument("--end",   default="22:00", help="End time HH:MM local (default: 22:00)")
    p.add_argument("--out",   default="captures", help="Output directory (default: captures)")
    return p.parse_args()


def main():
    args = parse_args()

    # Parse date
    if args.date:
        date = datetime.date.fromisoformat(args.date)
    else:
        date = datetime.datetime.now(LOCAL_TZ).date()

    # Parse time range
    def _parse_hm(s):
        h, m = map(int, s.split(":"))
        return h, m

    sh, sm = _parse_hm(args.start)
    eh, em = _parse_hm(args.end)
    start_ms, end_ms = local_time_range_ms(date, sh, sm, eh, em)

    # Auth
    state = get_session_state()
    if not state:
        state = _interactive_login()
    if not state:
        print("Authentication failed.")
        sys.exit(1)

    # Build output dir: captures/20260509_2100_2200/
    folder = f"{date.strftime('%Y%m%d')}_{sh:02d}{sm:02d}_{eh:02d}{em:02d}"
    out_dir = f"{args.out}/{folder}"

    # Run
    results = capture_time_range(state, start_ms, end_ms, out_dir)

    # Summary
    total_frames = sum(len(r.all_frames) for r in results)
    total_thumb  = sum(1 for r in results if r.thumbnail)
    print(f"\n{'='*50}")
    print(f"Done. {len(results)} events | {total_frames} frames | {total_thumb} thumbnails")
    print(f"Output: {out_dir}/")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
