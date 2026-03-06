#include "sensor_mq2.h"
#include "pin_config.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "adc_shared.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include <math.h>
#include <string.h>

static const char *TAG = "mq2";

static adc_cali_handle_t g_adc_cali_handle      = NULL;
static float             g_ro_value              = 10.0f;
static bool              g_is_calibrated         = false;
static bool              g_adc_channel_configured = false;

// ==================== 内部工具 ====================

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
        default: return ADC_CHANNEL_0;
    }
}

static esp_err_t mq2_adc_init(void)
{
    if (g_adc_channel_configured) return ESP_OK;

    esp_err_t ret = shared_adc_unit_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Shared ADC init failed: %s", esp_err_to_name(ret));
        return ret;
    }

    adc_oneshot_chan_cfg_t chan_cfg = {
        .bitwidth = MQ2_ADC_WIDTH,
        .atten    = MQ2_ADC_ATTEN,
    };

    adc_channel_t channel = get_adc_channel(MQ2_AO_GPIO);
    ret = adc_oneshot_config_channel(g_shared_adc_handle, channel, &chan_cfg);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "ADC channel config failed: %s", esp_err_to_name(ret));
        return ret;
    }

    ESP_LOGI(TAG, "MQ-2 ADC configured on GPIO%d (ADC1_CH%d)", MQ2_AO_GPIO, channel);
    g_adc_channel_configured = true;
    return ESP_OK;
}

static esp_err_t mq2_adc_calibration_init(void)
{
    if (g_adc_cali_handle != NULL) return ESP_OK;

    bool calibrated = false;

#if ADC_CALI_SCHEME_CURVE_FITTING_SUPPORTED
    adc_cali_curve_fitting_config_t cali_cfg = {
        .unit_id  = MQ2_ADC_UNIT,
        .atten    = MQ2_ADC_ATTEN,
        .bitwidth = MQ2_ADC_WIDTH,
    };
    if (adc_cali_create_scheme_curve_fitting(&cali_cfg, &g_adc_cali_handle) == ESP_OK) {
        calibrated = true;
        ESP_LOGI(TAG, "Curve Fitting calibration ready");
    }
#endif

#if ADC_CALI_SCHEME_LINE_FITTING_SUPPORTED
    if (!calibrated) {
        adc_cali_line_fitting_config_t cali_cfg = {
            .unit_id      = MQ2_ADC_UNIT,
            .atten        = MQ2_ADC_ATTEN,
            .bitwidth     = MQ2_ADC_WIDTH,
            .default_vref = 1100,
        };
        if (adc_cali_create_scheme_line_fitting(&cali_cfg, &g_adc_cali_handle) == ESP_OK) {
            calibrated = true;
            ESP_LOGI(TAG, "Line Fitting calibration ready");
        }
    }
#endif

    if (!calibrated) {
        ESP_LOGW(TAG, "ADC calibration unavailable, using raw values");
        g_adc_cali_handle = NULL;
        return ESP_ERR_NOT_SUPPORTED;
    }
    return ESP_OK;
}

static esp_err_t mq2_do_init(void)
{
    gpio_config_t io_conf = {
        .pin_bit_mask  = (1ULL << MQ2_DO_GPIO),
        .mode          = GPIO_MODE_INPUT,
        .pull_up_en    = GPIO_PULLUP_ENABLE,
        .pull_down_en  = GPIO_PULLDOWN_DISABLE,
        .intr_type     = GPIO_INTR_DISABLE,
    };
    esp_err_t ret = gpio_config(&io_conf);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "DO GPIO config failed");
        return ret;
    }
    ESP_LOGI(TAG, "DO initialized on GPIO%d", MQ2_DO_GPIO);
    return ESP_OK;
}

static int mq2_adc_read_average(int samples)
{
    if (g_shared_adc_handle == NULL) return 0;

    int32_t sum = 0;
    int raw;
    adc_channel_t channel = get_adc_channel(MQ2_AO_GPIO);

    for (int i = 0; i < samples; i++) {
        if (adc_oneshot_read(g_shared_adc_handle, channel, &raw) == ESP_OK) {
            sum += raw;
        }
        vTaskDelay(pdMS_TO_TICKS(5));
    }
    return sum / samples;
}

