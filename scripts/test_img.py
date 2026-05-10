import test_cloud
import time
import json

def main():
    state = test_cloud.load_session()
    if not state:
        return
        
    # Using the fileId from the first event dumped earlier
    file_id = "561557438153359744"
    img_store_id = "CAMERA_IMG_GKACAIqOE4ZQ1RF3KVsNroCtZJ8vG0f2gmy-UYjNMmmgOT5Kp11ZtyTiOqhtJgcA51R51blz1SrOIjePI8Wa_eZjT810h5_gUSPutfzVxFKfGXZxFonXg0Yj2T81hWYid1N3T3BHkzXXt9-L6yQOLSKC3-jPoTc_DSqOoGf_FKVlkU4723_hMbGOOGMOEOcnj5XcFOuh0gulzg8kSiLiC2jKI36l1CDw5mw-70t9_RX7Z8PnibVLjoSV_RhQSVn1zmcFkTULFUauWqJ1yd32-iwS2yZ9rlqjHYlheaOy53zwja_o-HSRzyzUZHtXLoyKsSi8uGx06e1AnATpwAuwT5sro3mvFol7qIgnfAP7znk5j9UtLVK0-miT_3KJ5i6067j9GBJSMmlbPIBJmYTWCZis0X_eUgEYEMgDVh3tAd6gmq58fadsPNoYFNxUSmMYC3hBhfE8MVDPC2RXgD5HJQASAA"

    print("Trying common/app/file/get...")
    resp = test_cloud.call_camera_api(state,
        host="business.smartcamera.api.io.mi.com",
        path="common/app/file/get",
        params={
            "did": test_cloud.DEVICE_ID,
            "model": "isa.camera.hlc6",
            "fileId": file_id,
        }
    )
    print(json.dumps(resp)[:200])

    print("\nTrying common/app/img/get...")
    resp2 = test_cloud.call_camera_api(state,
        host="business.smartcamera.api.io.mi.com",
        path="common/app/img/get",
        params={
            "did": test_cloud.DEVICE_ID,
            "model": "isa.camera.hlc6",
            "fileId": file_id,
        }
    )
    print(json.dumps(resp2)[:200])

if __name__ == "__main__":
    main()
