import json
import time
import threading
import sqlite3
import os
import sys
import traceback
import paho.mqtt.client as mqtt

# Push the parent directory (Edge root) into Python's module search path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from rule_engine.engine import RuleEngine

BROKER_HOST = "localhost"
BROKER_PORT = 1883
DB_PATH     = "/home/lubancat/smart_home/data/smart_home.db"

# ==================== 设备列表 ====================

DEVICES = {
    "s3_001": {
        "topic_status":    "smart_home/s3/s3_001/status",
        "topic_control":   "smart_home/s3/s3_001/control",
        "topic_gas_alert": "smart_home/s3/s3_001/gas_alert",
        "topic_flame":     "smart_home/s3/s3_001/flame",
    },
    "s3_002": {
        "topic_status":    "smart_home/s3/s3_002/status",
        "topic_control":   "smart_home/s3/s3_002/control",
        "topic_gas_alert": "smart_home/s3/s3_002/gas_alert",
        "topic_flame":     "smart_home/s3/s3_002/flame",
    },
    "esp32cam_001": {
        "topic_status":  "smart_home/cam/esp32cam_001/status",
        "topic_control": "smart_home/cam/esp32cam_001/control",
        "topic_detect":  "smart_home/cam/esp32cam_001/detect",
        # CAM 设备无 gas_alert / flame 主题
    },
}

# ==================== 内存数据总线 ====================

device_states = {}   # 兼容旧代码
data_bus      = {}   # 带时间戳的最新状态快照

client = None

# ==================== 数据库 ====================

