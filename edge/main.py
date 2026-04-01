# edge/main.py
"""
系统主入口，启动所有模块。
运行方式：
  cd /home/lubancat/smart_home
  python3 edge/main.py
"""
import sys
import time
import logging
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mqtt.broker_client import (
    data_bus, db_init, client as mqtt_client_ref,
    on_connect, on_message, on_disconnect,
    BROKER_HOST, BROKER_PORT
)
from rule_engine.engine import RuleEngine
from vision.vision_service import VisionService
from voice.voice_module import VoiceModule
from llm.llm_client import RKLLMClient
from llm.multimodal_agent import MultimodalAgent

import paho.mqtt.client as mqtt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

CONFIG = {
    "rules_path"   : "edge/config/rules/safety_rules.yaml",
    "rknn_model"   : "edge/vision/models/yolov8n_rk3588_airockchip_int8.rknn",
    "capture_url"  : "http://172.20.10.4/capture",
    "llm_url"      : "http://localhost:8080",
    "db_path"      : "/home/lubancat/smart_home/data/smart_home.db",
}


def main():
    # 1. 数据库
    db_init()

    # 2. MQTT
    import mqtt.broker_client as bc
    bc.client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION1,
        client_id="smart_home_main")
    bc.client.on_connect    = on_connect
    bc.client.on_message    = on_message
    bc.client.on_disconnect = on_disconnect
    bc.client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
    bc.client.loop_start()
    log.info("MQTT connected")
    time.sleep(1)

    # 3. 规则引擎（后台循环）
    engine = RuleEngine(
        CONFIG["rules_path"],
        db=bc.db_log_event,
        mqtt_client=bc,
    )
    engine.set_data_bus(data_bus)
    engine.start_loop(interval_sec=1.0)
    log.info("Rule engine started")

    # 4. 视觉服务（后台线程）
    vision_svc = VisionService(
        capture_url = CONFIG["capture_url"],
        model_path  = CONFIG["rknn_model"],
        interval    = 2.0,
        use_rknn    = True,
        enable_mqtt = True,
    )
    vision_svc.start_background()
    log.info("Vision service started")

    # 5. LLM 客户端
    llm = RKLLMClient(CONFIG["llm_url"])
    if not llm.is_alive():
        log.warning("LLM server not responding at %s", CONFIG["llm_url"])
        log.warning("Start it with: ./rkllm_server_demo model.rkllm --port 8080")

    # 6. 语音模块
    voice = VoiceModule(lang="zh", auto_usb=True, enable_mqtt=True)
    log.info("Voice module ready")

    # 7. 多模态融合代理
    agent = MultimodalAgent(
        data_bus       = data_bus,
        llm_client     = llm,
        vision_service = vision_svc,
        voice_module   = voice,
        rule_engine    = engine,
    )

    # 启动语音对话循环
    agent.start_voice_loop(stop_word="关闭系统")
    log.info("Multimodal agent running — voice loop active")

    print("\n[系统] 所有模块已启动，等待语音指令...")
    print("[系统] 说「关闭系统」退出\n")

    try:
        while True:
            time.sleep(5)
            # 可在此处加心跳监控、定时汇报等
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        agent.stop()
        engine.stop_loop()
        vision_svc.stop()
        bc.client.disconnect()


if __name__ == "__main__":
    main()