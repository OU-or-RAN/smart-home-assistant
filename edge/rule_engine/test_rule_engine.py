"""
阶段二验收测试：规则引擎独立测试
不依赖真实 ESP32，模拟注入传感器数据验证规则触发逻辑

运行方式：
  cd /home/lubancat/smart_home
  python3 edge/rule_engine/test_rule_engine.py

全部通过后输出：ALL TESTS PASSED
"""
import sys
import time
import os

# 添加 edge/ 到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rule_engine.engine import RuleEngine

# ==================== Mock 对象 ====================

class MockMQTT:
    """模拟 mqtt_client，记录发送的控制指令"""
    def __init__(self):
        self.sent = []

    def send_control(self, device_id, cmd):
        self.sent.append((device_id, cmd))
        print(f"    [MockMQTT] → {device_id}: {cmd}")


class MockDB:
    """模拟数据库，记录写入的事件"""
    def __init__(self):
        self.events = []

    def log_event(self, event_type, source, detail):
        self.events.append((event_type, source, detail))
        print(f"    [MockDB] event: {event_type} | {source} | {detail}")


# ==================== 辅助函数 ====================

RULES_PATH = os.path.join(
    os.path.dirname(__file__), "../config/rules/safety_rules.yaml")

def make_engine():
    mqtt = MockMQTT()
    db   = MockDB()
    eng  = RuleEngine(RULES_PATH, db, mqtt)
    return eng, mqtt, db


def make_data_bus(sensor_overrides: dict) -> dict:
    """
    构造模拟数据总线。
    sensor_overrides 格式：
      {"mq4": {"alert": True, "ppm": 600, "status": "active"}, ...}
    """
    # 默认：所有传感器正常
    defaults = {
        "dht11": {"temperature": 25.0, "humidity": 60,   "status": "active"},
        "sht40": {"temperature": 24.5, "humidity": 58.0, "status": "active"},
        "mq4":   {"alert": False, "ppm": 100.0, "calibrated": True, "status": "active"},
        "mq2":   {"alert": False, "ppm":  80.0, "calibrated": True, "status": "active"},
        "flame": {"detected": False, "level": "NONE",
                  "intensity_percent": 0, "status": "active"},
        "led":   {"state": 0, "r": 0, "g": 0, "b": 0},
    }
    defaults.update(sensor_overrides)

    return {
        "s3_001": {
            "data": {
                "device_id": "s3_001",
                "data": {
                    "dht11": defaults["dht11"],
                    "mq2":   defaults["mq2"],
                    "flame": defaults["flame"],
                    "led":   defaults["led"],
                }
            },
            "timestamp": time.time(),
        },
        "s3_002": {
            "data": {
                "device_id": "s3_002",
                "data": {
                    "sht40": defaults["sht40"],
                    "mq4":   defaults["mq4"],
                }
            },
            "timestamp": time.time(),
        },
    }


# ==================== 测试用例 ====================

def test_no_trigger_normal():
    """正常环境下不应触发任何规则"""
    print("\n[TEST 1] Normal environment - no rules should trigger")
    eng, mqtt, db = make_engine()
    bus = make_data_bus({})
    eng.set_data_bus(bus)

    triggered = eng.evaluate()
    assert triggered == [], f"Expected no trigger, got: {triggered}"
    assert len(mqtt.sent) == 0
    print("  ✅ No rules triggered")


def test_mq4_alert_triggers_safety001():
    """MQ4 报警应触发 SAFETY_001"""
    print("\n[TEST 2] MQ4 alert → SAFETY_001 should trigger")
    eng, mqtt, db = make_engine()
    bus = make_data_bus({"mq4": {"alert": True, "ppm": 600,
                                  "calibrated": True, "status": "active"}})
    eng.set_data_bus(bus)

    triggered = eng.evaluate()
    assert "SAFETY_001" in triggered, f"Expected SAFETY_001, got: {triggered}"

    # 验证 LED 关闭指令已发出
    devices_controlled = [d for d, _ in mqtt.sent]
    assert "s3_001" in devices_controlled, "s3_001 LED should be turned off"
    assert "s3_002" in devices_controlled, "s3_002 LED should be turned off"

    # 验证事件写库
    assert len(db.events) > 0, "DB event should be logged"
    print(f"  ✅ SAFETY_001 triggered, controls sent to: {devices_controlled}")


def test_mq2_alert_triggers_safety001():
    """MQ2 报警也应触发 SAFETY_001"""
    print("\n[TEST 3] MQ2 alert → SAFETY_001 should trigger")
    eng, mqtt, db = make_engine()
    bus = make_data_bus({"mq2": {"alert": True, "ppm": 400,
                                  "calibrated": True, "status": "active"}})
    eng.set_data_bus(bus)

    triggered = eng.evaluate()
    assert "SAFETY_001" in triggered, f"Expected SAFETY_001, got: {triggered}"
    print(f"  ✅ SAFETY_001 triggered by MQ2")


def test_flame_triggers_safety002():
    """火焰检测应触发 SAFETY_002"""
    print("\n[TEST 4] Flame detected → SAFETY_002 should trigger")
    eng, mqtt, db = make_engine()
    bus = make_data_bus({
        "flame": {"detected": True, "level": "MEDIUM",
                  "intensity_percent": 75, "status": "active"}
    })
    eng.set_data_bus(bus)

    triggered = eng.evaluate()
    assert "SAFETY_002" in triggered, f"Expected SAFETY_002, got: {triggered}"
    print(f"  ✅ SAFETY_002 triggered")


