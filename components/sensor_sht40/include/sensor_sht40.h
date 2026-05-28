#ifndef SENSOR_SHT40_H
#define SENSOR_SHT40_H

#include <stdbool.h>
#include "esp_err.h"

// ==================== SHT40 I2C 配置 ====================
#define SHT40_I2C_ADDR          0x44        // ADDR引脚接GND时为0x44，接VCC时为0x45
#define SHT40_I2C_FREQ_HZ       400000      // 400kHz 快速模式

// ==================== SHT40 命令字 ====================
#define SHT40_CMD_MEASURE_HIGH  0xFD        // 高精度测量（无加热）
#define SHT40_CMD_MEASURE_MED   0xF6        // 中精度测量
#define SHT40_CMD_MEASURE_LOW   0xE0        // 低精度测量
#define SHT40_CMD_SOFT_RESET    0x94        // 软复位
#define SHT40_CMD_READ_SERIAL   0x89        // 读序列号

// 高精度测量等待时间（ms）
#define SHT40_MEASURE_DELAY_MS  10

// ==================== 数据结构 ====================

typedef struct {
    float    temperature;       // 摄氏度
    float    humidity;          // 相对湿度 %RH
    bool     valid;             // 数据是否有效
    uint32_t serial;            // 传感器序列号（可选读取）
} sht40_data_t;

// ==================== 精度模式 ====================

typedef enum {
    SHT40_PRECISION_HIGH = 0,   // 高精度，测量时间约 8.2ms
    SHT40_PRECISION_MED,        // 中精度，测量时间约 4.5ms
    SHT40_PRECISION_LOW,        // 低精度，测量时间约 1.7ms
} sht40_precision_t;

// ==================== 公共接口 ====================

/**
 * @brief 初始化 SHT40（初始化 I2C 总线并复位传感器）
 * @return ESP_OK 成功，其他值失败
 */
esp_err_t sht40_init(void);

/**
 * @brief 读取温湿度数据
 * @param data    输出数据结构体指针
 * @param precision 测量精度
 * @return ESP_OK 成功，ESP_ERR_INVALID_CRC 校验失败，其他值通信失败
 */
esp_err_t sht40_read(sht40_data_t *data, sht40_precision_t precision);

/**
 * @brief 软复位 SHT40
 * @return ESP_OK 成功
 */
esp_err_t sht40_soft_reset(void);

/**
 * @brief 读取传感器序列号
 * @param serial 输出序列号
 * @return ESP_OK 成功
 */
esp_err_t sht40_read_serial(uint32_t *serial);

// ==================== Getter（供 json_builder 调用）====================

float sht40_get_temperature(const sht40_data_t *data);
float sht40_get_humidity(const sht40_data_t *data);

#endif // SENSOR_SHT40_H