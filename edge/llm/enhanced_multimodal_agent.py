# edge/llm/enhanced_multimodal_agent.py
"""
增强版多模态融合代理 - 演示优化版

优化目标：
1. 流畅的语音对话（流式TTS）
2. 用户可随时打断LLM输出
3. 无缝衔接新话题
4. 暂不使用传感器数据（简化演示）

架构变更：
- 使用StreamLLMClient替代RKLLMClient
- 使用InterruptibleVoiceModule替代VoiceModule
- 简化提示词，加速推理
"""
import sys
import time
import json
import re
import logging
import threading
from typing import Optional, Callable, Dict, Any
from pathlib import Path

# 确保当前目录在路径中
_CURRENT_DIR = Path(__file__).resolve().parent
if str(_CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(_CURRENT_DIR))

log = logging.getLogger("enhanced_agent")


class StreamingAgent:
    """
    流式对话代理
    针对演示优化：
    - 不使用传感器数据（避免LLM幻觉问题）
    - 使用流式输出+流式TTS
    - 支持随时打断
    """

    # 极简系统提示词 - 减少token，加速推理
    SYSTEM_PROMPT = "你是智能家居助手小灵。用中文简洁回答。"

    def __init__(self,
                 stream_llm_client,
                 voice_module=None,
                 enable_voice_loop: bool = True):
        self._llm = stream_llm_client
        self._voice = voice_module
        self._enable_voice = enable_voice_loop
        self._running = False

        # 打断控制
        self._interrupt_event = threading.Event()
        self._generating = False

    def _should_stop(self) -> bool:
        """检查是否应该停止生成"""
        return self._interrupt_event.is_set()

    def ask_stream(self, user_input: str,
                   on_token: Optional[Callable[[str], None]] = None,
                   on_sentence: Optional[Callable[[str], None]] = None) -> str:
        """
        流式问答

        Args:
            user_input: 用户输入
            on_token: 每生成一个token的回调
            on_sentence: 每生成一个完整句子的回调（用于TTS）

        Returns:
            str: 完整回复
        """
        self._generating = True
        self._interrupt_event.clear()

        # 构建极简提示词
        prompt = "用户问: {}\n答:".format(user_input)

        full_response = []
        sentence_buffer = ""

        try:
            for token in self._llm.chat_stream(
                prompt,
                system_prompt=self.SYSTEM_PROMPT,
                enable_thinking=False,
                on_token=lambda t: self._safe_callback(on_token, t),
                should_stop=self._should_stop
            ):
                full_response.append(token)
                sentence_buffer += token

                # 检测完整句子
                if on_sentence:
                    while True:
                        match = re.search(r'[。！？!?\.…；;]', sentence_buffer)
                        if not match:
                            break
                        end_pos = match.end()
                        sentence = sentence_buffer[:end_pos].strip()
                        sentence_buffer = sentence_buffer[end_pos:]
                        if sentence:
                            on_sentence(sentence)

            # 刷新剩余内容
            if sentence_buffer.strip() and on_sentence:
                on_sentence(sentence_buffer.strip())

        except Exception as e:
            log.error("Stream error: {}".format(e))

        self._generating = False
        return "".join(full_response)

    def _safe_callback(self, callback: Optional[Callable], *args):
        """安全调用回调"""
        if callback:
            try:
                callback(*args)
            except Exception as e:
                log.debug("Callback error: {}".format(e))

    def interrupt(self):
        """打断当前生成"""
        if self._generating:
            log.info("Interrupting generation...")
            self._interrupt_event.set()

    def start_voice_loop(self, stop_word: str = "退出"):
        """启动语音对话循环"""
        if self._voice is None:
            log.warning("Voice module not set")
            return

        self._running = True

        def on_user_speak(text: str):
            """用户说话回调"""
            print("\n用户: {}".format(text))

        def on_assistant_speak(text: str):
            """助手说话回调"""
            print("\n助手: {}".format(text))

        def llm_callback(user_text: str, on_token: Callable, should_stop: Callable):
            """LLM流式回调包装"""
            # 将should_stop包装为检查中断事件
            def wrapped_should_stop():
                return should_stop() or self._interrupt_event.is_set()

            full_response = []
            sentence_buffer = ""

            for token in self._llm.chat_stream(
                user_text,
                system_prompt=self.SYSTEM_PROMPT,
                enable_thinking=False,
                on_token=on_token,
                should_stop=wrapped_should_stop
            ):
                full_response.append(token)
                sentence_buffer += token

                # 句子检测
                while True:
                    match = re.search(r'[。！？!?\.…；;]', sentence_buffer)
                    if not match:
                        break
                    end_pos = match.end()
                    sentence = sentence_buffer[:end_pos].strip()
                    sentence_buffer = sentence_buffer[end_pos:]
                    if sentence:
                        # 这里可以触发TTS
                        pass

            # 刷新剩余
            if sentence_buffer.strip():
                pass

        self._voice.start_dialog_loop(
            llm_callback,
            stop_word=stop_word,
            on_user_speak=on_user_speak,
            on_assistant_speak=on_assistant_speak
        )

    def stop(self):
        """停止"""
        self._running = False
        self.interrupt()
        if self._voice:
            self._voice.stop()

    def test_ask(self, user_input: str) -> Dict[str, Any]:
        """测试接口"""
        reply = self.ask_stream(user_input)
        return {
            "user_msg": user_input,
            "reply_text": reply,
        }

    def interactive_chat(self):
        """命令行交互模式（用于测试）"""
        print("\n" + "=" * 50)
        print("  文本对话模式")
        print("  输入 'quit' 退出")
        print("=" * 50 + "\n")

        while True:
            try:
                user_input = input("用户 ").strip()
                if user_input.lower() in ["quit", "退出", "exit"]:
                    break
                if not user_input:
                    continue

                print("助手 ", end="", flush=True)

                def on_token(token: str):
                    print(token, end="", flush=True)

                reply = self.ask_stream(user_input, on_token=on_token)
                print("\n")

            except KeyboardInterrupt:
                self.interrupt()
                print("\n[已打断]\n")
            except EOFError:
                break

        print("\n再见！")


