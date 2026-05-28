# edge/llm/multimodal_agent.py
"""
多模态融合代理 - 生产优化版
优化目标：
1. 提示词精简，确保推理 < 5秒（RK3588 + DeepSeek-R1-Distill-Qwen-1.5B）
2. 传感器数据结构化呈现，LLM 可直接引用
3. 支持本地化部署，无外部依赖
"""
import time
import json
import re
import logging
import threading
from typing import Optional, Callable, Dict, Any

log = logging.getLogger("llm_agent")


class IntentParser:
    """解析 LLM 输出，提取控制指令"""

    VALID_ACTIONS = {"set_led", "set_rgb", "none"}
    VALID_DEVICES = {"s3_001", "s3_002", "esp32cam_001"}

    def parse(self, llm_output: str) -> Dict[str, Any]:
        """从 LLM 输出中解析 JSON 指令"""
        json_match = re.search(r'```json\s*(.*?)\s*```', llm_output, re.DOTALL)
        if not json_match:
            json_match = re.search(r'\{[^{}]+\}', llm_output, re.DOTALL)

        if not json_match:
            return {"action": "none"}

        json_str = json_match.group(1) if '```json' in llm_output else json_match.group(0)

        try:
            cmd = json.loads(json_str)
        except json.JSONDecodeError:
            return {"action": "none"}

        action = cmd.get("action", "none")
        if action not in self.VALID_ACTIONS:
            log.warning(f"IntentParser: unknown action '{action}'")
            return {"action": "none"}

        device = cmd.get("device", "")
        if device and not any(device.startswith(d) for d in self.VALID_DEVICES):
            log.warning(f"IntentParser: unknown device '{device}'")
            return {"action": "none"}

        if action == "set_rgb":
            for color in ["r", "g", "b"]:
                if color in cmd:
                    cmd[color] = max(0, min(255, int(cmd[color])))

        if action == "set_led":
            cmd["value"] = 1 if cmd.get("value") else 0

        return cmd

    def extract_reply_text(self, llm_output: str) -> str:
        """提取用于 TTS 的纯文本"""
        text = re.sub(r'```json\s*.*?\s*```', '', llm_output, flags=re.DOTALL)
        text = re.sub(r'\{[^{}]+\}', '', text, flags=re.DOTALL)
        return text.strip()


class ActionExecutor:
    """执行解析后的控制指令"""

    def __init__(self, mqtt_client=None):
        self.mqtt = mqtt_client

    def execute(self, cmd: Dict[str, Any], data_bus: Dict = None) -> bool:
        action = cmd.get("action", "none")
        if action == "none":
            return True

        device = cmd.get("device", "s3_001")

        if action == "set_led":
            value = cmd.get("value", 0)
            if self.mqtt:
                topic = f"home/{device}/led/set"
                payload = json.dumps({"state": value})
                log.info(f"[Action] MQTT: {topic} = {payload}")
            return True

        elif action == "set_rgb":
            r, g, b = cmd.get("r", 0), cmd.get("g", 0), cmd.get("b", 0)
            if self.mqtt:
                topic = f"home/{device}/rgb/set"
                payload = json.dumps({"r": r, "g": g, "b": b})
                log.info(f"[Action] MQTT: {topic} = {payload}")
            return True

        return False


