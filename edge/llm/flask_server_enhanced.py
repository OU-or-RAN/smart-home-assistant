import ctypes
import sys
import os
import subprocess
import resource
import threading
import time
import argparse
import json
import re
import logging
from flask import Flask, request, jsonify, Response

log = logging.getLogger("flask_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")

app = Flask(__name__)

rkllm_lib = ctypes.CDLL('/home/lubancat/rknn-llm/examples/rkllm_server_demo/rkllm_server/lib/librkllmrt.so')
RKLLM_Handle_t = ctypes.c_void_p
userdata = ctypes.c_void_p(None)

LLMCallState = ctypes.c_int
LLMCallState.RKLLM_RUN_NORMAL  = 0
LLMCallState.RKLLM_RUN_WAITING = 1
LLMCallState.RKLLM_RUN_FINISH  = 2
LLMCallState.RKLLM_RUN_ERROR   = 3

RKLLMInputType = ctypes.c_int
RKLLMInputType.RKLLM_INPUT_PROMPT     = 0
RKLLMInputType.RKLLM_INPUT_TOKEN      = 1
RKLLMInputType.RKLLM_INPUT_EMBED      = 2
RKLLMInputType.RKLLM_INPUT_MULTIMODAL = 3

RKLLMInferMode = ctypes.c_int
RKLLMInferMode.RKLLM_INFER_GENERATE              = 0
RKLLMInferMode.RKLLM_INFER_GET_LAST_HIDDEN_LAYER = 1
RKLLMInferMode.RKLLM_INFER_GET_LOGITS            = 2

class RKLLMExtendParam(ctypes.Structure):
    _fields_ = [("base_domain_id",ctypes.c_int32),("embed_flash",ctypes.c_int8),("enabled_cpus_num",ctypes.c_int8),("enabled_cpus_mask",ctypes.c_uint32),("n_batch",ctypes.c_uint8),("use_cross_attn",ctypes.c_int8),("reserved",ctypes.c_uint8*104)]

class RKLLMParam(ctypes.Structure):
    _fields_ = [("model_path",ctypes.c_char_p),("max_context_len",ctypes.c_int32),("max_new_tokens",ctypes.c_int32),("top_k",ctypes.c_int32),("n_keep",ctypes.c_int32),("top_p",ctypes.c_float),("temperature",ctypes.c_float),("repeat_penalty",ctypes.c_float),("frequency_penalty",ctypes.c_float),("presence_penalty",ctypes.c_float),("mirostat",ctypes.c_int32),("mirostat_tau",ctypes.c_float),("mirostat_eta",ctypes.c_float),("skip_special_token",ctypes.c_bool),("is_async",ctypes.c_bool),("img_start",ctypes.c_char_p),("img_end",ctypes.c_char_p),("img_content",ctypes.c_char_p),("extend_param",RKLLMExtendParam)]

class RKLLMLoraAdapter(ctypes.Structure):
    _fields_ = [("lora_adapter_path",ctypes.c_char_p),("lora_adapter_name",ctypes.c_char_p),("scale",ctypes.c_float)]
class RKLLMEmbedInput(ctypes.Structure):
    _fields_ = [("embed",ctypes.POINTER(ctypes.c_float)),("n_tokens",ctypes.c_size_t)]
class RKLLMTokenInput(ctypes.Structure):
    _fields_ = [("input_ids",ctypes.POINTER(ctypes.c_int32)),("n_tokens",ctypes.c_size_t)]
class RKLLMMultiModelInput(ctypes.Structure):
    _fields_ = [("prompt",ctypes.c_char_p),("image_embed",ctypes.POINTER(ctypes.c_float)),("n_image_tokens",ctypes.c_size_t),("n_image",ctypes.c_size_t),("image_width",ctypes.c_size_t),("image_height",ctypes.c_size_t)]
class RKLLMInputUnion(ctypes.Union):
    _fields_ = [("prompt_input",ctypes.c_char_p),("embed_input",RKLLMEmbedInput),("token_input",RKLLMTokenInput),("multimodal_input",RKLLMMultiModelInput)]
class RKLLMInput(ctypes.Structure):
    _fields_ = [("role",ctypes.c_char_p),("enable_thinking",ctypes.c_bool),("input_type",RKLLMInputType),("input_data",RKLLMInputUnion)]
