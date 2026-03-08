#ifndef SENSOR_MQ2_H
#define SENSOR_MQ2_H

#include <stdbool.h>
#include "esp_err.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"

// ==================== 引脚配置 ====================
#define MQ2_AO_GPIO         GPIO_NUM_1      // 与 pin_config.h 中 PIN_MQ2_AO 一致
#define MQ2_DO_GPIO         GPIO_NUM_14     // 与 pin_config.h 中 PIN_MQ2_DO 一致

// ==================== ADC 配置 ====================
#define MQ2_ADC_UNIT        ADC_UNIT_1
#define MQ2_ADC_ATTEN       ADC_ATTEN_DB_12
#define MQ2_ADC_WIDTH       ADC_BITWIDTH_12

// ==================== 传感器参数 ====================
// MQ2 对 LPG 的曲线参数（对数坐标）
#define MQ2_RL_VALUE            5.0f        // 负载电阻 5KΩ
#define MQ2_RO_CLEAN_AIR        9.83f       // 清洁空气中 RS/RO 参考比值
#define MQ2_CURVE_M             (-0.47f)    // 曲线斜率
#define MQ2_CURVE_B             (1.84f)     // 曲线截距

// ==================== 阈值与采样 ====================
#define MQ2_DEFAULT_THRESHOLD_PPM   300.0f  // 默认报警阈值（ppm）
#define MQ2_CALIBRATION_SAMPLES     50
#define MQ2_READ_SAMPLES            5

// ==================== 数据结构 ====================
typedef struct {
    int     raw_value;          // ADC 原始值
    float   voltage;            // 电压（V）
    float   rs;                 // 传感器电阻 RS（KΩ）
    float   ro;                 // 校准电阻 RO（KΩ）
    float   ppm;                // 换算浓度（ppm）
    bool    calibrated;         // 是否已校准
    bool    digital_alert;      // DO 引脚状态（低电平有效）
} mq2_data_t;

// ==================== 公共接口 ====================

esp_err_t mq2_init(void);
esp_err_t mq2_calibrate(mq2_data_t *data);
esp_err_t mq2_read(mq2_data_t *data);
bool      mq2_get_digital_status(void);
float     mq2_get_ppm(mq2_data_t *data);
int       mq2_get_raw(mq2_data_t *data);
bool      mq2_is_above_threshold(mq2_data_t *data, float threshold_ppm);

#endif // SENSOR_MQ2_H