class MultimodalAgent:
    """
    多模态融合代理 - 针对 RK3588 本地推理优化
    使用 DeepSeek-R1-Distill-Qwen-1.5B_W8A8_RK3588.rkllm
    """

    # 精简版系统提示词 - 减少 token 数，加速推理
    SYSTEM_PROMPT = """你是智能家居助手小灵。根据以下实时数据回答用户问题。

【环境数据】
{env_data}

【指令】
- 回答温度/湿度/空气质量时，必须引用上方具体数值
- 控制设备时，在回复末尾加JSON: {{"action": "set_led", "device": "s3_001", "value": 1}}
- 使用中文，简洁明了

用户问: {user_question}
答:"""

    def __init__(self,
                 data_bus: dict,
                 llm_client,
                 vision_service=None,
                 voice_module=None,
                 rule_engine=None,
                 enable_voice_loop: bool = True,
                 mqtt_client=None):
        self._bus = data_bus
        self._llm = llm_client
        self._vision = vision_service
        self._voice = voice_module
        self._engine = rule_engine
        self._enable_voice = enable_voice_loop
        self._running = False
        self._mqtt = mqtt_client

        self._parser = IntentParser()
        self._executor = ActionExecutor(mqtt_client)

        # 缓存机制
        self._cached_env_str = ""
        self._cache_time = 0
        self._cache_ttl = 2.0  # 2秒缓存

    def _extract_key_metrics(self) -> Dict[str, Any]:
        """提取关键指标（精简版，只取最新有效数据）"""
        now = time.time()
        metrics = {
            "temp": None,
            "humidity": None,
            "mq2": None,
            "mq4": None,
            "flame": False,
            "led": None,
            "person": False
        }

        for dev_id, state in self._bus.items():
            if not isinstance(state, dict):
                continue

            age = now - state.get("timestamp", 0)
            if age > 60:  # 只使用1分钟内的数据
                continue

            inner = state.get("data", {}).get("data", {})

            # 温度/湿度 - 优先 SHT40，其次 DHT11
            if "sht40" in inner:
                sht = inner["sht40"]
                if sht.get("status") == "active":
                    if metrics["temp"] is None:
                        metrics["temp"] = sht.get("temperature")
                    if metrics["humidity"] is None:
                        metrics["humidity"] = sht.get("humidity")

            if "dht11" in inner and metrics["temp"] is None:
                dht = inner["dht11"]
                if dht.get("status") == "active":
                    metrics["temp"] = dht.get("temperature")
                    metrics["humidity"] = dht.get("humidity")

            # 气体传感器
            if "mq2" in inner:
                mq = inner["mq2"]
                if mq.get("status") == "active":
                    metrics["mq2"] = mq.get("ppm")

            if "mq4" in inner:
                mq = inner["mq4"]
                if mq.get("status") == "active":
                    metrics["mq4"] = mq.get("ppm")

            # 火焰检测
            if "flame" in inner:
                flame = inner["flame"]
                if flame.get("status") == "active" and flame.get("detected"):
                    metrics["flame"] = True

            # LED 状态
            if "led" in inner:
                led = inner["led"]
                if led.get("status") == "active":
                    metrics["led"] = "开" if led.get("state") else "关"

        # 视觉检测
        if self._vision:
            try:
                det = self._vision.get_latest_detection() if hasattr(self._vision, 'get_latest_detection') else None
                if det and "semantic" in det:
                    metrics["person"] = det["semantic"].get("has_person", False)
            except Exception:
                pass

        return metrics

    def _build_env_string(self) -> str:
        """构建环境数据字符串（精简格式）"""
        now = time.time()

        # 检查缓存
        if now - self._cache_time < self._cache_ttl and self._cached_env_str:
            return self._cached_env_str

        m = self._extract_key_metrics()
        parts = []

        if m["temp"] is not None:
            parts.append(f"温度:{m['temp']:.1f}°C")
        if m["humidity"] is not None:
            parts.append(f"湿度:{m['humidity']:.0f}%")
        if m["mq2"] is not None:
            alert = "⚠️" if m["mq2"] > 200 else ""
            parts.append(f"MQ2:{m['mq2']:.0f}ppm{alert}")
        if m["mq4"] is not None:
            alert = "⚠️" if m["mq4"] > 200 else ""
            parts.append(f"MQ4:{m['mq4']:.0f}ppm{alert}")
        if m["flame"]:
            parts.append("🔥火焰检测!")
        if m["led"]:
            parts.append(f"灯:{m['led']}")
        if m["person"]:
            parts.append("有人")

        result = " | ".join(parts) if parts else "暂无数据"

        # 更新缓存
        self._cached_env_str = result
        self._cache_time = now

        return result

    def ask(self, user_input: str,
            on_thinking: Optional[Callable] = None) -> str:
        """
        单次问答 - 优化版，确保 < 5秒延迟
        """
        env_str = self._build_env_string()

        # 构建精简提示词
        prompt = self.SYSTEM_PROMPT.format(
            env_data=env_str,
            user_question=user_input
        )

        log.info(f"Prompt ({len(prompt)} chars): {prompt[:100]}...")

        if on_thinking:
            on_thinking()

        t0 = time.perf_counter()

        # 直接调用，不使用额外的 system_prompt 参数（避免重复）
        reply = self._llm.chat(prompt)  # 移除 max_tokens 参数

        latency = time.perf_counter() - t0
        log.info(f"LLM reply ({latency:.2f}s): '{reply[:60]}...'")

        return reply

    def process_command(self, user_input: str) -> Dict[str, Any]:
        """处理用户指令的完整流程"""
        reply = self.ask(user_input)
        cmd = self._parser.parse(reply)
        action_result = self._executor.execute(cmd, self._bus)
        tts_text = self._parser.extract_reply_text(reply)

        return {
            "reply_text": tts_text or "好的",
            "raw_reply": reply,
            "cmd": cmd,
            "action_result": action_result
        }

    def voice_llm_callback(self, user_text: str) -> str:
        """VoiceModule 的 llm_callback"""
        result = self.process_command(user_text)
        if result["cmd"]["action"] != "none":
            log.info(f"执行: {result['cmd']}")
        return result["reply_text"]

    def start_voice_loop(self, stop_word: str = "退出"):
        """启动语音对话循环"""
        if self._voice is None:
            log.warning("VoiceModule not set")
            return

        self._running = True

        def thinking_hint():
            try:
                if hasattr(self._voice, 'speak'):
                    self._voice.speak("稍等", block=False)
            except Exception:
                pass

        def wrapped_callback(user_text: str) -> str:
            if stop_word in user_text:
                self._running = False
                return "好的，再见"
            thinking_hint()
            return self.voice_llm_callback(user_text)

        self._voice.start_loop(
            llm_callback=wrapped_callback,
            stop_word=stop_word,
        )
        log.info(f"Voice loop started")

    def stop(self):
        self._running = False
        if self._voice:
            self._voice.stop()
        log.info("Stopped")

    def test_ask(self, user_input: str) -> Dict[str, Any]:
        """
        测试接口 - 用于 acceptance_test_integration.py
        """
        env_str = self._build_env_string()
        prompt = self.SYSTEM_PROMPT.format(
            env_data=env_str,
            user_question=user_input
        )

        reply = self._llm.chat(prompt)  # 移除 max_tokens 参数
        cmd = self._parser.parse(reply)

        return {
            "user_msg": user_input,
            "reply_text": reply,
            "cmd": cmd,
            "env_context": env_str
        }

    def get_current_metrics(self) -> Dict[str, Any]:
        """获取当前环境指标（供外部查询）"""
        return self._extract_key_metrics()