"""
edge/voice/voice_module.py  v6

根本原因修复：SenseVoice 使用 LFR（Low Frame Rate）特征
  正确流水线：
    音频(16kHz)
    → 80维 log-fbank（标准梅尔滤波器组）
    → 全局 CMVN（am.mvn，作用于80维）
    → LFR 拼接：每7帧拼成1帧，步长3帧 → 560维 (= 80×7)
    → 分窗送入 RKNN（固定 INPUT_LEN=124 窗口帧数）
    → CTC decode

  之前错误：直接提560维 fbank，跳过了 LFR 步骤，
  导致特征内容与训练时完全不同，模型输出噪声。

必要文件（需从PC端 SenseVoiceSmall 目录复制）：
  models/tokens.txt
  models/am.mvn          （80维 CMVN 参数）
  models/sensevoice_small.rknn
"""

import re
import sys
import time
import logging
import threading
import numpy as np
from pathlib import Path
from typing import Optional, Callable

_THIS_DIR = Path(__file__).resolve().parent
_MODELS   = _THIS_DIR / "models"
_CACHE    = _THIS_DIR / "audio_cache"
_CACHE.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("voice")

ASR_RATE               = 16000
CHANNELS               = 1
RECORD_DTYPE           = "int16"
VAD_CHUNK_MS           = 30
VAD_CALIB_SEC          = 0.5
VAD_THRESH_FACTOR      = 4.0
VAD_THRESH_MIN         = 300
VAD_THRESH_MAX         = 4000
SILENCE_TIMEOUT        = 1.5
MAX_RECORD_SEC         = 10.0

DEVICE_RATE_CANDIDATES = [44100, 48000, 32000, 22050, 16000]

# SenseVoice 特征参数
FBANK_DIM      = 80     # 底层 fbank 维度
LFR_M          = 7      # LFR 拼接帧数（每7帧合1帧）
LFR_N          = 3      # LFR 步长（每3帧取一次）
# 最终特征维度 = FBANK_DIM * LFR_M = 80 * 7 = 560 ✓

# RKNN 模型参数（与 export_onnx.py 中 INPUT_LEN 一致）
RKNN_INPUT_LEN = 124    # 模型接受的固定帧数（LFR帧，非 fbank 帧）
RKNN_STEP_LEN  = 64     # 滑动步长

TTS_MAX_CHARS  = 20


# ─────────────────────────────────────────────────────────────
# 设备工具
# ─────────────────────────────────────────────────────────────

def list_audio_devices():
    import sounddevice as sd
    print("\n" + "="*65)
    print(f"{'ID':<4} {'方向':<8} {'声道':<12} {'设备名称'}")
    print("="*65)
    for i, d in enumerate(sd.query_devices()):
        ic, oc = d["max_input_channels"], d["max_output_channels"]
        direction = ("输入+输出" if ic > 0 and oc > 0
                     else "输入" if ic > 0 else "输出" if oc > 0 else "-")
        print(f"[{i:<2}] {direction:<8} in={ic} out={oc:<4}  {d['name']}")
    print("="*65)


def find_usb_audio_devices():
    import sounddevice as sd
    keywords = ["USB", "usb", "GeneralPlus", "Audio Device"]
    in_id = out_id = None
    for i, d in enumerate(sd.query_devices()):
        if not any(k in d["name"] for k in keywords):
            continue
        if d["max_input_channels"]  > 0 and in_id  is None: in_id  = i
        if d["max_output_channels"] > 0 and out_id is None: out_id = i
    return in_id, out_id


def probe_device_rate(device_id: Optional[int], direction: str = "output") -> int:
    import sounddevice as sd
    for rate in DEVICE_RATE_CANDIDATES:
        try:
            if direction == "input":
                sd.check_input_settings(
                    device=device_id, channels=CHANNELS,
                    dtype=RECORD_DTYPE, samplerate=rate)
            else:
                sd.check_output_settings(
                    device=device_id, channels=CHANNELS,
                    dtype="float32", samplerate=rate)
            log.info(f"设备[{device_id}] {direction} 支持 {rate}Hz")
            return rate
        except Exception:
            continue
    log.warning(f"设备[{device_id}] 采样率探测失败，默认44100Hz")
    return 44100


# ─────────────────────────────────────────────────────────────
# 文本分句
# ─────────────────────────────────────────────────────────────

