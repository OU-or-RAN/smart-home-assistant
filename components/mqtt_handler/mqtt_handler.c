#include "mqtt_handler.h"
#include "json_builder.h"
#include "led_control.h"
#include "sensor_mq4.h"
#include "sensor_dht11.h"
#include "sensor_yl38.h"
#include "esp_log.h"
#include "mqtt_client.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "cJSON.h"
#include <string.h>
#include <stdlib.h>

static const char *TAG = "mqtt_handler";

// ==================== 传感器数据（模块内持有）====================

static dht11_data_t s_dht11_data  = {0};
static mq4_data_t   s_mq4_data    = {0};
static yl38_data_t  s_yl38_data   = {0};

static bool s_mq4_initialized    = false;
static bool s_mq4_alert_active   = false;
static bool s_yl38_initialized   = false;

// ==================== Getter 供 json_builder 调用 ====================

// --- DHT11 ---
bool        sensor_dht11_is_valid(void)       { return s_dht11_data.valid; }
float       sensor_dht11_get_temperature(void){ return dht11_get_temperature(&s_dht11_data); }
float       sensor_dht11_get_humidity(void)   { return dht11_get_humidity(&s_dht11_data); }

// --- MQ4 ---
bool        sensor_mq4_is_initialized(void)  { return s_mq4_initialized; }
bool        sensor_mq4_is_alert(void)        { return s_mq4_alert_active; }
float       sensor_mq4_get_ppm(void)         { return s_mq4_data.ppm; }
int         sensor_mq4_get_raw(void)         { return s_mq4_data.raw_value; }
bool        sensor_mq4_is_calibrated(void)   { return s_mq4_data.calibrated; }

// --- YL38 ---
bool        sensor_yl38_is_initialized(void)      { return s_yl38_initialized; }
bool        sensor_yl38_is_flame_detected(void)   { return s_yl38_data.flame_detected; }
float       sensor_yl38_get_voltage(void)         { return s_yl38_data.voltage; }
int         sensor_yl38_get_raw(void)             { return s_yl38_data.raw_value; }
const char* sensor_yl38_get_level_string(void)    { return yl38_get_level_string(s_yl38_data.flame_level); }
float       sensor_yl38_get_intensity(void)       { return s_yl38_data.intensity_percent; }
bool        sensor_yl38_is_digital_triggered(void){ return s_yl38_data.digital_detected; }

// ==================== 内部任务 ====================

static void sensor_dht11_task(void *pvParameters)
{
    ESP_ERROR_CHECK(dht11_init());
    vTaskDelay(pdMS_TO_TICKS(2000));

    while (1) {
        esp_err_t ret = dht11_read(&s_dht11_data);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "DHT11 read failed: %s", esp_err_to_name(ret));
            s_dht11_data.valid = false;
            vTaskDelay(pdMS_TO_TICKS(3000));
            continue;
        }
        ESP_LOGI(TAG, "DHT11: Temp=%.1f°C, Hum=%.1f%%",
                 sensor_dht11_get_temperature(),
                 sensor_dht11_get_humidity());
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}

static void sensor_mq4_task(void *pvParameters)
{
    esp_err_t ret = mq4_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "MQ-4 init failed: %s", esp_err_to_name(ret));
        vTaskDelete(NULL);
        return;
    }

    // 预热 30 秒
    ESP_LOGI(TAG, "MQ-4 warming up 30s...");
    for (int i = 0; i < 30; i++) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        if (i % 10 == 0) {
            ESP_LOGI(TAG, "MQ-4 warmup: %d/30s", i);
        }
    }

    // 校准
    ret = mq4_calibrate(&s_mq4_data);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "MQ-4 calibration failed: %s", esp_err_to_name(ret));
    }

    s_mq4_initialized = true;
    ESP_LOGI(TAG, "MQ-4 ready");

    bool last_alert  = false;
    bool blink_state = false;

    while (1) {
        ret = mq4_read(&s_mq4_data);
        if (ret == ESP_OK) {
            bool current_alert = (s_mq4_data.ppm > MQ4_DEFAULT_THRESHOLD_PPM);

            ESP_LOGI(TAG, "MQ-4: PPM=%.1f, Raw=%d, Alert=%s",
                     s_mq4_data.ppm, s_mq4_data.raw_value,
                     current_alert ? "YES" : "NO");

            if (current_alert != last_alert) {
                if (current_alert) {
                    ESP_LOGW(TAG, "!!! GAS ALERT !!! PPM=%.1f", s_mq4_data.ppm);
                    led_set_gas_alert(true);
                    s_mq4_alert_active = true;
                } else {
                    ESP_LOGI(TAG, "Gas alert cleared");
                    led_set_gas_alert(false);
                    s_mq4_alert_active = false;
                }
                last_alert = current_alert;
            }

            if (current_alert) {
                blink_state = !blink_state;
                int intensity = blink_state ? 255 : 100;
                led_set_color_with_priority(intensity, 0, 0, LED_PRIO_GAS);
            }
        } else {
            ESP_LOGE(TAG, "MQ-4 read failed");
            s_mq4_data.calibrated = false;
        }
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}

