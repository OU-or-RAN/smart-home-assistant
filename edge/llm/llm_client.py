# edge/llm/llm_client.py
import requests
import json

class RKLLMClient:
    """
    封装 rkllm_server_demo 的 Flask HTTP 接口。
    启动方式：
      cd /home/lubancat/rknn-llm/examples/rkllm_api_demo/
      ./rkllm_server_demo model.rkllm --port 8080
    """
    def __init__(self, base_url: str = "http://localhost:8080"):
        self._url = base_url.rstrip("/")

    def chat(self, prompt: str, max_tokens: int = 512,
             timeout: float = 30.0) -> str:
        try:
            resp = requests.post(
                f"{self._url}/chat",
                json={"input": prompt, "max_new_tokens": max_tokens},
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            # rkllm_server_demo 返回格式：{"output": "..."}
            return data.get("output", "").strip()
        except requests.exceptions.Timeout:
            return "[LLM超时，请重试]"
        except Exception as e:
            return f"[LLM错误: {e}]"

    def is_alive(self) -> bool:
        try:
            resp = requests.get(f"{self._url}/health", timeout=2)
            return resp.status_code == 200
        except Exception:
            return False