static int mq2_raw_to_voltage(int raw)
{
    if (g_adc_cali_handle == NULL) {
        return (raw * 3300) / 4095;
    }
    int voltage_mv = 0;
    if (adc_cali_raw_to_voltage(g_adc_cali_handle, raw, &voltage_mv) != ESP_OK) {
        return (raw * 3300) / 4095;
    }
    return voltage_mv;
}

static float mq2_calculate_rs(int raw)
{
    if (raw == 0) raw = 1;
    float vout = mq2_raw_to_voltage(raw) / 1000.0f;
    if (vout >= 3.3f) vout = 3.29f;
    return MQ2_RL_VALUE * (3.3f - vout) / vout;
}

static float mq2_calculate_ppm(float rs_ro_ratio)
{
    if (rs_ro_ratio <= 0) rs_ro_ratio = 0.001f;
    float ppm = powf(10.0f, MQ2_CURVE_M * log10f(rs_ro_ratio) + MQ2_CURVE_B);
    if (ppm < 0)      ppm = 0;
    if (ppm > 10000)  ppm = 10000;
    return ppm;
}

// ==================== 公共接口 ====================

esp_err_t mq2_init(void)
{
    ESP_LOGI(TAG, "Initializing MQ-2...");

    esp_err_t ret = mq2_adc_init();
    if (ret != ESP_OK) return ret;

    mq2_adc_calibration_init();

    ret = mq2_do_init();
    if (ret != ESP_OK) return ret;

    ESP_LOGI(TAG, "MQ-2 init complete. Warmup required before calibration.");
    return ESP_OK;
}

esp_err_t mq2_calibrate(mq2_data_t *data)
{
    if (data == NULL) return ESP_ERR_INVALID_ARG;

    ESP_LOGW(TAG, "Starting MQ-2 calibration in CLEAN AIR...");
    vTaskDelay(pdMS_TO_TICKS(5000));

    int raw  = mq2_adc_read_average(MQ2_CALIBRATION_SAMPLES);
    float rs = mq2_calculate_rs(raw);

    g_ro_value      = rs / MQ2_RO_CLEAN_AIR;
    g_is_calibrated = true;

    data->raw_value  = raw;
    data->rs         = rs;
    data->ro         = g_ro_value;
    data->calibrated = true;
    data->ppm        = 0;

    ESP_LOGI(TAG, "MQ-2 calibration done. Raw=%d RS=%.2fKΩ RO=%.2fKΩ",
             raw, rs, g_ro_value);
    return ESP_OK;
}

esp_err_t mq2_read(mq2_data_t *data)
{
    if (data == NULL) return ESP_ERR_INVALID_ARG;

    memset(data, 0, sizeof(mq2_data_t));

    int raw          = mq2_adc_read_average(MQ2_READ_SAMPLES);
    data->raw_value  = raw;
    data->voltage    = mq2_raw_to_voltage(raw) / 1000.0f;

    float rs         = mq2_calculate_rs(raw);
    data->rs         = rs;
    data->ro         = g_ro_value;
    data->calibrated = g_is_calibrated;

    float ro_ref     = g_is_calibrated ? g_ro_value : MQ2_RO_CLEAN_AIR;
    data->ppm        = mq2_calculate_ppm(rs / ro_ref);
    data->digital_alert = mq2_get_digital_status();

    ESP_LOGD(TAG, "Raw=%d Volt=%.2fV RS=%.2fK PPM=%.1f DO=%s",
             data->raw_value, data->voltage, data->rs, data->ppm,
             data->digital_alert ? "ALERT" : "NORMAL");

    return ESP_OK;
}

bool mq2_get_digital_status(void)
{
    return (gpio_get_level(MQ2_DO_GPIO) == 0);
}

float mq2_get_ppm(mq2_data_t *data)
{
    if (data == NULL) return -1.0f;
    return data->ppm;
}

int mq2_get_raw(mq2_data_t *data)
{
    if (data == NULL) return -1;
    return data->raw_value;
}

bool mq2_is_above_threshold(mq2_data_t *data, float threshold_ppm)
{
    if (data == NULL) return false;
    return (data->ppm >= threshold_ppm);
}