static void sensor_yl38_task(void *pvParameters)
{
    esp_err_t ret = yl38_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "YL-38 init failed: %s", esp_err_to_name(ret));
        vTaskDelete(NULL);
        return;
    }

    ret = yl38_calibrate_baseline();
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "YL-38 calibration failed, using defaults");
    }

    s_yl38_initialized = true;
    ESP_LOGI(TAG, "YL-38 ready");

    bool last_flame = false;
    bool blink      = false;

    while (1) {
        ret = yl38_read(&s_yl38_data);
        if (ret == ESP_OK) {
            bool current_flame = s_yl38_data.flame_detected;

            ESP_LOGI(TAG, "YL-38: Raw=%d, Base=%d, Volt=%.2fV, Level=%s, Detected=%s",
                     s_yl38_data.raw_value,
                     s_yl38_data.baseline_raw,
                     s_yl38_data.voltage,
                     yl38_get_level_string(s_yl38_data.flame_level),
                     current_flame ? "YES" : "NO");

            if (current_flame != last_flame) {
                if (current_flame) {
                    ESP_LOGW(TAG, "!!! FLAME DETECTED !!! Level: %s",
                             yl38_get_level_string(s_yl38_data.flame_level));
                    led_set_flame_alert(true, s_yl38_data.flame_level);
                } else {
                    ESP_LOGI(TAG, "Flame cleared");
                    led_set_flame_alert(false, YL38_NO_FLAME);
                }
                last_flame = current_flame;
            }

            if (current_flame) {
                blink = !blink;
                int intensity = (s_yl38_data.flame_level == YL38_FLAME_STRONG) ? 255 : 150;
                int g_val = (s_yl38_data.flame_level == YL38_FLAME_WEAK)   ? 255 :
                            (s_yl38_data.flame_level == YL38_FLAME_MEDIUM) ? 50  : 0;
                int r_val = blink ? intensity : intensity / 2;
                led_set_color_with_priority(r_val, g_val, 0, LED_PRIO_FLAME);
            }
        } else {
            ESP_LOGE(TAG, "YL-38 read failed");
        }
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}

static void status_publish_task(void *pvParameters)
{
    esp_mqtt_client_handle_t client = (esp_mqtt_client_handle_t)pvParameters;
    vTaskDelay(pdMS_TO_TICKS(5000));

    while (1) {
        mqtt_publish_status(client);
        vTaskDelay(pdMS_TO_TICKS(30000));
    }
}

// ==================== 发布接口 ====================

void mqtt_publish_status(esp_mqtt_client_handle_t client)
{
    char *json = json_build_status();
    if (json == NULL) return;

    int msg_id = esp_mqtt_client_publish(client, TOPIC_STATUS, json, 0, 1, 0);
    ESP_LOGI(TAG, "Published status, msg_id=%d", msg_id);
    free(json);
}

void mqtt_publish_gas_alert(esp_mqtt_client_handle_t client, float ppm, bool alert)
{
    char *json = json_build_gas_alert(ppm, alert);
    if (json == NULL) return;

    int msg_id = esp_mqtt_client_publish(client, TOPIC_GAS_ALERT, json, 0, 2, 0);
    ESP_LOGW(TAG, "Published gas alert, msg_id=%d", msg_id);
    free(json);
}

// ==================== 控制命令解析 ====================

