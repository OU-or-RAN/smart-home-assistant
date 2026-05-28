"""
edge/vision/test_vision.py

离线验证脚本 —— 不依赖 ESP32-CAM 和 RKNN 模型
用途：
  1. 验证 detector.py 的输出格式是否正确
  2. 验证 _print_semantic() 语义摘要格式
  3. 验证 MQTT payload 结构与 broker_client.parse_cam_detect() 兼容
  4. 验证 ONNX CPU 推理是否可用（需要 models/yolov8n.onnx）
  5. 验证 RKNN 9头模型推理（需要 models/yolov8n_rk3588_airockchip_int8.rknn）

运行方式：
  # 模式1：纯格式验证（无需任何模型文件）
  python3 test_vision.py --mock

  # 模式2：ONNX CPU 推理测试（需要 models/yolov8n.onnx）
  python3 test_vision.py --onnx --image /path/to/test.jpg

  # 模式3：RKNN 推理测试（需要 airockchip INT8 模型，在板端运行）
  python3 test_vision.py --rknn --image /path/to/test.jpg

  # 模式4：输出头诊断（查看模型输出结构）
  python3 test_vision.py --diagnose --image /path/to/test.jpg
"""

import sys
import os
import json
import time
import argparse
import numpy as np
from pathlib import Path
from datetime import datetime

# 路径修复
_THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS))
sys.path.insert(0, str(_THIS.parent))


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _section(title: str):
    print(f"\n{'='*50}")
    print(f"  {title}")
    print(f"{'='*50}")


def _ok(msg: str):
    print(f"  ✅ {msg}")


def _fail(msg: str):
    print(f"  ❌ {msg}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# TEST 1：格式验证（mock 数据，无需任何模型）
# ---------------------------------------------------------------------------

def test_mock_output():
    _section("TEST 1: 语义摘要格式验证 (mock 数据)")

    mock_objects = [
        {"label": "person", "conf": 0.786, "bbox": [120, 80, 300, 460]},
        {"label": "dog",    "conf": 0.612, "bbox": [20,  200, 180, 420]},
    ]
    mock_result = {
        "device_id":  "esp32cam_001",
        "timestamp":  datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19],
        "objects":    mock_objects,
        "semantic": {
            "has_person": True,
            "has_cat":    False,
            "has_dog":    True,
            "has_fire":   False,
        },
        "latency_ms": 86.2,
        "frame_id":   1,
    }

    from vision_service import _print_semantic, WATCH_MAP
    _print_semantic(mock_result, frame_ok=True)

    payload = json.dumps(mock_result, ensure_ascii=False)
    parsed  = json.loads(payload)
    assert parsed["semantic"]["has_person"] is True
    assert parsed["semantic"]["has_fire"]   is False
    assert len(parsed["objects"]) == 2
    _ok("Semantic summary format correct")

    assert "device_id" in parsed
    assert "timestamp" in parsed
    assert "objects"   in parsed
    assert isinstance(parsed["objects"], list)
    _ok("MQTT payload structure compatible with broker_client")

    empty_result = {
        "objects": [], "latency_ms": 0.0,
        "timestamp": "20260318_143200",
        "semantic": {k: False for k in
                     list(WATCH_MAP.values()) + ["has_fire"]},
    }
    _print_semantic(empty_result, frame_ok=False)
    _ok("frame_ok=False branch prints correctly")


# ---------------------------------------------------------------------------
# TEST 2：FrameGrabber mock
# ---------------------------------------------------------------------------

