# edge/voice/voice_module.py
import re
import sys
import time
import logging
import threading
import queue
import numpy as np
from pathlib import Path
from typing import Optional, Callable

_THIS_DIR = Path(__file__).resolve().parent
_MODELS   = _THIS_DIR / "models"
_CACHE    = _THIS_DIR / "audio_cache"
_CACHE.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("voice")

ASR_RATE = 16000
CHANNELS = 1
RECORD_DTYPE = "int16"
VAD_CHUNK_MS = 30
VAD_CALIB_SEC = 0.5
VAD_THRESH_FACTOR = 4.0
VAD_THRESH_MIN = 300
VAD_THRESH_MAX = 4000
SILENCE_TIMEOUT = 1.5
MAX_RECORD_SEC = 10.0
DEVICE_RATE_CANDIDATES = [44100, 48000, 32000, 22050, 16000]

FBANK_DIM = 80
LFR_M = 7
LFR_N = 3
RKNN_INPUT_LEN = 124
RKNN_STEP_LEN = 64

TTS_MAX_CHARS = 50

# ==================== TTS 文本工具 ====================
SENTENCE_END_CHARS = set('。！？!?…；;')

def split_sentences(text: str, max_chars: int = TTS_MAX_CHARS) -> list:
    if not text or not text.strip():
        return []
    parts = []
    current = ""
    for ch in text:
        current += ch
        if ch in SENTENCE_END_CHARS:
            parts.append(current.strip())
            current = ""
    if current.strip():
        parts.append(current.strip())
    
    sentences = []
    for part in parts:
        if not part:
            continue
        if len(part) <= max_chars:
            sentences.append(part)
            continue
            
        sub_parts = []
        sub_current = ""
        for ch in part:
            sub_current += ch
            if ch in '，,、；;：:':
                sub_parts.append(sub_current.strip())
                sub_current = ""
        if sub_current.strip():
            sub_parts.append(sub_current.strip())
        
        buf = ""
        for sp in sub_parts:
            if not sp:
                continue
            if len(buf) + len(sp) <= max_chars:
                buf += sp
            else:
                if buf:
                    sentences.append(buf)
                while len(sp) > max_chars:
                    sentences.append(sp[:max_chars])
                    sp = sp[max_chars:]
                buf = sp
        if buf:
            sentences.append(buf)
    
    return [s for s in sentences if s.strip()]


# ==================== 设备工具 ====================
def find_usb_audio_devices():
    import sounddevice as sd
    keywords = ["USB", "usb", "GeneralPlus", "Audio Device"]
    in_id = out_id = None
    for i, d in enumerate(sd.query_devices()):
        if not any(k in d["name"] for k in keywords):
            continue
        if d["max_input_channels"] > 0 and in_id is None:
            in_id = i
        if d["max_output_channels"] > 0 and out_id is None:
            out_id = i
    return in_id, out_id

def probe_device_rate(device_id: Optional[int], direction: str = "output") -> int:
    import sounddevice as sd
    for rate in DEVICE_RATE_CANDIDATES:
        try:
            if direction == "input":
                sd.check_input_settings(device=device_id, channels=CHANNELS, dtype=RECORD_DTYPE, samplerate=rate)
            else:
                sd.check_output_settings(device=device_id, channels=CHANNELS, dtype="float32", samplerate=rate)
            return rate
        except Exception:
            continue
    return 44100

def load_cmvn(mvn_path: str):
    path = Path(mvn_path)
    if not path.exists():
        return None, None
    try:
        neg_mean, inv_stddev = None, None
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("<LearnRateCoef>"):
                    continue
                tokens = line.split()
                try:
                    vals = [float(x) for x in tokens[tokens.index('[')+1:tokens.index(']')]]
                except ValueError:
                    vals = [float(x) for x in tokens[3:] if x not in ['[', ']']]
                arr = np.array(vals, dtype=np.float32)
                if neg_mean is None:
                    neg_mean = arr
                else:
                    inv_stddev = arr
                    break
        return neg_mean, inv_stddev
    except Exception:
        return None, None

