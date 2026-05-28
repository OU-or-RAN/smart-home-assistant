# # edge/voice/interruptible_voice.py v4.0
# """
# 可打断的流式语音交互模块 - 批量TTS修复版

# ★ v4.0 关键修复：
# 1. 批量TTS：LLM生成期间收集token，生成完毕后清洗文本再合成语音
#    解决了 ThinkingFilter 泄露垃圾文本到TTS的问题
# 2. 修复 is_playing() 竞态条件：增加 _has_content + _synth_done 追踪
#    解决了播放等待循环立刻退出的问题
# 3. 保留流式打断：LLM生成期间用户可随时打断
# 4. TTS文本预处理：数字/数学符号/LaTeX/Markdown → 中文可读
# """
# import re
# import sys
# import time
# import signal
# import logging
# import threading
# import queue
# import collections
# import numpy as np
# from pathlib import Path
# from typing import Optional, Callable, Tuple

# _EDGE_ROOT = Path(__file__).resolve().parent.parent
# if str(_EDGE_ROOT) not in sys.path:
#     sys.path.insert(0, str(_EDGE_ROOT))

# _THIS_DIR = Path(__file__).resolve().parent
# _MODELS = _THIS_DIR / "models"
# _CACHE = _THIS_DIR / "audio_cache"
# _CACHE.mkdir(parents=True, exist_ok=True)

# logging.basicConfig(
#     level=logging.INFO,
#     format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
#     datefmt="%H:%M:%S",
# )
# log = logging.getLogger("interruptible_voice")

# USB_KEYWORDS = [
#     "generalplus",
#     "usb audio device",
#     "usb-audio",
#     "usb audio",
#     "usb",
# ]

# # ── VAD 打断参数 ─────────────────────────────────────────────────────────────
# INTERRUPT_GUARD_SEC = 1.2
# VAD_INTERRUPT_FACTOR = 4.5
# VAD_CONFIRM_FRAMES = 5
# TTS_TAIL_SILENCE_SEC = 0.25

# # ── 分句参数 ──────────────────────────────────────────────────────────────────
# SENTENCE_END_RE = re.compile(r'[。！？!?\.…；;]')
# SUB_SENTENCE_RE = re.compile(r'[，,、：:]')
# LONG_SENTENCE_THRESHOLD = 18

# from voice.voice_module import (
#     ASR_RATE, CHANNELS, RECORD_DTYPE, VAD_CHUNK_MS, VAD_CALIB_SEC,
#     VAD_THRESH_FACTOR, VAD_THRESH_MIN, VAD_THRESH_MAX,
#     SILENCE_TIMEOUT, MAX_RECORD_SEC,
#     split_sentences, SenseVoiceASR, SherpaTTS, VoiceRecorder,
# )


# # ══════════════════════════════════════════════════════════════════════════════
# # ★ LLM 回复清洗（用于 TTS 前处理）
# # ══════════════════════════════════════════════════════════════════════════════
# def sanitize_llm_response(text: str) -> str:
#     """
#     清洗 LLM 原始回复，提取最终答案。
#     - 去除所有 </think> 之前的思考内容
#     - 去除特殊标记、LaTeX、Markdown
#     """
#     if not text:
#         return ""
#     # 取最后一个 </think> 之后的内容
#     last_think = text.rfind('</think>')
#     if last_think >= 0:
#         text = text[last_think + len('</think>'):]
#     # 兜底：<think>...</think>
#     text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
#     # 特殊 token
#     text = re.sub(r'<\|[^|]+\|>', '', text)
#     text = re.sub(r'<[^>]+>', '', text)
#     # LaTeX
#     text = re.sub(r'\\boxed\{([^}]*)\}', r'\1', text)
#     text = re.sub(r'\\text\{([^}]*)\}', r'\1', text)
#     text = re.sub(r'\\frac\{([^}]*)\}\{([^}]*)\}', r'\1分之\2', text)
#     text = re.sub(r'\\[a-zA-Z]+\s*', '', text)
#     text = re.sub(r'\\\(|\\\)|\\\[|\\\]', '', text)
#     # Markdown
#     text = re.sub(r'\*\*([^*\n]+?)\*\*', r'\1', text)
#     text = re.sub(r'\*([^*\n]+?)\*', r'\1', text)
#     text = re.sub(r'`([^`\n]+?)`', r'\1', text)
#     # 合并空白
#     text = re.sub(r'\s+', ' ', text).strip()
#     return text


# # ══════════════════════════════════════════════════════════════════════════════
# # TTS 文本预处理
# # ══════════════════════════════════════════════════════════════════════════════
# _DIGITS_CN = ['零', '一', '二', '三', '四', '五', '六', '七', '八', '九']


# def _int_to_chinese(s: str) -> str:
#     if not s or not s.isdigit():
#         return s
#     n = int(s)
#     if n == 0:
#         return '零'
#     if n < 10:
#         return _DIGITS_CN[n]
#     if n < 100:
#         tens, ones = n // 10, n % 10
#         head = '十' if tens == 1 else _DIGITS_CN[tens] + '十'
#         return head if ones == 0 else head + _DIGITS_CN[ones]
#     if n < 1000:
#         hundreds, rest = n // 100, n % 100
#         head = _DIGITS_CN[hundreds] + '百'
#         if rest == 0:
#             return head
#         if rest < 10:
#             return head + '零' + _DIGITS_CN[rest]
#         tens, ones = rest // 10, rest % 10
#         sub = _DIGITS_CN[tens] + '十'
#         if ones > 0:
#             sub += _DIGITS_CN[ones]
#         return head + sub
#     if n < 10000:
#         thousands, rest = n // 1000, n % 1000
#         head = _DIGITS_CN[thousands] + '千'
#         if rest == 0:
#             return head
#         if rest < 100:
#             return head + '零' + _int_to_chinese(str(rest))
#         return head + _int_to_chinese(str(rest))
#     return ''.join(_DIGITS_CN[int(d)] for d in s if d.isdigit())


# def _num_to_chinese(num_str: str) -> str:
#     if '.' in num_str:
#         int_part, dec_part = num_str.split('.', 1)
#         int_chinese = _int_to_chinese(int_part) if int_part else '零'
#         dec_chinese = ''.join(_DIGITS_CN[int(d)] if d.isdigit() else d for d in dec_part)
#         return int_chinese + '点' + dec_chinese
#     return _int_to_chinese(num_str)


# def preprocess_for_tts(text: str) -> str:
#     if not text:
#         return ""
#     # LaTeX
#     text = re.sub(r'\\boxed\{([^}]*)\}', r'\1', text)
#     text = re.sub(r'\\text\{([^}]*)\}', r'\1', text)
#     text = re.sub(r'\\frac\{([^}]*)\}\{([^}]*)\}', r'\1分之\2', text)
#     text = re.sub(r'\\[a-zA-Z]+\s*', '', text)
#     text = re.sub(r'\\\(|\\\)|\\\[|\\\]', '', text)
#     # Markdown
#     text = re.sub(r'\*\*([^*\n]+?)\*\*', r'\1', text)
#     text = re.sub(r'\*([^*\n]+?)\*', r'\1', text)
#     text = re.sub(r'`([^`\n]+?)`', r'\1', text)
#     text = re.sub(r'^#{1,6}\s*', '', text, flags=re.MULTILINE)
#     text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
#     text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
#     # 括号
#     text = re.sub(r'[\(\)\[\]\{\}（）【】]', '', text)
#     # 百分号
#     text = re.sub(
#         r'(\d+(?:\.\d+)?)\s*%',
#         lambda m: '百分之' + _num_to_chinese(m.group(1)),
#         text,
#     )
#     # 指数
#     text = re.sub(
#         r'\^\s*(\d+)',
#         lambda m: '的' + _num_to_chinese(m.group(1)) + '次方',
#         text,
#     )
#     # 数字
#     text = re.sub(r'\d+(?:\.\d+)?', lambda m: _num_to_chinese(m.group(0)), text)
#     # 数学符号
#     math_repl = {
#         '+': '加', '-': '减', '×': '乘', '*': '乘',
#         '÷': '除以', '=': '等于',
#         '≤': '小于等于', '≥': '大于等于', '≠': '不等于',
#         '<': '小于', '>': '大于',
#         '\\': '', '_': '', '^': '',
#         '|': '', '~': '', '@': '',
#     }
#     for k, v in math_repl.items():
#         text = text.replace(k, v)
#     text = re.sub(
#         r'(?<=[\u4e00-\u9fff])\s*/\s*(?=[\u4e00-\u9fff])', '除以', text)
#     text = text.replace('/', ' ')
#     # 空白规整
#     text = re.sub(r'\n+', '。', text)
#     text = re.sub(r'。+', '。', text)
#     text = re.sub(r'[ \t]+', ' ', text)
#     text = re.sub(r'\s*，\s*', '，', text)
#     text = text.strip(' 。\n')
#     return text


