#ifndef YL38_H
#define YL38_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

// 配置
// GPIO 定义 - 通过 pin_config.h 配置
#include "pin_config.h"

#define YL38_ADC_WIDTH      ADC_BITWIDTH_12
#define YL38_ADC_ATTEN      ADC_ATTEN_DB_12
#define YL38_ADC_UNIT       ADC_UNIT_1
#define YL38_READ_SAMPLES   10

// 动态阈值配置
#define YL38_READ_SAMPLES       10      // 读取时的平均采样数
#define YL38_CALIBRATION_SAMPLES    20      // 校准采样次数
#define YL38_FLAME_HYSTERESIS       800     // 迟滞值，避免抖动
#define YL38_MIN_VALID_RAW          200     // 最小有效值（避免短路）

// 确认阈值
#define YL38_CONFIRM_FRAMES     3    // 连续3次检测到才报警（对应 3×500ms=1.5s）
#define YL38_CLEAR_FRAMES       5    // 连续5次未检测到才解除（对应 5×500ms=2.5s）

typedef enum {
    YL38_NO_FLAME = 0,
    YL38_FLAME_WEAK,
    YL38_FLAME_MEDIUM,
    YL38_FLAME_STRONG
} yl38_flame_level_t;

typedef struct {
    int                 raw_value;
    float               voltage;
    bool                digital_detected;
    yl38_flame_level_t  flame_level;
    bool                flame_detected;
    float               intensity_percent;
    int                 baseline_raw;       // 新增：环境基线值
} yl38_data_t;

// 函数接口
esp_err_t yl38_init(void);
esp_err_t yl38_calibrate_baseline(void);    // 新增：校准基线
esp_err_t yl38_read(yl38_data_t *data);
bool yl38_get_digital_status(void);
void yl38_set_threshold(int raw_threshold);
const char* yl38_get_level_string(yl38_flame_level_t level);

#endif