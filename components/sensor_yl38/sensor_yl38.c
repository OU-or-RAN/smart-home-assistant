#include "sensor_yl38.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <math.h>
#include <string.h>

#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "adc_shared.h"

static const char *TAG = "yl38";

static int  s_consecutive_flame_count  = 0;   // 连续检测到火焰的次数
static int  s_consecutive_clear_count  = 0;   // 连续未检测到火焰的次数


// 全局变量
static adc_cali_handle_t g_adc_cali_handle = NULL;
static int g_flame_threshold_raw = 2000;    // 默认阈值，会被校准覆盖
static int g_baseline_raw = 4000;           // 环境基线（无火焰时的值）
static bool g_adc_channel_configured = false;
static bool g_is_calibrated = false;

// 内部函数
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
        default: return ADC_CHANNEL_8;
    }
}

static esp_err_t yl38_adc_init(void)
{
    if (g_adc_channel_configured) {
        return ESP_OK;
    }

    esp_err_t ret = shared_adc_unit_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Shared ADC init failed: %s", esp_err_to_name(ret));
        return ret;
    }

    adc_oneshot_chan_cfg_t chan_config = {
        .bitwidth = YL38_ADC_WIDTH,
        .atten = YL38_ADC_ATTEN,
    };

    adc_channel_t channel = get_adc_channel(YL38_AO_GPIO);
    ret = adc_oneshot_config_channel(g_shared_adc_handle, channel, &chan_config);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "ADC channel config failed: %s", esp_err_to_name(ret));
        return ret;
    }

    ESP_LOGI(TAG, "YL-38 ADC channel configured on GPIO%d (ADC1_CH%d)", 
             YL38_AO_GPIO, channel);
    g_adc_channel_configured = true;
    return ESP_OK;
}