# # ══════════════════════════════════════════════════════════════════════════════
# # 设备工具
# # ══════════════════════════════════════════════════════════════════════════════
# def list_audio_devices():
#     import sounddevice as sd
#     print("\n=== Audio Devices ===")
#     try:
#         devices = sd.query_devices()
#     except Exception as e:
#         print(f"  query_devices 失败: {e}")
#         return
#     for i, dev in enumerate(devices):
#         name_lower = dev["name"].lower()
#         marker = " [USB]" if any(k in name_lower for k in USB_KEYWORDS) else ""
#         print(
#             f"  {i}: {dev['name']}{marker}  "
#             f"in={dev['max_input_channels']} out={dev['max_output_channels']} "
#             f"rate={int(dev['default_samplerate'])}"
#         )
#     print()


# def find_usb_audio_devices() -> Tuple[Optional[int], Optional[int]]:
#     import sounddevice as sd
#     try:
#         devices = sd.query_devices()
#     except Exception as e:
#         log.warning(f"query_devices 失败: {e}")
#         return None, None
#     input_idx = None
#     output_idx = None
#     for kw in USB_KEYWORDS:
#         for i, dev in enumerate(devices):
#             name_lower = dev["name"].lower()
#             if kw not in name_lower:
#                 continue
#             if input_idx is None and dev["max_input_channels"] > 0:
#                 input_idx = i
#             if output_idx is None and dev["max_output_channels"] > 0:
#                 output_idx = i
#         if input_idx is not None and output_idx is not None:
#             break
#     return input_idx, output_idx


# def probe_device_rate(device_idx: Optional[int],
#                       preferred_rates=(16000, 44100, 48000)) -> int:
#     if device_idx is None:
#         return 16000
#     import sounddevice as sd
#     try:
#         info = sd.query_devices(device_idx)
#         return int(info.get("default_samplerate", 16000))
#     except Exception as e:
#         log.debug(f"probe_device_rate({device_idx}) 失败: {e}")
#         return 16000


# # ══════════════════════════════════════════════════════════════════════════════
# # ContinuousAudioPlayer
# # ══════════════════════════════════════════════════════════════════════════════
# class ContinuousAudioPlayer:
#     """
#     sd.OutputStream 回调实现无间隙连续播放。
#     """

#     def __init__(self, output_device: Optional[int], device_rate: int):
#         self._device = output_device
#         self._rate = device_rate
#         self._audio_chunks: collections.deque = collections.deque()
#         self._chunks_lock = threading.Lock()
#         self._stream = None
#         self._active = False
#         self.is_speaking = threading.Event()
#         self._ever_fed = False
#         self._feeding_done = False

#     def start(self):
#         import sounddevice as sd
#         if self._stream is not None:
#             return
#         self._active = True
#         self._ever_fed = False
#         self._feeding_done = False
#         try:
#             self._stream = sd.OutputStream(
#                 samplerate=self._rate,
#                 channels=1,
#                 dtype='float32',
#                 device=self._device,
#                 callback=self._callback,
#                 blocksize=1024,
#             )
#             self._stream.start()
#         except Exception as e:
#             log.error(f"OutputStream 启动失败: {e}")
#             self._stream = None

#     def stop(self):
#         self._active = False
#         with self._chunks_lock:
#             self._audio_chunks.clear()
#         self.is_speaking.clear()
#         self._ever_fed = False
#         self._feeding_done = False
#         if self._stream is not None:
#             try:
#                 self._stream.stop()
#                 self._stream.close()
#             except Exception:
#                 pass
#             self._stream = None

#     def feed_audio(self, audio_float32: np.ndarray):
#         if not self._active or audio_float32 is None or len(audio_float32) == 0:
#             return
#         with self._chunks_lock:
#             self._audio_chunks.append(audio_float32.copy())
#         self._ever_fed = True
#         self.is_speaking.set()

#     def mark_done(self):
#         self._feeding_done = True

#     def _callback(self, outdata, frames, time_info, status):
#         needed = frames
#         result = np.zeros(needed, dtype=np.float32)
#         pos = 0
#         with self._chunks_lock:
#             while pos < needed and self._audio_chunks:
#                 chunk = self._audio_chunks[0]
#                 available = len(chunk)
#                 take = min(available, needed - pos)
#                 result[pos:pos + take] = chunk[:take]
#                 if take < available:
#                     self._audio_chunks[0] = chunk[take:]
#                 else:
#                     self._audio_chunks.popleft()
#                 pos += take
#             has_data = len(self._audio_chunks) > 0
#         outdata[:, 0] = result
#         if pos > 0:
#             self.is_speaking.set()
#         elif not has_data and self._feeding_done:
#             self.is_speaking.clear()

#     def is_playing(self) -> bool:
#         with self._chunks_lock:
#             has_data = len(self._audio_chunks) > 0
#         if has_data:
#             return True
#         if self._ever_fed and not self._feeding_done:
#             return True
#         return self.is_speaking.is_set()


# # ══════════════════════════════════════════════════════════════════════════════
# # StreamTTSPlayer
# # ★ v4.0: 修复 is_playing() 竞态 - 增加 _has_content + _synth_done
# # ══════════════════════════════════════════════════════════════════════════════
# class StreamTTSPlayer:
#     def __init__(self, tts_engine: SherpaTTS, audio_player: ContinuousAudioPlayer,
#                  lang: str = "zh"):
#         self._tts = tts_engine
#         self._player = audio_player
#         self._lang = lang
#         self._buffer = ""
#         self._stop_flag = threading.Event()
#         self._sentence_queue: queue.Queue = queue.Queue()
#         self._synth_thread: Optional[threading.Thread] = None
#         # ★ 新增：追踪是否有待处理的内容
#         self._has_content = False
#         self._synth_done = threading.Event()

#     def start(self):
#         self._stop_flag.clear()
#         self._buffer = ""
#         self._has_content = False
#         self._synth_done.clear()
#         self._player.start()
#         self._synth_thread = threading.Thread(target=self._synth_loop, daemon=True)
#         self._synth_thread.start()

#     def stop(self):
#         self._stop_flag.set()
#         while not self._sentence_queue.empty():
#             try:
#                 self._sentence_queue.get_nowait()
#             except queue.Empty:
#                 break
#         self._player.mark_done()
#         self._player.stop()
#         self._buffer = ""
#         self._has_content = False
#         self._synth_done.set()

#     def feed_text(self, text: str):
#         """
#         累积文本并按规则切句送入合成队列。
#         """
#         if text.strip():
#             self._has_content = True

#         self._buffer += text
#         while True:
#             end_match = SENTENCE_END_RE.search(self._buffer)
#             sub_match = SUB_SENTENCE_RE.search(self._buffer)

#             if not end_match and not sub_match:
#                 break

#             if end_match:
#                 end_pos = end_match.end()
#                 sentence = self._buffer[:end_pos].strip()
#                 self._buffer = self._buffer[end_pos:]
#                 if sentence:
#                     self._sentence_queue.put(sentence)
#                 continue

