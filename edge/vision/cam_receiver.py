import cv2
import json
import threading
import paho.mqtt.client as mqtt

CAM_STREAM_URL = None   # 从 MQTT status 消息动态获取

def on_cam_status(client, userdata, msg):
    global CAM_STREAM_URL
    data = json.loads(msg.payload)
    CAM_STREAM_URL = data.get("stream_url")
    print(f"[CAM] Stream URL: {CAM_STREAM_URL}")

def start_inference_stream():
    """从 ESP32-CAM 拉取 MJPEG 流并进行视觉推理"""
    if CAM_STREAM_URL is None:
        print("[CAM] No stream URL yet")
        return

    cap = cv2.VideoCapture(CAM_STREAM_URL)
    if not cap.isOpened():
        print(f"[CAM] Cannot open stream: {CAM_STREAM_URL}")
        return

    print(f"[CAM] Stream opened: {CAM_STREAM_URL}")

    while True:
        ret, frame = cap.read()
        if not ret:
            print("[CAM] Frame read failed, retrying...")
            break

        # ===== 在此处接入 YOLO 或其他推理 =====
        # result = yolo_model(frame)
        # publish_detection_result(result)

        cv2.imshow("ESP32-CAM Stream", frame)
        if cv2.waitKey(1) == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()