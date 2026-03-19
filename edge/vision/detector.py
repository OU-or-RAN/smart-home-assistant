"""
edge/vision/detector.py

支持两种模型格式：
  A) airockchip/ultralytics_yolov8 导出的9头 RKNN 模型（推荐，INT8量化无精度损失）
     输出: [box80, scr80, sum80, box40, scr40, sum40, box20, scr20, sum20]
  B) 标准 ultralytics 导出的单头 ONNX 模型（开发调试用，CPU fallback）
     输出: [(1, 84, 8400)]

模型类型自动识别：推理后检查 len(outputs)，9个为A，1个为B。
"""

import cv2
import numpy as np
import logging
import time
from typing import List, Dict, Any, Tuple

log = logging.getLogger("detector")

COCO_CLASSES = [
    "person","bicycle","car","motorcycle","airplane","bus","train","truck","boat",
    "traffic light","fire hydrant","stop sign","parking meter","bench","bird","cat",
    "dog","horse","sheep","cow","elephant","bear","zebra","giraffe","backpack",
    "umbrella","handbag","tie","suitcase","frisbee","skis","snowboard","sports ball",
    "kite","baseball bat","baseball glove","skateboard","surfboard","tennis racket",
    "bottle","wine glass","cup","fork","knife","spoon","bowl","banana","apple",
    "sandwich","orange","broccoli","carrot","hot dog","pizza","donut","cake","chair",
    "couch","potted plant","bed","dining table","toilet","tv","laptop","mouse",
    "remote","keyboard","cell phone","microwave","oven","toaster","sink",
    "refrigerator","book","clock","vase","scissors","teddy bear","hair drier",
    "toothbrush",
]

INPUT_SIZE  = 640
CONF_THRESH = 0.45   # airockchip模型scores未经sigmoid，阈值可适当降低
IOU_THRESH  = 0.35

# 框过滤参数
MIN_AREA_RATIO = 0.005
MAX_AREA_RATIO = 0.80
MIN_ASPECT     = 0.15
MAX_ASPECT     = 3.5

# 过曝抑制参数
OVEREXP_THRESH   = 235
OVEREXP_MIN_AREA = 0.02
OVEREXP_FILL     = 114


# ──────────────────────────────────────────────────────────────────────────────
# 过曝区域抑制
# ──────────────────────────────────────────────────────────────────────────────

def _suppress_overexposure(rgb: np.ndarray) -> np.ndarray:
    """将大块过曝白色区域替换为灰色(114)，消除幻觉框。"""
    gray    = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    _, mask = cv2.threshold(gray, OVEREXP_THRESH, 255, cv2.THRESH_BINARY)
    kernel  = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    mask    = cv2.dilate(mask, kernel, iterations=2)
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        mask, connectivity=8)
    img_area = rgb.shape[0] * rgb.shape[1]
    result   = rgb.copy()
    for i in range(1, num_labels):
        if stats[i, cv2.CC_STAT_AREA] > img_area * OVEREXP_MIN_AREA:
            result[labels == i] = OVEREXP_FILL
    return result


# ──────────────────────────────────────────────────────────────────────────────
# 预处理：letterbox
# ──────────────────────────────────────────────────────────────────────────────

