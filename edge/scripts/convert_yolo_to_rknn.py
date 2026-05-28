# convert_yolo_to_rknn.py
# Run on Windows inside rknn_convert venv

import os
import urllib.request
import numpy as np
from ultralytics import YOLO
from rknn.api import RKNN

MODEL_NAME   = "yolov8n"
ONNX_PATH    = f"{MODEL_NAME}.onnx"
RKNN_PATH    = f"{MODEL_NAME}_320_rk3588.rknn"
INPUT_SIZE   = 320

# ==================== Step 1: Export ONNX ====================

print("Step 1: Exporting YOLOv8n to ONNX ...")
model = YOLO(f"{MODEL_NAME}.pt")
model.export(
    format  = "onnx",
    imgsz   = INPUT_SIZE,
    opset   = 12,          # RKNN requires opset <= 12
    simplify= True,
    dynamic = False,
)
print(f"ONNX saved: {ONNX_PATH}")

# ==================== Step 2: Prepare calibration images ====================
# RKNN quantization needs a small set of representative images.
# We generate synthetic ones if you have no real dataset.

DATASET_DIR = "calibration_images"
os.makedirs(DATASET_DIR, exist_ok=True)
dataset_file = "dataset.txt"

if not os.path.exists(dataset_file) or os.path.getsize(dataset_file) == 0:
    print("Step 2: Generating synthetic calibration images ...")
    import cv2
    paths = []
    for i in range(20):
        img  = np.random.randint(0, 255,
               (INPUT_SIZE, INPUT_SIZE, 3), dtype=np.uint8)
        path = os.path.join(DATASET_DIR, f"calib_{i:03d}.jpg")
        cv2.imwrite(path, img)
        paths.append(path)
    with open(dataset_file, "w") as f:
        f.write("\n".join(paths))
    print(f"  Generated {len(paths)} calibration images")
else:
    print(f"Step 2: Using existing {dataset_file}")

# ==================== Step 3: Convert to RKNN ====================

print("Step 3: Converting to RKNN ...")
rknn = RKNN(verbose=False)

rknn.config(
    mean_values        = [[0, 0, 0]],
    std_values         = [[255, 255, 255]],
    target_platform    = "rk3588",
    optimization_level = 3,
    quantized_dtype    = "asymmetric_quantized-8",
)

ret = rknn.load_onnx(model=ONNX_PATH)
assert ret == 0, f"load_onnx failed: {ret}"

ret = rknn.build(do_quantization=True, dataset=dataset_file)
assert ret == 0, f"build failed: {ret}"

ret = rknn.export_rknn(RKNN_PATH)
assert ret == 0, f"export_rknn failed: {ret}"

rknn.release()

size_mb = os.path.getsize(RKNN_PATH) / 1024 / 1024
print(f"\nDone: {RKNN_PATH}  ({size_mb:.1f} MB)")
print("Transfer to LubanCat4:")
print(f"  scp {RKNN_PATH} root@172.20.10.2:/home/lubancat/smart_home/models/")