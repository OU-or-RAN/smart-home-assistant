"""
规则引擎
数据来源：直接读 mqtt/broker_client.py 中的 data_bus 字典
不依赖独立的 storage/data_bus.py，避免两个数据总线实例不同步
"""
import yaml
import time
import logging
import operator
import threading
from typing import List, Dict, Any, Optional

log = logging.getLogger("rule_engine")

OPS = {
    "==": operator.eq,
    "!=": operator.ne,
    ">":  operator.gt,
    ">=": operator.ge,
    "<":  operator.lt,
    "<=": operator.le,
}


class RuleEngine:

    def __init__(self, rules_path: str, db, mqtt_client=None):
        """
        Parameters
        ----------
        rules_path  : YAML 规则文件路径
        db          : broker_client 里的 db_log_event 函数 或 Database 对象
                      只需要支持 .log_event(type, source, detail) 即可
        mqtt_client : broker_client 模块本身（用于调用 send_control）
        """
        self._rules_path = rules_path
        self._rules:      List[dict]        = []
        self._db                            = db
        self._mqtt                          = mqtt_client
        self._last_fired: Dict[str, float]  = {}
        self._running                       = False
        self._data_bus: Optional[dict]      = None   # 运行时注入

        self._load_rules()
        log.info(f"Rule engine: {len(self._rules)} rules loaded")

    # ==================== 规则加载 ====================

    def _load_rules(self):
        try:
            with open(self._rules_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            rules = cfg.get("rules", [])
            self._rules = sorted(rules, key=lambda r: -r.get("priority", 0))
            log.info(f"Loaded {len(self._rules)} rules from {self._rules_path}")
        except Exception as e:
            log.error(f"Failed to load rules: {e}")
            self._rules = []

    def reload_rules(self):
        """热重载规则文件，无需重启"""
        self._load_rules()
        log.info("Rules reloaded")

    # ==================== 数据总线注入 ====================

    def set_data_bus(self, data_bus: dict):
        """
        注入数据总线字典引用。
        直接传入 broker_client.data_bus 的引用，
        引擎每次 evaluate 时读取其最新内容。
        """
        self._data_bus = data_bus
        log.info("Data bus injected into rule engine")

    # ==================== 传感器值提取 ====================

    def _get_sensor_value(self, sensor: str, field: str) -> Any:
        """
        从数据总线找到 sensor 对应的字段值。
        数据总线格式：
          data_bus = {
            "s3_001": {"data": {"data": {"mq2": {...}, "flame": {...}}}, "timestamp": ...},
            "s3_002": {"data": {"data": {"mq4": {...}, "sht40": {...}}}, "timestamp": ...},
          }
        """
        if self._data_bus is None:
            log.warning("Data bus not set, cannot evaluate rules")
            return None

        # 遍历所有设备，找到包含该 sensor 的设备
        for dev_id, state in self._data_bus.items():
            try:
                # 数据新鲜度检查：超过 120 秒的数据不参与规则判断
                age = time.time() - state.get("timestamp", 0)
                if age > 120:
                    continue

                inner = state.get("data", {})
                # 兼容两种数据结构：
                # 1. broker_client.data_bus: state["data"] 是完整 JSON
                #    {"device_id":..., "data": {"mq4": {...}}}
                # 2. 直接是 {"mq4": {...}}
                sensor_map = inner.get("data", inner)

                if sensor in sensor_map:
                    sensor_data = sensor_map[sensor]
                    if isinstance(sensor_data, dict):
                        val = sensor_data.get(field)
                        if val is not None:
                            return val
            except Exception:
                continue

        return None

    # ==================== 条件评估 ====================

    def _eval_condition(self, cond: dict) -> bool:
        val = self._get_sensor_value(cond["sensor"], cond["field"])
        if val is None:
            return False

        op_fn = OPS.get(cond["op"])
        if op_fn is None:
            log.warning(f"Unknown operator: {cond['op']}")
            return False

        try:
            result = op_fn(val, cond["value"])
            log.debug(f"  cond [{cond['sensor']}.{cond['field']}="
                      f"{val}] {cond['op']} {cond['value']} → {result}")
            return result
        except Exception as e:
            log.debug(f"  cond eval error: {e}")
            return False

    def _eval_conditions(self, conditions: dict) -> bool:
        if not conditions:
            return False
        if "all" in conditions:
            return all(self._eval_condition(c)
                       for c in conditions["all"])
        if "any" in conditions:
            return any(self._eval_condition(c)
                       for c in conditions["any"])
        return False

    # ==================== 动作执行 ====================

    def _execute_actions(self, rule: dict):
        for action in rule.get("actions", []):

            # 发送控制指令到 ESP32
            if "device" in action and self._mqtt is not None:
                try:
                    self._mqtt.send_control(
                        action["device"], action["cmd"])
                    log.info(f"  → Control sent to "
                             f"{action['device']}: {action['cmd']}")
                except Exception as e:
                    log.error(f"  send_control error: {e}")

            # 通知日志
            if "notify" in action:
                msg = action["notify"]
                log.warning(f"[{rule['id']}] {msg}")

                # 写事件日志
                try:
                    if hasattr(self._db, "log_event"):
                        # Database 对象
                        self._db.log_event(
                            "rule_triggered", rule["id"],
                            {"name": rule["name"], "notify": msg})
                    elif callable(self._db):
                        # 直接传入 db_log_event 函数
                        self._db(
                            "rule_triggered", rule["id"],
                            {"name": rule["name"], "notify": msg})
                except Exception as e:
                    log.error(f"  db log error: {e}")

    # ==================== 单次评估 ====================

    def evaluate(self) -> List[str]:
        """
        执行一轮规则评估。
        返回本轮触发的规则 ID 列表（用于测试验证）。
        """
        now      = time.time()
        triggered = []

        for rule in self._rules:
            rule_id  = rule["id"]
            cooldown = rule.get("cooldown_sec", 0)

            # 冷却期检查
            last = self._last_fired.get(rule_id, 0)
            if now - last < cooldown:
                remaining = cooldown - (now - last)
                log.debug(f"[{rule_id}] in cooldown ({remaining:.0f}s left)")
                continue

            if self._eval_conditions(rule.get("conditions", {})):
                log.info(f"Rule triggered: [{rule_id}] {rule['name']}")
                self._execute_actions(rule)
                self._last_fired[rule_id] = now
                triggered.append(rule_id)

        return triggered

    # ==================== 后台循环 ====================

    def start_loop(self, interval_sec: float = 1.0):
        """在独立守护线程中周期性评估规则"""
        if self._running:
            log.warning("Rule engine loop already running")
            return

        self._running = True

        def _loop():
            log.info(f"Rule engine loop started "
                     f"(interval={interval_sec}s, rules={len(self._rules)})")
            while self._running:
                try:
                    triggered = self.evaluate()
                    if triggered:
                        log.info(f"Triggered this cycle: {triggered}")
                except Exception as e:
                    log.error(f"Rule engine loop error: {e}")
                time.sleep(interval_sec)
            log.info("Rule engine loop stopped")

        t = threading.Thread(target=_loop, daemon=True, name="rule_engine")
        t.start()

    def stop_loop(self):
        self._running = False

    # ==================== 状态查询 ====================

    def get_status(self) -> dict:
        """返回规则引擎当前状态，用于调试"""
        now = time.time()
        return {
            "rules_count": len(self._rules),
            "running":     self._running,
            "data_bus_set": self._data_bus is not None,
            "cooldowns": {
                rule_id: round(cooldown - (now - fired), 1)
                for rule_id, fired in self._last_fired.items()
                for rule in self._rules
                if rule["id"] == rule_id
                for cooldown in [rule.get("cooldown_sec", 0)]
                if now - fired < cooldown
            }
        }