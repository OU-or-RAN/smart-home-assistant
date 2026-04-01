# edge/llm/multimodal_agent.py
"""
多模态融合代理。
从 data_bus / vision / rule_engine 聚合上下文，
构建结构化 Prompt，调用 LLM，路由回复。
"""
import time
import json
import logging
import threading
from typing import Optional, Callable

log = logging.getLogger("llm_agent")


class MultimodalAgent:
    """
    参数：
      data_bus       : broker_client.data_bus 的引用（字典）
      vision_service : VisionService 实例（可选，None则跳过视觉）
      voice_module   : VoiceModule 实例（可选，None则跳过语音）
      llm_client     : RKLLMClient 实例
      rule_engine    : RuleEngine 实例（可选）
    """

    SYSTEM_PROMPT = """你是一个智能家居助理，运行在边缘设备上。
你的职责是根据实时传感器数据、摄像头观测和用户语音指令，
给出简洁、准确的回答或执行建议。
回答必须简短（不超过50字），优先使用中文。
如果涉及安全警报，回答开头加「紧急」二字。"""

    def __init__(self,
                 data_bus: dict,
                 llm_client,
                 vision_service=None,
                 voice_module=None,
                 rule_engine=None,
                 enable_voice_loop: bool = True):
        self._bus     = data_bus
        self._llm     = llm_client
        self._vision  = vision_service
        self._voice   = voice_module
        self._engine  = rule_engine
        self._enable_voice = enable_voice_loop
        self._running = False

    # ── 上下文构建 ──────────────────────────────────────────

    def _build_sensor_context(self) -> str:
        """从 data_bus 提取最新传感器摘要"""
        now = time.time()
        lines = []
        for dev_id, state in self._bus.items():
            age = now - state.get("timestamp", 0)
            if age > 120:
                continue   # 超过2分钟的数据跳过
            d = state.get("data", {}).get("data", {})

            if dev_id.startswith("s3_"):
                for sensor, val in d.items():
                    if not isinstance(val, dict):
                        continue
                    if sensor == "dht11" and val.get("status") == "active":
                        lines.append(
                            f"温度{val['temperature']:.1f}°C"
                            f" 湿度{val['humidity']}%")
                    elif sensor == "sht40" and val.get("status") == "active":
                        lines.append(
                            f"精密温度{val['temperature']:.2f}°C"
                            f" 湿度{val['humidity']:.1f}%")
                    elif sensor in ("mq4", "mq2") and val.get("status") == "active":
                        alert = "【报警】" if val.get("alert") else ""
                        lines.append(
                            f"{sensor.upper()}{alert}: {val['ppm']:.0f}ppm")
                    elif sensor == "flame" and val.get("status") == "active":
                        if val.get("detected"):
                            lines.append(f"火焰检测: {val.get('level','?')}")

        return "，".join(lines) if lines else "传感器数据暂无"

    def _build_vision_context(self) -> str:
        """从 vision_service 获取视觉摘要"""
        if self._vision is None:
            return "摄像头未启用"
        try:
            return self._vision.get_semantic_summary()
        except Exception:
            return "摄像头状态未知"

    def _build_rule_context(self) -> str:
        """获取规则引擎最近触发记录"""
        if self._engine is None:
            return ""
        status = self._engine.get_status()
        cooldowns = status.get("cooldowns", {})
        if cooldowns:
            active = list(cooldowns.keys())
            return f"活跃告警规则: {active}"
        return ""

    def build_prompt(self, user_input: str) -> str:
        """
        构建完整 Prompt：
          [系统提示]
          当前环境：<传感器>
          视觉状态：<摄像头>
          [规则告警]
          用户说：<user_input>
          请回答：
        """
        sensor_ctx = self._build_sensor_context()
        vision_ctx = self._build_vision_context()
        rule_ctx   = self._build_rule_context()

        parts = [
            self.SYSTEM_PROMPT,
            f"\n当前环境：{sensor_ctx}",
            f"视觉状态：{vision_ctx}",
        ]
        if rule_ctx:
            parts.append(f"告警状态：{rule_ctx}")
        parts.append(f"\n用户说：{user_input}\n请回答：")

        return "\n".join(parts)

    # ── LLM 推理 ────────────────────────────────────────────

    def ask(self, user_input: str,
            on_thinking: Optional[Callable] = None) -> str:
        """
        单次问答，返回 LLM 回复文本。
        on_thinking: LLM推理中的回调（用于播放思考提示音等）
        """
        prompt = self.build_prompt(user_input)
        log.info(f"LLM prompt ({len(prompt)} chars)")

        if on_thinking:
            on_thinking()

        t0     = time.perf_counter()
        reply  = self._llm.chat(prompt, max_tokens=256)
        latency = (time.perf_counter() - t0) * 1000
        log.info(f"LLM reply: '{reply[:60]}'  ({latency:.0f}ms)")
        return reply

    # ── 语音对话循环集成 ─────────────────────────────────────

    def voice_llm_callback(self, user_text: str) -> str:
        """
        VoiceModule.start_loop 的 llm_callback。
        user_text → build_prompt → LLM → reply
        """
        return self.ask(user_text)

    def start_voice_loop(self, stop_word: str = "退出"):
        """启动语音对话循环（在守护线程中运行）"""
        if self._voice is None:
            log.warning("VoiceModule not set, voice loop skipped")
            return

        def thinking_hint():
            # LLM推理期间播报提示，避免用户以为系统死机
            try:
                self._voice._tts.speak("稍等", lang="zh", block=False)
            except Exception:
                pass

        self._voice.start_loop(
            llm_callback = self.voice_llm_callback,
            stop_word    = stop_word,
            on_thinking  = thinking_hint,
        )
        log.info("Voice+LLM loop started")

    def stop(self):
        self._running = False
        if self._voice:
            self._voice.stop()
        log.info("MultimodalAgent stopped")