class RKLLMLoraParam(ctypes.Structure):
    _fields_ = [("lora_adapter_name",ctypes.c_char_p)]
class RKLLMPromptCacheParam(ctypes.Structure):
    _fields_ = [("save_prompt_cache",ctypes.c_int),("prompt_cache_path",ctypes.c_char_p)]
class RKLLMInferParam(ctypes.Structure):
    _fields_ = [("mode",RKLLMInferMode),("lora_params",ctypes.POINTER(RKLLMLoraParam)),("prompt_cache_params",ctypes.POINTER(RKLLMPromptCacheParam)),("keep_history",ctypes.c_int)]
class RKLLMResultLastHiddenLayer(ctypes.Structure):
    _fields_ = [("hidden_states",ctypes.POINTER(ctypes.c_float)),("embd_size",ctypes.c_int),("num_tokens",ctypes.c_int)]
class RKLLMResultLogits(ctypes.Structure):
    _fields_ = [("logits",ctypes.POINTER(ctypes.c_float)),("vocab_size",ctypes.c_int),("num_tokens",ctypes.c_int)]
class RKLLMPerfStat(ctypes.Structure):
    _fields_ = [("prefill_time_ms",ctypes.c_float),("prefill_tokens",ctypes.c_int),("generate_time_ms",ctypes.c_float),("generate_tokens",ctypes.c_int),("memory_usage_mb",ctypes.c_float)]
class RKLLMResult(ctypes.Structure):
    _fields_ = [("text",ctypes.c_char_p),("token_id",ctypes.c_int),("last_hidden_layer",RKLLMResultLastHiddenLayer),("logits",RKLLMResultLogits),("perf",RKLLMPerfStat)]

lock             = threading.Lock()
is_blocking      = False
global_text      = []
global_state     = -1
split_byte_data  = bytes(b"")
abort_requested  = threading.Event()

def callback_impl(result, userdata, state):
    global global_text, global_state, abort_requested
    if abort_requested.is_set():
        global_state = LLMCallState.RKLLM_RUN_ERROR
        sys.stdout.flush()
        return 1
    if state == LLMCallState.RKLLM_RUN_FINISH:
        global_state = state
        sys.stdout.flush()
    elif state == LLMCallState.RKLLM_RUN_ERROR:
        global_state = state
        sys.stdout.flush()
    elif state == LLMCallState.RKLLM_RUN_NORMAL:
        global_state = state
        text_chunk = result.contents.text.decode('utf-8')
        global_text.append(text_chunk)
        print(text_chunk, end='', flush=True)
    return 0

callback_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(RKLLMResult), ctypes.c_void_p, ctypes.c_int)
callback = callback_type(callback_impl)