#             if sub_match and sub_match.end() >= LONG_SENTENCE_THRESHOLD:
#                 end_pos = sub_match.end()
#                 sentence = self._buffer[:end_pos].strip()
#                 self._buffer = self._buffer[end_pos:]
#                 if sentence:
#                     self._sentence_queue.put(sentence)
#                 continue

#             break

#     def flush(self):
#         if self._buffer.strip():
#             self._has_content = True
#             self._sentence_queue.put(self._buffer.strip())
#             self._buffer = ""
#         self._sentence_queue.put(None)

#     def _synth_loop(self):
#         import librosa
#         while not self._stop_flag.is_set():
#             try:
#                 sentence = self._sentence_queue.get(timeout=0.1)
#             except queue.Empty:
#                 continue
#             if sentence is None:
#                 self._player.mark_done()
#                 break
#             if self._stop_flag.is_set():
#                 break

#             processed = preprocess_for_tts(sentence)
#             if not processed:
#                 continue

#             sub_sentences = split_sentences(processed, max_chars=50)
#             for sent in sub_sentences:
#                 if self._stop_flag.is_set():
#                     break
#                 try:
#                     audio, sr = self._tts.synthesize_sentence(
#                         sent, self._lang, speed=1.0)
#                     if audio is not None and len(audio) > 0:
#                         if sr != self._player._rate:
#                             audio = librosa.resample(
#                                 audio, orig_sr=sr, target_sr=self._player._rate)
#                         self._player.feed_audio(audio)
#                 except Exception as e:
#                     log.error(f"TTS synth error: {e}")

#         self._player.mark_done()
#         self._synth_done.set()  # ★ 标记合成完成

#     def is_playing(self) -> bool:
#         """
#         ★ 修复后的 is_playing()：
#         - 如果有内容且合成线程未完成 → True
#         - 否则检查音频播放器是否在播放
#         """
#         if self._has_content and not self._synth_done.is_set():
#             return True
#         return self._player.is_playing()


# # ══════════════════════════════════════════════════════════════════════════════
# # InterruptibleVoiceModule
# # ★ v4.0: 批量TTS模式 - 生成完毕后清洗文本再合成
# # ══════════════════════════════════════════════════════════════════════════════
# class InterruptibleVoiceModule:
#     def __init__(self,
#                  lang: str = "zh",
#                  input_device: Optional[int] = None,
#                  output_device: Optional[int] = None,
#                  auto_usb: bool = True,
#                  tts_speed: float = 1.1,
#                  enable_mqtt: bool = False):
#         self._lang = lang
#         self._tts_speed = tts_speed
#         self._running = False
#         self._in_dialog = False
#         self._tts_active = False
#         self._interrupt_flag = threading.Event()
#         self._dialog_lock = threading.Lock()

#         if auto_usb and (input_device is None or output_device is None):
#             ai, ao = find_usb_audio_devices()
#             if input_device is None:
#                 input_device = ai
#             if output_device is None:
#                 output_device = ao

#         try:
#             import sounddevice as sd
#             in_name = (sd.query_devices(input_device)["name"]
#                        if input_device is not None else "default")
#             out_name = (sd.query_devices(output_device)["name"]
#                         if output_device is not None else "default")
#             print(f"[InterruptibleVoice] mic  = #{input_device} {in_name}")
#             print(f"[InterruptibleVoice] spk  = #{output_device} {out_name}")
#         except Exception:
#             print(f"[InterruptibleVoice] mic={input_device} speaker={output_device}")

#         self._asr = SenseVoiceASR(lang=lang)
#         self._tts = SherpaTTS(output_device=output_device)
#         self._recorder = VoiceRecorder(input_device=input_device)

#         self._output_device = output_device
#         self._output_rate = self._tts._device_rate

#         self._audio_player = ContinuousAudioPlayer(output_device, self._output_rate)
#         self._stream_player = StreamTTSPlayer(
#             self._tts, self._audio_player, lang=lang)

#         self._vad_running = False
#         self._vad_thread: Optional[threading.Thread] = None

#         print(f"[InterruptibleVoice] 就绪 lang={lang}")

#     def _should_interrupt(self) -> bool:
#         return self._interrupt_flag.is_set()

#     # ── VAD 打断监控 ────────────────────────────────────────────────────────────

#     def _start_vad_monitor(self):
#         if self._vad_thread is not None and self._vad_thread.is_alive():
#             return
#         self._vad_running = True

#         def vad_loop():
#             import sounddevice as sd
#             chunk_samples = int(
#                 self._recorder._device_rate * VAD_CHUNK_MS / 1000)
#             base_thresh = self._recorder._silence_thresh
#             interrupt_thresh = base_thresh * VAD_INTERRUPT_FACTOR

#             try:
#                 stream = sd.InputStream(
#                     samplerate=self._recorder._device_rate,
#                     channels=CHANNELS,
#                     dtype=RECORD_DTYPE,
#                     blocksize=chunk_samples,
#                     device=self._recorder._input_device,
#                 )
#             except Exception as e:
#                 log.error(f"VAD InputStream 启动失败: {e}")
#                 return

#             consecutive_loud = 0

#             with stream:
#                 while self._vad_running and self._running:
#                     if not self._in_dialog:
#                         consecutive_loud = 0
#                         time.sleep(0.05)
#                         continue

#                     if self._tts_active or self._audio_player.is_speaking.is_set():
#                         consecutive_loud = 0
#                         try:
#                             stream.read(chunk_samples)
#                         except Exception:
#                             pass
#                         time.sleep(0.02)
#                         continue

#                     try:
#                         data, _ = stream.read(chunk_samples)
#                         arr = np.asarray(data)
#                         if arr.ndim > 1:
#                             arr = arr[:, 0]
#                         rms = float(np.sqrt(
#                             np.mean(arr.astype(np.float32) ** 2)))

#                         if rms > interrupt_thresh:
#                             consecutive_loud += 1
#                             if consecutive_loud >= VAD_CONFIRM_FRAMES:
#                                 if (self._in_dialog
#                                         and not self._interrupt_flag.is_set()):
#                                     log.info(
#                                         f"[VAD] 检测到用户说话 "
#                                         f"(rms={rms:.0f} > {interrupt_thresh:.0f}), "
#                                         f"连续{consecutive_loud}帧，触发打断")
#                                     self._interrupt_flag.set()
#                                 consecutive_loud = 0
#                         else:
#                             consecutive_loud = 0
#                     except Exception as e:
#                         log.debug(f"VAD read error: {e}")
#                         time.sleep(0.05)

#         self._vad_thread = threading.Thread(target=vad_loop, daemon=True)
#         self._vad_thread.start()
#         log.info("VAD monitor started")

#     def _stop_vad_monitor(self):
#         self._vad_running = False
#         if self._vad_thread is not None:
#             self._vad_thread.join(timeout=2.0)
#             self._vad_thread = None
#         log.info("VAD monitor stopped")

#     # ── ASR 录音 ────────────────────────────────────────────────────────────────

#     def _perform_asr(self) -> str:
#         self._stop_vad_monitor()
#         print("\n🎤 请说话...")

#         def on_start():
#             print("  [录音中...]")

#         def on_stop():
#             print("  [识别中...]")

#         try:
#             audio = self._recorder.record_once(
#                 on_start=on_start,
#                 on_stop=on_stop,
#                 auto_calibrate=True,
#                 running_check=lambda: self._running,
#             )
#         finally:
#             self._start_vad_monitor()

#         if not self._running:
#             return ""

#         text = self._asr.recognize(audio)
#         if text.strip():
#             print(f"  [识别结果: {text}]")
#         else:
#             print("  [未识别到语音]")
#         return text.strip()

