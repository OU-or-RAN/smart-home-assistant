#include "sensor_mq4.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <math.h>
#include <string.h>

// ESP-IDF 5.0+ 新的 ADC API
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"

#include "adc_shared.h"  // 共享 ADC 初始化

static const char *TAG = "mq4";

// ==================== 全局变量 ====================
static adc_cali_handle_t g_adc_cali_handle = NULL;      // ADC 校准句柄
static float g_ro_value = 10.0f;                        // 校准电阻值
static bool g_is_calibrated = false;                    // 校准标志
static bool g_adc_channel_configured = false;           // ADC 通道配置标志

// ==================== 内部函数 ====================

/**
 * @brief 获取 ADC 通道对应的 GPIO 号
 */
static adc_channel_t get_adc_channel(int gpio_num)
{
    switch (gpio_num) {
        case 1: return ADC_CHANNEL_0;
        case 2: return ADC_CHANNEL_1;
        case 3: return ADC_CHANNEL_2;
        case 4: return ADC_CHANNEL_3;
        case 5: return ADC_CHANNEL_4;
        case 6: return ADC_CHANNEL_5;
        case 7: return ADC_CHANNEL_6;   
        case 8: return ADC_CHANNEL_7;   
        case 9: return ADC_CHANNEL_8;   
        default: return ADC_CHANNEL_6;
    }
}

/**
 * @brief 初始化 ADC 单元和通道（使用共享 ADC）
 */
static esp_err_t mq4_adc_init(void)
{
    if (g_adc_channel_configured) {
        return ESP_OK;
    }

    // 1. 初始化共享 ADC 单元
    esp_err_t ret = shared_adc_unit_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Shared ADC init failed: %s", esp_err_to_name(ret));
        return ret;
    }

    // 2. 配置 MQ-4 通道
    adc_oneshot_chan_cfg_t chan_config = {
        .bitwidth = MQ4_ADC_WIDTH,
        .atten = MQ4_ADC_ATTEN,
    };

    adc_channel_t channel = get_adc_channel(MQ4_AO_GPIO);
    ret = adc_oneshot_config_channel(g_shared_adc_handle, channel, &chan_config);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "ADC channel config failed: %s", esp_err_to_name(ret));
        return ret;
    }

    ESP_LOGI(TAG, "MQ-4 ADC channel configured on GPIO%d (ADC1_CH%d)", MQ4_AO_GPIO, channel);
    g_adc_channel_configured = true;
    return ESP_OK;
}

/**
 * @brief 初始化 ADC 校准（ESP-IDF 5.0+ Curve Fitting 方案，ESP32-S3 支持）
 */
static esp_err_t mq4_adc_calibration_init(void)
{
    if (g_adc_cali_handle != NULL) {
        return ESP_OK;  // 已初始化
    }

    esp_err_t ret = ESP_FAIL;
    bool calibrated = false;

#if ADC_CALI_SCHEME_CURVE_FITTING_SUPPORTED
    ESP_LOGI(TAG, "Using Curve Fitting calibration scheme");
    
    adc_cali_curve_fitting_config_t cali_config = {
        .unit_id = MQ4_ADC_UNIT,
        .atten = MQ4_ADC_ATTEN,
        .bitwidth = MQ4_ADC_WIDTH,
    };
    
    ret = adc_cali_create_scheme_curve_fitting(&cali_config, &g_adc_cali_handle);
    if (ret == ESP_OK) {
        calibrated = true;
        ESP_LOGI(TAG, "Curve Fitting calibration created successfully");
    } else if (ret == ESP_ERR_NOT_SUPPORTED) {
        ESP_LOGW(TAG, "Curve Fitting not supported (eFuse not burnt), falling back");
    } else {
        ESP_LOGE(TAG, "Curve Fitting creation failed: %s", esp_err_to_name(ret));
    }
#endif

#if ADC_CALI_SCHEME_LINE_FITTING_SUPPORTED
    if (!calibrated) {
        ESP_LOGI(TAG, "Using Line Fitting calibration scheme");
        
        adc_cali_line_fitting_config_t cali_config = {
            .unit_id = MQ4_ADC_UNIT,
            .atten = MQ4_ADC_ATTEN,
            .bitwidth = MQ4_ADC_WIDTH,
            .default_vref = 1100,
        };
        
        ret = adc_cali_create_scheme_line_fitting(&cali_config, &g_adc_cali_handle);
        if (ret == ESP_OK) {
            calibrated = true;
            ESP_LOGI(TAG, "Line Fitting calibration created successfully");
        } else {
            ESP_LOGW(TAG, "Line Fitting calibration failed: %s", esp_err_to_name(ret));
        }
    }
#endif

    if (!calibrated) {
        ESP_LOGW(TAG, "ADC calibration not available, using raw values");
        g_adc_cali_handle = NULL;
        return ESP_ERR_NOT_SUPPORTED;
    }

    return ESP_OK;
}

/**
 * @brief 初始化数字输入 GPIO
 */
static esp_err_t mq4_do_init(void)
{
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << MQ4_DO_GPIO),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };

    esp_err_t ret = gpio_config(&io_conf);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "DO GPIO config failed");
        return ret;
    }

    ESP_LOGI(TAG, "DO initialized on GPIO%d", MQ4_DO_GPIO);
    return ESP_OK;
}

/**
 * @brief 多次采样取平均（使用共享 ADC 句柄）
 */
