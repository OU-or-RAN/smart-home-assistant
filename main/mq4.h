#ifndef MQ4_H
#define MQ4_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

// ==================== 配置 ====================

//enter pin_config.h for GPIO definitions
#include "pin_config.h"

// ADC 配置 - ESP-IDF 5.0+ 新 API
#define MQ4_ADC_WIDTH       ADC_BITWIDTH_12     // 12位分辨率 (0-4095)
#define MQ4_ADC_ATTEN       ADC_ATTEN_DB_12     // 12dB衰减，量程 0-3.3V
#define MQ4_ADC_UNIT        ADC_UNIT_1          // 使用 ADC1

// 校准与计算
#define MQ4_RL_VALUE        10.0f       // 负载电阻值，单位 KΩ (常见 10K)
#define MQ4_RO_CLEAN_AIR    4.4f        // 清洁空气中 RS/RO 比值（ datasheet 典型值）
#define MQ4_CALIBRATION_SAMPLES  50      // 校准时采样次数
#define MQ4_READ_SAMPLES    10          // 正常读取采样次数（用于平均）

// 甲烷浓度曲线参数（来自 MQ-4 datasheet）
#define MQ4_CURVE_M         -0.36f
#define MQ4_CURVE_B         2.3f

// 阈值配置
#define MQ4_DEFAULT_THRESHOLD_PPM   1000.0f     // 默认报警阈值 1000ppm

typedef struct {
    float   ppm;                // 计算出的甲烷浓度 (ppm)
    int     raw_value;          // ADC 原始值 (0-4095)
    float   voltage;            // 电压值 (V)
    float   rs;                 // 传感器电阻值 (KΩ)
    float   ro;                 // 校准电阻值 (KΩ)，在清洁空气中校准得到
    bool    digital_alert;      // 数字输出状态 (true = 超过阈值)
    bool    calibrated;         // 是否已完成校准
} mq4_data_t;

// ==================== 函数接口 ====================

esp_err_t mq4_init(void);
esp_err_t mq4_calibrate(mq4_data_t *data);
esp_err_t mq4_read(mq4_data_t *data);
bool mq4_get_digital_status(void);
float mq4_get_ppm(mq4_data_t *data);
int mq4_get_raw(mq4_data_t *data);
bool mq4_is_above_threshold(mq4_data_t *data, float threshold_ppm);

#endif // MQ4_H