#     # ── 主对话循环 ──────────────────────────────────────────────────────────────
#     # ★ v4.0: 批量TTS - 收集token → 清洗 → 合成 → 播放
#     # ──────────────────────────────────────────────────────────────────────────

#     def start_dialog_loop(
#             self,
#             llm_stream_callback: Callable,
#             stop_word: str = "退出",
#             on_user_speak: Optional[Callable[[str], None]] = None,
#             on_assistant_speak: Optional[Callable[[str], None]] = None):

#         if self._running:
#             log.warning("Dialog loop already running")
#             return

#         self._running = True
#         self._start_vad_monitor()

#         print(f"\n{'=' * 50}")
#         print("  🏠 智能家居语音助手已启动")
#         print(f"  🛑 说「{stop_word}」结束对话")
#         print("  💡 助手回答时，直接说话即可打断")
#         print(f"{'=' * 50}\n")

#         try:
#             while self._running:
#                 user_text = self._perform_asr()
#                 if not self._running:
#                     break
#                 if not user_text:
#                     continue
#                 if stop_word in user_text:
#                     print("\n👋 再见！")
#                     self._tts.speak("好的，再见", block=False)
#                     break

#                 if on_user_speak:
#                     on_user_speak(user_text)

#                 with self._dialog_lock:
#                     self._in_dialog = True
#                     self._interrupt_flag.clear()

#                 dialog_start_time = time.time()
#                 print(f"\n🤖 助手思考中...")

#                 # ★ 步骤1：收集 LLM 所有 token（不喂给 TTS）
#                 full_response = []

#                 def on_token(token: str):
#                     full_response.append(token)

#                 def should_stop_with_guard() -> bool:
#                     if not self._interrupt_flag.is_set():
#                         return False
#                     elapsed = time.time() - dialog_start_time
#                     if elapsed < INTERRUPT_GUARD_SEC:
#                         self._interrupt_flag.clear()
#                         return False
#                     return True

#                 was_interrupted = False
#                 try:
#                     llm_stream_callback(
#                         user_text,
#                         on_token=on_token,
#                         should_stop=should_stop_with_guard,
#                     )
#                 except Exception as e:
#                     log.error(f"LLM callback error: {e}")

#                 was_interrupted = self._interrupt_flag.is_set()

#                 # ★ 步骤2：清洗 LLM 回复（去除思考内容）
#                 raw_text = "".join(full_response)
#                 clean_text = sanitize_llm_response(raw_text)
#                 print(f"  💬 助手: {clean_text}" if clean_text else "  💬 (无有效回复)")

#                 if on_assistant_speak:
#                     on_assistant_speak(clean_text)

#                 # ★ 步骤3：用 TTS 播放清洗后的文本（如果没被打断）
#                 if clean_text.strip() and not was_interrupted:
#                     # 重建播放器
#                     self._stream_player.stop()
#                     self._audio_player.stop()
#                     time.sleep(0.02)

#                     self._audio_player = ContinuousAudioPlayer(
#                         self._output_device, self._output_rate)
#                     self._stream_player = StreamTTSPlayer(
#                         self._tts, self._audio_player, lang=self._lang)
#                     self._stream_player.start()

#                     self._tts_active = True

#                     # 预处理并喂给 TTS
#                     processed = preprocess_for_tts(clean_text)
#                     if processed.strip():
#                         self._stream_player.feed_text(processed)

#                     self._stream_player.flush()

#                     # 等待播放完成（仅支持用户语音打断，不设时间限制）
#                     # ★ 修改：移除 30 秒超时，让 TTS 完整播放完毕
#                     # ★ 退出条件仅有三个：
#                     #   1. TTS 播放队列空 + 合成线程已结束（is_playing() 返回 False）
#                     #   2. 用户语音打断（_interrupt_flag 被 VAD 设置）
#                     #   3. 模块停止运行（_running 为 False）
#                     while (self._stream_player.is_playing()
#                            and not self._interrupt_flag.is_set()
#                            and self._running):
#                         time.sleep(0.05)

#                     # 检查是否被打断
#                     if self._interrupt_flag.is_set():
#                         was_interrupted = True

#                     self._stream_player.stop()
#                     self._audio_player.stop()
#                     self._tts_active = False

#                 if was_interrupted:
#                     log.info("对话被打断，准备下一轮")
#                     try:
#                         import sounddevice as sd
#                         sd.stop()
#                     except Exception:
#                         pass
#                     time.sleep(TTS_TAIL_SILENCE_SEC)

#                 with self._dialog_lock:
#                     self._in_dialog = False

#                 print(f"\n{'─' * 50}")

#         except KeyboardInterrupt:
#             print("\n\n收到中断信号，退出中...")
#         finally:
#             self.stop()

#     def stop(self):
#         self._running = False
#         self._interrupt_flag.set()
#         self._tts_active = False
#         self._stream_player.stop()
#         self._audio_player.stop()
#         self._stop_vad_monitor()
#         try:
#             import sounddevice as sd
#             sd.stop()
#         except Exception:
#             pass
#         try:
#             self._asr.release()
#         except Exception:
#             pass
#         log.info("InterruptibleVoice stopped")


# # ══════════════════════════════════════════════════════════════════════════════
# # 便捷工厂函数
# # ══════════════════════════════════════════════════════════════════════════════
# def create_streaming_dialog(stream_llm_client,
#                             lang: str = "zh",
#                             stop_word: str = "退出") -> InterruptibleVoiceModule:
#     vm = InterruptibleVoiceModule(lang=lang)

#     def llm_callback(user_text: str,
#                      on_token: Callable,
#                      should_stop: Callable):
#         system = "直接简短回答，不超过30个字。"
#         for _ in stream_llm_client.chat_stream(
#             user_text,
#             system_prompt=system,
#             enable_thinking=False,
#             on_token=on_token,
#             should_stop=should_stop,
#         ):
#             pass

#     dialog_thread = threading.Thread(
#         target=vm.start_dialog_loop,
#         args=(llm_callback, stop_word),
#         daemon=True,
#     )
#     dialog_thread.start()
#     return vm


# if __name__ == "__main__":
#     import argparse
#     parser = argparse.ArgumentParser()
#     parser.add_argument("--mode", default="dialog",
#                         choices=["list-devices", "test-tts", "dialog"])
#     parser.add_argument("--lang", default="zh")
#     parser.add_argument("--input-device", type=int, default=None)
#     parser.add_argument("--output-device", type=int, default=None)
#     parser.add_argument("--no-auto-usb", action="store_true")
#     args = parser.parse_args()

#     if args.mode == "list-devices":
#         list_audio_devices()
#         ai, ao = find_usb_audio_devices()
#         print(f"自动识别到的 USB: input={ai}, output={ao}")

#     elif args.mode == "test-tts":
#         vm = InterruptibleVoiceModule(
#             lang=args.lang,
#             input_device=args.input_device,
#             output_device=args.output_device,
#             auto_usb=not args.no_auto_usb,
#         )
#         print("测试：播放长文本，3秒后打断")
#         vm._stream_player.start()
#         vm._stream_player.feed_text("这是一段测试文本。1加1等于2。用于测试TTS打断功能。")
#         vm._stream_player.flush()
#         time.sleep(3)
#         print("\n[打断！]")
#         vm._stream_player.stop()
#         vm.stop()

