"""
edge/vision/vision_service.py

Vision 主服务
  1. 从 ESP32-CAM /capture 拉取 JPEG 帧
  2. YOLOv8n 推理（RKNN NPU 或 ONNX CPU）
  3. 打印结构化语义摘要
  4. 发布到 MQTT detect 话题（与 broker_client.py 对接）
  5. 检测到"关注目标"时保存带标注框的快照

与现有模块的关系
  - 不修改 data_bus / rule_engine
  - MQTT 发布格式与 broker_client.parse_cam_detect() 已有接口匹配
  - 可单独运行（python vision_service.py），也可被主程序 import 后启动

运行方式（板端）：
  cd /home/lubancat/smart_home
  python3 edge/vision/vision_service.py              # 默认配置
  python3 edge/vision/vision_service.py --onnx       # 强制 ONNX CPU（联调用）
  python3 edge/vision/vision_service.py --interval 2 # 2 秒采一帧
  python3 edge/vision/vision_service.py --no-mqtt    # 不发 MQTT，只打印
"""

import os
import sys
import cv2
import json
import time
import logging
import argparse
import threading
import requests
import numpy as np
from datetime import datetime
from pathlib import Path

# ---- 路径修复：无论从哪里执行都能 import detector ----
_THIS_DIR    = Path(__file__).resolve().parent
_EDGE_DIR    = _THIS_DIR.parent
_PROJECT_DIR = _EDGE_DIR.parent
for p in [str(_THIS_DIR), str(_EDGE_DIR), str(_PROJECT_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from detector import YOLOv8Detector

# ---------------------------------------------------------------------------
# 配置常量（可被 main() 的 CLI 参数覆盖）
# ---------------------------------------------------------------------------

DEFAULT_CAPTURE_URL = "http://172.20.10.4/capture"
DEFAULT_MODEL_PATH  = str(_THIS_DIR / "models" / "yolov8n_rk3588_airockchip_int8.rknn")
DEFAULT_INTERVAL    = 1.0        # 采帧间隔（秒）
SNAPSHOT_DIR        = Path("/home/lubancat/smart_home/data/snapshots")

# MQTT
MQTT_BROKER   = "localhost"
MQTT_PORT     = 1883
MQTT_TOPIC    = "smart_home/cam/esp32cam_001/detect"
MQTT_QOS      = 0                # detect 话题用 QoS 0，不阻塞推理线程

# 关注类别：写入语义摘要的 has_xxx 字段 & 触发快照保存
WATCH_MAP = {
    "person":  "has_person",
    "cat":     "has_cat",
    "dog":     "has_dog",
    # 可按需扩展："fire hydrant": "has_fire_hydrant"
}

# 日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vision")


# ---------------------------------------------------------------------------
# 帧采集
# ---------------------------------------------------------------------------

class FrameGrabber:
    """HTTP pull-mode 帧采集，复用 TCP 连接"""

    def __init__(self, capture_url: str, timeout: float = 3.0):
        self._url     = capture_url
        self._timeout = timeout
        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=1, pool_maxsize=1,
            max_retries=0)
        self._session.mount("http://", adapter)
        log.info(f"FrameGrabber → {capture_url}")

    def grab(self) -> "np.ndarray | None":
        """拉取一帧，返回 BGR numpy array；失败返回 None"""
        try:
            resp = self._session.get(
                self._url, timeout=self._timeout)
            resp.raise_for_status()
            arr = np.frombuffer(resp.content, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                log.warning("imdecode failed (empty JPEG?)")
            return img
        except requests.exceptions.Timeout:
            log.warning("Frame grab timeout")
        except requests.exceptions.ConnectionError:
            log.warning("ESP32-CAM unreachable")
        except Exception as e:
            log.warning(f"Frame grab error: {e}")
        return None


# ---------------------------------------------------------------------------
# MQTT 发布（轻量封装，不依赖 broker_client）
# ---------------------------------------------------------------------------

class MQTTPublisher:

    def __init__(self, broker: str, port: int):
        try:
            import paho.mqtt.client as mqtt
            self._client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION1,
                client_id="vision_service")
            self._client.connect(broker, port, keepalive=60)
            self._client.loop_start()
            self._ok = True
            log.info(f"MQTT publisher connected: {broker}:{port}")
        except Exception as e:
            log.warning(f"MQTT publisher init failed ({e}), publish disabled")
            self._client = None
            self._ok = False

    def publish(self, topic: str, payload: str, qos: int = 0):
        if not self._ok or self._client is None:
            return
        try:
            self._client.publish(topic, payload, qos=qos)
        except Exception as e:
            log.warning(f"MQTT publish error: {e}")

    def stop(self):
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()


# ---------------------------------------------------------------------------
# 语义摘要打印
# ---------------------------------------------------------------------------

def _print_semantic(result: dict, frame_ok: bool):
    """
    打印一帧的结构化语义输出，格式示例：

    ────────────────────────────────────────
    Frame available : True
    Person detected : True
    Fire detected   : False
    Objects         : [{'label': 'person', 'confidence': 0.786, 'latency': 0.086}]
    ────────────────────────────────────────
    """
    objects   = result.get("objects", [])
    latency   = result.get("latency_ms", 0.0)
    ts        = result.get("timestamp", "")
    semantic  = result.get("semantic", {})

    sep = "─" * 48

    summary_objs = [
        {
            "label":      o["label"],
            "confidence": o["conf"],
            "latency":    round(latency / 1000, 3),
        }
        for o in objects
    ]

    print(sep)
    print(f"Frame available : {frame_ok}")
    print(f"Person detected : {semantic.get('has_person', False)}")
    print(f"Fire detected   : {semantic.get('has_fire', False)}")

    for cls, key in WATCH_MAP.items():
        if cls == "person":
            continue
        print(f"{cls.capitalize()+' detected':<17}: {semantic.get(key, False)}")

    print(f"Object count    : {len(objects)}")
    print(f"Latency (ms)    : {latency:.1f}")
    print(f"Timestamp       : {ts}")
    print(f"Objects         : {summary_objs}")
    print(sep)


# ---------------------------------------------------------------------------
# VisionService 主类
# ---------------------------------------------------------------------------

class VisionService:

    def __init__(self,
                 capture_url: str   = DEFAULT_CAPTURE_URL,
                 model_path:  str   = DEFAULT_MODEL_PATH,
                 interval:    float = DEFAULT_INTERVAL,
                 use_rknn:    bool  = True,
                 enable_mqtt: bool  = True,
                 save_snapshots: bool = True):

        self._interval   = interval
        self._save_snaps = save_snapshots
        self._running    = False

        # 子模块
        self._grabber  = FrameGrabber(capture_url)
        self._detector = YOLOv8Detector(
            model_path,
            use_rknn=use_rknn,
            suppress_overexp=True,   # 开启过曝抑制
        )

        if enable_mqtt:
            self._mqtt = MQTTPublisher(MQTT_BROKER, MQTT_PORT)
        else:
            self._mqtt = None
            log.info("MQTT publish disabled")

        if save_snapshots:
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            log.info(f"Snapshots → {SNAPSHOT_DIR}")

        # 统计
        self._frame_count    = 0
        self._detect_count   = 0
        self._start_time     = 0.0

    # ---------------------------------------------------------------- 处理单帧

    def _process_one_frame(self):
        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]

        # 1. 拉帧
        img      = self._grabber.grab()
        frame_ok = img is not None

        if not frame_ok:
            _print_semantic(
                {"objects": [], "latency_ms": 0.0,
                 "timestamp": ts_str,
                 "semantic": {k: False for k in
                              list(WATCH_MAP.values()) + ["has_fire"]}},
                frame_ok=False)
            return

        self._frame_count += 1

        # 2. 推理
        infer_result = self._detector.infer(img)
        objects      = infer_result["objects"]
        latency      = infer_result["latency_ms"]

        # 3. 语义聚合
        semantic = {}
        for cls, key in WATCH_MAP.items():
            semantic[key] = any(o["label"] == cls for o in objects)
        semantic["has_fire"] = any(
            o["label"] == "fire" for o in objects)

        any_watched = any(semantic.values())
        if any_watched:
            self._detect_count += 1

        # 4. 完整结果字典
        result = {
            "device_id":  "esp32cam_001",
            "timestamp":  ts_str,
            "objects":    objects,
            "semantic":   semantic,
            "latency_ms": latency,
            "frame_id":   self._frame_count,
        }

        # 5. 打印语义摘要
        _print_semantic(result, frame_ok=True)

        # 6. 发布 MQTT
        if self._mqtt:
            payload = json.dumps(result, ensure_ascii=False)
            self._mqtt.publish(MQTT_TOPIC, payload, qos=MQTT_QOS)
            log.debug(f"Published to {MQTT_TOPIC}")

        # 7. 快照保存（只在有关注目标时保存）
        if self._save_snaps and any_watched:
            self._save_snapshot(img, objects, ts_str)

    # -------------------------------------------------------------- 快照存储

    def _save_snapshot(self, img: np.ndarray,
                       objects: list, ts_str: str):
        annotated = img.copy()
        for obj in objects:
            x1, y1, x2, y2 = obj["bbox"]
            label = f"{obj['label']} {obj['conf']:.2f}"
            color = (0, 200, 0) if obj["label"] == "person" else (0, 140, 255)
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            (tw, th), _ = cv2.getTextSize(
                label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.rectangle(annotated,
                          (x1, max(0, y1 - th - 6)),
                          (x1 + tw + 4, y1), color, -1)
            cv2.putText(annotated, label,
                        (x1 + 2, max(th, y1 - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                        (255, 255, 255), 1, cv2.LINE_AA)

        path = SNAPSHOT_DIR / f"{ts_str}.jpg"
        cv2.imwrite(str(path), annotated,
                    [cv2.IMWRITE_JPEG_QUALITY, 85])
        log.info(f"Snapshot saved: {path}")

    # ------------------------------------------------------------------- 主循环

    def run_forever(self):
        """阻塞式主循环（单线程）"""
        self._start_time = time.time()
        log.info(f"Vision loop started  interval={self._interval}s")
        print(f"\n[VisionService] Started — interval={self._interval}s")
        print(f"[VisionService] Capture URL: {self._grabber._url}")
        print(f"[VisionService] MQTT topic : {MQTT_TOPIC}")
        print(f"[VisionService] Snapshots  : {SNAPSHOT_DIR}\n")

        try:
            while True:
                t0 = time.perf_counter()
                try:
                    self._process_one_frame()
                except Exception as e:
                    log.error(f"Frame processing error: {e}", exc_info=True)

                elapsed = time.perf_counter() - t0
                sleep   = max(0.0, self._interval - elapsed)
                if sleep > 0:
                    time.sleep(sleep)

        except KeyboardInterrupt:
            self._print_stats()
        finally:
            self.stop()

    def start_background(self):
        """非阻塞：在守护线程中运行"""
        if self._running:
            log.warning("VisionService already running")
            return
        self._running = True

        def _loop():
            self._start_time = time.time()
            log.info(f"Vision background loop started  interval={self._interval}s")
            while self._running:
                t0 = time.perf_counter()
                try:
                    self._process_one_frame()
                except Exception as e:
                    log.error(f"Frame error: {e}", exc_info=True)
                elapsed = time.perf_counter() - t0
                sleep   = max(0.0, self._interval - elapsed)
                if sleep > 0:
                    time.sleep(sleep)
            log.info("Vision background loop stopped")

        t = threading.Thread(target=_loop, daemon=True, name="vision")
        t.start()

    def stop(self):
        self._running = False
        self._detector.release()
        if self._mqtt:
            self._mqtt.stop()
        log.info("VisionService stopped")

    def _print_stats(self):
        elapsed = time.time() - self._start_time
        fps = self._frame_count / elapsed if elapsed > 0 else 0
        print(f"\n[Stats] Frames: {self._frame_count}  "
              f"Detections: {self._detect_count}  "
              f"Avg FPS: {fps:.2f}  "
              f"Elapsed: {elapsed:.0f}s")


# ---------------------------------------------------------------------------
# 独立运行入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="ESP32-CAM + YOLOv8n Vision Service")
    parser.add_argument(
        "--capture-url", default=DEFAULT_CAPTURE_URL,
        help=f"ESP32-CAM capture URL (default: {DEFAULT_CAPTURE_URL})")
    parser.add_argument(
        "--model", default=DEFAULT_MODEL_PATH,
        help="RKNN or ONNX model path")
    parser.add_argument(
        "--interval", type=float, default=DEFAULT_INTERVAL,
        help="Frame grab interval in seconds (default: 1.0)")
    parser.add_argument(
        "--onnx", action="store_true",
        help="Force ONNX CPU backend (skip RKNN, useful for x86 dev)")
    parser.add_argument(
        "--no-mqtt", action="store_true",
        help="Disable MQTT publish (print-only mode)")
    parser.add_argument(
        "--no-snapshot", action="store_true",
        help="Disable snapshot saving")
    args = parser.parse_args()

    svc = VisionService(
        capture_url    = args.capture_url,
        model_path     = args.model,
        interval       = args.interval,
        use_rknn       = not args.onnx,
        enable_mqtt    = not args.no_mqtt,
        save_snapshots = not args.no_snapshot,
    )
    svc.run_forever()


if __name__ == "__main__":
    main()