def test_flame_none_no_trigger():
    """火焰 level=NONE 不应触发 SAFETY_002"""
    print("\n[TEST 5] Flame level=NONE → SAFETY_002 should NOT trigger")
    eng, mqtt, db = make_engine()
    bus = make_data_bus({
        "flame": {"detected": False, "level": "NONE",
                  "intensity_percent": 0, "status": "active"}
    })
    eng.set_data_bus(bus)

    triggered = eng.evaluate()
    assert "SAFETY_002" not in triggered, \
        f"SAFETY_002 should not trigger with level=NONE"
    print(f"  ✅ SAFETY_002 correctly NOT triggered")


def test_high_temp_triggers_comfort001():
    """高温应触发 COMFORT_001"""
    print("\n[TEST 6] High temperature → COMFORT_001 should trigger")
    eng, mqtt, db = make_engine()
    bus = make_data_bus({
        "sht40": {"temperature": 37.5, "humidity": 60.0, "status": "active"}
    })
    eng.set_data_bus(bus)

    triggered = eng.evaluate()
    assert "COMFORT_001" in triggered, f"Expected COMFORT_001, got: {triggered}"
    print(f"  ✅ COMFORT_001 triggered at 37.5°C")


def test_cooldown_prevents_retrigger():
    """冷却期内不应重复触发"""
    print("\n[TEST 7] Cooldown prevents re-trigger")
    eng, mqtt, db = make_engine()
    bus = make_data_bus({"mq4": {"alert": True, "ppm": 600,
                                  "calibrated": True, "status": "active"}})
    eng.set_data_bus(bus)

    # 第一次触发
    triggered1 = eng.evaluate()
    assert "SAFETY_001" in triggered1

    # 立即再次评估，应在冷却期内不触发
    triggered2 = eng.evaluate()
    assert "SAFETY_001" not in triggered2, \
        "SAFETY_001 should be in cooldown"
    print(f"  ✅ Cooldown working: 2nd eval did not re-trigger")


def test_stale_data_ignored():
    """过期数据（>120s）不应触发规则"""
    print("\n[TEST 8] Stale data (>120s) should not trigger rules")
    eng, mqtt, db = make_engine()
    bus = make_data_bus({"mq4": {"alert": True, "ppm": 600,
                                  "calibrated": True, "status": "active"}})
    # 手动设置过期时间戳
    bus["s3_002"]["timestamp"] = time.time() - 200

    eng.set_data_bus(bus)
    triggered = eng.evaluate()
    assert "SAFETY_001" not in triggered, \
        "Stale data should not trigger rules"
    print(f"  ✅ Stale data correctly ignored")


def test_gas_and_flame_triggers_safety003():
    """燃气+火焰同时触发 SAFETY_003"""
    print("\n[TEST 9] Gas + Flame → SAFETY_003 (high danger)")
    eng, mqtt, db = make_engine()
    bus = make_data_bus({
        "mq4":   {"alert": True, "ppm": 700,
                   "calibrated": True, "status": "active"},
        "flame": {"detected": True, "level": "STRONG",
                  "intensity_percent": 100, "status": "active"},
    })
    eng.set_data_bus(bus)

    triggered = eng.evaluate()
    assert "SAFETY_003" in triggered, \
        f"Expected SAFETY_003 for gas+flame, got: {triggered}"
    print(f"  ✅ SAFETY_003 triggered for gas+flame combination")


def test_engine_status():
    """规则引擎状态查询"""
    print("\n[TEST 10] Engine status query")
    eng, _, _ = make_engine()
    bus = make_data_bus({})
    eng.set_data_bus(bus)

    status = eng.get_status()
    assert status["rules_count"] > 0
    assert status["data_bus_set"] is True
    assert status["running"] is False
    print(f"  ✅ Status: {status['rules_count']} rules, "
          f"data_bus_set={status['data_bus_set']}")


# ==================== 运行所有测试 ====================

if __name__ == "__main__":
    tests = [
        test_no_trigger_normal,
        test_mq4_alert_triggers_safety001,
        test_mq2_alert_triggers_safety001,
        test_flame_triggers_safety002,
        test_flame_none_no_trigger,
        test_high_temp_triggers_comfort001,
        test_cooldown_prevents_retrigger,
        test_stale_data_ignored,
        test_gas_and_flame_triggers_safety003,
        test_engine_status,
    ]

    passed  = 0
    failed  = 0
    errors  = []

    for test_fn in tests:
        try:
            test_fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            errors.append(f"  ❌ {test_fn.__name__}: {e}")
            print(f"  ❌ FAILED: {e}")
        except Exception as e:
            failed += 1
            errors.append(f"  ❌ {test_fn.__name__}: EXCEPTION {e}")
            print(f"  ❌ EXCEPTION: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'='*50}")
    print(f"Results: {passed}/{len(tests)} passed")
    if errors:
        print("Failures:")
        for e in errors:
            print(e)
    else:
        print("ALL TESTS PASSED ✅")
    print(f"{'='*50}")

    sys.exit(0 if failed == 0 else 1)