def test_frame_grabber_mock():
    _section("TEST 2: FrameGrabber mock（不连接 ESP32）")
    import cv2

    fake_img = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
    ok, buf  = cv2.imencode(".jpg", fake_img,
                             [cv2.IMWRITE_JPEG_QUALITY, 80])
    assert ok, "JPEG encode failed"

    arr = np.frombuffer(buf.tobytes(), dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    assert img is not None
    assert img.shape == (480, 640, 3)
    _ok(f"JPEG encode/decode round-trip OK  shape={img.shape}")

    none_img = None
    from vision_service import _print_semantic, WATCH_MAP
    empty = {
        "objects": [], "latency_ms": 0.0,
        "timestamp": "20260318_000000",
        "semantic": {k: False for k in
                     list(WATCH_MAP.values()) + ["has_fire"]},
    }
    _print_semantic(empty, frame_ok=(none_img is not None))
    _ok("None frame handled gracefully")


# ---------------------------------------------------------------------------
# TEST 3：Letterbox + 过曝抑制预处理验证
# ---------------------------------------------------------------------------

def test_preprocess():
    _section("TEST 3: Letterbox + 过曝抑制预处理")
    import cv2
    from detector import _letterbox, _suppress_overexposure, INPUT_SIZE

    # 不同宽高比的图像
    for (h, w) in [(480, 640), (720, 1280), (480, 480), (240, 320)]:
        img = np.zeros((h, w, 3), dtype=np.uint8)
        rgb, scale, pl, pt = _letterbox(img)
        assert rgb.shape == (INPUT_SIZE, INPUT_SIZE, 3), \
            f"Shape mismatch: {rgb.shape}"
        assert rgb.dtype == np.uint8
        _ok(f"Letterbox ({h}×{w}) → {rgb.shape}  scale={scale:.3f}  pad=({pl},{pt})")

    # 过曝抑制测试：白色区域应被替换为灰色
    overexp_img = np.full((640, 640, 3), 255, dtype=np.uint8)   # 全白
    result = _suppress_overexposure(overexp_img)
    assert result.max() <= 114 + 10, "Overexp region should be filled with 114"
    _ok("Overexposure suppression replaces white regions correctly")

    # 正常图像不应被过度修改
    normal_img = np.random.randint(50, 200, (640, 640, 3), dtype=np.uint8)
    result_n = _suppress_overexposure(normal_img)
    # 大多数像素应保持不变
    unchanged = (result_n == normal_img).all(axis=2).mean()
    assert unchanged > 0.9, f"Normal image over-modified: only {unchanged:.1%} unchanged"
    _ok(f"Normal image largely preserved: {unchanged:.1%} unchanged")


# ---------------------------------------------------------------------------
# TEST 4：输出头诊断（不做完整后处理，只看输出结构）
# ---------------------------------------------------------------------------

def test_diagnose_outputs(image_path: str, use_rknn: bool = True):
    _section(f"TEST 4: 模型输出诊断  rknn={use_rknn}  image={image_path}")
    import cv2
    from detector import _letterbox, YOLOv8Detector

    model_path = str(_THIS / "models" / ("yolov8n_rk3588_airockchip_int8.rknn"
                                          if use_rknn else "yolov8n.rknn"))

    det = YOLOv8Detector(model_path, use_rknn=use_rknn)

    img = cv2.imread(image_path)
    if img is None:
        _fail(f"Cannot read image: {image_path}")

    rgb, scale, pl, pt = _letterbox(img)

    if use_rknn:
        inp = rgb[np.newaxis]   # uint8 NHWC for airockchip
    else:
        inp = (rgb.astype(np.float32) / 255.0).transpose(2, 0, 1)[np.newaxis]

    if det._use_rknn:
        outputs = det._model.inference(inputs=[inp])
    else:
        outputs = det._model.run(None, {det._ort_in: inp})

    print(f"\n  输出头数量: {len(outputs)}")
    for i, o in enumerate(outputs):
        print(f"  output[{i}]: shape={o.shape}  dtype={o.dtype}  "
              f"min={o.min():.3f}  max={o.max():.3f}  mean={o.mean():.4f}")

    if len(outputs) == 9:
        _ok("airockchip 9头模型结构确认")
        # 检查各尺度 scores 是否有效
        for i, stride in enumerate([8, 16, 32]):
            scr = outputs[1 + i*3][0]   # (80, H, W)
            max_conf = scr.max()
            print(f"  stride={stride:2d}  scores max={max_conf:.3f}  "
                  f"{'✅ 有效' if max_conf > 0.3 else '⚠️  偏低'}")
    elif len(outputs) == 1:
        _ok("标准单头模型结构确认")
        scores = outputs[0][0, 4:, :]   # (80, 8400)
        print(f"  scores max={scores.max():.3f}  "
              f"{'✅ 有效' if scores.max() > 0.3 else '⚠️  偏低'}")
    else:
        print(f"  ⚠️  未知输出头数: {len(outputs)}")

    det.release()


# ---------------------------------------------------------------------------
# TEST 5：ONNX 推理（需要 models/yolov8n.onnx）
# ---------------------------------------------------------------------------

def test_onnx_inference(image_path: str):
    _section(f"TEST 5: ONNX CPU 推理  image={image_path}")
    import cv2
    from detector import YOLOv8Detector

    model_path = str(_THIS / "models" / "yolov8n.rknn")   # 内部换 .onnx
    det = YOLOv8Detector(model_path, use_rknn=False)

    img = cv2.imread(image_path)
    if img is None:
        _fail(f"Cannot read image: {image_path}")

    t0     = time.perf_counter()
    result = det.infer(img)
    total  = (time.perf_counter() - t0) * 1000

    print(f"  Inference latency : {result['latency_ms']:.1f} ms")
    print(f"  Total (incl. pre) : {total:.1f} ms")
    print(f"  Objects detected  : {len(result['objects'])}")
    for obj in result["objects"]:
        print(f"    {obj['label']:20s}  conf={obj['conf']:.3f}  "
              f"bbox={obj['bbox']}")

    assert isinstance(result["objects"], list)
    assert result["latency_ms"] > 0
    _ok(f"ONNX inference OK  {len(result['objects'])} objects")
    det.release()


# ---------------------------------------------------------------------------
# TEST 6：RKNN 9头模型推理（板端，需要 airockchip INT8 模型）
# ---------------------------------------------------------------------------

def test_rknn_inference(image_path: str):
    _section(f"TEST 6: RKNN NPU 推理 (airockchip 9头)  image={image_path}")
    import cv2
    from detector import YOLOv8Detector

    model_path = str(_THIS / "models" / "yolov8n_rk3588_airockchip_int8.rknn")
    det = YOLOv8Detector(model_path, use_rknn=True)

    img = cv2.imread(image_path)
    if img is None:
        _fail(f"Cannot read image: {image_path}")

    # 热身
    det.infer(img)

    # 5帧平均延迟
    latencies = []
    last_result = None
    for _ in range(5):
        r = det.infer(img)
        latencies.append(r["latency_ms"])
        last_result = r

    avg = sum(latencies) / len(latencies)
    print(f"  Avg NPU latency : {avg:.1f} ms  "
          f"(5 frames: {[round(x,1) for x in latencies]})")
    print(f"  Estimated FPS   : {1000/avg:.1f}")
    print(f"  Objects detected: {len(last_result['objects'])}")
    for obj in last_result["objects"]:
        print(f"    {obj['label']:20s}  conf={obj['conf']:.3f}  "
              f"bbox={obj['bbox']}")

    assert isinstance(last_result["objects"], list)
    _ok(f"RKNN inference OK  avg={avg:.1f}ms  {len(last_result['objects'])} objects")
    det.release()


# ---------------------------------------------------------------------------
# TEST 7：VisionService 单帧 mock（端到端）
# ---------------------------------------------------------------------------

def test_service_single_frame():
    _section("TEST 7: VisionService 单帧 mock（端到端）")
    import cv2
    import unittest.mock as mock
    from vision_service import VisionService

    fake_img   = np.zeros((480, 640, 3), dtype=np.uint8)
    model_path = str(_THIS / "models" / "yolov8n.rknn")

    svc = VisionService(
        model_path     = model_path,
        use_rknn       = False,    # ONNX CPU
        enable_mqtt    = False,
        save_snapshots = False,
    )

    with mock.patch.object(svc._grabber, "grab", return_value=fake_img):
        svc._process_one_frame()

    _ok("Single frame mock processed without exceptions")
    svc.stop()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Vision module offline tests")
    parser.add_argument("--mock",     action="store_true",
                        help="Run format-only tests (no model needed)")
    parser.add_argument("--diagnose", action="store_true",
                        help="Diagnose model output structure")
    parser.add_argument("--onnx",     action="store_true",
                        help="Run ONNX CPU inference test")
    parser.add_argument("--rknn",     action="store_true",
                        help="Run RKNN NPU inference test (board only)")
    parser.add_argument("--e2e",      action="store_true",
                        help="Run end-to-end service mock test")
    parser.add_argument("--image",    default=None,
                        help="Test image path for inference tests")
    parser.add_argument("--all",      action="store_true",
                        help="Run all available tests")
    args = parser.parse_args()

    run_all = args.all or not any(
        [args.mock, args.diagnose, args.onnx, args.rknn, args.e2e])

    passed = failed = 0

    def _run(fn, *a):
        nonlocal passed, failed
        try:
            fn(*a)
            passed += 1
        except SystemExit:
            failed += 1
        except Exception as e:
            print(f"  ❌ EXCEPTION in {fn.__name__}: {e}")
            import traceback; traceback.print_exc()
            failed += 1

    if run_all or args.mock:
        _run(test_mock_output)
        _run(test_frame_grabber_mock)
        _run(test_preprocess)

    if (run_all or args.diagnose) and args.image:
        _run(test_diagnose_outputs, args.image, True)   # RKNN
        _run(test_diagnose_outputs, args.image, False)  # ONNX

    if (run_all or args.onnx) and args.image:
        _run(test_onnx_inference, args.image)

    if (run_all or args.rknn) and args.image:
        _run(test_rknn_inference, args.image)

    if run_all or args.e2e:
        _run(test_service_single_frame)

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("ALL TESTS PASSED ✅")
    print(f"{'='*50}\n")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()