def _letterbox(img_bgr: np.ndarray,
               size: int = INPUT_SIZE) -> Tuple[np.ndarray, float, int, int]:
    """
    等比例 letterbox resize，填充灰色(114)。
    返回 RGB uint8 canvas + (scale, pad_left, pad_top)
    """
    h, w  = img_bgr.shape[:2]
    scale = size / max(h, w)
    nw    = int(w * scale)
    nh    = int(h * scale)
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    pt     = (size - nh) // 2
    pl     = (size - nw) // 2
    canvas[pt:pt+nh, pl:pl+nw] = cv2.resize(
        img_bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
    rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
    return rgb, scale, pl, pt


# ──────────────────────────────────────────────────────────────────────────────
# 后处理 A：airockchip 9头模型（RKNN INT8）
# ──────────────────────────────────────────────────────────────────────────────

def _decode_9heads(outputs: list,
                   scale: float, pl: int, pt: int,
                   orig_w: int, orig_h: int) -> List[Dict[str, Any]]:
    """
    airockchip/ultralytics_yolov8 9头输出解码。

    输出顺序（每个尺度3个）：
      outputs[0]: (1, 64, 80, 80)  boxes  stride=8
      outputs[1]: (1, 80, 80, 80)  scores stride=8
      outputs[2]: (1,  1, 80, 80)  cls_sum stride=8  （快速过滤，可忽略）
      outputs[3]: (1, 64, 40, 40)  boxes  stride=16
      outputs[4]: (1, 80, 40, 40)  scores stride=16
      outputs[5]: (1,  1, 40, 40)  cls_sum stride=16
      outputs[6]: (1, 64, 20, 20)  boxes  stride=32
      outputs[7]: (1, 80, 20, 20)  scores stride=32
      outputs[8]: (1,  1, 20, 20)  cls_sum stride=32

    scores 已是 sigmoid 概率（airockchip 模型包含 sigmoid 层）。
    boxes 是 DFL 分布，需要 softmax + 加权求和解码为 ltrb。
    """
    REG      = 16   # dfl reg_max，固定值
    strides  = [8,  16, 32]
    box_idx  = [0,   3,  6]
    scr_idx  = [1,   4,  7]

    all_x1, all_y1, all_x2, all_y2 = [], [], [], []
    all_confs, all_cls = [], []
    proj = np.arange(REG, dtype=np.float32)   # [0,1,2,...,15]

    for i, stride in enumerate(strides):
        raw_box = outputs[box_idx[i]][0]   # (64, H, W)
        raw_scr = outputs[scr_idx[i]][0]   # (80, H, W)
        _, H, W = raw_scr.shape

        # ── 分类置信度 ────────────────────────────────────────────────
        scores  = raw_scr.reshape(80, -1).T          # (H*W, 80)
        cls_ids = scores.argmax(axis=1)
        confs   = scores[np.arange(H * W), cls_ids]  # (H*W,)

        mask = confs > CONF_THRESH
        if not mask.any():
            continue

        # ── DFL 框解码 ────────────────────────────────────────────────
        # raw_box: (64, H, W) → (H*W, 4, 16)
        dfl  = raw_box.reshape(4, REG, -1).transpose(2, 0, 1)  # (H*W,4,16)
        dfl  = dfl[mask]                                         # (N, 4, 16)
        # softmax over reg_max axis
        dfl  = dfl - dfl.max(axis=2, keepdims=True)             # 数值稳定
        exp  = np.exp(dfl)
        soft = exp / exp.sum(axis=2, keepdims=True)             # (N, 4, 16)
        ltrb = (soft * proj).sum(axis=2) * stride               # (N, 4) px

        # ── 格点坐标 → 原图坐标 ───────────────────────────────────────
        grid_idx = np.where(mask)[0]
        gy = (grid_idx // W + 0.5) * stride
        gx = (grid_idx  % W + 0.5) * stride

        x1 = np.clip((gx - ltrb[:, 0] - pl) / scale, 0, orig_w).astype(int)
        y1 = np.clip((gy - ltrb[:, 1] - pt) / scale, 0, orig_h).astype(int)
        x2 = np.clip((gx + ltrb[:, 2] - pl) / scale, 0, orig_w).astype(int)
        y2 = np.clip((gy + ltrb[:, 3] - pt) / scale, 0, orig_h).astype(int)

        all_x1.append(x1); all_y1.append(y1)
        all_x2.append(x2); all_y2.append(y2)
        all_confs.append(confs[mask])
        all_cls.append(cls_ids[mask])

    if not all_confs:
        return []

    x1      = np.concatenate(all_x1)
    y1      = np.concatenate(all_y1)
    x2      = np.concatenate(all_x2)
    y2      = np.concatenate(all_y2)
    confs   = np.concatenate(all_confs)
    cls_ids = np.concatenate(all_cls)

    # ── NMS ──────────────────────────────────────────────────────────
    nms_in  = np.stack([x1, y1, x2-x1, y2-y1], axis=1).tolist()
    indices = cv2.dnn.NMSBoxes(nms_in, confs.tolist(), CONF_THRESH, IOU_THRESH)

    img_area = orig_w * orig_h
    results  = []
    for idx in (indices.flatten() if len(indices) else []):
        w_box  = int(x2[idx]) - int(x1[idx])
        h_box  = int(y2[idx]) - int(y1[idx])
        area   = w_box * h_box
        aspect = w_box / (h_box + 1e-6)
        if area   < img_area * MIN_AREA_RATIO: continue
        if area   > img_area * MAX_AREA_RATIO: continue
        if aspect < MIN_ASPECT or aspect > MAX_ASPECT: continue
        results.append({
            "label": COCO_CLASSES[int(cls_ids[idx])],
            "conf":  round(float(confs[idx]), 3),
            "bbox":  [int(x1[idx]), int(y1[idx]),
                      int(x2[idx]), int(y2[idx])],
        })
    return results


# ──────────────────────────────────────────────────────────────────────────────
# 后处理 B：标准单头模型（ONNX CPU fallback）(1, 84, 8400)
# ──────────────────────────────────────────────────────────────────────────────

def _decode_1head(raw: np.ndarray,
                  scale: float, pl: int, pt: int,
                  orig_w: int, orig_h: int) -> List[Dict[str, Any]]:
    """
    标准 ultralytics 导出的单输出模型解码。
    raw : (1, 84, 8400)
      [:4]  = boxes (cx cy w h，INPUT_SIZE 空间)
      [4:]  = scores (80类，已是 sigmoid 概率，直接用)
    """
    pred    = raw[0].T         # (8400, 84)
    boxes   = pred[:, :4]
    scores  = pred[:, 4:]      # 已是概率

    cls_ids = np.argmax(scores, axis=1)
    confs   = scores[np.arange(8400), cls_ids]

    mask = confs > CONF_THRESH
    if not mask.any():
        return []

    boxes   = boxes[mask]
    confs   = confs[mask]
    cls_ids = cls_ids[mask]

    bx, by, bw, bh = boxes[:,0], boxes[:,1], boxes[:,2], boxes[:,3]
    x1 = np.clip((bx - bw/2 - pl) / scale, 0, orig_w).astype(int)
    y1 = np.clip((by - bh/2 - pt) / scale, 0, orig_h).astype(int)
    x2 = np.clip((bx + bw/2 - pl) / scale, 0, orig_w).astype(int)
    y2 = np.clip((by + bh/2 - pt) / scale, 0, orig_h).astype(int)

    nms_in  = np.stack([x1, y1, x2-x1, y2-y1], axis=1).tolist()
    indices = cv2.dnn.NMSBoxes(nms_in, confs.tolist(), CONF_THRESH, IOU_THRESH)

    img_area = orig_w * orig_h
    results  = []
    for idx in (indices.flatten() if len(indices) else []):
        w_box  = int(x2[idx]) - int(x1[idx])
        h_box  = int(y2[idx]) - int(y1[idx])
        area   = w_box * h_box
        aspect = w_box / (h_box + 1e-6)
        if area   < img_area * MIN_AREA_RATIO: continue
        if area   > img_area * MAX_AREA_RATIO: continue
        if aspect < MIN_ASPECT or aspect > MAX_ASPECT: continue
        results.append({
            "label": COCO_CLASSES[int(cls_ids[idx])],
            "conf":  round(float(confs[idx]), 3),
            "bbox":  [int(x1[idx]), int(y1[idx]),
                      int(x2[idx]), int(y2[idx])],
        })
    return results


# ──────────────────────────────────────────────────────────────────────────────
# 主类
# ──────────────────────────────────────────────────────────────────────────────

class YOLOv8Detector:

    def __init__(self, model_path: str,
                 use_rknn: bool = True,
                 suppress_overexp: bool = True):
        """
        model_path       : .rknn（RKNN模式）或 .onnx（ONNX模式）
        use_rknn         : True = 优先 RKNN NPU，失败自动 fallback 到 ONNX CPU
        suppress_overexp : True = 开启过曝抑制预处理
        """
        self._path             = model_path
        self._use_rknn         = use_rknn
        self._suppress_overexp = suppress_overexp
        self._model            = None
        self._ort_in           = None
        # 运行时确定输出头数（9=airockchip, 1=standard）
        self._n_outputs        = None
        self._load()

    # ── 加载 ──────────────────────────────────────────────────────────────────

    def _load(self):
        if self._use_rknn:
            try:
                self._load_rknn()
                return
            except Exception as e:
                log.warning(f"RKNN load failed ({e}), fallback → ONNX CPU")
                print(f"[Detector] RKNN failed ({e}), → ONNX CPU")
                self._use_rknn = False
        self._load_onnx()

    def _load_rknn(self):
        from rknnlite.api import RKNNLite
        m = RKNNLite(verbose=False)
        assert m.load_rknn(self._path) == 0,        "load_rknn failed"
        assert m.init_runtime(
            core_mask=RKNNLite.NPU_CORE_AUTO) == 0, "init_runtime failed"
        self._model = m
        print(f"[Detector] Backend         : RKNN NPU")
        print(f"[Detector] Overexp suppress: {self._suppress_overexp}")
        log.info(f"RKNN loaded: {self._path}")

    def _load_onnx(self):
        import onnxruntime as ort
        # .rknn → .onnx 自动替换路径
        path = self._path
        if path.endswith(".rknn"):
            path = path.replace(".rknn", ".onnx")
        sess = ort.InferenceSession(
            path, providers=["CPUExecutionProvider"])
        self._model  = sess
        self._ort_in = sess.get_inputs()[0].name
        print(f"[Detector] Backend         : ONNX CPU  ({path})")
        print(f"[Detector] Overexp suppress: {self._suppress_overexp}")
        log.info(f"ONNX loaded: {path}")

    # ── 推理 ──────────────────────────────────────────────────────────────────

    def infer(self, img_bgr: np.ndarray) -> Dict[str, Any]:
        """
        输入  : BGR numpy array，任意分辨率
        输出  : {
                  "objects":    [{"label":str, "conf":float, "bbox":[x1,y1,x2,y2]}, ...],
                  "latency_ms": float
                }

        预处理：
          RKNN (airockchip INT8) → 输入 uint8 NHWC，runtime内部做归一化
          ONNX (标准单头)        → 输入 float32 NCHW，外部做 /255
        """
        orig_h, orig_w = img_bgr.shape[:2]

        # 1. letterbox
        rgb, scale, pl, pt = _letterbox(img_bgr)

        # 2. 过曝抑制
        if self._suppress_overexp:
            rgb = _suppress_overexposure(rgb)

        # 3. 按 backend 准备输入张量
        if self._use_rknn:
            # airockchip INT8 RKNN：uint8 NHWC
            # runtime 根据 mean/std 配置内部做归一化，外部送 0~255 即可
            inp = rgb[np.newaxis]                             # (1,640,640,3) uint8
        else:
            # 标准 ONNX：float32 NCHW，外部 /255
            inp = (rgb.astype(np.float32) / 255.0).transpose(2, 0, 1)[np.newaxis]

        # 4. 推理计时
        t0 = time.perf_counter()
        if self._use_rknn:
            outputs = self._model.inference(inputs=[inp])
        else:
            outputs = self._model.run(None, {self._ort_in: inp})
        latency_ms = (time.perf_counter() - t0) * 1000

        # 5. 首次推理时确定输出头数，并打印
        if self._n_outputs is None:
            self._n_outputs = len(outputs)
            print(f"[Detector] Output heads    : {self._n_outputs} "
                  f"({'airockchip 9-head' if self._n_outputs == 9 else 'standard 1-head'})")
            for i, o in enumerate(outputs):
                log.debug(f"  output[{i}] shape={o.shape} "
                          f"dtype={o.dtype} max={o.max():.3f}")

        # 6. 后处理（自动识别模型类型）
        if self._n_outputs == 9:
            # airockchip 9头模型：完整 outputs 列表传入
            objects = _decode_9heads(outputs, scale, pl, pt, orig_w, orig_h)
        else:
            # 标准单头模型：outputs[0] 是 (1,84,8400)
            objects = _decode_1head(outputs[0], scale, pl, pt, orig_w, orig_h)

        return {
            "objects":    objects,
            "latency_ms": round(latency_ms, 1),
        }

    # ── 参数热调整 ────────────────────────────────────────────────────────────

    def set_conf_thresh(self, thresh: float):
        global CONF_THRESH
        CONF_THRESH = thresh
        log.info(f"CONF_THRESH → {thresh}")

    def set_suppress_overexp(self, enabled: bool):
        self._suppress_overexp = enabled
        log.info(f"Overexp suppress → {enabled}")

    # ── 释放 ──────────────────────────────────────────────────────────────────

    def release(self):
        if self._model and self._use_rknn:
            try:
                self._model.release()
            except Exception:
                pass
        self._model = None
        log.info("Detector released")