def split_sentences(text: str, max_chars: int = TTS_MAX_CHARS) -> list:
    if not text.strip():
        return []
    parts = re.split(r'(?<=[。！？!?\.…])', text)
    sentences = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(part) <= max_chars:
            sentences.append(part)
        else:
            sub_parts = re.split(r'(?<=[，,、；;：:])', part)
            buf = ""
            for sp in sub_parts:
                sp = sp.strip()
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


# ─────────────────────────────────────────────────────────────
# CMVN 工具
# ─────────────────────────────────────────────────────────────

def load_cmvn(mvn_path: str):
    """
    从 am.mvn 读取全局 CMVN 参数（80维）。
    格式：
      <LearnRateCoef> 0 [  neg_mean_0 ... neg_mean_79 ]
      <LearnRateCoef> 0 [  inv_stddev_0 ... inv_stddev_79 ]
    公式：output = (feats + neg_mean) * inv_stddev
         即 (feats - mean) / std
    """
    path = Path(mvn_path)
    if not path.exists():
        log.warning(f"am.mvn 不存在: {mvn_path}")
        return None, None
    try:
        neg_mean   = None
        inv_stddev = None
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("<LearnRateCoef>"):
                    continue
                # 格式：<LearnRateCoef> N [ v0 v1 ... vN ]
                # 去掉首尾标记，提取数值
                tokens = line.split()
                # 找 '[' 和 ']' 的位置
                try:
                    l_idx = tokens.index('[')
                    r_idx = tokens.index(']')
                    vals  = [float(x) for x in tokens[l_idx+1:r_idx]]
                except ValueError:
                    # 有些格式没有方括号，直接取第3个以后的数值
                    vals = [float(x) for x in tokens[3:]
                            if x not in ['[', ']']]
                arr = np.array(vals, dtype=np.float32)
                if neg_mean is None:
                    neg_mean   = arr
                else:
                    inv_stddev = arr
                    break

        if neg_mean is not None and inv_stddev is not None:
            log.info(f"CMVN 加载: dim={len(neg_mean)}  "
                     f"neg_mean范围[{neg_mean.min():.2f},{neg_mean.max():.2f}]  "
                     f"inv_std范围[{inv_stddev.min():.4f},{inv_stddev.max():.4f}]")
            return neg_mean, inv_stddev
        else:
            log.warning("am.mvn 解析不完整")
            return None, None
    except Exception as e:
        log.warning(f"am.mvn 读取异常: {e}")
        return None, None


def apply_cmvn_80(feats_80: np.ndarray,
                  neg_mean: Optional[np.ndarray],
                  inv_stddev: Optional[np.ndarray]) -> np.ndarray:
    """
    对 80 维 fbank 特征应用全局 CMVN。
    feats_80: (T, 80)
    公式：output = (feats + neg_mean) * inv_stddev
    """
    if neg_mean is None or inv_stddev is None:
        return feats_80
    dim = feats_80.shape[1]
    nm  = neg_mean[:dim]
    ivs = inv_stddev[:dim]
    return (feats_80 + nm) * ivs


# ─────────────────────────────────────────────────────────────
# LFR 特征拼接
# ─────────────────────────────────────────────────────────────

def apply_lfr(feats: np.ndarray, lfr_m: int = LFR_M, lfr_n: int = LFR_N) -> np.ndarray:
    """
    LFR (Low Frame Rate) 特征拼接。
    把连续 lfr_m 帧的 fbank 拼成一帧，步长 lfr_n。
    输入: (T, 80) → 输出: (T', 80*lfr_m) = (T', 560)

    边界处理：开头不足 lfr_m 帧时，用第一帧复制填充（左填充）。
    """
    T, feat_dim = feats.shape
    lfr_feats   = []

    # 从帧 0 开始，步长 lfr_n
    for i in range(0, T, lfr_n):
        # 取 [i-left_context, i+right_context] 共 lfr_m 帧
        # SenseVoice 使用左对齐：取 [i, i+lfr_m)，不足时右填充最后一帧
        start = i
        end   = min(i + lfr_m, T)
        chunk = feats[start:end]                        # (k, 80), k≤lfr_m

        if len(chunk) < lfr_m:
            # 右填充：用最后一帧重复
            pad = np.tile(feats[-1:], (lfr_m - len(chunk), 1))
            chunk = np.concatenate([chunk, pad], axis=0)

        lfr_feats.append(chunk.reshape(1, -1))          # (1, 560)

    if not lfr_feats:
        return np.zeros((1, feat_dim * lfr_m), dtype=np.float32)

    return np.concatenate(lfr_feats, axis=0)            # (T', 560)