class RKLLM(object):
    def __init__(self, model_path, lora_model_path=None, prompt_cache_path=None, platform="rk3588"):
        rkllm_param = RKLLMParam()
        rkllm_param.model_path        = bytes(model_path, 'utf-8')
        rkllm_param.max_context_len   = 4096
        rkllm_param.max_new_tokens    = 2048     # ★ 放开生成长度，让LLM完整输出（仅由用户语音打断或EOS停止）
        rkllm_param.skip_special_token = True
        rkllm_param.n_keep            = -1
        rkllm_param.top_k             = 1
        rkllm_param.top_p             = 0.9
        rkllm_param.temperature       = 0.3      # ★ 低温度，更确定性
        rkllm_param.repeat_penalty    = 1.1
        rkllm_param.frequency_penalty = 0.0
        rkllm_param.presence_penalty  = 0.0
        rkllm_param.mirostat          = 0
        rkllm_param.mirostat_tau      = 5.0
        rkllm_param.mirostat_eta      = 0.1
        rkllm_param.is_async          = False
        rkllm_param.img_start         = "".encode('utf-8')
        rkllm_param.img_end           = "".encode('utf-8')
        rkllm_param.img_content       = "".encode('utf-8')
        rkllm_param.extend_param.base_domain_id  = 0
        rkllm_param.extend_param.embed_flash     = 1
        rkllm_param.extend_param.n_batch         = 1
        rkllm_param.extend_param.use_cross_attn  = 0
        rkllm_param.extend_param.enabled_cpus_num = 4
        if platform.lower() in ["rk3576", "rk3588"]:
            rkllm_param.extend_param.enabled_cpus_mask = ((1<<4)|(1<<5)|(1<<6)|(1<<7))
        else:
            rkllm_param.extend_param.enabled_cpus_mask = ((1<<0)|(1<<1)|(1<<2)|(1<<3))

        self.handle = RKLLM_Handle_t()
        self.rkllm_init = rkllm_lib.rkllm_init
        self.rkllm_init.argtypes = [ctypes.POINTER(RKLLM_Handle_t), ctypes.POINTER(RKLLMParam), callback_type]
        self.rkllm_init.restype = ctypes.c_int
        ret = self.rkllm_init(ctypes.byref(self.handle), ctypes.byref(rkllm_param), callback)
        if ret != 0:
            print("\nrkllm init failed\n"); exit(0)
        else:
            print("\nrkllm init success!\n")

        self.rkllm_run = rkllm_lib.rkllm_run
        self.rkllm_run.argtypes = [RKLLM_Handle_t, ctypes.POINTER(RKLLMInput), ctypes.POINTER(RKLLMInferParam), ctypes.c_void_p]
        self.rkllm_run.restype = ctypes.c_int
        self.set_chat_template = rkllm_lib.rkllm_set_chat_template
        self.set_chat_template.argtypes = [RKLLM_Handle_t, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
        self.set_chat_template.restype = ctypes.c_int
        self.set_function_tools_ = rkllm_lib.rkllm_set_function_tools
        self.set_function_tools_.argtypes = [RKLLM_Handle_t, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
        self.set_function_tools_.restype = ctypes.c_int
        self.rkllm_destroy = rkllm_lib.rkllm_destroy
        self.rkllm_destroy.argtypes = [RKLLM_Handle_t]
        self.rkllm_destroy.restype = ctypes.c_int
        self.rkllm_abort = rkllm_lib.rkllm_abort

        rkllm_lora_params = None
        if lora_model_path:
            lora_adapter_name = "test"
            lora_adapter = RKLLMLoraAdapter()
            ctypes.memset(ctypes.byref(lora_adapter), 0, ctypes.sizeof(RKLLMLoraAdapter))
            lora_adapter.lora_adapter_path = ctypes.c_char_p(lora_model_path.encode('utf-8'))
            lora_adapter.lora_adapter_name = ctypes.c_char_p(lora_adapter_name.encode('utf-8'))
            lora_adapter.scale = 1.0
            rkllm_load_lora = rkllm_lib.rkllm_load_lora
            rkllm_load_lora.argtypes = [RKLLM_Handle_t, ctypes.POINTER(RKLLMLoraAdapter)]
            rkllm_load_lora.restype = ctypes.c_int
            rkllm_load_lora(self.handle, ctypes.byref(lora_adapter))
            rkllm_lora_params = RKLLMLoraParam()
            rkllm_lora_params.lora_adapter_name = ctypes.c_char_p(lora_adapter_name.encode('utf-8'))

        self.rkllm_infer_params = RKLLMInferParam()
        ctypes.memset(ctypes.byref(self.rkllm_infer_params), 0, ctypes.sizeof(RKLLMInferParam))
        self.rkllm_infer_params.mode = RKLLMInferMode.RKLLM_INFER_GENERATE
        self.rkllm_infer_params.lora_params = (ctypes.pointer(rkllm_lora_params) if rkllm_lora_params else None)
        self.rkllm_infer_params.keep_history = 0

        if prompt_cache_path:
            rkllm_load_prompt_cache = rkllm_lib.rkllm_load_prompt_cache
            rkllm_load_prompt_cache.argtypes = [RKLLM_Handle_t, ctypes.c_char_p]
            rkllm_load_prompt_cache.restype = ctypes.c_int
            rkllm_load_prompt_cache(self.handle, ctypes.c_char_p(prompt_cache_path.encode('utf-8')))
        self.tools = None

    def set_function_tools(self, system_prompt, tools, tool_response_str):
        if self.tools is None or self.tools != tools:
            self.tools = tools
            self.set_function_tools_(self.handle, ctypes.c_char_p(system_prompt.encode('utf-8')), ctypes.c_char_p(tools.encode('utf-8')), ctypes.c_char_p(tool_response_str.encode('utf-8')))

    def run(self, role, enable_thinking, prompt):
        rkllm_input = RKLLMInput()
        rkllm_input.role = (role or "user").encode('utf-8')
        rkllm_input.enable_thinking = ctypes.c_bool(bool(enable_thinking))
        rkllm_input.input_type = RKLLMInputType.RKLLM_INPUT_PROMPT
        rkllm_input.input_data.prompt_input = ctypes.c_char_p(prompt.encode('utf-8'))
        self.rkllm_run(self.handle, ctypes.byref(rkllm_input), ctypes.byref(self.rkllm_infer_params), None)

    def abort(self):
        return self.rkllm_abort(self.handle)
    def release(self):
        self.rkllm_destroy(self.handle)


rkllm_model = None

# ══════════════════════════════════════════════════════════════════════════════
# 文本清洗
# ══════════════════════════════════════════════════════════════════════════════
def _strip_special_tokens(text: str) -> str:
    text = re.sub(r'<\|[^|]+\|>', '', text)
    text = re.sub(r'<(?![\u4e00-\u9fff])[^>]{0,30}>', '', text)
    return text

class ThinkingFilter:
    """
    流式 </think> 过滤器。
    COMMIT_THRESHOLD=15: 等 15 字符再发出，防止 </think> 中间断开。
    """
    COMMIT_THRESHOLD = 15

    def __init__(self):
        self._pending = ""
        self._buf = ""

    def feed(self, chunk: str) -> str:
        text = self._buf + chunk
        self._buf = ""
        while True:
            idx = text.find('</think>')
            if idx == -1: break
            text = text[idx + len('</think>'):]
            self._pending = ""
        keep = 0
        for L in range(min(7, len(text)), 0, -1):
            if '</think>'.startswith(text[-L:]):
                keep = L; break
        if keep:
            self._buf = text[-keep:]
            text = text[:-keep]
        self._pending += text
        if len(self._pending) > self.COMMIT_THRESHOLD:
            emit = self._pending[:-self.COMMIT_THRESHOLD]
            self._pending = self._pending[-self.COMMIT_THRESHOLD:]
            return _strip_special_tokens(emit)
        return ""

    def flush(self) -> str:
        result = self._pending + self._buf
        self._pending = ""
        self._buf = ""
        return _strip_special_tokens(result)

def _sanitize_full_response(text: str) -> str:
    last = text.rfind('</think>')
    if last >= 0:
        text = text[last + len('</think>'):]
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = _strip_special_tokens(text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# ══════════════════════════════════════════════════════════════════════════════
# Flask 路由
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/health', methods=['GET'])
def health():
    if rkllm_model is not None:
        return jsonify({"status": "ok", "model": "rkllm"}), 200
    return jsonify({"status": "error", "message": "model not loaded"}), 503

@app.route('/abort', methods=['POST'])
def abort_generation():
    abort_requested.set()
    if rkllm_model is not None:
        rkllm_model.abort()
    return jsonify({"status": "aborted"}), 200

@app.route('/rkllm_chat', methods=['POST'])
def receive_message():
    global global_text, global_state
    global is_blocking, rkllm_model, abort_requested

    if is_blocking:
        return jsonify({'status': 'error', 'message': 'RKLLM_Server is busy!'}), 503

    lock.acquire()
    try:
        is_blocking     = True
        global_state    = -1
        abort_requested.clear()

        data = request.json
        if not data or 'messages' not in data:
            return jsonify({'status': 'error', 'message': 'Invalid JSON data!'}), 400

        global_text = []

        messages        = data['messages']
        enable_thinking = data.get('enable_thinking', False)
        stream          = data.get('stream', False)
        TOOLS           = data.get('tools')

        # ★ 提取系统提示词和最后一条用户消息
        current_system = ""
        all_messages   = []
        for msg in messages:
            if msg.get('role') == 'system':
                current_system = msg.get('content', '')
            else:
                all_messages.append(msg)

        if not all_messages:
            return jsonify({'status': 'error', 'message': 'No user messages found'}), 400

        last_user_text = all_messages[-1].get('content', '')

        # ★★★ 核心修复：把系统提示词直接嵌入用户消息
        # 不再用 _run_and_drain + keep_history 的两步方式
        # 1.5B模型用那种方式完全看不到用户的问题
        if current_system:
            combined_prompt = f"{current_system}\n{last_user_text}"
        else:
            combined_prompt = last_user_text

        print(f"\n[Chat] prompt: {combined_prompt}")

        rkllm_responses = {
            "id": "rkllm_chat", "object": "rkllm_chat",
            "created": int(time.time()), "choices": [],
            "usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None},
        }

        # ★ 每次请求 keep_history=0，全新上下文
        rkllm_model.rkllm_infer_params.keep_history = 0

        if not stream:
            if TOOLS is not None:
                rkllm_model.set_function_tools(system_prompt=current_system, tools=json.dumps(TOOLS), tool_response_str="tool_response")

            global_text = []
            global_state = -1
            model_thread = threading.Thread(target=rkllm_model.run, args=("user", enable_thinking, combined_prompt))
            model_thread.start()

            full_output = ""
            while True:
                while global_text:
                    full_output += global_text.pop(0)
                    time.sleep(0.005)
                model_thread.join(timeout=0.005)
                if not model_thread.is_alive(): break

            if global_state == LLMCallState.RKLLM_RUN_ERROR:
                return jsonify({'status': 'error', 'message': 'Model inference error'}), 500

            full_output = _sanitize_full_response(full_output)
            rkllm_responses["choices"].append({
                "index": 0, "message": {"role": "assistant", "content": full_output},
                "logprobs": None, "finish_reason": "stop",
            })
            return jsonify(rkllm_responses), 200

        else:
            def generate():
                global global_text, global_state, abort_requested

                if TOOLS is not None:
                    rkllm_model.set_function_tools(system_prompt=current_system, tools=json.dumps(TOOLS), tool_response_str="tool_response")

                global_text = []
                global_state = -1
                abort_requested.clear()
                think_filter = ThinkingFilter()

                model_thread = threading.Thread(target=rkllm_model.run, args=("user", enable_thinking, combined_prompt))
                model_thread.start()

                while True:
                    if abort_requested.is_set():
                        rkllm_model.abort()
                        yield f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': 'abort'}]})}\n\n"
                        # ★ 等待模型线程结束，避免残留
                        model_thread.join(timeout=2.0)
                        return

                    while global_text:
                        raw_chunk = global_text.pop(0)
                        chunk = think_filter.feed(raw_chunk)
                        if chunk:
                            rkllm_responses["choices"] = [{
                                "index": 0,
                                "delta": {"role": "assistant", "content": chunk},
                                "logprobs": None, "finish_reason": None,
                            }]
                            yield f"data: {json.dumps(rkllm_responses)}\n\n"

                    model_thread.join(timeout=0.005)
                    if not model_thread.is_alive(): break

                tail = think_filter.flush()
                if tail:
                    rkllm_responses["choices"] = [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": tail},
                        "logprobs": None, "finish_reason": None,
                    }]
                    yield f"data: {json.dumps(rkllm_responses)}\n\n"

                rkllm_responses["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                yield f"data: {json.dumps(rkllm_responses)}\n\n"

            return Response(generate(), content_type='text/event-stream')

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500
    finally:
        is_blocking = False
        lock.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--rkllm_model_path', type=str, required=True)
    parser.add_argument('--target_platform',  type=str, required=True)
    parser.add_argument('--lora_model_path',  type=str)
    parser.add_argument('--prompt_cache_path', type=str)
    args = parser.parse_args()
    for path, label in [(args.rkllm_model_path,"rkllm model"),(args.lora_model_path,"lora model"),(args.prompt_cache_path,"prompt cache")]:
        if path and not os.path.exists(path):
            print(f"Error: {label} not found: {path}"); sys.exit(1)
    subprocess.run(f"sudo bash fix_freq_{args.target_platform}.sh", shell=True)
    resource.setrlimit(resource.RLIMIT_NOFILE, (102400, 102400))
    print("=========init....==========="); sys.stdout.flush()
    rkllm_model = RKLLM(args.rkllm_model_path, args.lora_model_path, args.prompt_cache_path, args.target_platform)
    print("============================="); sys.stdout.flush()
    app.run(host='0.0.0.0', port=8080, threaded=True, debug=False)
    print("RKLLM model inference completed, releasing resources...")
    rkllm_model.release()