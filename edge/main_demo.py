# edge/main_demo.py
"""
演示版主入口 - 流畅语音对话

★ v4 修复：
  - 系统提示词改为"回答前缀"方式，嵌入用户消息中
  - 服务端会把 system+user 合并为一条消息发给模型
  - 模型能同时看到指令和问题
"""
import sys
import time
import signal
import logging
from pathlib import Path
from typing import Optional, Tuple, Callable

_EDGE_DIR = Path(__file__).resolve().parent
if str(_EDGE_DIR) not in sys.path:
    sys.path.insert(0, str(_EDGE_DIR))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("main_demo")

_USB_KEYWORDS = ["generalplus", "usb audio device", "usb-audio", "usb audio", "usb"]

def find_usb_audio() -> Tuple[Optional[int], Optional[int], str, str]:
    import sounddevice as sd
    devices = sd.query_devices()
    input_idx = output_idx = None
    input_name = output_name = ""
    for kw in _USB_KEYWORDS:
        for i, dev in enumerate(devices):
            name_lower = dev["name"].lower()
            if kw not in name_lower: continue
            if input_idx is None and dev["max_input_channels"] > 0:
                input_idx = i; input_name = dev["name"]
            if output_idx is None and dev["max_output_channels"] > 0:
                output_idx = i; output_name = dev["name"]
        if input_idx is not None and output_idx is not None: break
    return input_idx, output_idx, input_name, output_name

# ============================================================
# ★ 系统提示词 - 作为前缀嵌入用户消息
# 服务端会把 system content + user content 合并为：
#   "简短回答以下问题。\n你是谁"
# 这样模型能同时看到指令和问题
# ============================================================
SYSTEM_PROMPT = "简短回答以下问题。"


def main():
    print("\n" + "=" * 60)
    print("  智能家居语音助手 - 演示版")
    print("=" * 60 + "\n")

    print("正在扫描音频设备...")
    try:
        in_idx, out_idx, in_name, out_name = find_usb_audio()
    except Exception as e:
        print(f"❌ 音频设备扫描失败: {e}"); return

    if in_idx is None or out_idx is None:
        print("❌ 未找到 USB 音频设备"); return

    print(f"✅ USB 输入设备: #{in_idx} {in_name}")
    print(f"✅ USB 输出设备: #{out_idx} {out_name}\n")

    try:
        from llm.stream_llm_client import StreamLLMClient
        llm = StreamLLMClient("http://localhost:8080")
        if not llm.is_alive():
            print("❌ LLM服务未运行\n   请先启动: python3 -m edge.llm.flask_server_enhanced"); return
        print("✅ LLM服务连接正常")
        llm.clear_history()
    except ImportError as e:
        print(f"❌ 导入错误: {e}"); return

    voice = None
    try:
        from voice.interruptible_voice import InterruptibleVoiceModule

        print("初始化语音模块（强制使用 USB 设备）...")
        voice = InterruptibleVoiceModule(lang="zh", input_device=in_idx, output_device=out_idx, auto_usb=False)

        def signal_handler(signum, frame):
            print("\n\n收到中断信号，正在退出...")
            if voice is not None: voice.stop()
            try:
                import sounddevice as sd; sd.stop()
            except Exception: pass
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        print("\n" + "-" * 60)
        print("  使用说明：")
        print("     说「退出」结束对话")
        print("     助手回答时，直接说话即可打断")
        print("     打断后可立即提出新话题")
        print("     按 Ctrl+C 强制退出")
        print("-" * 60 + "\n")

        def llm_callback(user_text: str, on_token: Callable, should_stop: Callable):
            for chunk in llm.chat_stream(
                user_text,
                system_prompt=SYSTEM_PROMPT,
                enable_thinking=False,
                on_token=on_token,
                should_stop=should_stop,
            ):
                pass

        voice.start_dialog_loop(llm_callback, stop_word="退出")

    except ImportError as e:
        print(f"❌ 语音模块导入错误: {e}")
        import traceback; traceback.print_exc()
    except SystemExit: pass
    except Exception as e:
        print(f"❌ 启动错误: {e}")
        import traceback; traceback.print_exc()
    finally:
        if voice:
            try: voice.stop()
            except Exception: pass
        try:
            import sounddevice as sd; sd.stop()
        except Exception: pass

    print("\n演示结束")


if __name__ == "__main__":
    main()