def apply_cmvn_80(feats_80: np.ndarray, neg_mean: Optional[np.ndarray], inv_stddev: Optional[np.ndarray]) -> np.ndarray:
    if neg_mean is None or inv_stddev is None:
        return feats_80
    dim = feats_80.shape[1]
    return (feats_80 + neg_mean[:dim]) * inv_stddev[:dim]

def apply_lfr(feats: np.ndarray, lfr_m: int = LFR_M, lfr_n: int = LFR_N) -> np.ndarray:
    T, feat_dim = feats.shape
    lfr_feats = []
    for i in range(0, T, lfr_n):
        chunk = feats[i:min(i + lfr_m, T)]
        if len(chunk) < lfr_m:
            chunk = np.concatenate([chunk, np.tile(feats[-1:], (lfr_m - len(chunk), 1))], axis=0)
        lfr_feats.append(chunk.reshape(1, -1))
    return np.concatenate(lfr_feats, axis=0) if lfr_feats else np.zeros((1, feat_dim * lfr_m), dtype=np.float32)

# ==================== ASR ====================
class SenseVoiceASR:
    LANG_TOKEN = {"zh": 24884, "en": 24885, "auto": 24884}
    TEXTNORM_ITN, TEXTNORM_NOITN = 25016, 25017

    def __init__(self, lang: str = "zh", use_itn: bool = True):
        self._lang, self._use_itn = lang, use_itn
        self._model, self._vocab = None, {}
        self._neg_mean, self._inv_stddev = None, None
        self._load()

    def _load(self):
        tokens_path = _MODELS / "tokens.txt"
        self._vocab = self._load_vocab(str(tokens_path))
        self._neg_mean, self._inv_stddev = load_cmvn(str(_MODELS / "am.mvn"))
        from rknnlite.api import RKNNLite
        self._model = RKNNLite(verbose=False)
        self._model.load_rknn(str(_MODELS / "sensevoice_small.rknn"))
        self._model.init_runtime(core_mask=RKNNLite.NPU_CORE_AUTO)

    @staticmethod
    def _load_vocab(path: str) -> dict:
        vocab = {}
        with open(path, encoding="utf-8") as f:
            for line in f:
                parts = line.strip().rsplit(" ", 1)
                if len(parts) == 2:
                    try:
                        vocab[int(parts[1])] = parts[0]
                    except ValueError:
                        pass
        return vocab

    def _extract_features(self, audio_int16: np.ndarray) -> np.ndarray:
        import kaldi_native_fbank as knf
        opts = knf.FbankOptions()
        opts.frame_opts.samp_freq = ASR_RATE
        opts.frame_opts.dither = 0.0
        opts.frame_opts.preemph_coeff = 0.97
        opts.frame_opts.frame_length_ms = 25.0
        opts.frame_opts.frame_shift_ms = 10.0
        opts.frame_opts.remove_dc_offset = True
        opts.mel_opts.num_bins = FBANK_DIM
        
        fb = knf.OnlineFbank(opts)
        fb.accept_waveform(ASR_RATE, (audio_int16.astype(np.float32) / 32768.0).tolist())
        fb.input_finished()
        
        if fb.num_frames_ready == 0:
            return np.zeros((1, FBANK_DIM * LFR_M), dtype=np.float32)
        feats_80 = np.array([fb.get_frame(i) for i in range(fb.num_frames_ready)], dtype=np.float32)
        feats_80 = apply_cmvn_80(feats_80, self._neg_mean, self._inv_stddev)
        return apply_lfr(feats_80)

    def recognize(self, audio_int16: np.ndarray) -> str:
        feats = self._extract_features(audio_int16)
        T = feats.shape[0]
        if T == 0:
            return ""

        all_ids = []
        if T <= RKNN_INPUT_LEN:
            window = np.pad(feats, ((0, RKNN_INPUT_LEN - T), (0, 0)), mode="constant")
            all_ids.extend(self._infer_window(window))
        else:
            start = 0
            while start < T:
                end = start + RKNN_INPUT_LEN
                window = feats[start:end] if end <= T else feats[T-RKNN_INPUT_LEN:T]
                all_ids.extend(self._infer_window(window))
                if end >= T:
                    break
                start += RKNN_STEP_LEN

        merged, prev = [], 0
        for tok in all_ids:
            if tok != 0 and tok != prev:
                merged.append(tok)
            prev = tok

        pieces = []
        for i in merged:
            tok = self._vocab.get(i, "")
            if not tok:
                continue
            if tok.startswith("<") and tok.endswith(">"):
                continue
            if tok in ("<<unk>", "<s>", "</s>"):
                continue
            pieces.append(tok)

        text = "".join(pieces).replace("▁", " ").strip()
        if not text or text.isspace():
            return ""
        return text

    def _infer_window(self, window_feats: np.ndarray) -> list:
        lang_id = self.LANG_TOKEN.get(self._lang, 24884)
        textnorm = self.TEXTNORM_ITN if self._use_itn else self.TEXTNORM_NOITN
        outputs = self._model.inference(inputs=[
            window_feats[np.newaxis].astype(np.float32),
            np.array([RKNN_INPUT_LEN], dtype=np.int32),
            np.array([lang_id], dtype=np.int32),
            np.array([textnorm], dtype=np.int32),
        ])
        ids, prev = [], 0
        for tok in outputs[0][0].argmax(axis=-1).tolist():
            if tok != 0 and tok != prev:
                ids.append(tok)
            prev = tok
        return ids

    def release(self):
        if self._model:
            try:
                self._model.release()
            except Exception:
                pass

