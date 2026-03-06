#include "json_builder.h"
#include "mqtt_handler.h"   // DEVICE_ID, DEVICE_TYPE
#include "time_sync.h"
#include "sensor_dht11.h"
#include "sensor_mq4.h"
#include "sensor_yl38.h"
#include "led_control.h"
#include "cJSON.h"
#include "esp_log.h"
#include <string.h>
#include <time.h>

static const char *TAG = "json_builder";

// ==================== 状态 JSON ====================

char *json_build_status(void)
{
    cJSON *root = cJSON_CreateObject();
    cJSON *data = cJSON_CreateObject();

    if (root == NULL || data == NULL) {
        ESP_LOGE(TAG, "Failed to create JSON objects");
        cJSON_Delete(root);
        return NULL;
    }

    // 时间戳
    time_t now = time_sync_get_timestamp();
    struct tm timeinfo;
    localtime_r(&now, &timeinfo);

    char time_str[64];
    strftime(time_str, sizeof(time_str), "%Y-%m-%dT%H:%M:%S", &timeinfo);
    char tz_str[10];
    snprintf(tz_str, sizeof(tz_str), "+08:00");
    strncat(time_str, tz_str, sizeof(time_str) - strlen(time_str) - 1);

    // 设备基础信息
    cJSON_AddStringToObject(root, "device_id",  DEVICE_ID);
    cJSON_AddStringToObject(root, "type",        DEVICE_TYPE);
    cJSON_AddNumberToObject(root, "timestamp",   (double)now);
    cJSON_AddStringToObject(root, "datetime",    time_str);

    // DHT11
    float temperature = sensor_dht11_get_temperature();
    float humidity    = sensor_dht11_get_humidity();

    if (sensor_dht11_is_valid() && temperature > -100.0f) {
        cJSON_AddNumberToObject(data, "temperature", temperature);
        cJSON_AddNumberToObject(data, "humidity",    (int)humidity);
    } else {
        cJSON_AddNumberToObject(data, "temperature", 25.0);
        cJSON_AddNumberToObject(data, "humidity",    50);
        cJSON_AddStringToObject(data, "dht11_status", "offline");
    }

    // MQ-4 气体
    cJSON *gas_obj = cJSON_CreateObject();
    if (sensor_mq4_is_initialized()) {
        cJSON_AddNumberToObject(gas_obj, "ppm",       sensor_mq4_get_ppm());
        cJSON_AddNumberToObject(gas_obj, "raw_adc",   sensor_mq4_get_raw());
        cJSON_AddBoolToObject  (gas_obj, "alert",     sensor_mq4_is_alert());
        cJSON_AddBoolToObject  (gas_obj, "calibrated",sensor_mq4_is_calibrated());
        cJSON_AddStringToObject(gas_obj, "status",    "active");
    } else {
        cJSON_AddStringToObject(gas_obj, "status",    "initializing");
        cJSON_AddBoolToObject  (gas_obj, "calibrated", false);
    }
    cJSON_AddItemToObject(data, "gas", gas_obj);

    // YL-38 火焰
    cJSON *flame_obj = cJSON_CreateObject();
    if (sensor_yl38_is_initialized()) {
        cJSON_AddNumberToObject(flame_obj, "raw",             sensor_yl38_get_raw());
        cJSON_AddNumberToObject(flame_obj, "voltage",         sensor_yl38_get_voltage());
        cJSON_AddStringToObject(flame_obj, "level",           sensor_yl38_get_level_string());
        cJSON_AddBoolToObject  (flame_obj, "detected",        sensor_yl38_is_flame_detected());
        cJSON_AddNumberToObject(flame_obj, "intensity_percent",sensor_yl38_get_intensity());
        cJSON_AddBoolToObject  (flame_obj, "digital_trigger", sensor_yl38_is_digital_triggered());
    } else {
        cJSON_AddStringToObject(flame_obj, "status", "offline");
    }
    cJSON_AddItemToObject(data, "flame", flame_obj);

    // LED 状态
    cJSON *led_obj = cJSON_CreateObject();
    int brightness = led_get_brightness();
    cJSON_AddNumberToObject(led_obj, "state",       brightness > 0 ? 1 : 0);
    cJSON_AddNumberToObject(led_obj, "r",            led_get_r());
    cJSON_AddNumberToObject(led_obj, "g",            led_get_g());
    cJSON_AddNumberToObject(led_obj, "b",            led_get_b());
    cJSON_AddNumberToObject(led_obj, "brightness",   brightness);
    cJSON_AddBoolToObject  (led_obj, "forced_alert", led_is_forced_by_gas());
    cJSON_AddItemToObject(data, "led", led_obj);

    cJSON_AddItemToObject(root, "data", data);

    char *json_str = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return json_str;
}

// ==================== 报警 JSON ====================

char *json_build_gas_alert(float ppm, bool alert)
{
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) return NULL;

    cJSON_AddStringToObject(root, "device_id", DEVICE_ID);
    cJSON_AddNumberToObject(root, "timestamp", (double)time_sync_get_timestamp());
    cJSON_AddNumberToObject(root, "ppm",        ppm);
    cJSON_AddBoolToObject  (root, "alert",      alert);
    cJSON_AddStringToObject(root, "type",       "gas_alert");

    char *json_str = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return json_str;
}