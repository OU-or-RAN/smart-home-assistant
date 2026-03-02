#ifndef DHT11_H
#define DHT11_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

// DHT11 配置
#define DHT11_GPIO          GPIO_NUM_4      // 数据引脚，可修改
#define DHT11_TIMEOUT_US    1000            // 超时时间（微秒）

// 数据结构
typedef struct {
    uint8_t humidity_int;      // 湿度整数部分
    uint8_t humidity_dec;      // 湿度小数部分（DHT11固定为0）
    uint8_t temperature_int;   // 温度整数部分
    uint8_t temperature_dec;   // 温度小数部分（DHT11固定为0）
    bool valid;                // 数据是否有效
} dht11_data_t;

// 函数声明
esp_err_t dht11_init(void);
esp_err_t dht11_read(dht11_data_t *data);
float dht11_get_temperature(dht11_data_t *data);
float dht11_get_humidity(dht11_data_t *data);

#endif // DHT11_H