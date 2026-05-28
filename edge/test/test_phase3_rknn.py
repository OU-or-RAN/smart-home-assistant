import sys, time, os
sys.path.insert(0, ".")
from vision.analyzer import RKNNVisionAnalyzer

# 获取绝对路径
model_abs = os.path.abspath("models/yolov8n_rk3588.rknn")
print(f"Loading model from: {model_abs}")
print(f"File exists? {os.path.exists(model_abs)}")

va = RKNNVisionAnalyzer(
    capture_url     = "http://172.20.10.4/capture",
    stream_url      = "http://172.20.10.4/stream",
    rknn_model_path = model_abs,   # 传入绝对路径
)

t0     = time.time()
result = va.capture_and_analyze()
elapsed= time.time() - t0

print(f"Method  : {result.get('method')}")
print(f"Latency : {elapsed*1000:.1f} ms")
print(f"Person  : {result.get('person_detected')}")
print(f"Fire    : {result.get('fire_detected')}")
print(f"Objects : {result.get('objects')}")
print(f"Summary : {va.get_semantic_summary()}")
va.release()