static esp_err_t yl38_adc_calibration_init(void)
{
    if (g_adc_cali_handle != NULL) {
        return ESP_OK;
    }

    esp_err_t ret = ESP_FAIL;
    bool calibrated = false;

#if ADC_CALI_SCHEME_CURVE_FITTING_SUPPORTED
    adc_cali_curve_fitting_config_t cali_config = {
        .unit_id = YL38_ADC_UNIT,
        .atten = YL38_ADC_ATTEN,
        .bitwidth = YL38_ADC_WIDTH,
    };
    
    ret = adc_cali_create_scheme_curve_fitting(&cali_config, &g_adc_cali_handle);
    if (ret == ESP_OK) {
        calibrated = true;
        ESP_LOGI(TAG, "Curve Fitting calibration created");
    }
#endif

#if ADC_CALI_SCHEME_LINE_FITTING_SUPPORTED
    if (!calibrated) {
        adc_cali_line_fitting_config_t cali_config = {
            .unit_id = YL38_ADC_UNIT,
            .atten = YL38_ADC_ATTEN,
            .bitwidth = YL38_ADC_WIDTH,
            .default_vref = 1100,
        };
        
        ret = adc_cali_create_scheme_line_fitting(&cali_config, &g_adc_cali_handle);
        if (ret == ESP_OK) {
            calibrated = true;
            ESP_LOGI(TAG, "Line Fitting calibration created");
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

static esp_err_t yl38_do_init(void)
{
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << YL38_DO_GPIO),
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

    ESP_LOGI(TAG, "DO initialized on GPIO%d", YL38_DO_GPIO);
    return ESP_OK;
}

static int yl38_adc_read_average(int samples)
{
    if (g_shared_adc_handle == NULL) {
        ESP_LOGE(TAG, "Shared ADC not initialized");
        return 0;
    }

    int32_t sum = 0;
    int raw_value;
    adc_channel_t channel = get_adc_channel(YL38_AO_GPIO);

    for (int i = 0; i < samples; i++) {
        if (adc_oneshot_read(g_shared_adc_handle, channel, &raw_value) == ESP_OK) {
            sum += raw_value;
        }
        vTaskDelay(pdMS_TO_TICKS(5));
    }
    
    return sum / samples;
}

static int yl38_raw_to_voltage(int raw)
{
    if (g_adc_cali_handle == NULL) {
        return (raw * 3300) / 4095;
    }

    int voltage_mv = 0;
    esp_err_t ret = adc_cali_raw_to_voltage(g_adc_cali_handle, raw, &voltage_mv);
    if (ret != ESP_OK) {
        return (raw * 3300) / 4095;
    }
    
    return voltage_mv;
}

// ==================== 公共接口 ====================

esp_err_t yl38_init(void)
{
    ESP_LOGI(TAG, "Initializing YL-38 flame sensor...");
    
    esp_err_t ret = yl38_adc_init();
    if (ret != ESP_OK) {
        return ret;
    }
    
    yl38_adc_calibration_init();
    
    ret = yl38_do_init();
    if (ret != ESP_OK) {
        return ret;
    }
    
    ESP_LOGI(TAG, "YL-38 init complete. Please run yl38_calibrate_baseline() in clean environment");
    return ESP_OK;
}

/**
 * @brief 校准环境基线（必须在无火焰、正常光照环境下执行！）
 */
esp_err_t yl38_calibrate_baseline(void)
{
    ESP_LOGW(TAG, "Starting YL-38 baseline calibration...");
    vTaskDelay(pdMS_TO_TICKS(3000));

    // 丢弃前3帧（传感器刚开始采样时不稳定）
    for (int i = 0; i < 3; i++) {
        yl38_adc_read_average(3);
        vTaskDelay(pdMS_TO_TICKS(50));
    }

    int raw_sum = 0;
    int min_raw = 4095;
    int max_raw = 0;
    int valid_count = 0;

    // 采样并剔除离群值
    int samples[YL38_CALIBRATION_SAMPLES];
    for (int i = 0; i < YL38_CALIBRATION_SAMPLES; i++) {
        samples[i] = yl38_adc_read_average(5);
        ESP_LOGI(TAG, "Calibration sample %d/%d: Raw=%d",
                 i + 1, YL38_CALIBRATION_SAMPLES, samples[i]);
        vTaskDelay(pdMS_TO_TICKS(100));
    }

    // 第一轮：计算粗略均值
    int32_t rough_sum = 0;
    for (int i = 0; i < YL38_CALIBRATION_SAMPLES; i++) rough_sum += samples[i];
    int rough_avg = rough_sum / YL38_CALIBRATION_SAMPLES;

    // 第二轮：剔除偏离均值 30% 以上的样本
    for (int i = 0; i < YL38_CALIBRATION_SAMPLES; i++) {
        int deviation = abs(samples[i] - rough_avg);
        if (deviation > rough_avg * 30 / 100) {
            ESP_LOGW(TAG, "Sample %d Raw=%d rejected (deviation=%d)", i+1, samples[i], deviation);
            continue;
        }
        raw_sum += samples[i];
        if (samples[i] < min_raw) min_raw = samples[i];
        if (samples[i] > max_raw) max_raw = samples[i];
        valid_count++;
    }

    if (valid_count < YL38_CALIBRATION_SAMPLES / 2) {
        ESP_LOGE(TAG, "Too many invalid samples (%d valid), calibration aborted",
                 valid_count);
        return ESP_ERR_INVALID_STATE;
    }

    int avg_raw  = raw_sum / valid_count;
    int variance = max_raw - min_raw;

    ESP_LOGI(TAG, "Calibration: valid=%d/%d Avg=%d Min=%d Max=%d Variance=%d",
             valid_count, YL38_CALIBRATION_SAMPLES, avg_raw, min_raw, max_raw, variance);

    if (avg_raw < YL38_MIN_VALID_RAW) {
        ESP_LOGE(TAG, "Baseline too low (%d), sensor may not be connected", avg_raw);
        return ESP_ERR_INVALID_STATE;
    }

    g_baseline_raw = avg_raw;
    g_flame_threshold_raw = g_baseline_raw - YL38_FLAME_HYSTERESIS;

    // 安全检查：阈值不能为负数或过低
    if (g_flame_threshold_raw < 100) {
        g_flame_threshold_raw = 100;
        ESP_LOGW(TAG, "Threshold clipped to 100 (baseline too low for configured hysteresis)");
    }

    ESP_LOGI(TAG, "Baseline=%d Threshold=%d", g_baseline_raw, g_flame_threshold_raw);
    g_is_calibrated = true;
    return ESP_OK;
}

esp_err_t yl38_read(yl38_data_t *data)
{
    if (data == NULL) return ESP_ERR_INVALID_ARG;
    memset(data, 0, sizeof(yl38_data_t));

    // ===== ADC 读取（多次平均，采样数从原来的 YL38_READ_SAMPLES 增大）=====
    int raw = yl38_adc_read_average(YL38_READ_SAMPLES);
    data->raw_value    = raw;
    data->baseline_raw = g_baseline_raw;

    int voltage_mv  = yl38_raw_to_voltage(raw);
    data->voltage   = voltage_mv / 1000.0f;
    data->digital_detected = yl38_get_digital_status();

    int threshold = g_is_calibrated ? g_flame_threshold_raw : 2000;

    ESP_LOGD(TAG, "Raw=%d Threshold=%d Baseline=%d",
             raw, threshold, g_baseline_raw);

    // ===== 原始检测（本帧是否疑似有火焰）=====
    bool raw_detected = false;
    yl38_flame_level_t raw_level = YL38_NO_FLAME;
    float raw_intensity = 0.0f;

    if (raw < threshold) {
        int drop = g_baseline_raw - raw;
        float drop_percent = (float)drop / g_baseline_raw * 100.0f;

        if (drop_percent > 60.0f || raw < 400) {        // 降低 STRONG 阈值：70% → 60%
            raw_level     = YL38_FLAME_STRONG;
            raw_intensity = 100.0f;
        } else if (drop_percent > 30.0f) {              // 降低 MEDIUM 阈值：40% → 30%
            raw_level     = YL38_FLAME_MEDIUM;
            raw_intensity = 75.0f;
        } else {
            raw_level     = YL38_FLAME_WEAK;
            raw_intensity = 50.0f;
        }
        raw_detected = true;
    }

    // DO 辅助触发（硬件比较器，优先级高）
    if (data->digital_detected && !raw_detected) {
        ESP_LOGW(TAG, "DO triggered but analog not, possible threshold issue");
        raw_detected  = true;
        raw_level     = YL38_FLAME_WEAK;
        raw_intensity = 50.0f;
    }

    // ===== 连续帧确认（消抖）=====
    if (raw_detected) {
        s_consecutive_flame_count++;
        s_consecutive_clear_count = 0;
    } else {
        s_consecutive_clear_count++;
        s_consecutive_flame_count = 0;
    }

    // 报警触发：需要连续 N 帧检测到
    if (s_consecutive_flame_count >= YL38_CONFIRM_FRAMES) {
        data->flame_detected    = true;
        data->flame_level       = raw_level;
        data->intensity_percent = raw_intensity;
    }
    // 报警解除：需要连续 N 帧未检测到
    else if (s_consecutive_clear_count >= YL38_CLEAR_FRAMES) {
        data->flame_detected    = false;
        data->flame_level       = YL38_NO_FLAME;
        data->intensity_percent = 0.0f;
    }
    // 中间状态：保持上一次的结论（不在此函数维护，由任务层 last_flame 保持）
    else {
        data->flame_detected    = raw_detected;
        data->flame_level       = raw_level;
        data->intensity_percent = raw_intensity;
    }

    return ESP_OK;
}

bool yl38_get_digital_status(void)
{
    int level = gpio_get_level(YL38_DO_GPIO);
    return (level == 0);  // 低电平有效
}

void yl38_set_threshold(int raw_threshold)
{
    g_flame_threshold_raw = raw_threshold;
    ESP_LOGI(TAG, "Flame threshold manually set to %d", raw_threshold);
}

const char* yl38_get_level_string(yl38_flame_level_t level)
{
    switch (level) {
        case YL38_NO_FLAME:     return "NONE";
        case YL38_FLAME_WEAK:   return "WEAK";
        case YL38_FLAME_MEDIUM: return "MEDIUM";
        case YL38_FLAME_STRONG: return "STRONG";
        default:                return "UNKNOWN";
    }
}