"""
LLM 客户端封装，用于与 RKLLM Flask 服务通信
"""
import requests
import json
import time


class RKLLMClient:
    def __init__(self, base_url="http://localhost:8080", timeout=30.0):
        self.base_url = base_url
        self.timeout = timeout
        self.chat_endpoint = f"{base_url}/rkllm_chat"
        self.health_endpoint = f"{base_url}/health"
    
    def is_alive(self):
        """检查 LLM 服务是否在线"""
        try:
            resp = requests.get(self.health_endpoint, timeout=5.0)
            return resp.status_code == 200
        except Exception as e:
            print(f"LLM health check failed: {e}")
            return False
    
    def chat(self, message, system_prompt=None, timeout=None, enable_thinking=False):
        """
        发送单轮对话请求
        
        Args:
            message: 用户消息
            system_prompt: 可选的系统提示词
            timeout: 超时时间
            enable_thinking: 是否启用思考模式（DeepSeek-R1 特有）
        
        Returns:
            str: LLM 回复文本
        """
        if timeout is None:
            timeout = self.timeout
            
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": message})
        
        try:
            resp = requests.post(
                self.chat_endpoint,
                json={"messages": messages, "stream": False, "enable_thinking": enable_thinking},
                timeout=timeout
            )
            resp.raise_for_status()
            data = resp.json()
            
            if "choices" in data and len(data["choices"]) > 0:
                return data["choices"][0]["message"]["content"]
            else:
                return "[LLM错误: 空响应]"
                
        except requests.exceptions.Timeout:
            return "[LLM错误: 请求超时]"
        except requests.exceptions.ConnectionError:
            return "[LLM错误: 无法连接到服务]"
        except Exception as e:
            return f"[LLM错误: {str(e)}]"
    
    def chat_with_system(self, system, user, timeout=None, enable_thinking=False):
        """使用指定系统提示词进行对话"""
        return self.chat(user, system_prompt=system, timeout=timeout, enable_thinking=enable_thinking)
    
    def chat_stream(self, messages, enable_thinking=False):
        """
        流式对话（用于语音交互等实时场景）
        
        Args:
            messages: 消息列表
            enable_thinking: 是否启用思考模式
        
        Yields:
            str: 生成的文本片段
        """
        try:
            resp = requests.post(
                self.chat_endpoint,
                json={"messages": messages, "stream": True, "enable_thinking": enable_thinking},
                stream=True,
                timeout=self.timeout
            )
            resp.raise_for_status()
            
            for line in resp.iter_lines():
                if line:
                    line = line.decode('utf-8')
                    if line.startswith('data: '):
                        line = line[6:]
                    try:
                        data = json.loads(line)
                        if "choices" in data and len(data["choices"]) > 0:
                            delta = data["choices"][0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield content
                    except json.JSONDecodeError:
                        continue
                        
        except Exception as e:
            yield f"[LLM错误: {str(e)}]"
            return