# ═══════════════════════════════════════════════════════════════
# ASR
# ═══════════════════════════════════════════════════════════════

class SenseVoiceASR:
    """
    SenseVoice-Small ASR，RKNN NPU 推理。

    正确特征流水线：
      int16音频(16kHz)
        → 80维 log-fbank（25ms帧长，10ms帧移）
        → 全局 CMVN（am.mvn，80维参数）
        → LFR拼接（7帧→1帧，步长3）→ 560维特征
        → 分窗(124帧/窗，步长64)送入RKNN
        → CTC greedy decode
        → tokens.txt映射 → 文字
    """

    LANG_TOKEN = {"zh": 24884, "en": 24885, "auto": 24884}
    TEXTNORM_ITN   = 25016
    TEXTNORM_NOITN = 25017

    def __init__(self, lang: str = "zh", use_itn: bool = True):
        self._lang     = lang
        self._use_itn  = use_itn
        self._model    = None
        self._vocab    = {}
        self._neg_mean   = None
        self._inv_stddev = None
        self._load()

    def _load(self):
        try:
            import kaldi_native_fbank  # noqa
        except ImportError:
            raise ImportError(
                "缺少 kaldi-native-fbank：\n"
                "  pip3 install kaldi-native-fbank --break-system-packages")

        # 词表
        tokens_path = _MODELS / "tokens.txt"
        if not tokens_path.exists():
            raise FileNotFoundError(f"tokens.txt 不存在: {tokens_path}")
        self._vocab = self._load_vocab(str(tokens_path))
        log.info(f"词表: {len(self._vocab)} tokens")

        # CMVN（am.mvn 作用于80维 fbank，不是560维）
        mvn_path = _MODELS / "am.mvn"
        self._neg_mean, self._inv_stddev = load_cmvn(str(mvn_path))
        if self._neg_mean is None:
            print("[ASR] ⚠️  am.mvn 未找到，不做 CMVN")
            print("     复制命令: scp ~/sensevoice/models/SenseVoiceSmall/am.mvn "
                  f"lubancat@<IP>:{_MODELS}/")
        else:
            print(f"[ASR] CMVN(80维) 加载成功")

        # RKNN
        rknn_path = _MODELS / "sensevoice_small.rknn"
        if not rknn_path.exists():
            raise FileNotFoundError(f"RKNN 模型不存在: {rknn_path}")
        from rknnlite.api import RKNNLite
        m = RKNNLite(verbose=False)
        assert m.load_rknn(str(rknn_path)) == 0
        assert m.init_runtime(core_mask=RKNNLite.NPU_CORE_AUTO) == 0
        self._model = m
        print(f"[ASR] RKNN NPU  lang={self._lang}  "
              f"窗={RKNN_INPUT_LEN}帧  步长={RKNN_STEP_LEN}帧")

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
        """
        完整特征提取：80维fbank → CMVN → LFR → (T', 560)
        """
        import kaldi_native_fbank as knf

        audio_f32 = audio_int16.astype(np.float32) / 32768.0

        # Step 1: 提取80维 log-fbank
        opts = knf.FbankOptions()
        opts.frame_opts.samp_freq       = ASR_RATE
        opts.frame_opts.dither          = 0.0
        opts.frame_opts.preemph_coeff   = 0.97
        opts.frame_opts.frame_length_ms = 25.0
        opts.frame_opts.frame_shift_ms  = 10.0
        opts.frame_opts.remove_dc_offset = True
        opts.mel_opts.num_bins          = FBANK_DIM   # 80，不是560
        opts.mel_opts.low_freq          = 20.0
        opts.mel_opts.high_freq         = 0.0

        fb = knf.OnlineFbank(opts)
        fb.accept_waveform(ASR_RATE, audio_f32.tolist())
        fb.input_finished()

        n = fb.num_frames_ready
        if n == 0:
            log.warning("fbank 提取结果为 0 帧")
            return np.zeros((1, FBANK_DIM * LFR_M), dtype=np.float32)

        feats_80 = np.array([fb.get_frame(i) for i in range(n)],
                            dtype=np.float32)   # (T, 80)

        # Step 2: 全局 CMVN（在 LFR 之前，作用于80维）
        feats_80 = apply_cmvn_80(feats_80, self._neg_mean, self._inv_stddev)

        # Step 3: LFR 拼接 → (T', 560)
        feats_560 = apply_lfr(feats_80, lfr_m=LFR_M, lfr_n=LFR_N)

        log.info(f"特征: fbank_80={feats_80.shape}  "
                 f"LFR_560={feats_560.shape}  "
                 f"值域[{feats_560.min():.2f},{feats_560.max():.2f}]")
        return feats_560   # (T', 560)

    def _infer_window(self, window_feats: np.ndarray) -> list:
        """
        单窗推理。window_feats: (RKNN_INPUT_LEN, 560)，已经是最终特征。
        直接送入模型，不做任何额外归一化。
        返回 CTC collapse 后的 token ID 列表。
        """
        feats    = window_feats[np.newaxis].astype(np.float32)   # (1, 124, 560)
        lang_id  = self.LANG_TOKEN.get(self._lang, 24884)
        textnorm = self.TEXTNORM_ITN if self._use_itn else self.TEXTNORM_NOITN

        outputs  = self._model.inference(inputs=[
            feats,
            np.array([RKNN_INPUT_LEN], dtype=np.int32),
            np.array([lang_id],        dtype=np.int32),
            np.array([textnorm],       dtype=np.int32),
        ])
        logits   = outputs[0][0]   # (RKNN_INPUT_LEN, vocab_size)
        ids_raw  = logits.argmax(axis=-1).tolist()

        ids, prev = [], 0
        for tok in ids_raw:
            if tok != 0 and tok != prev:
                ids.append(tok)
            prev = tok
        return ids

    def _ids_to_text(self, ids: list) -> str:
        pieces = []
        for i in ids:
            tok = self._vocab.get(i, "")
            # 过滤特殊token
            if tok.startswith("<|") and tok.endswith("|>"):
                continue
            if tok in ("<unk>", "<s>", "</s>", ""):
                continue
            pieces.append(tok)
        return "".join(pieces).replace("▁", " ").strip()

    def recognize(self, audio_int16: np.ndarray,
                  debug: bool = False) -> str:
        t0    = time.perf_counter()
        feats = self._extract_features(audio_int16)   # (T', 560)
        T     = feats.shape[0]

        if T == 0:
            return ""

        all_ids = []

        if T <= RKNN_INPUT_LEN:
            # 短音频：右填充到 INPUT_LEN
            pad    = RKNN_INPUT_LEN - T
            window = np.pad(feats, ((0, pad), (0, 0)), mode="constant")
            ids    = self._infer_window(window)
            if debug:
                toks = [self._vocab.get(i, "?") for i in ids]
                print(f"  [debug] 单窗 pad={pad}帧  ids={ids}  tokens={toks}")
            all_ids.extend(ids)
        else:
            # 滑动窗口推理
            n_wins = 0
            start  = 0
            while start < T:
                end    = start + RKNN_INPUT_LEN
                window = feats[start:end] if end <= T else feats[T-RKNN_INPUT_LEN:T]
                ids    = self._infer_window(window)
                if debug:
                    toks = [self._vocab.get(i, "?") for i in ids]
                    print(f"  [debug] 窗{n_wins+1} [{start}:{min(end,T)}]  "
                          f"ids={ids}  tokens={toks}")
                all_ids.extend(ids)
                n_wins += 1
                if end >= T:
                    break
                start += RKNN_STEP_LEN
            log.info(f"ASR: {n_wins}个窗口")

        # 全局 CTC collapse
        merged, prev = [], 0
        for tok in all_ids:
            if tok != 0 and tok != prev:
                merged.append(tok)
            prev = tok

        if debug:
            toks = [self._vocab.get(i, "?") for i in merged[:30]]
            print(f"  [debug] 合并IDs({len(merged)}): {merged[:30]}")
            print(f"  [debug] tokens: {toks}")

        text = self._ids_to_text(merged)
        lat  = (time.perf_counter() - t0) * 1000
        log.info(f"ASR结果: '{text}'  ({lat:.0f}ms)")
        return text

    def set_language(self, lang: str):
        self._lang = lang

    def release(self):
        if self._model:
            try:
                self._model.release()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════
