import cv2
import time
import threading
import requests
import numpy as np
import logging
import os

from rknnlite.api import RKNNLite

log = logging.getLogger("vision")

class RKNNVisionAnalyzer:
    COCO_NAMES = {0: "person", 15: "cat", 16: "dog", 56: "chair", 67: "phone"}

    def __init__(self, capture_url: str, stream_url: str,
                 rknn_model_path: str,
                 inference_interval: float = 1.0,
                 save_dir: str = "storage/detections"):
        self._capture_url = capture_url
        self._stream_url  = stream_url
        self._interval    = inference_interval
        self._save_dir    = save_dir
        self._rknn        = None
        self._result      = {
            "frame_available": False, 
            "person_detected": False, 
            "confidence": 0.0,
            "method": "rknn_npu",
            "objects": []
        }
        self._lock        = threading.Lock()
        
        if not os.path.exists(self._save_dir):
            os.makedirs(self._save_dir, exist_ok=True)

        self._load_rknn(rknn_model_path)

    def _load_rknn(self, path: str):
        try:
            log.info(f"Loading RKNN model: {path}")
            self._rknn = RKNNLite(verbose=False)
            if self._rknn.load_rknn(path) != 0:
                raise RuntimeError("Failed to load RKNN model")
            if self._rknn.init_runtime(core_mask=RKNNLite.NPU_CORE_AUTO) != 0:
                raise RuntimeError("Failed to init RKNN runtime")
            log.info("RKNN NPU initialized successfully.")
        except Exception as e:
            log.error(f"RKNN Init Error: {e}")
            self._rknn = None

    def _preprocess(self, frame):
        """预处理：resize → RGB → float32归一化 → NHWC布局 → 确保内存连续"""
        img = cv2.resize(frame, (320, 320))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        # 转换为 float32 并归一化到 [0,1]（模型期望 float32 输入）
        img = img.astype(np.float32) / 255.0
        img = np.expand_dims(img, axis=0)          # (1, 320, 320, 3) NHWC
        # 【关键】保证内存连续，避免 RKNN 驱动读取错误
        img = np.ascontiguousarray(img)
        return img

    def _postprocess(self, outputs, conf_thresh=0.35, nms_thresh=0.45):
        # 如果推理失败，outputs 可能为 None 或空列表
        if outputs is None or len(outputs) == 0:
            return {
                "person_detected": False,
                "objects": [],
                "confidence": 0.0,
                "method": "rknn_npu",
                "frame_available": True
            }

        output = outputs[0]
        # 打印输出形状以便调试（可注释掉）
        log.debug(f"Output shape: {output.shape}")

        # 统一转为 (num_boxes, 84)
        if len(output.shape) == 3:
            output = output[0]          # (84, 2100) 或 (2100, 84)
        if output.shape[0] == 84:       # (84, 2100) -> (2100, 84)
            output = output.T

        boxes_raw = output[:, :4]
        scores_raw = output[:, 4:]

        # 自动检测输出是概率还是 logits
        raw_max = float(np.max(scores_raw))
        raw_min = float(np.min(scores_raw))

        if raw_max > 1.0 or raw_min < 0.0:
            # logits -> 应用 sigmoid
            scores = 1 / (1 + np.exp(-np.clip(scores_raw, -15, 15)))
            activated_max = float(np.max(scores))
            log.debug(f"[Probe] Logits mode. Raw max: {raw_max:.2f} -> Activated max: {activated_max:.3f}")
        else:
            # 已经是概率
            scores = scores_raw
            activated_max = raw_max
            log.debug(f"[Probe] Probability mode. Max conf: {activated_max:.3f}")

        confidences = np.max(scores, axis=1)
        class_ids = np.argmax(scores, axis=1)

        # 基础返回结构（始终包含 NPU 看到的最高置信度，便于调试）
        res = {
            "person_detected": False,
            "objects": [],
            "confidence": round(activated_max, 3),
            "method": "rknn_npu",
            "frame_available": True
        }

        mask = confidences > conf_thresh
        if not np.any(mask):
            return res

        sel_boxes = boxes_raw[mask]
        sel_confs = confidences[mask]
        sel_ids = class_ids[mask]

        boxes_list, confs_list, ids_list = [], [], []
        for i in range(len(sel_boxes)):
            cx, cy, w, h = sel_boxes[i]
            # 注意：此处坐标可能是绝对像素值（0~320），我们保留为绝对坐标供 NMS 使用
            x = int(cx - w/2)
            y = int(cy - h/2)
            boxes_list.append([x, y, int(w), int(h)])
            confs_list.append(float(sel_confs[i]))
            ids_list.append(int(sel_ids[i]))

        indices = cv2.dnn.NMSBoxes(boxes_list, confs_list, conf_thresh, nms_thresh)

        final_max_conf = 0.0
        if len(indices) > 0:
            for i in indices.flatten():
                label_id = ids_list[i]
                if label_id in self.COCO_NAMES:
                    label_name = self.COCO_NAMES[label_id]
                    conf = confs_list[i]
                    res["objects"].append({
                        "label": label_name,
                        "confidence": round(conf, 3),
                        "box": boxes_list[i]          # 保存原始像素坐标（在 320x320 图像上）
                    })
                    if label_id == 0:
                        res["person_detected"] = True
                        final_max_conf = max(final_max_conf, conf)

            if res["person_detected"]:
                res["confidence"] = final_max_conf

        return res

    def capture_and_analyze(self) -> dict:
        try:
            resp = requests.get(self._capture_url, timeout=3)
            if resp.status_code != 200:
                return {"frame_available": False, "method": "rknn_npu"}

            frame = cv2.imdecode(np.frombuffer(resp.content, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                return {"frame_available": False, "method": "rknn_npu"}

            input_data = self._preprocess(frame)
            # 执行推理，data_format 必须与输入布局匹配：NHWC
            outputs = self._rknn.inference(inputs=[input_data], data_format='nhwc')

            status = self._postprocess(outputs)

            if status.get("person_detected"):
                self._save_detection_image(frame, status)

            with self._lock:
                self._result = status
            return status

        except Exception as e:
            log.error(f"Analysis Error: {e}")
            return {
                "frame_available": False,
                "person_detected": False,
                "confidence": 0.0,
                "objects": [],
                "method": "rknn_npu",
                "error": str(e)
            }

    def _save_detection_image(self, frame, result):
        h, w = frame.shape[:2]
        for obj in result.get("objects", []):
            bx, by, bw, bh = obj["box"]          # 这些坐标是在 320x320 图像上的绝对坐标
            # 映射回原始图像尺寸
            x1 = max(0, int(bx * w / 320))
            y1 = max(0, int(by * h / 320))
            x2 = min(w, int((bx + bw) * w / 320))
            y2 = min(h, int((by + bh) * h / 320))
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.putText(frame, f"{obj['label']} {obj['confidence']:.2f}", (x1, y1-10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

        path = os.path.join(self._save_dir, f"alert_{int(time.time())}.jpg")
        cv2.imwrite(path, frame)

    def get_semantic_summary(self) -> str:
        with self._lock:
            r = dict(self._result)

        if not r.get("frame_available"):
            return "Vision: camera offline"

        max_conf = r.get('confidence', 0.0)

        if r.get("person_detected"):
            return f"Vision: person detected (conf={max_conf:.3f})"
        else:
            return f"Vision: normal (max_NPU_conf_seen={max_conf:.3f})"

    def release(self):
        if self._rknn:
            self._rknn.release()