#include "adc_shared.h"
#include "esp_log.h"

static const char *TAG = "adc_shared";

adc_oneshot_unit_handle_t g_shared_adc_handle = NULL;

esp_err_t shared_adc_unit_init(void)
{
    if (g_shared_adc_handle != NULL) {
        return ESP_OK;  // 已经初始化
    }

    adc_oneshot_unit_init_cfg_t init_config = {
        .unit_id = ADC_UNIT_1,
    };
    
    esp_err_t ret = adc_oneshot_new_unit(&init_config, &g_shared_adc_handle);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "ADC unit init failed: %s", esp_err_to_name(ret));
        return ret;
    }

    ESP_LOGI(TAG, "Shared ADC1 unit initialized successfully");
    return ESP_OK;
}