#     else:
#         print("流式对话模式（需要 LLM 服务运行）")
#         try:
#             from llm.stream_llm_client import StreamLLMClient
#             llm = StreamLLMClient()
#             if not llm.is_alive():
#                 print("❌ LLM服务未启动，请先运行 flask_server_enhanced")
#                 sys.exit(1)
#             vm = create_streaming_dialog(llm, lang=args.lang)
#             while True:
#                 time.sleep(1)
#         except ImportError as e:
#             print(f"❌ 导入错误: {e}")
#             sys.exit(1)
#         except KeyboardInterrupt:
#             print("\n退出")
# edge/voice/interruptible_voice.py v5.0
"""
可打断的流式语音交互模块 - 真·流式 TTS 版

★ v5.0 关键改动（仅修改 start_dialog_loop 的 LLM→TTS 流转）：
   - 取消"批量TTS"：不再等 LLM 完整输出后才喂 TTS
   - LLM 流式输出的每个 token，立即喂给 StreamTTSPlayer.feed_text()
   - StreamTTSPlayer 内部已具备：增量分句 + 后台合成线程 + 连续音频播放
     → 第一句话合成完毕即开始播放，后续句子无缝衔接
   - LLM 仍在生成时，TTS 已经在播放前面的句子，显著缩短用户等待
   - 客户端附带轻量 </think> 边界过滤器作为安全网：若服务端漏出 </think>，
     客户端会丢弃 </think> 之前的思考内容（不影响服务端已过滤的常规情况）

★ v4.0 保留：
1. is_playing() 竞态修复（_has_content + _synth_done）
2. VAD 流式打断（LLM 思考阶段可打断）
3. TTS 文本预处理：数字/数学符号/LaTeX/Markdown → 中文可读
"""
import re
import sys
import time
import signal
import logging
import threading
import queue
import collections
import numpy as np
from pathlib import Path
from typing import Optional, Callable, Tuple

_EDGE_ROOT = Path(__file__).resolve().parent.parent
if str(_EDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_EDGE_ROOT))

_THIS_DIR = Path(__file__).resolve().parent
_MODELS = _THIS_DIR / "models"
_CACHE = _THIS_DIR / "audio_cache"
_CACHE.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("interruptible_voice")

USB_KEYWORDS = [
    "generalplus",
    "usb audio device",
    "usb-audio",
    "usb audio",
    "usb",
]

# ── VAD 打断参数 ─────────────────────────────────────────────────────────────
INTERRUPT_GUARD_SEC = 1.2
VAD_INTERRUPT_FACTOR = 4.5
VAD_CONFIRM_FRAMES = 5
TTS_TAIL_SILENCE_SEC = 0.25

# ── 分句参数 ──────────────────────────────────────────────────────────────────
SENTENCE_END_RE = re.compile(r'[。！？!?\.…；;]')
SUB_SENTENCE_RE = re.compile(r'[，,、：:]')
LONG_SENTENCE_THRESHOLD = 18

from voice.voice_module import (
    ASR_RATE, CHANNELS, RECORD_DTYPE, VAD_CHUNK_MS, VAD_CALIB_SEC,
    VAD_THRESH_FACTOR, VAD_THRESH_MIN, VAD_THRESH_MAX,
    SILENCE_TIMEOUT, MAX_RECORD_SEC,
    split_sentences, SenseVoiceASR, SherpaTTS, VoiceRecorder,
)


# ══════════════════════════════════════════════════════════════════════════════
# ★ LLM 回复清洗（用于 TTS 前处理）
# ══════════════════════════════════════════════════════════════════════════════
def sanitize_llm_response(text: str) -> str:
    """
    清洗 LLM 原始回复，提取最终答案。
    - 去除所有 </think> 之前的思考内容
    - 去除特殊标记、LaTeX、Markdown
    """
    if not text:
        return ""
    # 取最后一个 </think> 之后的内容
    last_think = text.rfind('</think>')
    if last_think >= 0:
        text = text[last_think + len('</think>'):]
    # 兜底：<think>...</think>
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    # 特殊 token
    text = re.sub(r'<\|[^|]+\|>', '', text)
    text = re.sub(r'<[^>]+>', '', text)
    # LaTeX
    text = re.sub(r'\\boxed\{([^}]*)\}', r'\1', text)
    text = re.sub(r'\\text\{([^}]*)\}', r'\1', text)
    text = re.sub(r'\\frac\{([^}]*)\}\{([^}]*)\}', r'\1分之\2', text)
    text = re.sub(r'\\[a-zA-Z]+\s*', '', text)
    text = re.sub(r'\\\(|\\\)|\\\[|\\\]', '', text)
    # Markdown
    text = re.sub(r'\*\*([^*\n]+?)\*\*', r'\1', text)
    text = re.sub(r'\*([^*\n]+?)\*', r'\1', text)
    text = re.sub(r'`([^`\n]+?)`', r'\1', text)
    # 合并空白
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# ══════════════════════════════════════════════════════════════════════════════
# TTS 文本预处理
# ══════════════════════════════════════════════════════════════════════════════
_DIGITS_CN = ['零', '一', '二', '三', '四', '五', '六', '七', '八', '九']


def _int_to_chinese(s: str) -> str:
    if not s or not s.isdigit():
        return s
    n = int(s)
    if n == 0:
        return '零'
    if n < 10:
        return _DIGITS_CN[n]
    if n < 100:
        tens, ones = n // 10, n % 10
        head = '十' if tens == 1 else _DIGITS_CN[tens] + '十'
        return head if ones == 0 else head + _DIGITS_CN[ones]
    if n < 1000:
        hundreds, rest = n // 100, n % 100
        head = _DIGITS_CN[hundreds] + '百'
        if rest == 0:
            return head
        if rest < 10:
            return head + '零' + _DIGITS_CN[rest]
        tens, ones = rest // 10, rest % 10
        sub = _DIGITS_CN[tens] + '十'
        if ones > 0:
            sub += _DIGITS_CN[ones]
        return head + sub
    if n < 10000:
        thousands, rest = n // 1000, n % 1000
        head = _DIGITS_CN[thousands] + '千'
        if rest == 0:
            return head
        if rest < 100:
            return head + '零' + _int_to_chinese(str(rest))
        return head + _int_to_chinese(str(rest))
    return ''.join(_DIGITS_CN[int(d)] for d in s if d.isdigit())


def _num_to_chinese(num_str: str) -> str:
    if '.' in num_str:
        int_part, dec_part = num_str.split('.', 1)
        int_chinese = _int_to_chinese(int_part) if int_part else '零'
        dec_chinese = ''.join(_DIGITS_CN[int(d)] if d.isdigit() else d for d in dec_part)
        return int_chinese + '点' + dec_chinese
    return _int_to_chinese(num_str)