# ==================== TTS ====================
class SherpaTTS:
    def __init__(self, output_device: Optional[int] = None):
        self._output_device = output_device
        self._device_rate = probe_device_rate(output_device, "output")
        self._tts_zh = self._tts_en = None
        self.abort_event = threading.Event()
        self.is_playing = False
        self._load()

    def _load(self):
        import sherpa_onnx
        d_zh = _MODELS / "tts" / "vits-zh-hf-theresa"
        if d_zh.exists():
            onnx_list = list(d_zh.glob("*.onnx"))
            if onnx_list:
                self._tts_zh = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(
                    model=sherpa_onnx.OfflineTtsModelConfig(
                        vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                            model=str(onnx_list[0]),
                            lexicon=str(d_zh / "lexicon.txt"),
                            tokens=str(d_zh / "tokens.txt")
                        )
                    )
                ))
        d_en = _MODELS / "tts" / "kokoro-multi-lang-v1_0"
        if d_en.exists():
            onnx_list = list(d_en.glob("*int8*.onnx")) + list(d_en.glob("*.onnx"))
            if onnx_list:
                lexicon_en = next((str(d_en / n) for n in ["lexicon-us-en.txt", "lexicon-gb-en.txt", "lexicon.txt"] if (d_en / n).exists()), "")
                _espeak_data = d_en / "espeak-ng-data"
                if not _espeak_data.is_dir():
                    for cand in (d_en.parent / "espeak-ng-data", d_en / "data" / "espeak-ng-data"):
                        if cand.is_dir():
                            _espeak_data = cand
                            break
                self._tts_en = sherpa_onnx.OfflineTts(
                    sherpa_onnx.OfflineTtsConfig(
                        model=sherpa_onnx.OfflineTtsModelConfig(
                            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                                model=str(onnx_list[0]),
                                voices=str(d_en / "voices.bin"),
                                tokens=str(d_en / "tokens.txt"),
                                lexicon=lexicon_en,
                                data_dir=str(_espeak_data),
                                lang="en-us",
                            )
                        )
                    )
                )

    @staticmethod
    def _detect_lang(text: str) -> str:
        for ch in text:
            if '\u4e00' <= ch <= '\u9fff':
                return "zh"
        return "en"

    @staticmethod
    def _clean_tts_text(text: str) -> str:
        if not text:
            return ""
        text = re.sub(r'<\|[^|]+\|>', '', text)
        text = re.sub(r'<[^>]*>', '', text)
        text = text.replace('?<<', '').replace('?<', '')
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    @staticmethod
    def _is_mostly_chinese(text: str) -> bool:
        """检查文本是否主要是中文"""
        if not text:
            return False
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        total = len(text.strip())
        return total > 0 and chinese_chars / total >= 0.5

    def _get_engine(self, lang: str):
        if lang == "zh" and self._tts_zh:
            return self._tts_zh
        if lang == "en" and self._tts_en:
            return self._tts_en
        return self._tts_zh or self._tts_en

    def synthesize_sentence(self, sent: str, lang: str, speed: float = 1.0):
        sent = self._clean_tts_text(sent)
        if not sent:
            return None, None
        
        # ★ 不再检查 _is_mostly_chinese，允许中英文混合内容通过 TTS
        #   中文 TTS 引擎能处理混合内容中的英文单词
        engine = self._get_engine(lang)
        if not engine:
            return None, None
        try:
            result = engine.generate(sent, sid=0, speed=speed)
            return np.array(result.samples, dtype=np.float32), result.sample_rate
        except Exception as e:
            log.warning(f"TTS合成失败: {e}, 跳过: {sent[:30]}")
            return None, None

    def _play(self, audio: np.ndarray, sr: int):
        import sounddevice as sd, librosa
        if sr != self._device_rate:
            audio = librosa.resample(audio, orig_sr=sr, target_sr=self._device_rate)
        sd.play(audio, samplerate=self._device_rate, device=self._output_device)
        sd.wait()

    def speak(self, text: str, lang: Optional[str] = None, speed: float = 1.0, block: bool = True):
        if not text.strip():
            return
        text = self._clean_tts_text(text)
        if not text:
            return
        if lang is None:
            lang = self._detect_lang(text)
        
        # ★ 如果检测到主要是英文且没有英文TTS引擎，跳过
        if lang == "en" and not self._tts_en:
            log.warning("无英文TTS引擎，跳过英文内容")
            return
            
        sentences = split_sentences(text, max_chars=TTS_MAX_CHARS)
        if not sentences:
            return
        log.info(f"TTS分句: {len(sentences)}句")

        if len(sentences) == 1:
            audio, sr = self.synthesize_sentence(sentences[0], lang, speed)
            if audio is None:
                return
            if block:
                self._play(audio, sr)
            else:
                threading.Thread(target=self._play, args=(audio, sr), daemon=True).start()
            return

        q = queue.Queue(maxsize=2)

        def synth():
            for i, s in enumerate(sentences):
                t0 = time.perf_counter()
                audio, sr = self.synthesize_sentence(s, lang, speed)
                if audio is not None:
                    log.info(f"TTS[{i+1}/{len(sentences)}] '{s[:12]}' {(time.perf_counter()-t0)*1000:.0f}ms")
                    q.put((audio, sr))
            q.put(None)

        def play():
            while True:
                item = q.get()
                if item is None:
                    break
                self._play(*item)

        st = threading.Thread(target=synth, daemon=True)
        pt = threading.Thread(target=play, daemon=True)
        st.start()
        pt.start()
        if block:
            st.join()
            pt.join()

    def interrupt(self):
        self.abort_event.set()
        import sounddevice as sd
        sd.stop()
        self.is_playing = False

    def speak_stream(self, sentence_iter):
        self.abort_event.clear()
        q = queue.Queue(maxsize=3)

        def synth_worker():
            for sent, lang in sentence_iter:
                if self.abort_event.is_set():
                    break
                if not sent.strip():
                    continue
                audio, sr = self.synthesize_sentence(sent, lang)
                if audio is not None:
                    q.put((audio, sr))
            q.put(None)

        def play_worker():
            import sounddevice as sd, librosa
            while not self.abort_event.is_set():
                try:
                    item = q.get(timeout=0.1)
                    if item is None:
                        break
                    audio, sr = item
                    if sr != self._device_rate:
                        audio = librosa.resample(audio, orig_sr=sr, target_sr=self._device_rate)
                    self.is_playing = True
                    if not self.abort_event.is_set():
                        sd.play(audio, samplerate=self._device_rate, device=self._output_device)
                        while sd.get_stream().active:
                            if self.abort_event.is_set():
                                sd.stop()
                                break
                            time.sleep(0.02)
                    self.is_playing = False
                except queue.Empty:
                    continue
            self.is_playing = False

        st = threading.Thread(target=synth_worker, daemon=True)
        pt = threading.Thread(target=play_worker, daemon=True)
        st.start()
        pt.start()
        st.join()
        pt.join()
        self.abort_event.clear()