# TTS
# ═══════════════════════════════════════════════════════════════

class SherpaTTS:
    def __init__(self, output_device: Optional[int] = None):
        self._output_device = output_device
        self._device_rate   = probe_device_rate(output_device, "output")
        print(f"[TTS] 输出={output_device}  采样率={self._device_rate}Hz")
        self._tts_zh = None
        self._tts_en = None
        self._load()

    def _load(self):
        import sherpa_onnx

        d = _MODELS / "tts" / "vits-zh-hf-theresa"
        if d.exists():
            onnx_list = list(d.glob("*.onnx"))
            tokens_p  = d / "tokens.txt"
            lexicon_p = d / "lexicon.txt"
            data_dir  = str(d/"espeak-ng-data") if (d/"espeak-ng-data").exists() else ""
            rule_fsts = ",".join(str(p) for p in
                [d/"phone.fst", d/"date.fst", d/"number.fst"] if p.exists())
            if onnx_list and tokens_p.exists() and lexicon_p.exists():
                try:
                    self._tts_zh = sherpa_onnx.OfflineTts(
                        sherpa_onnx.OfflineTtsConfig(
                            model=sherpa_onnx.OfflineTtsModelConfig(
                                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                                    model=str(onnx_list[0]), lexicon=str(lexicon_p),
                                    tokens=str(tokens_p), data_dir=data_dir)),
                            rule_fsts=rule_fsts, max_num_sentences=1))
                    print(f"[TTS] 中文: {onnx_list[0].name}")
                except Exception as e:
                    log.warning(f"中文TTS: {e}")

        d = _MODELS / "tts" / "kokoro-multi-lang-v1_0"
        if d.exists():
            onnx_list  = list(d.glob("*int8*.onnx")) + list(d.glob("*.onnx"))
            voices_p   = d / "voices.bin"
            tokens_p   = d / "tokens.txt"
            data_dir   = str(d/"espeak-ng-data") if (d/"espeak-ng-data").exists() else ""
            lexicon_en = next((str(d/n) for n in
                ["lexicon-us-en.txt","lexicon-gb-en.txt","lexicon.txt"]
                if (d/n).exists()), "")
            if onnx_list and voices_p.exists() and tokens_p.exists() and lexicon_en:
                try:
                    self._tts_en = sherpa_onnx.OfflineTts(
                        sherpa_onnx.OfflineTtsConfig(
                            model=sherpa_onnx.OfflineTtsModelConfig(
                                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                                    model=str(onnx_list[0]), voices=str(voices_p),
                                    tokens=str(tokens_p), data_dir=data_dir,
                                    lang="en-us", lexicon=lexicon_en)),
                            max_num_sentences=1))
                    print(f"[TTS] 英文: {onnx_list[0].name}")
                except Exception as e:
                    log.warning(f"英文TTS: {e}")

        if self._tts_zh is None and self._tts_en is None:
            raise RuntimeError("没有可用的TTS模型")

    @staticmethod
    def _detect_lang(text: str) -> str:
        for ch in text:
            if '\u4e00' <= ch <= '\u9fff':
                return "zh"
        return "en"

    def _get_engine(self, lang: str):
        if lang == "zh" and self._tts_zh: return self._tts_zh
        if lang == "en" and self._tts_en: return self._tts_en
        return self._tts_zh or self._tts_en

    def synthesize_sentence(self, sent: str, lang: str, speed: float = 1.0):
        result = self._get_engine(lang).generate(sent, sid=0, speed=speed)
        return np.array(result.samples, dtype=np.float32), result.sample_rate

    def _play(self, audio: np.ndarray, sr: int):
        import sounddevice as sd, librosa
        if sr != self._device_rate:
            audio = librosa.resample(audio, orig_sr=sr, target_sr=self._device_rate)
        sd.play(audio, samplerate=self._device_rate, device=self._output_device)
        sd.wait()

    def speak(self, text: str, lang: Optional[str] = None,
              speed: float = 1.0, block: bool = True):
        if not text.strip(): return
        if lang is None: lang = self._detect_lang(text)
        sentences = split_sentences(text, max_chars=TTS_MAX_CHARS)
        if not sentences: return
        log.info(f"TTS分句: {len(sentences)}句")

        if len(sentences) == 1:
            audio, sr = self.synthesize_sentence(sentences[0], lang, speed)
            if block: self._play(audio, sr)
            else: threading.Thread(target=self._play, args=(audio,sr), daemon=True).start()
            return

        import queue
        q: queue.Queue = queue.Queue(maxsize=2)

        def synth():
            for i, s in enumerate(sentences):
                t0 = time.perf_counter()
                audio, sr = self.synthesize_sentence(s, lang, speed)
                log.info(f"TTS[{i+1}/{len(sentences)}] '{s[:12]}' "
                         f"{(time.perf_counter()-t0)*1000:.0f}ms")
                q.put((audio, sr))
            q.put(None)

        def play():
            while True:
                item = q.get()
                if item is None: break
                self._play(*item)

        st = threading.Thread(target=synth, daemon=True)
        pt = threading.Thread(target=play,  daemon=True)
        st.start(); pt.start()
        if block: st.join(); pt.join()