def db_init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sensor_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT    NOT NULL,
            timestamp REAL    NOT NULL,
            data      TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sensor_ts
            ON sensor_log(timestamp DESC);

        CREATE TABLE IF NOT EXISTS event_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp  REAL    NOT NULL,
            event_type TEXT    NOT NULL,
            source     TEXT,
            detail     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_event_ts
            ON event_log(timestamp DESC);

        CREATE TABLE IF NOT EXISTS cam_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id   TEXT  NOT NULL,
            timestamp   REAL  NOT NULL,
            fps         REAL,
            streaming   INTEGER,
            stream_url  TEXT,
            capture_url TEXT,
            resolution  TEXT,
            status      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_cam_ts
            ON cam_log(timestamp DESC);
    """)
    conn.commit()
    conn.close()
    print(f"✅ Database initialized: {DB_PATH}")


def _db_write(sql: str, params: tuple):
    """后台线程写库，不阻塞主循环"""
    def _run():
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(sql, params)
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"  [DB] write error: {e}")
    threading.Thread(target=_run, daemon=True).start()


def db_log_sensor(device_id: str, timestamp: float, data: dict):
    _db_write(
        "INSERT INTO sensor_log(device_id,timestamp,data) VALUES(?,?,?)",
        (device_id, timestamp, json.dumps(data))
    )


def db_log_event(event_type: str, source: str, detail: dict):
    _db_write(
        "INSERT INTO event_log(timestamp,event_type,source,detail) VALUES(?,?,?,?)",
        (time.time(), event_type, source, json.dumps(detail))
    )


def db_log_cam(device_id: str, data: dict):
    _db_write(
        "INSERT INTO cam_log"
        "(device_id,timestamp,fps,streaming,stream_url,capture_url,resolution,status)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (
            device_id,
            time.time(),
            data.get("fps", 0),
            1 if data.get("streaming") else 0,
            data.get("stream_url", ""),
            data.get("capture_url", ""),
            data.get("resolution", ""),
            data.get("status", ""),
        )
    )


def db_query_latest(device_id: str = None, limit: int = 5) -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if device_id:
        rows = conn.execute(
            "SELECT * FROM sensor_log WHERE device_id=? "
            "ORDER BY timestamp DESC LIMIT ?",
            (device_id, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM sensor_log ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def db_query_count() -> dict:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT device_id, COUNT(*) FROM sensor_log GROUP BY device_id"
    ).fetchall()
    # cam_log 也一起统计
    cam_rows = conn.execute(
        "SELECT device_id, COUNT(*) FROM cam_log GROUP BY device_id"
    ).fetchall()
    conn.close()
    result = {r[0]: r[1] for r in rows}
    for r in cam_rows:
        result[f"{r[0]}(cam)"] = r[1]
    return result

# ==================== 数据解析：S3 传感器 ====================

def parse_status(device_id: str, payload: str):
    try:
        data = json.loads(payload)

        device_states[device_id] = data
        data_bus[device_id] = {"data": data, "timestamp": time.time()}

        # 写库
        db_log_sensor(device_id, data.get("timestamp", time.time()), data)

        d = data.get("data", {})
        print(f"\n[{device_id}] {data.get('datetime', '')}")

        dht11 = d.get("dht11", {})
        sht40 = d.get("sht40", {})
        if dht11.get("status") == "active":
            print(f"  DHT11  : {dht11['temperature']:.1f}°C  "
                  f"{dht11['humidity']}%RH")
        if sht40.get("status") == "active":
            print(f"  SHT40  : {sht40['temperature']:.2f}°C  "
                  f"{sht40['humidity']:.2f}%RH")

        mq4 = d.get("mq4", {})
        mq2 = d.get("mq2", {})
        if mq4.get("status") == "active":
            alert_str = " ⚠ ALERT" if mq4.get("alert") else ""
            print(f"  MQ4    : {mq4['ppm']:.1f} ppm{alert_str}")
        if mq2.get("status") == "active":
            alert_str = " ⚠ ALERT" if mq2.get("alert") else ""
            print(f"  MQ2    : {mq2['ppm']:.1f} ppm{alert_str}")

        flame = d.get("flame", {})
        if flame.get("status") == "active":
            detected_str = " 🔥 FLAME" if flame.get("detected") else ""
            print(f"  Flame  : {flame.get('level', 'NONE')}{detected_str}")

        led = d.get("led", {})
        print(f"  LED    : R={led.get('r',0)} G={led.get('g',0)} "
              f"B={led.get('b',0)}")

    except json.JSONDecodeError as e:
        print(f"[{device_id}] JSON parse error: {e}")


def parse_gas_alert(device_id: str, payload: str):
    try:
        data = json.loads(payload)
        print(f"\n🚨 [{device_id}] GAS ALERT: "
              f"{data['ppm']:.1f} ppm  alert={data['alert']}")
        db_log_event("gas_alert", device_id, data)
    except Exception as e:
        print(f"[{device_id}] gas_alert parse error: {e}")

# ==================== 数据解析：ESP32-CAM ====================

# CAM 最新状态（内存，供视觉模块读取）
cam_state = {
    "stream_url":  "",
    "capture_url": "",
    "fps":         0.0,
    "streaming":   False,
    "status":      "unknown",
    "timestamp":   0,
}


def parse_cam_status(device_id: str, payload: str):
    """解析 ESP32-CAM 状态消息"""
    try:
        data = json.loads(payload)

        # 更新内存状态
        cam_state.update({
            "stream_url":  data.get("stream_url",  ""),
            "capture_url": data.get("capture_url", ""),
            "fps":         data.get("fps",          0.0),
            "streaming":   data.get("streaming",    False),
            "status":      data.get("status",       "unknown"),
            "timestamp":   time.time(),
        })

        # 更新数据总线
        data_bus[device_id] = {"data": data, "timestamp": time.time()}

        # 写 cam_log 表
        db_log_cam(device_id, data)

        # 打印
        streaming_str = "🎥 LIVE" if data.get("streaming") else "⏸ idle"
        print(f"\n[{device_id}] CAM Status")
        print(f"  IP      : {data.get('ip', '?')}")
        print(f"  Stream  : {data.get('stream_url', '?')}")
        print(f"  FPS     : {data.get('fps', 0):.1f}  {streaming_str}")
        print(f"  Res     : {data.get('resolution', '?')}")
        print(f"  Status  : {data.get('status', '?')}")

    except json.JSONDecodeError as e:
        print(f"[{device_id}] CAM JSON parse error: {e}")


def parse_cam_detect(device_id: str, payload: str):
    """解析 ESP32-CAM 视觉检测结果（YOLO等推理输出）"""
    try:
        data = json.loads(payload)
        print(f"\n👁 [{device_id}] DETECT: {data}")
        db_log_event("cam_detect", device_id, data)
    except Exception as e:
        print(f"[{device_id}] detect parse error: {e}")

# ==================== 控制指令 ====================

def send_control(device_id: str, cmd: dict):
    if device_id not in DEVICES:
        print(f"Unknown device: {device_id}")
        return
    topic = DEVICES[device_id]["topic_control"]
    payload = json.dumps(cmd)
    client.publish(topic, payload, qos=1)
    print(f"[→ {device_id}] {payload}")


def set_led(device_id: str, state: int):
    send_control(device_id, {"cmd": "set", "target": "led", "value": state})


def set_rgb(device_id: str, r: int, g: int, b: int):
    send_control(device_id, {"cmd": "set", "target": "rgb",
                              "r": r, "g": g, "b": b})


def get_status(device_id: str):
    send_control(device_id, {"cmd": "get", "target": "status"})


def calibrate_mq(device_id: str, sensor: str):
    send_control(device_id, {"cmd": "set", "target": sensor})


def cam_set_resolution(resolution: str):
    """设置 CAM 分辨率：QVGA / VGA / SVGA"""
    send_control("esp32cam_001",
                 {"cmd": "set", "target": "resolution",
                  "value": resolution})


def cam_set_quality(quality: int):
    """设置 CAM JPEG 质量 0-63（越小质量越高）"""
    send_control("esp32cam_001",
                 {"cmd": "set", "target": "quality", "value": quality})


def cam_capture():
    """触发 CAM 单帧抓取并上报"""
    send_control("esp32cam_001",
                 {"cmd": "get", "target": "status"})

# ==================== 工具：数据总线快照 ====================

def get_data_bus_snapshot() -> dict:
    now = time.time()
    result = {}
    for dev_id, state in data_bus.items():
        age = now - state["timestamp"]
        result[dev_id] = {
            "age_sec": round(age, 1),
            "fresh":   age < 60,
            "data":    state["data"],
        }
    return result

# ==================== MQTT 回调 ====================

def on_connect(mqttc, userdata, flags, rc):
    if rc == 0:
        print("✅ Connected to MQTT broker")

        # 订阅 S3 设备
        for dev_id, topics in DEVICES.items():
            if dev_id.startswith("s3_"):
                mqttc.subscribe(topics["topic_status"],    qos=1)
                mqttc.subscribe(topics["topic_gas_alert"], qos=1)
                mqttc.subscribe(topics["topic_flame"],     qos=1)
                print(f"  Subscribed: {topics['topic_status']}")

        # 订阅 ESP32-CAM
        cam_topics = DEVICES["esp32cam_001"]
        mqttc.subscribe(cam_topics["topic_status"],  qos=1)
        mqttc.subscribe(cam_topics["topic_detect"],  qos=1)
        print(f"  Subscribed: {cam_topics['topic_status']}")
        print(f"  Subscribed: {cam_topics['topic_detect']}")
    else:
        print(f"❌ Connection failed, rc={rc}")


def on_message(mqttc, userdata, msg):
    topic   = msg.topic
    payload = msg.payload.decode("utf-8")

    # S3 设备路由
    for dev_id, topics in DEVICES.items():
        if dev_id.startswith("s3_"):
            if topic == topics.get("topic_status"):
                parse_status(dev_id, payload)
                return
            if topic == topics.get("topic_gas_alert"):
                parse_gas_alert(dev_id, payload)
                return

    # ESP32-CAM 路由
    cam_topics = DEVICES["esp32cam_001"]
    if topic == cam_topics["topic_status"]:
        parse_cam_status("esp32cam_001", payload)
        return
    if topic == cam_topics["topic_detect"]:
        parse_cam_detect("esp32cam_001", payload)
        return


def on_disconnect(mqttc, userdata, rc):
    print(f"⚠ Disconnected from broker (rc={rc}), reconnecting...")

# ==================== 交互式命令行 ====================

def interactive_loop():
    time.sleep(2)
    print("\n=== Smart Home Control ===")
    print("S3 Commands:")
    print("  led <device_id> <0|1>            - LED on/off")
    print("  rgb <device_id> <r> <g> <b>      - set RGB")
    print("  status <device_id>               - request status")
    print("  cali <device_id> <mq2|mq4>       - recalibrate sensor")
    print("CAM Commands:")
    print("  cam                              - show CAM state")
    print("  res <QVGA|VGA|SVGA>              - set CAM resolution")
    print("  quality <0-63>                   - set JPEG quality")
    print("Debug Commands:")
    print("  bus                              - data bus snapshot")
    print("  db [device_id]                   - query latest DB records")
    print("  count                            - DB record counts")
    print("  quit                             - exit")
    print("Rule Engine Commands:")
    print("  eval                             - run rule engine once")
    print("  engine                           - show rule engine status")
    print("")

    while True:
        try:
            line = input("> ").strip()
            if not line:
                continue
            parts = line.split()
            cmd   = parts[0].lower()

            # S3 控制
            if cmd == "quit":
                break
            elif cmd == "led" and len(parts) == 3:
                set_led(parts[1], int(parts[2]))
            elif cmd == "rgb" and len(parts) == 5:
                set_rgb(parts[1],
                        int(parts[2]), int(parts[3]), int(parts[4]))
            elif cmd == "status" and len(parts) == 2:
                get_status(parts[1])
            elif cmd == "cali" and len(parts) == 3:
                calibrate_mq(parts[1], f"{parts[2]}_calibrate")

            # CAM 控制
            elif cmd == "cam":
                age = time.time() - cam_state["timestamp"]
                fresh = "✅" if age < 60 else "❌ stale"
                print(f"  Stream  : {cam_state['stream_url']} {fresh}")
                print(f"  Capture : {cam_state['capture_url']}")
                print(f"  FPS     : {cam_state['fps']:.1f}")
                print(f"  Live    : {cam_state['streaming']}")
                print(f"  Status  : {cam_state['status']}")
                print(f"  Age     : {age:.0f}s")
            elif cmd == "res" and len(parts) == 2:
                cam_set_resolution(parts[1].upper())
            elif cmd == "quality" and len(parts) == 2:
                cam_set_quality(int(parts[1]))

            # 验证命令
            elif cmd == "bus":
                snap = get_data_bus_snapshot()
                if not snap:
                    print("  (empty - no data received yet)")
                for dev_id, info in snap.items():
                    fresh = "✅" if info["fresh"] else "❌ stale"
                    print(f"  [{dev_id}] age={info['age_sec']}s {fresh}")
            elif cmd == "db":
                dev  = parts[1] if len(parts) > 1 else None
                rows = db_query_latest(dev, limit=3)
                if not rows:
                    print("  (no records in DB)")
                for r in rows:
                    d = json.loads(r["data"])
                    print(f"  [{r['device_id']}] "
                          f"ts={r['timestamp']:.0f} "
                          f"datetime={d.get('datetime', d.get('ip','?'))}")
            elif cmd == "count":
                counts = db_query_count()
                if not counts:
                    print("  (no records)")
                for dev_id, cnt in counts.items():
                    print(f"  {dev_id}: {cnt} records")
####################################################################################
            elif cmd == "eval":
                # 延迟导入，只在需要时加载
                try:
                    
                    eng = RuleEngine(
                        "config/rules/safety_rules.yaml",
                        db_log_event   # 直接用 broker_client 里的写库函数
                    )
                    eng.set_data_bus(data_bus)
                    triggered = eng.evaluate()
                    if triggered:
                        print(f"  ⚠ Rules triggered: {triggered}")
                    else:
                        print("  ✅ No rules triggered (normal environment)")

                except Exception as e:
                    print(f"  Error: {e}")
                    traceback.print_exc()

            elif cmd == "engine":
                try:
                    eng = RuleEngine(
                        "config/rules/safety_rules.yaml",
                        db_log_event
                    )
                    eng.set_data_bus(data_bus)
                    s = eng.get_status()
                    print(f"  Rules   : {s['rules_count']}")
                    print(f"  Data bus: {'✅ set' if s['data_bus_set'] else '❌ empty'}")
                    print(f"  Bus keys: {list(data_bus.keys())}")
                    # 显示每个传感器当前值
                    for dev_id, state in data_bus.items():
                        age = time.time() - state.get("timestamp", 0)
                        d = state.get("data", {}).get("data", {})
                        print(f"\n  [{dev_id}] age={age:.0f}s")
                        for sensor, val in d.items():
                            if isinstance(val, dict) and val.get("status") == "active":
                                print(f"    {sensor}: {val}")
                except Exception as e:
                    print(f"  Error: {e}")

####################################################################################
            else:
                print("Unknown command or wrong arguments")

        except (KeyboardInterrupt, EOFError):
            break
        except Exception as e:
            print(f"Error: {e}")

# ==================== 主程序 ====================

def main():
    global client

    db_init()

    # Fixed the deprecation warning by specifying the API version
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="lubancat4_service")
    client.on_connect    = on_connect
    client.on_message    = on_message
    client.on_disconnect = on_disconnect

    client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)

    t = threading.Thread(target=interactive_loop, daemon=True)
    t.start()

    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\nExiting...")
        client.disconnect()


if __name__ == "__main__":
    main()