#include "dht11.h"
#include "driver/gpio.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "dht11";

// 微秒级延时（使用 ESP 定时器）
static void delay_us(uint32_t us)
{
    uint32_t start = esp_timer_get_time();
    while (esp_timer_get_time() - start < us) {
        ;
    }
}

// 设置 GPIO 方向
static void set_gpio_output(void)
{
    gpio_set_direction(DHT11_GPIO, GPIO_MODE_OUTPUT);
}

static void set_gpio_input(void)
{
    gpio_set_direction(DHT11_GPIO, GPIO_MODE_INPUT);
}

// 等待电平变化，返回等待时间（微秒），超时返回 -1
static int32_t wait_for_level(int level, uint32_t timeout_us)
{
    uint32_t start = esp_timer_get_time();
    while (gpio_get_level(DHT11_GPIO) != level) {
        if (esp_timer_get_time() - start > timeout_us) {
            return -1;
        }
    }
    return esp_timer_get_time() - start;
}

esp_err_t dht11_init(void)
{
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << DHT11_GPIO),
        .mode = GPIO_MODE_OUTPUT_OD,    // 开漏输出
        .pull_up_en = GPIO_PULLUP_ENABLE,  // 启用内部上拉
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io_conf);
    
    // 初始状态高电平
    gpio_set_level(DHT11_GPIO, 1);
    
    ESP_LOGI(TAG, "DHT11 initialized on GPIO%d", DHT11_GPIO);
    return ESP_OK;
}

esp_err_t dht11_read(dht11_data_t *data)
{
    if (data == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    
    memset(data, 0, sizeof(dht11_data_t));
    
    uint8_t bits[5] = {0};  // 40位数据：湿度(2字节) + 温度(2字节) + 校验(1字节)
    
    // 发送起始信号：拉低18ms
    set_gpio_output();
    gpio_set_level(DHT11_GPIO, 0);
    vTaskDelay(20 / portTICK_PERIOD_MS);  // 18-30ms
    
    // 释放总线，等待DHT11响应
    gpio_set_level(DHT11_GPIO, 1);
    set_gpio_input();
    delay_us(30);
    
    // 等待DHT11拉低（80us）
    if (wait_for_level(0, 100) < 0) {
        ESP_LOGE(TAG, "DHT11 not responding (no low)");
        return ESP_ERR_TIMEOUT;
    }
    
    // 等待DHT11拉高（80us）
    if (wait_for_level(1, 100) < 0) {
        ESP_LOGE(TAG, "DHT11 not responding (no high)");
        return ESP_ERR_TIMEOUT;
    }
    
    // 读取40位数据
    for (int i = 0; i < 5; i++) {
        for (int j = 7; j >= 0; j--) {
            // 等待低电平结束（50us开始信号）
            if (wait_for_level(1, 100) < 0) {
                ESP_LOGE(TAG, "Bit timeout (low)");
                return ESP_ERR_TIMEOUT;
            }
            
            // 测量高电平时间：26-28us=0，70us=1
            int32_t high_time = wait_for_level(0, 100);
            if (high_time < 0) {
                ESP_LOGE(TAG, "Bit timeout (high)");
                return ESP_ERR_TIMEOUT;
            }
            
            // 高电平 > 40us 认为是 1
            if (high_time > 40) {
                bits[i] |= (1 << j);
            }
        }
    }
    
    // 校验
    uint8_t checksum = bits[0] + bits[1] + bits[2] + bits[3];
    if (checksum != bits[4]) {
        ESP_LOGE(TAG, "Checksum failed: calc=%d, recv=%d", checksum, bits[4]);
        return ESP_ERR_INVALID_CRC;
    }
    
    // 解析数据
    data->humidity_int = bits[0];
    data->humidity_dec = bits[1];
    data->temperature_int = bits[2];
    data->temperature_dec = bits[3];
    data->valid = true;
    
    // DHT11 小数部分通常为0，但某些版本可能有值
    ESP_LOGI(TAG, "Read OK: Temp=%d.%d°C, Hum=%d.%d%%", 
             data->temperature_int, data->temperature_dec,
             data->humidity_int, data->humidity_dec);
    
    return ESP_OK;
}

float dht11_get_temperature(dht11_data_t *data)
{
    if (data == NULL || !data->valid) {
        return -999.0f;
    }
    // 处理负数温度（DHT11最高位为1表示负数，但实际很少见）
    int16_t temp = data->temperature_int;
    if (temp & 0x80) {
        temp = -(temp & 0x7F);
    }
    return (float)temp + (float)data->temperature_dec / 10.0f;
}

float dht11_get_humidity(dht11_data_t *data)
{
    if (data == NULL || !data->valid) {
        return -999.0f;
    }
    return (float)data->humidity_int + (float)data->humidity_dec / 10.0f;
}