# ═══════════════════════════════════════════════════════════════
# VoiceRecorder
# ═══════════════════════════════════════════════════════════════

class VoiceRecorder:
    def __init__(self,
                 input_device:    Optional[int] = None,
                 silence_timeout: float = SILENCE_TIMEOUT,
                 max_record_sec:  float = MAX_RECORD_SEC):
        self._input_device    = input_device
        self._silence_timeout = silence_timeout
        self._max_record_sec  = max_record_sec
        self._device_rate     = probe_device_rate(input_device, "input")
        self._chunk_samples   = int(self._device_rate * VAD_CHUNK_MS / 1000)
        self._silence_thresh  = 500
        print(f"[Recorder] 设备={input_device}  采样率={self._device_rate}Hz")

    def calibrate_noise(self, duration: float = VAD_CALIB_SEC) -> int:
        import sounddevice as sd
        n = int(duration * self._device_rate)
        print(f"[Recorder] 校准背景噪声...", end=" ", flush=True)
        try:
            raw   = sd.rec(n, samplerate=self._device_rate, channels=CHANNELS,
                           dtype=RECORD_DTYPE, device=self._input_device)
            sd.wait()
            rms   = int(np.sqrt(np.mean(raw.astype(np.float32)**2)))
            thresh = int(np.clip(rms * VAD_THRESH_FACTOR,
                                 VAD_THRESH_MIN, VAD_THRESH_MAX))
            self._silence_thresh = thresh
            print(f"RMS={rms}  阈值={thresh}")
            return thresh
        except Exception as e:
            log.warning(f"校准失败: {e}")
            return self._silence_thresh

    def _resample_to_asr(self, raw: np.ndarray) -> np.ndarray:
        import librosa
        if self._device_rate != ASR_RATE:
            raw = librosa.resample(raw, orig_sr=self._device_rate, target_sr=ASR_RATE)
        return (raw * 32767).clip(-32768, 32767).astype(np.int16)

    def record_once(self, on_start=None, on_stop=None,
                    auto_calibrate: bool = True) -> np.ndarray:
        import sounddevice as sd
        if auto_calibrate:
            self.calibrate_noise()

        audio_buf   = []
        state       = "waiting"
        silence_cnt = 0
        thresh      = self._silence_thresh
        max_frames  = int(self._max_record_sec * self._device_rate / self._chunk_samples)

        def callback(indata, frames, time_info, status):
            nonlocal state, silence_cnt, audio_buf
            chunk = indata[:, 0].copy()
            rms   = int(np.sqrt(np.mean(chunk.astype(np.float32)**2)))
            if state == "waiting":
                if rms > thresh:
                    state = "recording"; audio_buf = [chunk.copy()]
                    silence_cnt = 0
                    if on_start: threading.Thread(target=on_start, daemon=True).start()
            elif state == "recording":
                audio_buf.append(chunk.copy())
                if rms < thresh:
                    silence_cnt += 1
                    if silence_cnt >= int(self._silence_timeout * 1000 / VAD_CHUNK_MS):
                        state = "done"
                        if on_stop: threading.Thread(target=on_stop, daemon=True).start()
                else:
                    silence_cnt = 0
                if len(audio_buf) >= max_frames:
                    state = "done"
                    log.warning("VAD: 达到最大录音时长")

        with sd.InputStream(samplerate=self._device_rate, channels=CHANNELS,
                            dtype=RECORD_DTYPE, blocksize=self._chunk_samples,
                            device=self._input_device, callback=callback):
            print(f"[Recorder] 等待说话... (阈值={thresh}  最长{self._max_record_sec:.0f}s)")
            while state != "done":
                time.sleep(0.05)

        if not audio_buf:
            return np.zeros(ASR_RATE, dtype=np.int16)
        raw = np.concatenate(audio_buf).astype(np.float32)
        log.info(f"录音: {len(raw)/self._device_rate:.1f}s")
        return self._resample_to_asr(raw)

    def record_fixed(self, duration_sec: float = 5.0) -> np.ndarray:
        import sounddevice as sd
        raw = sd.rec(int(duration_sec * self._device_rate),
                     samplerate=self._device_rate, channels=CHANNELS,
                     dtype=RECORD_DTYPE, device=self._input_device)
        sd.wait()
        return self._resample_to_asr(raw[:, 0].astype(np.float32))


