import test_cloud
import time
import json
import datetime
import tzlocal

def main():
    state = test_cloud.load_session()
    if not state:
        print("No session!")
        return

    # Define 9:30 PM to 9:45 PM today
    now = datetime.datetime.now()
    target_date = now.replace(hour=21, minute=0, second=0, microsecond=0)
    end_date = target_date.replace(hour=22, minute=0)
    
    begin_ms = int(target_date.timestamp() * 1000)
    end_ms = int(end_date.timestamp() * 1000)
    
    print(f"Fetching events from {target_date} to {end_date}")

    resp = test_cloud.call_camera_api(state,
        host="sg.business.smartcamera.api.io.mi.com",
        path="common/app/get/eventlist",
        params={
            "did":       test_cloud.DEVICE_ID,
            "model":     "isa.camera.hlc6",
            "doorBell":  0,
            "eventType": "Default",
            "needMerge": True,
            "sortType":  "DESC",
            "region":    "CN",
            "language":  "en_US",
            "beginTime": begin_ms,
            "endTime":   end_ms,
            "limit":     50,
        }
    )
    events = (resp.get("data") or {}).get("thirdPartPlayUnits") or []
    
    if not events:
        print("No events found in that window.")
        return
        
    print(f"Found {len(events)} events.")
    
    valid_videos = 0
    for ev in events:
        m3u8_resp = test_cloud.call_camera_api(state,
            host="business.smartcamera.api.io.mi.com",
            path="common/app/m3u8",
            params={
                "did":        test_cloud.DEVICE_ID,
                "model":      "isa.camera.hlc6",
                "fileId":     ev.get("fileId"),
                "isAlarm":    ev.get("isAlarm", False),
                "videoCodec": "H264",
                "region":     "CN",
            }
        )
        url = (m3u8_resp.get("data") or {}).get("url") or m3u8_resp.get("url") or ""
        if url:
            print(f"✅ Valid M3U8 found for fileId {ev.get('fileId')}: {url}")
            valid_videos += 1
        else:
            print(f"❌ Invalid M3U8 for {ev.get('fileId')}: {json.dumps(m3u8_resp)}")
            
    print(f"\nTotal valid videos: {valid_videos}/{len(events)}")
    
if __name__ == "__main__":
    main()
