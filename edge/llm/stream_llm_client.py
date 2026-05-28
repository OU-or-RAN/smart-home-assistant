"""
edge/llm/stream_llm_client.py

★ v3 修复：
  - 后台打断监控线程：检测到打断信号后立即发 /abort，不再等 iter_lines 解除阻塞
  - 打断延迟从 12 秒降到 < 0.5 秒
"""
import requests
import json
import time
import threading
import logging
import re
from typing import Iterator, Optional, Callable

log = logging.getLogger("stream_llm")


class StreamLLMClient:
    def __init__(self, base_url="http://localhost:8080", timeout=60.0):
        self.base_url = base_url
        self.timeout = timeout
        self.chat_endpoint = f"{base_url}/rkllm_chat"
        self.health_endpoint = f"{base_url}/health"
        self.abort_endpoint = f"{base_url}/abort"

        self._history = []
        self._history_lock = threading.Lock()
        self._generating = False
        self._current_response = []

    def is_alive(self):
        try:
            resp = requests.get(self.health_endpoint, timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    def clear_history(self):
        with self._history_lock:
            self._history.clear()
        log.info("Dialogue history cleared")

    def get_history(self) -> list:
        with self._history_lock:
            return list(self._history)

    def _build_messages(self, user_message: str, system_prompt: Optional[str] = None) -> list:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        with self._history_lock:
            recent = self._history[-4:] if len(self._history) > 4 else list(self._history)
            messages.extend(recent)
        messages.append({"role": "user", "content": user_message})
        return messages

    @staticmethod
    def _sanitize_text(text: str) -> str:
        if not text:
            return text
        last_think = text.rfind('</think>')
        if last_think >= 0:
            text = text[last_think + len('</think>'):]
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
        text = re.sub(r'<\|[^|]+\|>', '', text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\\boxed\{([^}]*)\}', r'\1', text)
        text = re.sub(r'\\text\{([^}]*)\}', r'\1', text)
        text = re.sub(r'\\frac\{([^}]*)\}\{([^}]*)\}', r'\1/\2', text)
        text = re.sub(r'\\[a-zA-Z]+\s*', '', text)
        text = re.sub(r'\\\(|\\\)|\\\[|\\\]', '', text)
        text = re.sub(r'\*\*([^*\n]+?)\*\*', r'\1', text)
        text = re.sub(r'\*([^*\n]+?)\*', r'\1', text)
        text = re.sub(r'`([^`\n]+?)`', r'\1', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def chat_stream(self,
                    user_message: str,
                    system_prompt: Optional[str] = None,
                    enable_thinking: bool = False,
                    on_token: Optional[Callable[[str], None]] = None,
                    should_stop: Optional[Callable[[], bool]] = None) -> Iterator[str]:
        self._generating = True
        self._current_response = []
        _abort_sent = threading.Event()

        messages = self._build_messages(user_message, system_prompt)

        # ★★★ 核心修复：后台打断监控线程
        # iter_lines() 阻塞时无法检查 should_stop
        # 这个线程每 50ms 检查一次，检测到打断信号后直接发 /abort 给服务端
        def _abort_monitor():
            while not _abort_sent.is_set() and self._generating:
                if should_stop and should_stop():
                    log.info("Generation interrupted by user")
                    self._abort_generation()
                    _abort_sent.set()
                    return
                time.sleep(0.05)

        if should_stop:
            threading.Thread(target=_abort_monitor, daemon=True).start()

        try:
            resp = requests.post(
                self.chat_endpoint,
                json={"messages": messages, "stream": True, "enable_thinking": enable_thinking},
                stream=True,
                timeout=self.timeout
            )
            resp.raise_for_status()

            for line in resp.iter_lines():
                # ★ 收到 abort 信号后立即跳出
                if _abort_sent.is_set():
                    break

                if not line:
                    continue
                line = line.decode('utf-8')
                if line.startswith('data: '):
                    line = line[6:]
                try:
                    data = json.loads(line)
                    if "choices" in data and len(data["choices"]) > 0:
                        choice = data["choices"][0]
                        delta = choice.get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            self._current_response.append(content)
                            if on_token:
                                on_token(content)
                            yield content
                        if choice.get("finish_reason") in ("stop", "abort"):
                            break
                except json.JSONDecodeError:
                    continue

        except requests.exceptions.Timeout:
            log.error("LLM stream timeout")
            yield "[LLM响应超时]"
        except requests.exceptions.ConnectionError:
            log.error("LLM connection error")
            yield "[LLM连接错误]"
        except Exception as e:
            log.error(f"LLM stream error: {e}")
        finally:
            _abort_sent.set()    # 确保监控线程退出
            self._generating = False
            full_response = "".join(self._current_response)
            full_response = self._sanitize_text(full_response)
            user_message_clean = self._sanitize_text(user_message)

            with self._history_lock:
                self._history.append({"role": "user", "content": user_message_clean})
                if full_response:
                    self._history.append({"role": "assistant", "content": full_response})
                else:
                    self._history.append({"role": "assistant", "content": "好的"})
                while len(self._history) > 8:
                    self._history.pop(0)

    def chat(self, user_message: str, system_prompt: Optional[str] = None, enable_thinking: bool = False) -> str:
        return "".join(self.chat_stream(user_message, system_prompt, enable_thinking))

    def _abort_generation(self):
        try:
            requests.post(self.abort_endpoint, timeout=2.0)
            log.info("Abort signal sent to LLM server")
        except Exception as e:
            log.debug(f"Abort request failed: {e}")

    def is_generating(self) -> bool:
        return self._generating