void mqtt_parse_control(const char *json_str, esp_mqtt_client_handle_t client)
{
    cJSON *root = cJSON_Parse(json_str);
    if (root == NULL) {
        ESP_LOGE(TAG, "JSON parse error");
        return;
    }

    cJSON *cmd    = cJSON_GetObjectItemCaseSensitive(root, "cmd");
    cJSON *target = cJSON_GetObjectItemCaseSensitive(root, "target");

    if (!cJSON_IsString(cmd) || !cJSON_IsString(target)) {
        ESP_LOGE(TAG, "Invalid command format");
        cJSON_Delete(root);
        return;
    }

    const char *cmd_str    = cmd->valuestring;
    const char *target_str = target->valuestring;

    ESP_LOGI(TAG, "CMD: %s  TARGET: %s", cmd_str, target_str);

    if (strcmp(cmd_str, "set") == 0) {

        if (strcmp(target_str, "led") == 0) {
            cJSON *value = cJSON_GetObjectItemCaseSensitive(root, "value");
            if (cJSON_IsNumber(value)) {
                led_set_state(value->valueint);
                mqtt_publish_status(client);
            }
        }
        else if (strcmp(target_str, "rgb") == 0) {
            cJSON *r = cJSON_GetObjectItemCaseSensitive(root, "r");
            cJSON *g = cJSON_GetObjectItemCaseSensitive(root, "g");
            cJSON *b = cJSON_GetObjectItemCaseSensitive(root, "b");
            if (cJSON_IsNumber(r) && cJSON_IsNumber(g) && cJSON_IsNumber(b)) {
                led_set_color(r->valueint, g->valueint, b->valueint);
                mqtt_publish_status(client);
            }
        }
        else if (strcmp(target_str, "mq4_calibrate") == 0) {
            // 直接调用实际存在的 mq4_calibrate()
            ESP_LOGW(TAG, "Manual MQ-4 calibration triggered via MQTT");
            mq4_calibrate(&s_mq4_data);
            mqtt_publish_status(client);
        }
    }
    else if (strcmp(cmd_str, "get") == 0) {
        if (strcmp(target_str, "status") == 0) {
            mqtt_publish_status(client);
        }
    }

    cJSON_Delete(root);
}

// ==================== MQTT 事件处理 ====================

static void mqtt_event_handler(void *handler_args, esp_event_base_t base,
                                int32_t event_id, void *event_data)
{
    esp_mqtt_event_handle_t  event  = event_data;
    esp_mqtt_client_handle_t client = event->client;

    switch ((esp_mqtt_event_id_t)event_id) {

    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "MQTT connected");
        esp_mqtt_client_subscribe(client, TOPIC_CONTROL, 1);
        mqtt_publish_status(client);
        break;

    case MQTT_EVENT_DATA: {
        char *topic = malloc(event->topic_len + 1);
        char *data  = malloc(event->data_len  + 1);

        if (topic && data) {
            memcpy(topic, event->topic, event->topic_len);
            topic[event->topic_len] = '\0';
            memcpy(data,  event->data,  event->data_len);
            data[event->data_len]   = '\0';

            ESP_LOGI(TAG, "Topic: %s  Data: %s", topic, data);

            if (strncmp(topic, TOPIC_CONTROL, strlen(TOPIC_CONTROL)) == 0) {
                mqtt_parse_control(data, client);
            }
        }
        free(topic);
        free(data);
        break;
    }

    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "MQTT disconnected");
        break;

    default:
        break;
    }
}

// ==================== 启动入口 ====================

void mqtt_handler_start(void)
{
    esp_mqtt_client_config_t mqtt_cfg = {
        .broker.address.uri = CONFIG_BROKER_URL,
    };

    esp_mqtt_client_handle_t client = esp_mqtt_client_init(&mqtt_cfg);
    esp_mqtt_client_register_event(client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);
    esp_mqtt_client_start(client);

    xTaskCreate(sensor_dht11_task,   "dht11_task",   4096, NULL,   2, NULL);
    xTaskCreate(sensor_mq4_task,     "mq4_task",     4096, NULL,   3, NULL);
    xTaskCreate(sensor_yl38_task,    "yl38_task",     4096, NULL,   4, NULL);
    xTaskCreate(status_publish_task, "status_task",  4096, client, 5, NULL);
}