# ═══════════════════════════════════════════════════════════════
# VoiceModule
# ═══════════════════════════════════════════════════════════════

class VoiceModule:
    def __init__(self,
                 lang:          str   = "zh",
                 input_device:  Optional[int] = None,
                 output_device: Optional[int] = None,
                 auto_usb:      bool  = True,
                 tts_speed:     float = 1.0,
                 enable_mqtt:   bool  = False,
                 mqtt_topic:    str   = "smart_home/voice/asr"):
        self._lang      = lang
        self._tts_speed = tts_speed
        self._running   = False

        if auto_usb and (input_device is None or output_device is None):
            ai, ao = find_usb_audio_devices()
            if input_device  is None: input_device  = ai
            if output_device is None: output_device = ao
            if ai is not None or ao is not None:
                print(f"[VoiceModule] USB: 麦克风={input_device} 扬声器={output_device}")

        self._asr      = SenseVoiceASR(lang=lang)
        self._tts      = SherpaTTS(output_device=output_device)
        self._recorder = VoiceRecorder(input_device=input_device)

        self._mqtt_client = None
        self._mqtt_topic  = mqtt_topic
        if enable_mqtt: self._init_mqtt()
        print(f"[VoiceModule] 就绪  lang={lang}")

    def _init_mqtt(self):
        try:
            import paho.mqtt.client as mqtt
            self._mqtt_client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION1, client_id="voice_module")
            self._mqtt_client.connect("localhost", 1883, keepalive=60)
            self._mqtt_client.loop_start()
        except Exception as e:
            log.warning(f"MQTT: {e}"); self._mqtt_client = None

    def _publish_asr(self, text: str):
        if self._mqtt_client:
            import json
            self._mqtt_client.publish(self._mqtt_topic,
                json.dumps({"text": text, "lang": self._lang,
                            "timestamp": time.strftime("%Y%m%d_%H%M%S")},
                           ensure_ascii=False), qos=0)

    def listen_once(self, on_start=None, on_stop=None) -> str:
        audio = self._recorder.record_once(on_start=on_start, on_stop=on_stop)
        text  = self._asr.recognize(audio)
        if text.strip(): self._publish_asr(text)
        return text

    def speak(self, text: str, lang: Optional[str] = None):
        if not text.strip(): return
        self._tts.speak(text, lang=lang, speed=self._tts_speed, block=True)

    def voice_turn(self, llm_callback: Callable[[str], str],
                   on_listen=None, on_thinking=None, on_speaking=None) -> tuple:
        if on_listen: on_listen()
        user_text = self.listen_once()
        if not user_text.strip(): return "", ""
        print(f"[VoiceModule] 用户: {user_text}")
        if on_thinking: on_thinking()
        llm_text = llm_callback(user_text)
        print(f"[VoiceModule] LLM : {llm_text}")
        if on_speaking: on_speaking()
        self.speak(llm_text)
        return user_text, llm_text

    def start_loop(self, llm_callback: Callable[[str], str],
                   stop_word: str = "退出",
                   on_listen=None, on_thinking=None, on_speaking=None):
        if self._running: return
        self._running = True

        def _loop():
            print(f"[VoiceModule] 对话循环  停止词='{stop_word}'")
            while self._running:
                try:
                    u, _ = self.voice_turn(llm_callback, on_listen=on_listen,
                        on_thinking=on_thinking, on_speaking=on_speaking)
                    if stop_word and stop_word in u:
                        self._running = False; break
                    time.sleep(0.1)
                except Exception as e:
                    log.error(f"Voice loop: {e}", exc_info=True)
                    time.sleep(1.0)

        t = threading.Thread(target=_loop, daemon=True, name="voice_loop")
        t.start()
        return t

    def set_language(self, lang: str):
        self._lang = lang
        self._asr.set_language(lang)

    def stop(self):
        self._running = False
        self._asr.release()
        if self._mqtt_client:
            self._mqtt_client.loop_stop()
            self._mqtt_client.disconnect()


