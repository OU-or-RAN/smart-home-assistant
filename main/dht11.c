#include "dht11.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_timer.h"
#include <string.h>

static const char *TAG = "dht11";

// ==================== 配置 ====================
#define DHT11_GPIO              CONFIG_DHT11_GPIO  // 通过 menuconfig 配置，默认 GPIO4
#define DHT11_MAX_RETRY         3                   // 最大重试次数
#define DHT11_RETRY_DELAY_MS    2000                // 重试间隔

// DHT11 时序参数（微秒）- 使用更宽松的值
#define DHT11_START_LOW_US      20000   // 起始信号拉低 18-30ms，取20ms
#define DHT11_START_HIGH_US     30      // 释放后等待 20-40us
#define DHT11_BIT_THRESHOLD_US  40      // 0/1 判断阈值：>40us 为1，<40us 为0
#define DHT11_TIMEOUT_US        200     // 单次等待超时 200us（比100更宽松）

// ==================== 精确延时函数 ====================

/**
 * @brief 使用 esp_timer 的精确微秒延时（不受 FreeRTOS 调度影响）
 */
static inline void dht11_precise_delay_us(uint32_t us)
{
    uint64_t start = esp_timer_get_time();
    while ((esp_timer_get_time() - start) < us) {
        // 自旋等待，不调用任何可能让出CPU的函数
        asm volatile ("nop");
    }
}

static inline void dht11_precise_delay_ms(uint32_t ms)
{
    for (uint32_t i = 0; i < ms; i++) {
        dht11_precise_delay_us(1000);
    }
}

// ==================== GPIO 操作 ====================

static inline void dht11_set_output_low(void)
{
    gpio_set_level(DHT11_GPIO, 0);
}

static inline void dht11_set_output_high(void)
{
    gpio_set_level(DHT11_GPIO, 1);
}

static inline void dht11_set_input(void)
{
    gpio_set_direction(DHT11_GPIO, GPIO_MODE_INPUT);
}

static inline void dht11_set_output(void)
{
    gpio_set_direction(DHT11_GPIO, GPIO_MODE_OUTPUT_OD);
}

static inline int dht11_read_pin(void)
{
    return gpio_get_level(DHT11_GPIO);
}

// ==================== 核心读取函数 ====================

/**
 * @brief 等待引脚电平变化，带超时
 * @param level 期望的电平 (0 或 1)
 * @param timeout_us 超时时间（微秒）
 * @return true: 成功等到电平, false: 超时
 */
static bool dht11_wait_for_level(int level, uint32_t timeout_us)
{
    uint64_t start = esp_timer_get_time();
    while (dht11_read_pin() != level) {
        if ((esp_timer_get_time() - start) > timeout_us) {
            return false;
        }
    }
    return true;
}

/**
 * @brief 读取单个位（关键：使用 esp_timer 计时，不受中断影响）
 */
static int dht11_read_bit(void)
{
    // 等待低电平结束（开始信号，约50us）
    if (!dht11_wait_for_level(1, DHT11_TIMEOUT_US)) {
        return -1; // 超时错误
    }
    
    // 记录高电平开始时间
    uint64_t high_start = esp_timer_get_time();
    
    // 等待高电平结束
    if (!dht11_wait_for_level(0, DHT11_TIMEOUT_US)) {
        return -1;
    }
    
    // 计算高电平持续时间
    uint32_t high_duration = (uint32_t)(esp_timer_get_time() - high_start);
    
    // 判断是 0 还是 1
    return (high_duration > DHT11_BIT_THRESHOLD_US) ? 1 : 0;
}

/**
 * @brief 读取40位数据（在调度器挂起状态下执行）
 */
