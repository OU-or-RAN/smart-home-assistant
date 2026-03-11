"""
内存数据总线：线程安全，存储各设备最新状态快照
供规则引擎、LLM、同步模块实时读取，不写磁盘
"""
import time
import threading
from typing import Optional, Dict, Any

class DataBus:
    def __init__(self):
        self._lock   = threading.RLock()
        self._states: Dict[str, dict] = {}   # device_id → 最新状态

    def update(self, device_id: str, data: dict):
        with self._lock:
            self._states[device_id] = {
                "data":      data,
                "timestamp": time.time(),
            }

    def get(self, device_id: str) -> Optional[dict]:
        with self._lock:
            return self._states.get(device_id)

    def get_all(self) -> Dict[str, dict]:
        with self._lock:
            return dict(self._states)

    def is_fresh(self, device_id: str, max_age_sec: float = 30.0) -> bool:
        state = self.get(device_id)
        if state is None:
            return False
        return (time.time() - state["timestamp"]) < max_age_sec

    def snapshot(self) -> dict:
        """返回所有设备的当前语义快照，供 LLM 使用"""
        with self._lock:
            result = {}
            for dev_id, state in self._states.items():
                result[dev_id] = {
                    "age_sec":  round(time.time() - state["timestamp"], 1),
                    "fresh":    self.is_fresh(dev_id),
                    "data":     state["data"],
                }
            return result

# 全局单例
data_bus = DataBus()