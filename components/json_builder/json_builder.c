#include "json_builder.h"
#include "mqtt_handler.h"
#include "time_sync.h"
#include "led_control.h"
#include "cJSON.h"
#include "esp_log.h"
#include <string.h>
#include <time.h>

static const char *TAG = "json_builder";

char *json_build_status(void)
{
    cJSON *root = cJSON_CreateObject();
    cJSON *data = cJSON_CreateObject();

    if (root == NULL || data == NULL) {
        ESP_LOGE(TAG, "Failed to create JSON objects");
        cJSON_Delete(root);
        return NULL;
    }

    // ==================== 时间戳 ====================
    time_t now = time_sync_get_timestamp();
    struct tm timeinfo;
    localtime_r(&now, &timeinfo);

    char time_str[64];
    strftime(time_str, sizeof(time_str), "%Y-%m-%dT%H:%M:%S", &timeinfo);
    strncat(time_str, "+08:00", sizeof(time_str) - strlen(time_str) - 1);

    cJSON_AddStringToObject(root, "device_id", DEVICE_ID);
    cJSON_AddStringToObject(root, "type",       DEVICE_TYPE);
    cJSON_AddNumberToObject(root, "timestamp",  (double)now);
    cJSON_AddStringToObject(root, "datetime",   time_str);

    // ==================== DHT11 ====================
    cJSON *dht11_obj = cJSON_CreateObject();
    if (sensor_dht11_is_valid() && sensor_dht11_get_temperature() > -100.0f) {
        cJSON_AddNumberToObject(dht11_obj, "temperature", sensor_dht11_get_temperature());
        cJSON_AddNumberToObject(dht11_obj, "humidity",    (int)sensor_dht11_get_humidity());
        cJSON_AddStringToObject(dht11_obj, "status",      "active");
    } else {
        cJSON_AddStringToObject(dht11_obj, "status", "offline");
    }
    cJSON_AddItemToObject(data, "dht11", dht11_obj);

    // ==================== SHT40 ====================
    cJSON *sht40_obj = cJSON_CreateObject();
    if (sensor_sht40_is_valid()) {
        cJSON_AddNumberToObject(sht40_obj, "temperature", sensor_sht40_get_temperature());
        cJSON_AddNumberToObject(sht40_obj, "humidity",    sensor_sht40_get_humidity());
        cJSON_AddStringToObject(sht40_obj, "status",      "active");
    } else {
        cJSON_AddStringToObject(sht40_obj, "status", "offline");
    }
    cJSON_AddItemToObject(data, "sht40", sht40_obj);

    // ==================== MQ4 甲烷 ====================
    cJSON *mq4_obj = cJSON_CreateObject();
    if (!sensor_mq4_is_initialized()) {
        cJSON_AddStringToObject(mq4_obj, "status",     "initializing");
        cJSON_AddBoolToObject  (mq4_obj, "calibrated", false);
    } else if (!sensor_mq4_is_online()) {
        cJSON_AddStringToObject(mq4_obj, "status",     "offline");
        cJSON_AddBoolToObject  (mq4_obj, "calibrated", sensor_mq4_is_calibrated());
    } else {
        cJSON_AddNumberToObject(mq4_obj, "ppm",        sensor_mq4_get_ppm());
        cJSON_AddNumberToObject(mq4_obj, "raw_adc",    sensor_mq4_get_raw());
        cJSON_AddBoolToObject  (mq4_obj, "alert",      sensor_mq4_is_alert());
        cJSON_AddBoolToObject  (mq4_obj, "calibrated", sensor_mq4_is_calibrated());
        cJSON_AddStringToObject(mq4_obj, "status",     "active");
    }
    cJSON_AddItemToObject(data, "mq4", mq4_obj);

    // ==================== MQ2 烟雾/可燃气 ====================
    cJSON *mq2_obj = cJSON_CreateObject();
    if (!sensor_mq2_is_initialized()) {
        cJSON_AddStringToObject(mq2_obj, "status",     "initializing");
        cJSON_AddBoolToObject  (mq2_obj, "calibrated", false);
    } else if (!sensor_mq2_is_online()) {
        cJSON_AddStringToObject(mq2_obj, "status",     "offline");
        cJSON_AddBoolToObject  (mq2_obj, "calibrated", sensor_mq2_is_calibrated());
    } else {
        cJSON_AddNumberToObject(mq2_obj, "ppm",        sensor_mq2_get_ppm());
        cJSON_AddNumberToObject(mq2_obj, "raw_adc",    sensor_mq2_get_raw());
        cJSON_AddBoolToObject  (mq2_obj, "alert",      sensor_mq2_is_alert());
        cJSON_AddBoolToObject  (mq2_obj, "calibrated", sensor_mq2_is_calibrated());
        cJSON_AddStringToObject(mq2_obj, "status",     "active");
    }
    cJSON_AddItemToObject(data, "mq2", mq2_obj);

    // ==================== YL-38 火焰 ====================
    cJSON *flame_obj = cJSON_CreateObject();
    if (!sensor_yl38_is_initialized()) {
        cJSON_AddStringToObject(flame_obj, "status", "initializing");
    } else if (!sensor_yl38_is_online()) {
        cJSON_AddStringToObject(flame_obj, "status", "offline");
    } else {
        cJSON_AddNumberToObject(flame_obj, "raw",               sensor_yl38_get_raw());
        cJSON_AddNumberToObject(flame_obj, "voltage",           sensor_yl38_get_voltage());
        cJSON_AddStringToObject(flame_obj, "level",             sensor_yl38_get_level_string());
        cJSON_AddBoolToObject  (flame_obj, "detected",          sensor_yl38_is_flame_detected());
        cJSON_AddNumberToObject(flame_obj, "intensity_percent", sensor_yl38_get_intensity());
        cJSON_AddBoolToObject  (flame_obj, "digital_trigger",   sensor_yl38_is_digital_triggered());
        cJSON_AddStringToObject(flame_obj, "status",            "active");
    }
    cJSON_AddItemToObject(data, "flame", flame_obj);

    // ==================== LED ====================
    cJSON *led_obj = cJSON_CreateObject();
    int brightness = led_get_brightness();
    cJSON_AddNumberToObject(led_obj, "state",        brightness > 0 ? 1 : 0);
    cJSON_AddNumberToObject(led_obj, "r",             led_get_r());
    cJSON_AddNumberToObject(led_obj, "g",             led_get_g());
    cJSON_AddNumberToObject(led_obj, "b",             led_get_b());
    cJSON_AddNumberToObject(led_obj, "brightness",    brightness);
    cJSON_AddBoolToObject  (led_obj, "forced_alert",  led_is_forced_by_gas());
    cJSON_AddItemToObject(data, "led", led_obj);

    cJSON_AddItemToObject(root, "data", data);

    char *json_str = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return json_str;
}

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