# ==================== VoiceRecorder ====================
class VoiceRecorder:
    def __init__(self, input_device: Optional[int] = None):
        self._input_device = input_device
        self._device_rate = probe_device_rate(input_device, "input")
        self._chunk_samples = int(self._device_rate * VAD_CHUNK_MS / 1000)
        self._silence_thresh = 500
        self.dynamic_thresh_func = None

    def calibrate_noise(self) -> int:
        import sounddevice as sd
        n = int(VAD_CALIB_SEC * self._device_rate)
        try:
            raw = sd.rec(n, samplerate=self._device_rate, channels=CHANNELS, dtype=RECORD_DTYPE, device=self._input_device)
            sd.wait()
            rms = int(np.sqrt(np.mean(raw.astype(np.float32)**2)))
            self._silence_thresh = int(np.clip(rms * VAD_THRESH_FACTOR, VAD_THRESH_MIN, VAD_THRESH_MAX))
            return self._silence_thresh
        except Exception:
            return self._silence_thresh

    def _resample_to_asr(self, raw: np.ndarray) -> np.ndarray:
        import librosa
        if self._device_rate != ASR_RATE:
            raw = librosa.resample(raw, orig_sr=self._device_rate, target_sr=ASR_RATE)
        return (raw * 32767).clip(-32768, 32767).astype(np.int16)

    def record_once(self, on_start=None, on_stop=None,
                    auto_calibrate: bool = True,
                    running_check: Optional[Callable] = None) -> np.ndarray:
        """
        录一句话。
        
        Args:
            running_check: 可选，返回 False 时立即退出录音（用于 Ctrl+C 退出）
        """
        import sounddevice as sd
        if auto_calibrate:
            self.calibrate_noise()
        
        audio_buf, state, silence_cnt = [], "waiting", 0
        max_frames = int(MAX_RECORD_SEC * self._device_rate / self._chunk_samples)
        max_wait_sec = 30.0
        wait_start_time = time.time()

        def callback(indata, frames, time_info, status):
            nonlocal state, silence_cnt, audio_buf
            chunk = indata[:, 0].copy()
            rms = int(np.sqrt(np.mean(chunk.astype(np.float32)**2)))
            
            current_thresh = self._silence_thresh
            if self.dynamic_thresh_func:
                current_thresh = int(self._silence_thresh * self.dynamic_thresh_func())

            if state == "waiting":
                if rms > current_thresh:
                    state = "recording"
                    audio_buf = [chunk.copy()]
                    if on_start:
                        threading.Thread(target=on_start, daemon=True).start()
            elif state == "recording":
                audio_buf.append(chunk.copy())
                if rms < current_thresh:
                    silence_cnt += 1
                    if silence_cnt >= int(SILENCE_TIMEOUT * 1000 / VAD_CHUNK_MS):
                        state = "done"
                else:
                    silence_cnt = 0
                if len(audio_buf) >= max_frames:
                    state = "done"

        try:
            with sd.InputStream(samplerate=self._device_rate, channels=CHANNELS, dtype=RECORD_DTYPE, blocksize=self._chunk_samples, device=self._input_device, callback=callback):
                while state != "done":
                    if running_check is not None and not running_check():
                        state = "done"
                        break
                    if state == "waiting" and (time.time() - wait_start_time) > max_wait_sec:
                        state = "done"
                        break
                    time.sleep(0.05)
        except Exception as e:
            log.error(f"record_once error: {e}")

        if on_stop:
            threading.Thread(target=on_stop, daemon=True).start()

        if not audio_buf:
            return np.zeros(ASR_RATE, dtype=np.int16)
        raw = np.concatenate(audio_buf).astype(np.float32)
        return self._resample_to_asr(raw)

    def record_fixed(self, duration_sec: float = 5.0) -> np.ndarray:
        import sounddevice as sd
        raw = sd.rec(int(duration_sec * self._device_rate), samplerate=self._device_rate, channels=CHANNELS, dtype=RECORD_DTYPE, device=self._input_device)
        sd.wait()
        return self._resample_to_asr(raw[:, 0].astype(np.float32))

class VoiceModule:
    def __init__(self, input_device: Optional[int] = None, output_device: Optional[int] = None):
        ai, ao = find_usb_audio_devices()
        self._asr = SenseVoiceASR()
        self._tts = SherpaTTS(output_device=output_device or ao)
        self._recorder = VoiceRecorder(input_device=input_device or ai)