# ═══════════════════════════════════════════════════════════════
# 独立运行入口
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="dialog",
                        choices=["list-devices","probe","calibrate",
                                 "debug","asr","tts","dialog"])
    parser.add_argument("--lang",          default="zh", choices=["zh","en"])
    parser.add_argument("--wav",           default=None)
    parser.add_argument("--text",
        default="你好，我是智能家居助手。客厅温度二十四度，空气质量良好。")
    parser.add_argument("--input-device",  type=int, default=None)
    parser.add_argument("--output-device", type=int, default=None)
    parser.add_argument("--no-auto-usb",   action="store_true")
    args = parser.parse_args()

    auto_usb = not args.no_auto_usb
    in_dev, out_dev = args.input_device, args.output_device
    if auto_usb and (in_dev is None or out_dev is None):
        ai, ao = find_usb_audio_devices()
        if in_dev  is None: in_dev  = ai
        if out_dev is None: out_dev = ao

    if args.mode == "list-devices":
        list_audio_devices()

    elif args.mode == "probe":
        print(f"输入[{in_dev}]: {probe_device_rate(in_dev,'input')}Hz")
        print(f"输出[{out_dev}]: {probe_device_rate(out_dev,'output')}Hz")

    elif args.mode == "calibrate":
        rec = VoiceRecorder(input_device=in_dev)
        rec.calibrate_noise()

    elif args.mode == "debug":
        print("="*55)
        print("  ASR 调试模式")
        print("="*55)
        asr = SenseVoiceASR(lang=args.lang)
        if args.wav:
            import soundfile as sf, librosa
            audio, sr = sf.read(args.wav, dtype="int16")
            if audio.ndim > 1: audio = audio[:, 0]
            if sr != ASR_RATE:
                audio = librosa.resample(audio.astype(np.float32),
                    orig_sr=sr, target_sr=ASR_RATE).astype(np.int16)
        else:
            rec   = VoiceRecorder(input_device=in_dev)
            audio = rec.record_once(
                on_start=lambda: print("  [录音中...]"),
                on_stop =lambda: print("  [识别中...]"))
        text = asr.recognize(audio, debug=True)
        print(f"\n最终识别结果: '{text}'\n")
        asr.release()

    elif args.mode == "asr":
        asr = SenseVoiceASR(lang=args.lang)
        if args.wav:
            import soundfile as sf, librosa
            audio, sr = sf.read(args.wav, dtype="int16")
            if audio.ndim > 1: audio = audio[:, 0]
            if sr != ASR_RATE:
                audio = librosa.resample(audio.astype(np.float32),
                    orig_sr=sr, target_sr=ASR_RATE).astype(np.int16)
            text = asr.recognize(audio)
        else:
            rec   = VoiceRecorder(input_device=in_dev)
            audio = rec.record_once(
                on_start=lambda: print("  [录音中...]"),
                on_stop =lambda: print("  [识别中...]"))
            text  = asr.recognize(audio)
        print(f"\n{'─'*50}")
        print(f"  识别结果: {text}")
        print(f"{'─'*50}\n")
        asr.release()

    elif args.mode == "tts":
        sents = split_sentences(args.text)
        print(f"分句({len(sents)}句): {sents}")
        tts = SherpaTTS(output_device=out_dev)
        tts.speak(args.text, lang=args.lang)

    else:  # dialog
        def echo_llm(t): return f"我听到你说：{t}。"
        vm = VoiceModule(lang=args.lang, input_device=in_dev,
                         output_device=out_dev, auto_usb=auto_usb)
        vm.speak("语音模块已启动，请说话。")
        try:
            for _ in range(3):
                u, r = vm.voice_turn(echo_llm)
                if not u: break
        except KeyboardInterrupt:
            pass
        finally:
            vm.stop()