static int mq4_adc_read_average(int samples)
{
    if (g_shared_adc_handle == NULL) {
        ESP_LOGE(TAG, "Shared ADC not initialized");
        return 0;
    }

    int32_t sum = 0;
    int raw_value;
    adc_channel_t channel = get_adc_channel(MQ4_AO_GPIO);

    for (int i = 0; i < samples; i++) {
        if (adc_oneshot_read(g_shared_adc_handle, channel, &raw_value) == ESP_OK) {
            sum += raw_value;
        } else {
            ESP_LOGW(TAG, "ADC read failed at sample %d", i);
        }
        vTaskDelay(pdMS_TO_TICKS(5));
    }
    
    return sum / samples;
}

/**
 * @brief 将 ADC 原始值转换为电压（mV）
 */
static int mq4_raw_to_voltage(int raw)
{
    if (g_adc_cali_handle == NULL) {
        return (raw * 3300) / 4095;
    }

    int voltage_mv = 0;
    esp_err_t ret = adc_cali_raw_to_voltage(g_adc_cali_handle, raw, &voltage_mv);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "Cali raw to voltage failed: %s", esp_err_to_name(ret));
        return (raw * 3300) / 4095;
    }
    
    return voltage_mv;
}

/**
 * @brief 计算 RS 电阻值
 */
static float mq4_calculate_rs(int raw)
{
    if (raw == 0) raw = 1;

    int voltage_mv = mq4_raw_to_voltage(raw);
    float vout = voltage_mv / 1000.0f;
    
    if (vout >= 3.3f) vout = 3.29f;
    
    float rs = MQ4_RL_VALUE * (3.3f - vout) / vout;
    return rs;
}

/**
 * @brief 根据 RS/RO 计算 PPM
 */
static float mq4_calculate_ppm(float rs_ro_ratio)
{
    if (rs_ro_ratio <= 0) rs_ro_ratio = 0.001f;
    
    float log_ppm = MQ4_CURVE_M * log10f(rs_ro_ratio) + MQ4_CURVE_B;
    float ppm = powf(10.0f, log_ppm);
    
    if (ppm < 0) ppm = 0;
    if (ppm > 10000) ppm = 10000;
    
    return ppm;
}

// ==================== 公共接口实现 ====================

esp_err_t mq4_init(void)
{
    ESP_LOGI(TAG, "Initializing MQ-4 sensor...");
    
    esp_err_t ret = mq4_adc_init();
    if (ret != ESP_OK) {
        return ret;
    }
    
    ret = mq4_adc_calibration_init();
    if (ret != ESP_OK && ret != ESP_ERR_NOT_SUPPORTED) {
        ESP_LOGW(TAG, "ADC calibration init warning: %s", esp_err_to_name(ret));
    }
    
    ret = mq4_do_init();
    if (ret != ESP_OK) {
        return ret;
    }
    
    ESP_LOGI(TAG, "MQ-4 init complete. Please wait 2-3 minutes for warmup, then calibrate.");
    return ESP_OK;
}

esp_err_t mq4_calibrate(mq4_data_t *data)
{
    if (data == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    
    ESP_LOGW(TAG, "========================================");
    ESP_LOGW(TAG, "Starting calibration...");
    ESP_LOGW(TAG, "Make sure sensor is in CLEAN AIR!");
    ESP_LOGW(TAG, "Calibrating in 5 seconds...");
    ESP_LOGW(TAG, "========================================");
    
    vTaskDelay(pdMS_TO_TICKS(5000));
    
    int raw_avg = mq4_adc_read_average(MQ4_CALIBRATION_SAMPLES);
    float rs = mq4_calculate_rs(raw_avg);
    
    g_ro_value = rs / MQ4_RO_CLEAN_AIR;
    g_is_calibrated = true;
    
    data->raw_value = raw_avg;
    data->rs = rs;
    data->ro = g_ro_value;
    data->calibrated = true;
    data->ppm = 0;
    
    ESP_LOGI(TAG, "Calibration complete!");
    ESP_LOGI(TAG, "Raw ADC: %d, RS: %.2f KΩ, RO: %.2f KΩ", raw_avg, rs, g_ro_value);
    
    return ESP_OK;
}

esp_err_t mq4_read(mq4_data_t *data)
{
    if (data == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    
    memset(data, 0, sizeof(mq4_data_t));
    
    int raw = mq4_adc_read_average(MQ4_READ_SAMPLES);
    data->raw_value = raw;
    
    int voltage_mv = mq4_raw_to_voltage(raw);
    data->voltage = voltage_mv / 1000.0f;
    
    float rs = mq4_calculate_rs(raw);
    data->rs = rs;
    data->ro = g_ro_value;
    data->calibrated = g_is_calibrated;
    
    if (g_is_calibrated && g_ro_value > 0) {
        float ratio = rs / g_ro_value;
        data->ppm = mq4_calculate_ppm(ratio);
    } else {
        ESP_LOGW(TAG, "Sensor not calibrated! Using default RO.");
        float ratio = rs / MQ4_RO_CLEAN_AIR;
        data->ppm = mq4_calculate_ppm(ratio);
    }
    
    data->digital_alert = mq4_get_digital_status();
    
    ESP_LOGD(TAG, "Raw: %d, Volt: %.2fV, RS: %.2fK, PPM: %.1f, DO: %s",
             data->raw_value, data->voltage, data->rs, data->ppm,
             data->digital_alert ? "ALERT" : "NORMAL");
    
    return ESP_OK;
}

bool mq4_get_digital_status(void)
{
    int level = gpio_get_level(MQ4_DO_GPIO);
    return (level == 0);
}

float mq4_get_ppm(mq4_data_t *data)
{
    if (data == NULL) return -1.0f;
    return data->ppm;
}

int mq4_get_raw(mq4_data_t *data)
{
    if (data == NULL) return -1;
    return data->raw_value;
}

bool mq4_is_above_threshold(mq4_data_t *data, float threshold_ppm)
{
    if (data == NULL) return false;
    return (data->ppm >= threshold_ppm);
}