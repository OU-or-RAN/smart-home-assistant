<div align="center">

# 🏠 Multi-modal Edge Smart Home Assistant
### Based on RK3588 & ESP32 · 基于 RK3588 与 ESP32 的多模态边缘智能家居助理

*A fully-localized, offline-capable, privacy-preserving intelligent home assistant running entirely on resource-constrained edge hardware.*

*完全本地化、可离线运行、隐私友好的边缘智能家居助理 —— 全部推理与决策都在家庭局域网内闭环完成。*

<br/>

[![Platform](https://img.shields.io/badge/Main_Controller-RK3588_(LubanCat_4)-1f6feb)](https://github.com/OU-or-RAN/smart-home-assistant)
[![Nodes](https://img.shields.io/badge/Nodes-ESP32--S3_×2_·_ESP32--CAM-7048e8)](https://github.com/OU-or-RAN/smart-home-assistant)
[![Privacy](https://img.shields.io/badge/Data-Never_Leaves_Home-2da44e)](https://github.com/OU-or-RAN/smart-home-assistant)
[![NPU](https://img.shields.io/badge/NPU-6_TOPS_INT8-fb8500)](https://github.com/OU-or-RAN/smart-home-assistant)
[![License](https://img.shields.io/badge/License-MIT-lightgrey)](LICENSE)

**English** · [中文](#-中文说明)

</div>

---

## 🎬 Demo Video / 演示视频

> The demo below shows node provisioning, sensor monitoring, flame/gas alerting, YOLO person detection, rule-engine response, and on-device LLM interaction.
> 下方演示涵盖节点入网、传感器监控、火焰/燃气告警、YOLO 人物检测、规则引擎响应以及板端 LLM 交互。

<div align="center">

<video src="https://github.com/user-attachments/assets/e56add76-23f7-45f3-b22c-1b5cbd687954" controls width="80%">
  Your browser does not support the video tag. 你的浏览器不支持内嵌视频播放。
</video>

<br/>

<!-- Fallback link in case inline playback fails / 内嵌播放失败时的备用链接 -->
▶️ **[Click here to watch the demo / 点此观看完整演示](https://github.com/OU-or-RAN/smart-home-assistant)**

</div>

---

## ✨ Highlights

- **Fully local & offline** — All raw data (video frames, audio, sensor readings) is processed inside the home LAN. The system keeps working even with the Internet completely disconnected. Nothing is sent to any cloud.
- **Three-tier heterogeneous architecture** — *Perception → Coprocessing → Decision*, letting each hardware tier do what it does best.
- **Dual-track decision pipeline** — A deterministic **YAML rule engine** for safety enforcement runs in parallel with a flexible **LLM agent** for natural-language interaction. Safety alerts work even if the LLM is offline.
- **Four AI models concurrently on one board** — SenseVoice (STT), sherpa-onnx (TTS), YOLOv8n (object detection) and DeepSeek-R1-Distill-Qwen-1.5B (LLM), exploiting the previously idle 6 TOPS NPU to offload the CPU.
- **Three-level time synchronization** — Hardware NTP alignment + temporal-window alignment + event-driven alignment, plus a **data-quality scoring** mechanism (`q ∈ [0,1]`) that drives graceful degradation.

---

## 🧱 System Architecture

```
┌──────────────────────── Decision Layer / 决策层 ────────────────────────┐
│  LubanCat 4 (RK3588, 8GB, 6 TOPS NPU)                                    │
│  ├─ Rule Engine (YAML, 1 Hz)   ├─ Multimodal Agent (LLM intent)         │
│  ├─ YOLOv8n (RKNN)             ├─ SenseVoice STT + sherpa-onnx TTS       │
│  └─ Time Sync (TimeMaster + MultimodalAligner) · SQLite · data_bus      │
└──────────────────────────────────┬──────────────────────────────────────┘
                MQTT (status/alert/control)  │  HTTP/MJPEG (video)
┌──────────────────── Coprocessing Layer / 协处理层 ──────────────────────┐
│  ESP32-CAM (OV3660) — MJPEG over HTTP streaming, MQTT control plane      │
└──────────────────────────────────┬──────────────────────────────────────┘
┌─────────────────────── Perception Layer / 感知层 ───────────────────────┐
│  ESP32-S3 #1 (S3_001): DHT11 + MQ-2 + YL-38   → kitchen                  │
│  ESP32-S3 #2 (S3_002): SHT40 + MQ-4           → gas-meter area           │
│  FreeRTOS tasks · local emergency response · MQTT status reporting       │
└──────────────────────────────────────────────────────────────────────────┘
```

All nodes connect over Wi-Fi on a `172.20.10.0/24` LAN with static IPs; a single Mosquitto broker on the RK3588 decouples every node.

---

## 🛠️ Hardware & Software

| Category | Item |
| --- | --- |
| **Main controller** | LubanCat 4 (RK3588, 8 GB LPDDR4x, 6 TOPS NPU) |
| **Perception nodes** | 2 × ESP32-S3-DevKitC |
| **Vision node** | AI-Thinker ESP32-CAM (OV3660, 4 MB PSRAM) |
| **Sensors** | DHT11, SHT40 (T/RH) · MQ-2, MQ-4 (gas) · YL-38 (flame) |
| **OS** | Ubuntu 20.04 LTS (board) · Windows 11 + WSL Ubuntu 22.04 (dev) |
| **Firmware** | ESP-IDF v5.3.1 (FreeRTOS) |
| **Runtime** | Python 3.8 (board) · Mosquitto 2.0.15 · RKNN / RKLLM |
| **Models** | YOLOv8n (W8A8 INT8 → RKNN) · DeepSeek-R1-Distill-Qwen-1.5B (W8A8 → RKLLM) · SenseVoice · sherpa-onnx |

---

## 📊 Measured Performance

| Metric | Target | Measured |
| --- | --- | --- |
| Flame alert → LED on | ≤ 2 s | **1.6 s** |
| YOLO single-frame latency | ≤ 100 ms | **45 ms** |
| YOLO inference frame rate | ≥ 5 fps | **0.45 fps** |
| LLM generation speed | ≥ 15 token/s | **7 token/s** |
| MQTT end-to-end latency | ≤ 500 ms | **128 ms** |

> Note: the on-device 1.5B LLM reliably handles conversational queries but is still limited at faithful sensor-grounded answers and strictly-formatted control-command output — an honest limitation discussed in the thesis and a key direction for future work.

---

## 🚀 Quick Start

```bash
# 1) Clone
git clone https://github.com/OU-or-RAN/smart-home-assistant.git
cd smart-home-assistant

# 2) Flash ESP32 firmware (ESP-IDF v5.3.1)
#    Set DEVICE_INDEX / sensor enable macros in mqtt_handler.h for each board
idf.py -p <PORT> flash monitor

# 3) On the RK3588 board: start the MQTT broker
mosquitto -c mosquitto.conf

# 4) Start the LLM service (loads RKLLM model)
python3 flask_server.py --rkllm_model_path <path>.rkllm --target_platform rk3588

# 5) Start the main controller
python3 edge/mqtt/broker_client.py
```

> Configure Wi-Fi SSID/password, static IPs and the broker address before flashing. See the firmware headers and `CONFIG` in `main.py` for details.

---

## 🗺️ Roadmap

- **Near-term** — Add physical actuators (Sonoff/Shelly smart switches) for closed-loop control; make the time-sync window & quality weights adaptive to live network conditions.
- **Mid-term** — Grow the rule library from 8 to 30+ rules; build a web-based visual rule editor; add knowledge-graph-based dialogue context.
- **Long-term** — Cross-home **federated learning** with differential privacy; per-home model self-evolution via LoRA fine-tuning on local logs.

---

## 📄 License & Citation

Released under the **MIT License** (see `LICENSE`).

If it helps your work, please consider linking back to this repository.

<br/>

---
<br/>

<div align="center">

# 🏠 中文说明

</div>

[English](#-multi-modal-edge-smart-home-assistant) · **中文**

智能家居正在走进千家万户，但用户对**响应速度、隐私安全、断网可用性**的期待也越来越高。主流方案大多把计算中心放在云端：网络一堵，体验就降级；数据上云，隐私就有泄露风险；连接一断，设备就成摆设。

本项目围绕这些痛点，搭建了一套以 **RK3588 为主控、ESP32 系列芯片为协处理器**的多模态边缘智能家居助理系统，所有原始传感器、图像、音频数据**足不出户**，全部推理与决策在家庭局域网内本地完成。

## ✨ 核心特性

- **全本地化 · 可离线** —— 视频帧、音频、传感器读数全部在本地消化，断网也能继续工作，不向任何云端发送原始数据。
- **三层异构架构** —— *感知层 → 协处理层 → 决策层*，让不同算力等级的硬件各司其职。
- **双轨并行决策** —— 确定性的 **YAML 规则引擎**负责安全兜底，灵活的 **LLM 代理**负责自然语言交互；二者并行，即便 LLM 离线，安全告警依然有效。
- **四模型并发驻留单板** —— SenseVoice（语音转文本）、sherpa-onnx（文本转语音）、YOLOv8n（目标检测）、DeepSeek-R1-Distill-Qwen-1.5B（语言模型），充分利用闲置的 6 TOPS NPU 分担 CPU 压力。
- **三级时间同步** —— 硬件 NTP 对齐 + 时间窗口对齐 + 事件驱动对齐，并引入**数据质量评分**（`q ∈ [0,1]`）驱动分层降级决策。

## 🧱 系统架构

```
┌──────────────────────────── 决策层 ────────────────────────────┐
│  鲁班猫 4 (RK3588, 8GB, 6 TOPS NPU)                              │
│  ├─ 规则引擎 (YAML, 1 Hz)     ├─ 多模态代理 (LLM 意图理解)       │
│  ├─ YOLOv8n (RKNN)            ├─ SenseVoice 语音转文本 + sherpa  │
│  └─ 时间同步 · SQLite 持久化 · data_bus 内存总线                │
└──────────────────────────────┬──────────────────────────────────┘
              MQTT (状态/告警/控制)  │  HTTP/MJPEG (视频)
┌──────────────────────────── 协处理层 ──────────────────────────┐
│  ESP32-CAM (OV3660) —— MJPEG over HTTP 推流，MQTT 控制面          │
└──────────────────────────────┬──────────────────────────────────┘
┌──────────────────────────── 感知层 ────────────────────────────┐
│  ESP32-S3 #1 (S3_001)：DHT11 + MQ-2 + YL-38   → 厨房             │
│  ESP32-S3 #2 (S3_002)：SHT40 + MQ-4           → 燃气表区         │
│  FreeRTOS 任务调度 · 本地应急响应 · MQTT 状态上报                │
└──────────────────────────────────────────────────────────────────┘
```

所有节点通过 Wi-Fi 接入 `172.20.10.0/24` 局域网并使用静态 IP；RK3588 上的单个 Mosquitto Broker 实现节点间空间解耦。

## 🛠️ 软硬件清单

| 类别 | 型号 / 版本 |
| --- | --- |
| **主控** | 鲁班猫 4（RK3588，8 GB LPDDR4x，6 TOPS NPU） |
| **感知节点** | ESP32-S3-DevKitC × 2 |
| **视觉节点** | AI-Thinker ESP32-CAM（OV3660，4 MB PSRAM） |
| **传感器** | DHT11、SHT40（温湿度）· MQ-2、MQ-4（可燃气体）· YL-38（火焰） |
| **操作系统** | Ubuntu 20.04 LTS（板端）· Windows 11 + WSL Ubuntu 22.04（开发端） |
| **固件** | ESP-IDF v5.3.1（FreeRTOS） |
| **运行时** | Python 3.8（板端）· Mosquitto 2.0.15 · RKNN / RKLLM |
| **模型** | YOLOv8n（W8A8 INT8 → RKNN）· DeepSeek-R1-Distill-Qwen-1.5B（W8A8 → RKLLM）· SenseVoice · sherpa-onnx |

## 📊 实测性能

| 指标 | 设计目标 | 实测均值 |
| --- | --- | --- |
| 火焰告警从触发到 LED 亮 | ≤ 2 s | **1.6 s** |
| YOLO 单帧检测延迟 | ≤ 100 ms | **45 ms** |
| YOLO 推理帧率 | ≥ 5 fps | **0.45 fps** |
| LLM 生成速度 | ≥ 15 token/s | **7 token/s** |
| MQTT 端到端延迟 | ≤ 500 ms | **128 ms** |

> 说明：板端 1.5B 模型可以胜任对话式查询，但在**忠实引用传感器数值**和**输出格式可靠的控制指令**上仍有短板 —— 这是论文中如实记录的局限，也是后续工作的重点方向。

## 🚀 快速开始

```bash
# 1) 克隆仓库
git clone https://github.com/OU-or-RAN/smart-home-assistant.git
cd smart-home-assistant

# 2) 烧录 ESP32 固件 (ESP-IDF v5.3.1)
#    在 mqtt_handler.h 顶部修改 DEVICE_INDEX 与传感器接入宏来切换板子角色
idf.py -p <端口> flash monitor

# 3) 在 RK3588 板端：启动 MQTT Broker
mosquitto -c mosquitto.conf

# 4) 启动 LLM 服务（加载 RKLLM 模型）
python3 flask_server.py --rkllm_model_path <模型路径>.rkllm --target_platform rk3588

# 5) 启动主控程序
python3 edge/main.py
```

> 烧录前请先配置 Wi-Fi 名称/密码、静态 IP 及 Broker 地址。具体可参考固件头文件与 `main.py` 中的 `CONFIG`。

## 🗺️ 后续规划

- **近期** —— 接入 Sonoff / Shelly 智能开关等物理执行器实现闭环控制；让时间同步窗口与质量权重随网络状况自适应调整。
- **中期** —— 规则库从 8 条扩充至 30 条以上；开发基于 Web 的可视化规则编辑器；引入基于知识图谱的对话上下文管理。
- **远期** —— 跨家庭**联邦学习** + 差分隐私；基于本地日志的 LoRA 微调实现模型自我进化。

## 🙏 致谢与许可

本项目以 **MIT 许可证**开源（见 `LICENSE`）。

在实施过程中借助了 ESP-IDF、RKNN、Mosquitto、DeepSeek 等大量开源项目的成果。若本项目对你有帮助，欢迎 Star 与引用本仓库。

<div align="center">

**仓库地址 / Repository**: https://github.com/OU-or-RAN/smart-home-assistant

*"真正的智能不在于堆出多复杂的模型，而在于把普通的组件以正确的方式组合到一块。"*

</div>