static esp_err_t dht11_read_bits_raw(uint8_t *data)
{
    // 步骤1: 发送起始信号（拉低20ms）
    dht11_set_output();
    dht11_set_output_low();
    dht11_precise_delay_ms(20);
    
    // 步骤2: 释放总线，切换到输入
    dht11_set_output_high();
    dht11_set_input();
    dht11_precise_delay_us(30);  // 等待 20-40us
    
    // 步骤3: 等待 DHT11 响应（拉低80us + 拉高80us）
    // 等待 DHT11 拉低（响应信号）
    if (!dht11_wait_for_level(0, 100)) {  // 100us 超时
        ESP_LOGD(TAG, "Wait for DHT11 low response timeout");
        return ESP_ERR_TIMEOUT;
    }
    
    // 等待 DHT11 拉高
    if (!dht11_wait_for_level(1, 100)) {
        ESP_LOGD(TAG, "Wait for DHT11 high response timeout");
        return ESP_ERR_TIMEOUT;
    }
    
    // 等待准备信号结束（拉低）
    if (!dht11_wait_for_level(0, 100)) {
        ESP_LOGD(TAG, "Wait for DHT11 ready timeout");
        return ESP_ERR_TIMEOUT;
    }
    
    // 步骤4: 读取40位数据
    for (int byte = 0; byte < 5; byte++) {
        data[byte] = 0;
        for (int bit = 7; bit >= 0; bit--) {
            int bit_val = dht11_read_bit();
            if (bit_val < 0) {
                ESP_LOGD(TAG, "Bit read timeout at byte %d, bit %d", byte, bit);
                return ESP_ERR_TIMEOUT;
            }
            if (bit_val) {
                data[byte] |= (1 << bit);
            }
        }
    }
    
    return ESP_OK;
}

// ==================== 公共接口 ====================

esp_err_t dht11_init(void)
{
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << DHT11_GPIO),
        .mode = GPIO_MODE_OUTPUT_OD,        // 开漏输出
        .pull_up_en = GPIO_PULLUP_ENABLE,   // 使能上拉（关键！）
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,     // 禁用中断
    };
    
    esp_err_t ret = gpio_config(&io_conf);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "GPIO config failed: %s", esp_err_to_name(ret));
        return ret;
    }
    
    // 初始状态：拉高（空闲状态）
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
    uint8_t bits[5] = {0};
    esp_err_t ret = ESP_FAIL;
    
    // 尝试多次读取
    for (int retry = 0; retry < DHT11_MAX_RETRY; retry++) {
        if (retry > 0) {
            ESP_LOGW(TAG, "Retry %d/%d...", retry, DHT11_MAX_RETRY);
            vTaskDelay(pdMS_TO_TICKS(DHT11_RETRY_DELAY_MS));
        }
        
        // ========== 关键：挂起调度器，防止 Wi-Fi 中断干扰 ==========
        // 这比 portENTER_CRITICAL 更强，禁止所有任务切换和中断（除了NMI）
        vTaskSuspendAll();
        
        // 可选：进一步提高稳定性，可以临时禁用特定中断
        // 但对于 DHT11，vTaskSuspendAll 通常足够
        
        ret = dht11_read_bits_raw(bits);
        
        // 恢复调度器
        xTaskResumeAll();
        
        if (ret == ESP_OK) {
            break; // 成功，跳出重试循环
        }
    }
    
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Read failed after %d retries: %s", DHT11_MAX_RETRY, esp_err_to_name(ret));
        return ret;
    }
    
    // 校验数据
    uint8_t checksum = bits[0] + bits[1] + bits[2] + bits[3];
    if (checksum != bits[4]) {
        ESP_LOGE(TAG, "Checksum failed: calc=%d, recv=%d", checksum, bits[4]);
        ESP_LOGD(TAG, "Raw data: [%02X, %02X, %02X, %02X, %02X]", 
                 bits[0], bits[1], bits[2], bits[3], bits[4]);
        return ESP_ERR_INVALID_CRC;
    }
    
    // 解析数据
    data->humidity_int = bits[0];
    data->humidity_dec = bits[1];
    data->temperature_int = bits[2];
    data->temperature_dec = bits[3];
    
    // 处理负数温度（DHT11 通常不支持负数，但保留兼容）
    if (data->temperature_int & 0x80) {
        data->temperature_int = -(data->temperature_int & 0x7F);
    }
    
    data->valid = true;
    
    ESP_LOGI(TAG, "Read success: Temp=%.1f°C, Hum=%d%%", 
             dht11_get_temperature(data), (int)dht11_get_humidity(data));
    
    return ESP_OK;
}

float dht11_get_temperature(dht11_data_t *data)
{
    if (data == NULL || !data->valid) {
        return -999.0f;
    }
    return (float)data->temperature_int + (float)data->temperature_dec / 10.0f;
}

float dht11_get_humidity(dht11_data_t *data)
{
    if (data == NULL || !data->valid) {
        return -999.0f;
    }
    return (float)data->humidity_int;
}