"""
Phase 3: Vision module
Pulls MJPEG stream from ESP32-CAM, runs YOLO inference on CPU,
outputs structured semantic result for LLM and rule engine.
"""
import cv2
import time
import logging
import threading
import requests
import numpy as np
import os
import sys

log = logging.getLogger("vision")

# Graceful fallback if ultralytics not available
try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    YOLO_AVAILABLE = False
    log.warning("ultralytics not available, using brightness fallback")


class VisionAnalyzer:

    # COCO class IDs relevant to smart home
    PERSON_ID = 0
    CAT_ID    = 15
    DOG_ID    = 16

    def __init__(self, capture_url: str, stream_url: str,
                 model_path: str = "/home/smart_home/edge/models/yolov8n.pt",
                 use_yolo: bool = True,
                 inference_interval: float = 3.0):
        """
        Parameters
        ----------
        capture_url       : http://172.20.10.4/capture  (single frame)
        stream_url        : http://172.20.10.4/stream   (MJPEG)
        model_path        : path to yolov8n.pt
        use_yolo          : set False to skip YOLO and use fallback only
        inference_interval: seconds between inferences in stream loop
        """
        self._capture_url = capture_url
        self._stream_url  = stream_url
        self._interval    = inference_interval
        self._model       = None
        self._result      = {}
        self._lock        = threading.Lock()
        self._running     = False

        if use_yolo and YOLO_AVAILABLE:
            self._load_yolo(model_path)

    # ==================== Model loading ====================

    def _load_yolo(self, path: str):
        """
        Load YOLO model from given path.
        If the path does not exist, YOLO will automatically download it.
        Ensures the parent directory exists to avoid download failures.
        """
        try:
            # Ensure the directory for the model exists
            model_dir = os.path.dirname(path)
            if model_dir and not os.path.exists(model_dir):
                os.makedirs(model_dir, exist_ok=True)
                log.info(f"Created model directory: {model_dir}")

            # Check if file already exists
            if os.path.isfile(path):
                log.info(f"Model found at {path}, loading...")
            else:
                log.info(f"Model not found at {path}, will download on load.")

            # Load model (downloads automatically if missing)
            log.info(f"Loading YOLO from {path} ...")
            self._model = YOLO(path)

            # Warm-up: run once on blank image so first real inference is fast
            blank = np.zeros((320, 320, 3), dtype=np.uint8)
            self._model(blank, verbose=False)
            log.info("YOLO ready")
        except Exception as e:
            log.error(f"YOLO load failed: {e}")
            self._model = None

    # ==================== Inference ====================

    def _infer_yolo(self, frame: np.ndarray,
                    conf: float = 0.40) -> dict:
        """Run YOLOv8 on frame, return structured result."""
        # Resize to 320 for speed; quality acceptable for smart home
        small   = cv2.resize(frame, (320, 320))
        results = self._model(small, conf=conf, verbose=False)

        objects         = []
        person_detected = False
        fire_detected   = False
        max_conf        = 0.0

        for box in results[0].boxes:
            cls_id = int(box.cls[0])
            label  = self._model.names[cls_id]
            score  = float(box.conf[0])

            objects.append({"label": label, "confidence": round(score, 3)})
            max_conf = max(max_conf, score)

            if cls_id == self.PERSON_ID:
                person_detected = True
            if label in ("fire", "flame", "smoke"):
                fire_detected = True

        return {
            "person_detected": person_detected,
            "fire_detected":   fire_detected,
            "objects":         objects,
            "confidence":      round(max_conf, 3),
            "method":          "yolo",
        }

    def _infer_fallback(self, frame: np.ndarray) -> dict:
        """
        No YOLO: detect potential fire by orange-red HSV range.
        Crude but zero-dependency.
        """
        hsv  = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(
            hsv,
            np.array([0,  120, 120]),   # lower: red-orange
            np.array([25, 255, 255])    # upper: red-orange
        )
        ratio = float(np.sum(mask > 0)) / mask.size
        return {
            "person_detected": False,
            "fire_detected":   ratio > 0.05,
            "objects":         [],
            "confidence":      round(ratio, 3),
            "method":          "fallback_hsv",
        }

    def _analyze_frame(self, frame: np.ndarray) -> dict:
        base = {
            "timestamp":       time.time(),
            "frame_available": True,
            "person_detected": False,
            "fire_detected":   False,
            "objects":         [],
            "confidence":      0.0,
            "method":          "none",
        }
        try:
            if self._model is not None:
                detected = self._infer_yolo(frame)
            else:
                detected = self._infer_fallback(frame)
            base.update(detected)
        except Exception as e:
            log.error(f"Frame analysis error: {e}")
        return base

    # ==================== Single-frame grab (event-driven) ====================

    def capture_and_analyze(self) -> dict:
        """
        Grab one JPEG from /capture, analyze, return result.
        Use this for event-driven triggers (gas alert, voice command, etc.)
        Does NOT require stream to be running.
        """
        try:
            resp = requests.get(self._capture_url, timeout=5)
            if resp.status_code != 200:
                log.warning(f"Capture HTTP {resp.status_code}")
                return {"frame_available": False}

            arr   = np.frombuffer(resp.content, np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                log.warning("Frame decode failed")
                return {"frame_available": False}

            result = self._analyze_frame(frame)
            log.info(f"Capture analysis: person={result['person_detected']} "
                     f"fire={result['fire_detected']} "
                     f"objects={[o['label'] for o in result['objects']]} "
                     f"method={result['method']}")
            return result

        except requests.exceptions.Timeout:
            log.warning("Capture timeout - ESP32-CAM not responding")
            return {"frame_available": False}
        except Exception as e:
            log.error(f"Capture error: {e}")
            return {"frame_available": False}

    # ==================== Stream loop (continuous monitoring) ====================

    def start_stream_loop(self):
        """
        Background thread: pull MJPEG stream, analyze every N seconds,
        push result to data_bus for rule engine and LLM.
        """
        if self._running:
            log.warning("Vision stream loop already running")
            return

        self._running = True

        def _loop():
            cap        = None
            last_infer = 0

            while self._running:
                try:
                    # (re)connect stream
                    if cap is None or not cap.isOpened():
                        log.info(f"Connecting to {self._stream_url}")
                        cap = cv2.VideoCapture(self._stream_url)
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                        if not cap.isOpened():
                            log.warning("Stream not available, retry in 5s")
                            time.sleep(5)
                            continue

                    ret, frame = cap.read()
                    if not ret:
                        log.warning("Frame read failed, reconnecting...")
                        cap.release()
                        cap = None
                        time.sleep(2)
                        continue

                    # Rate-limit inference
                    now = time.time()
                    if now - last_infer < self._interval:
                        time.sleep(0.1)
                        continue

                    result    = self._analyze_frame(frame)
                    last_infer = now

                    # Update internal cache
                    with self._lock:
                        self._result = result

                    # Push to data_bus (imported lazily to avoid circular import)
                    self._push_to_data_bus(result)

                    # Log alerts
                    if result.get("person_detected") or result.get("fire_detected"):
                        log.warning(
                            f"VISION ALERT | person={result['person_detected']} "
                            f"fire={result['fire_detected']} "
                            f"conf={result['confidence']}"
                        )

                except Exception as e:
                    log.error(f"Vision stream loop error: {e}")
                    if cap:
                        cap.release()
                        cap = None
                    time.sleep(3)

            if cap:
                cap.release()
            log.info("Vision stream loop stopped")

        t = threading.Thread(target=_loop, daemon=True, name="vision_stream")
        t.start()
        log.info(f"Vision stream loop started "
                 f"(interval={self._interval}s, "
                 f"yolo={'yes' if self._model else 'no'})")

    def _push_to_data_bus(self, result: dict):
        try:
            from mqtt.broker_client import data_bus
            data_bus["esp32cam_vision"] = {
                "data":      {"data": {"vision": result}},
                "timestamp": time.time(),
            }
        except Exception:
            pass  # data_bus not available yet, skip silently

    def stop_stream_loop(self):
        self._running = False

    # ==================== Get latest result ====================

    def get_result(self) -> dict:
        """Return latest cached result (thread-safe)."""
        with self._lock:
            return dict(self._result)

    def get_semantic_summary(self) -> str:
        """
        Return a human-readable string for LLM context injection.
        Example: "Vision: person detected (conf=0.87), no fire"
        """
        r = self.get_result()
        if not r.get("frame_available", False):
            return "Vision: camera offline"

        parts = []
        if r.get("person_detected"):
            parts.append(f"person detected (conf={r['confidence']:.2f})")
        if r.get("fire_detected"):
            parts.append("fire/smoke detected")
        if r.get("objects"):
            labels = [o["label"] for o in r["objects"][:3]]
            parts.append(f"objects: {', '.join(labels)}")

        summary = "Vision: " + ("; ".join(parts) if parts else "no alerts, scene normal")
        return summary