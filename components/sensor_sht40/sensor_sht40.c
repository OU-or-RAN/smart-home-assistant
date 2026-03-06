#include "sensor_sht40.h"
#include "pin_config.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/i2c.h"
#include <string.h>

static const char *TAG = "sht40";

// ==================== 内部工具函数 ====================

/**
 * @brief CRC-8 校验（SHT40 使用多项式 0x31，初始值 0xFF）
 */
static uint8_t sht40_crc8(const uint8_t *data, size_t len)
{
    uint8_t crc = 0xFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; bit++) {
            if (crc & 0x80) {
                crc = (crc << 1) ^ 0x31;
            } else {
                crc <<= 1;
            }
        }
    }
    return crc;
}

/**
 * @brief 向 SHT40 发送命令
 */
static esp_err_t sht40_send_cmd(uint8_t cmd)
{
    i2c_cmd_handle_t handle = i2c_cmd_link_create();

    i2c_master_start(handle);
    i2c_master_write_byte(handle, (SHT40_I2C_ADDR << 1) | I2C_MASTER_WRITE, true);
    i2c_master_write_byte(handle, cmd, true);
    i2c_master_stop(handle);

    esp_err_t ret = i2c_master_cmd_begin(I2C_PORT, handle, pdMS_TO_TICKS(100));
    i2c_cmd_link_delete(handle);

    return ret;
}

/**
 * @brief 从 SHT40 读取 N 字节
 */
static esp_err_t sht40_read_bytes(uint8_t *buf, size_t len)
{
    i2c_cmd_handle_t handle = i2c_cmd_link_create();

    i2c_master_start(handle);
    i2c_master_write_byte(handle, (SHT40_I2C_ADDR << 1) | I2C_MASTER_READ, true);
    if (len > 1) {
        i2c_master_read(handle, buf, len - 1, I2C_MASTER_ACK);
    }
    i2c_master_read_byte(handle, &buf[len - 1], I2C_MASTER_NACK);
    i2c_master_stop(handle);

    esp_err_t ret = i2c_master_cmd_begin(I2C_PORT, handle, pdMS_TO_TICKS(100));
    i2c_cmd_link_delete(handle);

    return ret;
}

// ==================== 公共接口实现 ====================

esp_err_t sht40_init(void)
{
    ESP_LOGI(TAG, "Initializing SHT40 on SDA=GPIO%d, SCL=GPIO%d",
             PIN_I2C_SDA, PIN_I2C_SCL);

    // 配置 I2C 主机
    i2c_config_t conf = {
        .mode             = I2C_MODE_MASTER,
        .sda_io_num       = PIN_I2C_SDA,
        .scl_io_num       = PIN_I2C_SCL,
        .sda_pullup_en    = GPIO_PULLUP_ENABLE,
        .scl_pullup_en    = GPIO_PULLUP_ENABLE,
        .master.clk_speed = SHT40_I2C_FREQ_HZ,
    };

    esp_err_t ret = i2c_param_config(I2C_PORT, &conf);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "I2C param config failed: %s", esp_err_to_name(ret));
        return ret;
    }

    ret = i2c_driver_install(I2C_PORT, I2C_MODE_MASTER, 0, 0, 0);
    if (ret != ESP_OK) {
        // 若 I2C 驱动已安装（其他组件先初始化），忽略该错误
        if (ret == ESP_ERR_INVALID_STATE) {
            ESP_LOGW(TAG, "I2C driver already installed, skipping");
        } else {
            ESP_LOGE(TAG, "I2C driver install failed: %s", esp_err_to_name(ret));
            return ret;
        }
    }

    // 软复位确认传感器在线
    ret = sht40_soft_reset();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "SHT40 not responding, check wiring");
        return ret;
    }

    vTaskDelay(pdMS_TO_TICKS(2));   // 复位后等待 1ms 以上
    ESP_LOGI(TAG, "SHT40 initialized successfully");
    return ESP_OK;
}