def start_streaming_agent(llm_url: str = "http://localhost:8080",
                          lang: str = "zh"):
    """
    快速启动流式对话代理

    Returns:
        StreamingAgent实例
    """
    from stream_llm_client import StreamLLMClient
    from voice.interruptible_voice import InterruptibleVoiceModule

    # 创建LLM客户端
    llm = StreamLLMClient(base_url=llm_url)

    if not llm.is_alive():
        raise RuntimeError("LLM服务未在 {} 运行".format(llm_url))

    # 创建语音模块
    voice = InterruptibleVoiceModule(lang=lang)

    # 创建代理
    agent = StreamingAgent(
        stream_llm_client=llm,
        voice_module=voice,
        enable_voice_loop=True
    )

    return agent


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="voice", 
                       choices=["text", "voice", "test"])
    parser.add_argument("--llm-url", default="http://localhost:8080")
    parser.add_argument("--lang", default="zh")
    args = parser.parse_args()

    if args.mode == "test":
        # 简单测试
        from stream_llm_client import StreamLLMClient
        llm = StreamLLMClient(args.llm_url)

        if not llm.is_alive():
            print("LLM服务未运行")
            sys.exit(1)

        agent = StreamingAgent(llm)

        print("测试流式输出（输入 'quit' 退出）:")
        while True:
            user = input("用户 ").strip()
            if user == "quit":
                break
            print("助手 ", end="", flush=True)

            def on_token(t):
                print(t, end="", flush=True)

            agent.ask_stream(user, on_token=on_token)
            print("\n")

    elif args.mode == "text":
        # 文本交互模式
        agent = start_streaming_agent(args.llm_url, args.lang)
        agent.interactive_chat()

    else:  # voice
        # 语音对话模式
        print("启动语音对话...")
        agent = start_streaming_agent(args.llm_url, args.lang)
        agent.start_voice_loop(stop_word="退出")

        # 保持运行
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            agent.stop()
            print("\n已停止")