def preprocess_for_tts(text: str) -> str:
    if not text:
        return ""
    # LaTeX
    text = re.sub(r'\\boxed\{([^}]*)\}', r'\1', text)
    text = re.sub(r'\\text\{([^}]*)\}', r'\1', text)
    text = re.sub(r'\\frac\{([^}]*)\}\{([^}]*)\}', r'\1分之\2', text)
    text = re.sub(r'\\[a-zA-Z]+\s*', '', text)
    text = re.sub(r'\\\(|\\\)|\\\[|\\\]', '', text)
    # Markdown
    text = re.sub(r'\*\*([^*\n]+?)\*\*', r'\1', text)
    text = re.sub(r'\*([^*\n]+?)\*', r'\1', text)
    text = re.sub(r'`([^`\n]+?)`', r'\1', text)
    text = re.sub(r'^#{1,6}\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    # 括号
    text = re.sub(r'[\(\)\[\]\{\}（）【】]', '', text)
    # 百分号
    text = re.sub(
        r'(\d+(?:\.\d+)?)\s*%',
        lambda m: '百分之' + _num_to_chinese(m.group(1)),
        text,
    )
    # 指数
    text = re.sub(
        r'\^\s*(\d+)',
        lambda m: '的' + _num_to_chinese(m.group(1)) + '次方',
        text,
    )
    # 数字
    text = re.sub(r'\d+(?:\.\d+)?', lambda m: _num_to_chinese(m.group(0)), text)
    # 数学符号
    math_repl = {
        '+': '加', '-': '减', '×': '乘', '*': '乘',
        '÷': '除以', '=': '等于',
        '≤': '小于等于', '≥': '大于等于', '≠': '不等于',
        '<': '小于', '>': '大于',
        '\\': '', '_': '', '^': '',
        '|': '', '~': '', '@': '',
    }
    for k, v in math_repl.items():
        text = text.replace(k, v)
    text = re.sub(
        r'(?<=[\u4e00-\u9fff])\s*/\s*(?=[\u4e00-\u9fff])', '除以', text)
    text = text.replace('/', ' ')
    # 空白规整
    text = re.sub(r'\n+', '。', text)
    text = re.sub(r'。+', '。', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\s*，\s*', '，', text)
    text = text.strip(' 。\n')
    return text


# ══════════════════════════════════════════════════════════════════════════════
# 设备工具
# ══════════════════════════════════════════════════════════════════════════════
def list_audio_devices():
    import sounddevice as sd
    print("\n=== Audio Devices ===")
    try:
        devices = sd.query_devices()
    except Exception as e:
        print(f"  query_devices 失败: {e}")
        return
    for i, dev in enumerate(devices):
        name_lower = dev["name"].lower()
        marker = " [USB]" if any(k in name_lower for k in USB_KEYWORDS) else ""
        print(
            f"  {i}: {dev['name']}{marker}  "
            f"in={dev['max_input_channels']} out={dev['max_output_channels']} "
            f"rate={int(dev['default_samplerate'])}"
        )
    print()


def find_usb_audio_devices() -> Tuple[Optional[int], Optional[int]]:
    import sounddevice as sd
    try:
        devices = sd.query_devices()
    except Exception as e:
        log.warning(f"query_devices 失败: {e}")
        return None, None
    input_idx = None
    output_idx = None
    for kw in USB_KEYWORDS:
        for i, dev in enumerate(devices):
            name_lower = dev["name"].lower()
            if kw not in name_lower:
                continue
            if input_idx is None and dev["max_input_channels"] > 0:
                input_idx = i
            if output_idx is None and dev["max_output_channels"] > 0:
                output_idx = i
        if input_idx is not None and output_idx is not None:
            break
    return input_idx, output_idx


def probe_device_rate(device_idx: Optional[int],
                      preferred_rates=(16000, 44100, 48000)) -> int:
    if device_idx is None:
        return 16000
    import sounddevice as sd
    try:
        info = sd.query_devices(device_idx)
        return int(info.get("default_samplerate", 16000))
    except Exception as e:
        log.debug(f"probe_device_rate({device_idx}) 失败: {e}")
        return 16000


# ══════════════════════════════════════════════════════════════════════════════
# ContinuousAudioPlayer
# ══════════════════════════════════════════════════════════════════════════════
class ContinuousAudioPlayer:
    """
    sd.OutputStream 回调实现无间隙连续播放。
    """

    def __init__(self, output_device: Optional[int], device_rate: int):
        self._device = output_device
        self._rate = device_rate
        self._audio_chunks: collections.deque = collections.deque()
        self._chunks_lock = threading.Lock()
        self._stream = None
        self._active = False
        self.is_speaking = threading.Event()
        self._ever_fed = False
        self._feeding_done = False

    def start(self):
        import sounddevice as sd
        if self._stream is not None:
            return
        self._active = True
        self._ever_fed = False
        self._feeding_done = False
        try:
            self._stream = sd.OutputStream(
                samplerate=self._rate,
                channels=1,
                dtype='float32',
                device=self._device,
                callback=self._callback,
                blocksize=1024,
            )
            self._stream.start()
        except Exception as e:
            log.error(f"OutputStream 启动失败: {e}")
            self._stream = None

    def stop(self):
        self._active = False
        with self._chunks_lock:
            self._audio_chunks.clear()
        self.is_speaking.clear()
        self._ever_fed = False
        self._feeding_done = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def feed_audio(self, audio_float32: np.ndarray):
        if not self._active or audio_float32 is None or len(audio_float32) == 0:
            return
        with self._chunks_lock:
            self._audio_chunks.append(audio_float32.copy())
        self._ever_fed = True
        self.is_speaking.set()

    def mark_done(self):
        self._feeding_done = True

    def _callback(self, outdata, frames, time_info, status):
        needed = frames
        result = np.zeros(needed, dtype=np.float32)
        pos = 0
        with self._chunks_lock:
            while pos < needed and self._audio_chunks:
                chunk = self._audio_chunks[0]
                available = len(chunk)
                take = min(available, needed - pos)
                result[pos:pos + take] = chunk[:take]
                if take < available:
                    self._audio_chunks[0] = chunk[take:]
                else:
                    self._audio_chunks.popleft()
                pos += take
            has_data = len(self._audio_chunks) > 0
        outdata[:, 0] = result
        if pos > 0:
            self.is_speaking.set()
        elif not has_data and self._feeding_done:
            self.is_speaking.clear()

    def is_playing(self) -> bool:
        with self._chunks_lock:
            has_data = len(self._audio_chunks) > 0
        if has_data:
            return True
        if self._ever_fed and not self._feeding_done:
            return True
        return self.is_speaking.is_set()


# ══════════════════════════════════════════════════════════════════════════════
# StreamTTSPlayer
# ★ v4.0: 修复 is_playing() 竞态 - 增加 _has_content + _synth_done
# ══════════════════════════════════════════════════════════════════════════════
class StreamTTSPlayer:
    def __init__(self, tts_engine: SherpaTTS, audio_player: ContinuousAudioPlayer,
                 lang: str = "zh"):
        self._tts = tts_engine
        self._player = audio_player
        self._lang = lang
        self._buffer = ""
        self._stop_flag = threading.Event()
        self._sentence_queue: queue.Queue = queue.Queue()
        self._synth_thread: Optional[threading.Thread] = None
        # ★ 新增：追踪是否有待处理的内容
        self._has_content = False
        self._synth_done = threading.Event()

    def start(self):
        self._stop_flag.clear()
        self._buffer = ""
        self._has_content = False
        self._synth_done.clear()
        self._player.start()
        self._synth_thread = threading.Thread(target=self._synth_loop, daemon=True)
        self._synth_thread.start()

    def stop(self):
        self._stop_flag.set()
        while not self._sentence_queue.empty():
            try:
                self._sentence_queue.get_nowait()
            except queue.Empty:
                break
        self._player.mark_done()
        self._player.stop()
        self._buffer = ""
        self._has_content = False
        self._synth_done.set()

    def feed_text(self, text: str):
        """
        累积文本并按规则切句送入合成队列。
        """
        if text.strip():
            self._has_content = True

        self._buffer += text
        while True:
            end_match = SENTENCE_END_RE.search(self._buffer)
            sub_match = SUB_SENTENCE_RE.search(self._buffer)

            if not end_match and not sub_match:
                break

            if end_match:
                end_pos = end_match.end()
                sentence = self._buffer[:end_pos].strip()
                self._buffer = self._buffer[end_pos:]
                if sentence:
                    self._sentence_queue.put(sentence)
                continue

            if sub_match and sub_match.end() >= LONG_SENTENCE_THRESHOLD:
                end_pos = sub_match.end()
                sentence = self._buffer[:end_pos].strip()
                self._buffer = self._buffer[end_pos:]
                if sentence:
                    self._sentence_queue.put(sentence)
                continue

            break

    def flush(self):
        if self._buffer.strip():
            self._has_content = True
            self._sentence_queue.put(self._buffer.strip())
            self._buffer = ""
        self._sentence_queue.put(None)

    def _synth_loop(self):
        import librosa
        while not self._stop_flag.is_set():
            try:
                sentence = self._sentence_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if sentence is None:
                self._player.mark_done()
                break
            if self._stop_flag.is_set():
                break

            processed = preprocess_for_tts(sentence)
            if not processed:
                continue

            sub_sentences = split_sentences(processed, max_chars=50)
            for sent in sub_sentences:
                if self._stop_flag.is_set():
                    break
                try:
                    audio, sr = self._tts.synthesize_sentence(
                        sent, self._lang, speed=1.0)
                    if audio is not None and len(audio) > 0:
                        if sr != self._player._rate:
                            audio = librosa.resample(
                                audio, orig_sr=sr, target_sr=self._player._rate)
                        self._player.feed_audio(audio)
                except Exception as e:
                    log.error(f"TTS synth error: {e}")

        self._player.mark_done()
        self._synth_done.set()  # ★ 标记合成完成

    def is_playing(self) -> bool:
        """
        ★ 修复后的 is_playing()：
        - 如果有内容且合成线程未完成 → True
        - 否则检查音频播放器是否在播放
        """
        if self._has_content and not self._synth_done.is_set():
            return True
        return self._player.is_playing()


# ══════════════════════════════════════════════════════════════════════════════
# InterruptibleVoiceModule
# ★ v4.0: 批量TTS模式 - 生成完毕后清洗文本再合成
# ══════════════════════════════════════════════════════════════════════════════
class InterruptibleVoiceModule:
    def __init__(self,
                 lang: str = "zh",
                 input_device: Optional[int] = None,
                 output_device: Optional[int] = None,
                 auto_usb: bool = True,
                 tts_speed: float = 1.1,
                 enable_mqtt: bool = False):
        self._lang = lang
        self._tts_speed = tts_speed
        self._running = False
        self._in_dialog = False
        self._tts_active = False
        self._interrupt_flag = threading.Event()
        self._dialog_lock = threading.Lock()

        if auto_usb and (input_device is None or output_device is None):
            ai, ao = find_usb_audio_devices()
            if input_device is None:
                input_device = ai
            if output_device is None:
                output_device = ao

        try:
            import sounddevice as sd
            in_name = (sd.query_devices(input_device)["name"]
                       if input_device is not None else "default")
            out_name = (sd.query_devices(output_device)["name"]
                        if output_device is not None else "default")
            print(f"[InterruptibleVoice] mic  = #{input_device} {in_name}")
            print(f"[InterruptibleVoice] spk  = #{output_device} {out_name}")
        except Exception:
            print(f"[InterruptibleVoice] mic={input_device} speaker={output_device}")

        self._asr = SenseVoiceASR(lang=lang)
        self._tts = SherpaTTS(output_device=output_device)
        self._recorder = VoiceRecorder(input_device=input_device)

        self._output_device = output_device
        self._output_rate = self._tts._device_rate

        self._audio_player = ContinuousAudioPlayer(output_device, self._output_rate)
        self._stream_player = StreamTTSPlayer(
            self._tts, self._audio_player, lang=lang)

        self._vad_running = False
        self._vad_thread: Optional[threading.Thread] = None

        print(f"[InterruptibleVoice] 就绪 lang={lang}")

    def _should_interrupt(self) -> bool:
        return self._interrupt_flag.is_set()

    # ── VAD 打断监控 ────────────────────────────────────────────────────────────

    def _start_vad_monitor(self):
        if self._vad_thread is not None and self._vad_thread.is_alive():
            return
        self._vad_running = True

        def vad_loop():
            import sounddevice as sd
            chunk_samples = int(
                self._recorder._device_rate * VAD_CHUNK_MS / 1000)
            base_thresh = self._recorder._silence_thresh
            interrupt_thresh = base_thresh * VAD_INTERRUPT_FACTOR

            try:
                stream = sd.InputStream(
                    samplerate=self._recorder._device_rate,
                    channels=CHANNELS,
                    dtype=RECORD_DTYPE,
                    blocksize=chunk_samples,
                    device=self._recorder._input_device,
                )
            except Exception as e:
                log.error(f"VAD InputStream 启动失败: {e}")
                return

            consecutive_loud = 0

            with stream:
                while self._vad_running and self._running:
                    if not self._in_dialog:
                        consecutive_loud = 0
                        time.sleep(0.05)
                        continue

                    if self._tts_active or self._audio_player.is_speaking.is_set():
                        consecutive_loud = 0
                        try:
                            stream.read(chunk_samples)
                        except Exception:
                            pass
                        time.sleep(0.02)
                        continue

                    try:
                        data, _ = stream.read(chunk_samples)
                        arr = np.asarray(data)
                        if arr.ndim > 1:
                            arr = arr[:, 0]
                        rms = float(np.sqrt(
                            np.mean(arr.astype(np.float32) ** 2)))

                        if rms > interrupt_thresh:
                            consecutive_loud += 1
                            if consecutive_loud >= VAD_CONFIRM_FRAMES:
                                if (self._in_dialog
                                        and not self._interrupt_flag.is_set()):
                                    log.info(
                                        f"[VAD] 检测到用户说话 "
                                        f"(rms={rms:.0f} > {interrupt_thresh:.0f}), "
                                        f"连续{consecutive_loud}帧，触发打断")
                                    self._interrupt_flag.set()
                                consecutive_loud = 0
                        else:
                            consecutive_loud = 0
                    except Exception as e:
                        log.debug(f"VAD read error: {e}")
                        time.sleep(0.05)

        self._vad_thread = threading.Thread(target=vad_loop, daemon=True)
        self._vad_thread.start()
        log.info("VAD monitor started")

    def _stop_vad_monitor(self):
        self._vad_running = False
        if self._vad_thread is not None:
            self._vad_thread.join(timeout=2.0)
            self._vad_thread = None
        log.info("VAD monitor stopped")

    # ── ASR 录音 ────────────────────────────────────────────────────────────────

    def _perform_asr(self) -> str:
        self._stop_vad_monitor()
        print("\n🎤 请说话...")

        def on_start():
            print("  [录音中...]")

        def on_stop():
            print("  [识别中...]")

        try:
            audio = self._recorder.record_once(
                on_start=on_start,
                on_stop=on_stop,
                auto_calibrate=True,
                running_check=lambda: self._running,
            )
        finally:
            self._start_vad_monitor()

        if not self._running:
            return ""

        text = self._asr.recognize(audio)
        if text.strip():
            print(f"  [识别结果: {text}]")
        else:
            print("  [未识别到语音]")
        return text.strip()

    # ── 主对话循环 ──────────────────────────────────────────────────────────────
    # ★ v4.0: 批量TTS - 收集token → 清洗 → 合成 → 播放
    # ──────────────────────────────────────────────────────────────────────────

    def start_dialog_loop(
            self,
            llm_stream_callback: Callable,
            stop_word: str = "退出",
            on_user_speak: Optional[Callable[[str], None]] = None,
            on_assistant_speak: Optional[Callable[[str], None]] = None):

        if self._running:
            log.warning("Dialog loop already running")
            return

        self._running = True
        self._start_vad_monitor()

        print(f"\n{'=' * 50}")
        print("  🏠 智能家居语音助手已启动")
        print(f"  🛑 说「{stop_word}」结束对话")
        print("  💡 助手回答时，直接说话即可打断")
        print(f"{'=' * 50}\n")

        try:
            while self._running:
                user_text = self._perform_asr()
                if not self._running:
                    break
                if not user_text:
                    continue
                if stop_word in user_text:
                    print("\n👋 再见！")
                    self._tts.speak("好的，再见", block=False)
                    break

                if on_user_speak:
                    on_user_speak(user_text)

                with self._dialog_lock:
                    self._in_dialog = True
                    self._interrupt_flag.clear()

                dialog_start_time = time.time()
                print(f"\n🤖 助手思考中...")

                # ★★★ v5.0 流式 TTS：在 LLM 开始生成前就启动 StreamTTSPlayer
                # 后续每个 token 立即喂给 TTS，第一句话合成完毕即开始播放
                self._stream_player.stop()
                self._audio_player.stop()
                time.sleep(0.02)
                self._audio_player = ContinuousAudioPlayer(
                    self._output_device, self._output_rate)
                self._stream_player = StreamTTSPlayer(
                    self._tts, self._audio_player, lang=self._lang)
                self._stream_player.start()

                # 收集完整 token 流，仅用于最后显示/回调（不影响 TTS 播放节奏）
                full_response = []

                # 客户端 </think> 边界过滤器（轻量安全网）：
                #   - 一旦在流中检测到 </think>，立刻丢弃前面的所有思考内容，
                #     并把 </think> 之后的部分喂给 TTS
                #   - 在未见 </think> 之前，仅缓冲不超过 LOOKBACK 字节，其余照常流入 TTS
                #     （这样即使服务端已把 </think> 完全过滤掉，也不会卡住 TTS）
                _THINK_END = '</think>'
                _LOOKBACK = len(_THINK_END)  # 8 字节即可覆盖 </think> 拆分

                think_state = {
                    "passed_end": False,     # 是否已越过 </think>
                    "hold": "",              # 在未见 </think> 时的尾部预留区（防拆分）
                }

                def _feed_tts_safely(text: str):
                    """喂 TTS（含 </think> 过滤），仅处理流中尚未越过 </think> 的部分。"""
                    if not text:
                        return
                    if think_state["passed_end"]:
                        # 已越过 </think>，直接喂
                        self._stream_player.feed_text(text)
                        return

                    # 还没越过 </think>，与 hold 一起判定
                    combined = think_state["hold"] + text
                    idx = combined.find(_THINK_END)
                    if idx >= 0:
                        # 找到 </think>：丢弃前面，发送之后的
                        after = combined[idx + len(_THINK_END):]
                        think_state["passed_end"] = True
                        think_state["hold"] = ""
                        if after:
                            self._stream_player.feed_text(after)
                        return

                    # 未找到：保留最后 _LOOKBACK 字节（可能是 </think> 的前缀），
                    # 其余正常喂出去 → 服务端已过滤 </think> 时这条路径占主导
                    if len(combined) > _LOOKBACK:
                        emit = combined[:-_LOOKBACK]
                        think_state["hold"] = combined[-_LOOKBACK:]
                        self._stream_player.feed_text(emit)
                    else:
                        think_state["hold"] = combined

                def on_token(token: str):
                    """流式回调：边收 token 边喂 TTS"""
                    full_response.append(token)
                    _feed_tts_safely(token)

                def should_stop_with_guard() -> bool:
                    if not self._interrupt_flag.is_set():
                        return False
                    elapsed = time.time() - dialog_start_time
                    if elapsed < INTERRUPT_GUARD_SEC:
                        self._interrupt_flag.clear()
                        return False
                    return True

                # 注意：此处不提前置 _tts_active = True
                # 在 LLM 还未产出第一句话之前（"思考"阶段），需要让 VAD 能监听
                # 用户语音，从而支持打断。等到 StreamTTSPlayer 开始往 ContinuousAudioPlayer
                # 喂音频之后，AudioPlayer.is_speaking 会自动置位，VAD 据此屏蔽回声。
                # _tts_active 留到 LLM 结束后再置位（与 v4.0 行为一致）。

                was_interrupted = False
                try:
                    llm_stream_callback(
                        user_text,
                        on_token=on_token,
                        should_stop=should_stop_with_guard,
                    )
                except Exception as e:
                    log.error(f"LLM callback error: {e}")

                was_interrupted = self._interrupt_flag.is_set()

                # 若 LLM 结束时仍未见 </think>（最常见情况：服务端已把 </think> 过滤掉），
                # 把 hold 中尚未流出的尾部字节也补喂给 TTS
                if not think_state["passed_end"] and think_state["hold"]:
                    self._stream_player.feed_text(think_state["hold"])
                    think_state["hold"] = ""

                # 显示清洗后的完整回复（仅供日志/外部回调）
                raw_text = "".join(full_response)
                clean_text = sanitize_llm_response(raw_text)
                print(f"  💬 助手: {clean_text}" if clean_text else "  💬 (无有效回复)")

                if on_assistant_speak:
                    on_assistant_speak(clean_text)

                # 没被打断 → 触发 flush，让剩余尾句进入合成队列；然后等待全部播放完
                if not was_interrupted:
                    # 此时进入"播放等待"阶段，置 _tts_active = True
                    # 作为对 is_speaking 的双重保险（与 v4.0 行为一致）
                    self._tts_active = True
                    self._stream_player.flush()

                    # 等待播放完成
                    #   退出条件：
                    #     1. TTS 播放队列空 + 合成线程已结束（is_playing() == False）
                    #     2. 用户语音打断（_interrupt_flag 被 VAD 设置）
                    #     3. 模块停止运行（_running == False）
                    while (self._stream_player.is_playing()
                           and not self._interrupt_flag.is_set()
                           and self._running):
                        time.sleep(0.05)

                    if self._interrupt_flag.is_set():
                        was_interrupted = True

                # 统一收尾：停止 TTS 通路
                self._stream_player.stop()
                self._audio_player.stop()
                self._tts_active = False

                if was_interrupted:
                    log.info("对话被打断，准备下一轮")
                    try:
                        import sounddevice as sd
                        sd.stop()
                    except Exception:
                        pass
                    time.sleep(TTS_TAIL_SILENCE_SEC)

                with self._dialog_lock:
                    self._in_dialog = False

                print(f"\n{'─' * 50}")

        except KeyboardInterrupt:
            print("\n\n收到中断信号，退出中...")
        finally:
            self.stop()

    def stop(self):
        self._running = False
        self._interrupt_flag.set()
        self._tts_active = False
        self._stream_player.stop()
        self._audio_player.stop()
        self._stop_vad_monitor()
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass
        try:
            self._asr.release()
        except Exception:
            pass
        log.info("InterruptibleVoice stopped")


# ══════════════════════════════════════════════════════════════════════════════
# 便捷工厂函数
# ══════════════════════════════════════════════════════════════════════════════
def create_streaming_dialog(stream_llm_client,
                            lang: str = "zh",
                            stop_word: str = "退出") -> InterruptibleVoiceModule:
    vm = InterruptibleVoiceModule(lang=lang)

    def llm_callback(user_text: str,
                     on_token: Callable,
                     should_stop: Callable):
        system = "直接简短回答，不超过30个字。"
        for _ in stream_llm_client.chat_stream(
            user_text,
            system_prompt=system,
            enable_thinking=False,
            on_token=on_token,
            should_stop=should_stop,
        ):
            pass

    dialog_thread = threading.Thread(
        target=vm.start_dialog_loop,
        args=(llm_callback, stop_word),
        daemon=True,
    )
    dialog_thread.start()
    return vm


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="dialog",
                        choices=["list-devices", "test-tts", "dialog"])
    parser.add_argument("--lang", default="zh")
    parser.add_argument("--input-device", type=int, default=None)
    parser.add_argument("--output-device", type=int, default=None)
    parser.add_argument("--no-auto-usb", action="store_true")
    args = parser.parse_args()

    if args.mode == "list-devices":
        list_audio_devices()
        ai, ao = find_usb_audio_devices()
        print(f"自动识别到的 USB: input={ai}, output={ao}")

    elif args.mode == "test-tts":
        vm = InterruptibleVoiceModule(
            lang=args.lang,
            input_device=args.input_device,
            output_device=args.output_device,
            auto_usb=not args.no_auto_usb,
        )
        print("测试：播放长文本，3秒后打断")
        vm._stream_player.start()
        vm._stream_player.feed_text("这是一段测试文本。1加1等于2。用于测试TTS打断功能。")
        vm._stream_player.flush()
        time.sleep(3)
        print("\n[打断！]")
        vm._stream_player.stop()
        vm.stop()

    else:
        print("流式对话模式（需要 LLM 服务运行）")
        try:
            from llm.stream_llm_client import StreamLLMClient
            llm = StreamLLMClient()
            if not llm.is_alive():
                print("❌ LLM服务未启动，请先运行 flask_server_enhanced")
                sys.exit(1)
            vm = create_streaming_dialog(llm, lang=args.lang)
            while True:
                time.sleep(1)
        except ImportError as e:
            print(f"❌ 导入错误: {e}")
            sys.exit(1)
        except KeyboardInterrupt:
            print("\n退出")