esp_err_t sht40_read(sht40_data_t *data, sht40_precision_t precision)
{
    if (data == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    memset(data, 0, sizeof(sht40_data_t));

    // 选择命令字
    uint8_t cmd;
    uint32_t delay_ms;
    switch (precision) {
        case SHT40_PRECISION_MED:
            cmd      = SHT40_CMD_MEASURE_MED;
            delay_ms = 5;
            break;
        case SHT40_PRECISION_LOW:
            cmd      = SHT40_CMD_MEASURE_LOW;
            delay_ms = 2;
            break;
        case SHT40_PRECISION_HIGH:
        default:
            cmd      = SHT40_CMD_MEASURE_HIGH;
            delay_ms = SHT40_MEASURE_DELAY_MS;
            break;
    }

    // 发送测量命令
    esp_err_t ret = sht40_send_cmd(cmd);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Send measure cmd failed: %s", esp_err_to_name(ret));
        return ret;
    }

    // 等待测量完成
    vTaskDelay(pdMS_TO_TICKS(delay_ms));

    // 读取 6 字节：T_MSB T_LSB T_CRC RH_MSB RH_LSB RH_CRC
    uint8_t buf[6] = {0};
    ret = sht40_read_bytes(buf, 6);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Read data failed: %s", esp_err_to_name(ret));
        return ret;
    }

    // 校验温度 CRC
    if (sht40_crc8(&buf[0], 2) != buf[2]) {
        ESP_LOGE(TAG, "Temperature CRC failed: calc=0x%02X, recv=0x%02X",
                 sht40_crc8(&buf[0], 2), buf[2]);
        return ESP_ERR_INVALID_CRC;
    }

    // 校验湿度 CRC
    if (sht40_crc8(&buf[3], 2) != buf[5]) {
        ESP_LOGE(TAG, "Humidity CRC failed: calc=0x%02X, recv=0x%02X",
                 sht40_crc8(&buf[3], 2), buf[5]);
        return ESP_ERR_INVALID_CRC;
    }

    // 解析温度：T(°C) = -45 + 175 * raw / 65535
    uint16_t t_raw  = ((uint16_t)buf[0] << 8) | buf[1];
    uint16_t rh_raw = ((uint16_t)buf[3] << 8) | buf[4];

    data->temperature = -45.0f + 175.0f * (float)t_raw  / 65535.0f;
    data->humidity    = -6.0f  + 125.0f * (float)rh_raw / 65535.0f;

    // 湿度钳位至 0~100%
    if (data->humidity < 0.0f)   data->humidity = 0.0f;
    if (data->humidity > 100.0f) data->humidity = 100.0f;

    data->valid = true;

    ESP_LOGI(TAG, "SHT40: Temp=%.2f°C, Hum=%.2f%%RH",
             data->temperature, data->humidity);

    return ESP_OK;
}

esp_err_t sht40_soft_reset(void)
{
    esp_err_t ret = sht40_send_cmd(SHT40_CMD_SOFT_RESET);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Soft reset failed: %s", esp_err_to_name(ret));
        return ret;
    }
    vTaskDelay(pdMS_TO_TICKS(1));
    ESP_LOGI(TAG, "SHT40 soft reset done");
    return ESP_OK;
}

esp_err_t sht40_read_serial(uint32_t *serial)
{
    if (serial == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    esp_err_t ret = sht40_send_cmd(SHT40_CMD_READ_SERIAL);
    if (ret != ESP_OK) {
        return ret;
    }

    vTaskDelay(pdMS_TO_TICKS(1));

    // 读取 6 字节：SN1_MSB SN1_LSB CRC SN2_MSB SN2_LSB CRC
    uint8_t buf[6] = {0};
    ret = sht40_read_bytes(buf, 6);
    if (ret != ESP_OK) {
        return ret;
    }

    if (sht40_crc8(&buf[0], 2) != buf[2] ||
        sht40_crc8(&buf[3], 2) != buf[5]) {
        ESP_LOGE(TAG, "Serial number CRC failed");
        return ESP_ERR_INVALID_CRC;
    }

    *serial = ((uint32_t)buf[0] << 24) | ((uint32_t)buf[1] << 16) |
              ((uint32_t)buf[3] <<  8) |  (uint32_t)buf[4];

    ESP_LOGI(TAG, "SHT40 Serial: 0x%08lX", (unsigned long)*serial);
    return ESP_OK;
}

// ==================== Getter ====================

float sht40_get_temperature(const sht40_data_t *data)
{
    if (data == NULL || !data->valid) return -999.0f;
    return data->temperature;
}

float sht40_get_humidity(const sht40_data_t *data)
{
    if (data == NULL || !data->valid) return -999.0f;
    return data->humidity;
}