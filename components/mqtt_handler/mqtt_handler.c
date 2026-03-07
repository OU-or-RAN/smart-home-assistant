#include "mqtt_handler.h"
#include "json_builder.h"
#include "led_control.h"
#include "sensor_mq2.h"
#include "sensor_mq4.h"
#include "sensor_dht11.h"
#include "sensor_yl38.h"
#include "sensor_sht40.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "mqtt_client.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "cJSON.h"
#include <string.h>
#include <stdlib.h>
#include "esp_adc/adc_oneshot.h"
#include "adc_shared.h"
#include <math.h>

// MQ2 → GPIO1 → ADC1_CH0
// MQ4 → GPIO2 → ADC1_CH1
// YL38 → GPIO3 → ADC1_CH2
#define MQ2_ADC_CHANNEL     ADC_CHANNEL_0
#define MQ4_ADC_CHANNEL     ADC_CHANNEL_1
#define YL38_ADC_CHANNEL    ADC_CHANNEL_2

static const char *TAG = "mqtt_handler";

// ==================== 内部工具：浮空检测 ====================
// 通过连续采样方差判断引脚是否真实接有负载
// 浮空引脚：读数随机抖动，stddev 通常 > 80
// 已接传感器：加热丝形成稳定负载，stddev 通常 < 30

#define HW_DETECT_SAMPLES       20
#define HW_DETECT_STDDEV_MAX    80      // 标准差超过此值判定为浮空
#define HW_DETECT_RAW_MIN       100     // 低于此值判定为短路/断路

// ==================== 离线超时 ====================
#define SENSOR_OFFLINE_TIMEOUT_MS   10000LL

// ==================== 传感器数据 ====================
static dht11_data_t s_dht11_data  = {0};
static mq2_data_t   s_mq2_data    = {0};
static mq4_data_t   s_mq4_data    = {0};
static yl38_data_t  s_yl38_data   = {0};
static sht40_data_t s_sht40_data  = {0};

// ==================== 传感器状态标志 ====================
static bool s_mq2_initialized   = false;
static bool s_mq2_alert_active  = false;
static bool s_mq2_hw_connected  = false;    // 硬件是否实际接入

static bool s_mq4_initialized   = false;
static bool s_mq4_alert_active  = false;
static bool s_mq4_hw_connected  = false;    // 硬件是否实际接入

static bool s_yl38_initialized  = false;
static bool s_yl38_hw_connected = false;    // 硬件是否实际接入

static bool s_sht40_initialized = false;

// ==================== 在线检测时间戳 ====================
static int64_t s_mq2_last_read_ms  = 0;
static int64_t s_mq4_last_read_ms  = 0;
static int64_t s_yl38_last_read_ms = 0;

// ==================== 内部工具：ADC 读数合理性检测 ====================

static bool mq_hw_is_connected(adc_channel_t channel)
{
    if (g_shared_adc_handle == NULL) return false;

    int samples[HW_DETECT_SAMPLES];
    int32_t sum = 0;

    for (int i = 0; i < HW_DETECT_SAMPLES; i++) {
        int raw = 0;
        adc_oneshot_read(g_shared_adc_handle, channel, &raw);
        samples[i] = raw;
        sum += raw;
        vTaskDelay(pdMS_TO_TICKS(10));
    }

    int mean = sum / HW_DETECT_SAMPLES;

    int32_t var_sum = 0;
    for (int i = 0; i < HW_DETECT_SAMPLES; i++) {
        int diff = samples[i] - mean;
        var_sum += diff * diff;
    }
    int stddev = (int)sqrtf((float)var_sum / HW_DETECT_SAMPLES);

    // 判断条件：
    // 1. stddev < 阈值：读数稳定（有负载）
    // 2. mean > 下限：不是短路/断路
    // 3. mean < 上限：不是满量程浮空（ESP32-S3 浮空上拉时可能读到4095）
    bool connected = (stddev < HW_DETECT_STDDEV_MAX)
                  && (mean   > HW_DETECT_RAW_MIN)
                  && (mean   < 4090);               // 新增：排除满量程

    ESP_LOGI(TAG, "HW detect channel=%d mean=%d stddev=%d → %s",
             channel, mean, stddev,
             connected ? "CONNECTED" : "NOT CONNECTED");

    return connected;
}

// ==================== Getter ====================

// DHT11
bool        sensor_dht11_is_valid(void)        { return s_dht11_data.valid; }
float       sensor_dht11_get_temperature(void) { return dht11_get_temperature(&s_dht11_data); }
float       sensor_dht11_get_humidity(void)    { return dht11_get_humidity(&s_dht11_data); }

// MQ2
bool sensor_mq2_is_initialized(void) { return s_mq2_initialized; }
bool sensor_mq2_is_online(void)
{
    if (!s_mq2_initialized || !s_mq2_hw_connected) return false;
    int64_t now = esp_timer_get_time() / 1000;
    return (now - s_mq2_last_read_ms) < SENSOR_OFFLINE_TIMEOUT_MS;
}
bool  sensor_mq2_is_alert(void)      { return s_mq2_alert_active; }
float sensor_mq2_get_ppm(void)       { return s_mq2_data.ppm; }
int   sensor_mq2_get_raw(void)       { return s_mq2_data.raw_value; }
bool  sensor_mq2_is_calibrated(void) { return s_mq2_data.calibrated; }

// MQ4
bool sensor_mq4_is_initialized(void) { return s_mq4_initialized; }
bool sensor_mq4_is_online(void)
{
    if (!s_mq4_initialized || !s_mq4_hw_connected) return false;
    int64_t now = esp_timer_get_time() / 1000;
    return (now - s_mq4_last_read_ms) < SENSOR_OFFLINE_TIMEOUT_MS;
}
bool  sensor_mq4_is_alert(void)      { return s_mq4_alert_active; }
float sensor_mq4_get_ppm(void)       { return s_mq4_data.ppm; }
int   sensor_mq4_get_raw(void)       { return s_mq4_data.raw_value; }
bool  sensor_mq4_is_calibrated(void) { return s_mq4_data.calibrated; }

// YL38
bool sensor_yl38_is_initialized(void) { return s_yl38_initialized; }
bool sensor_yl38_is_online(void)
{
    if (!s_yl38_initialized || !s_yl38_hw_connected) return false;
    int64_t now = esp_timer_get_time() / 1000;
    return (now - s_yl38_last_read_ms) < SENSOR_OFFLINE_TIMEOUT_MS;
}
bool        sensor_yl38_is_flame_detected(void)    { return s_yl38_data.flame_detected; }
float       sensor_yl38_get_voltage(void)          { return s_yl38_data.voltage; }
int         sensor_yl38_get_raw(void)              { return s_yl38_data.raw_value; }
const char *sensor_yl38_get_level_string(void)     { return yl38_get_level_string(s_yl38_data.flame_level); }
float       sensor_yl38_get_intensity(void)        { return s_yl38_data.intensity_percent; }
bool        sensor_yl38_is_digital_triggered(void) { return s_yl38_data.digital_detected; }

// SHT40
bool  sensor_sht40_is_valid(void)        { return s_sht40_data.valid; }
float sensor_sht40_get_temperature(void) { return sht40_get_temperature(&s_sht40_data); }
float sensor_sht40_get_humidity(void)    { return sht40_get_humidity(&s_sht40_data); }

// ==================== 传感器任务 ====================

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
        ESP_LOGI(TAG, "DHT11: Temp=%.1f°C Hum=%.1f%%",
                 sensor_dht11_get_temperature(),
                 sensor_dht11_get_humidity());
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}

static void sensor_mq2_task(void *pvParameters)
{
    esp_err_t ret = mq2_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "MQ-2 init failed: %s", esp_err_to_name(ret));
        vTaskDelete(NULL);
        return;
    }

    // 方差检测硬件是否接入
    vTaskDelay(pdMS_TO_TICKS(200));
    if (!mq_hw_is_connected(MQ2_ADC_CHANNEL)) {
        ESP_LOGW(TAG, "MQ-2 NOT connected (floating pin detected), task exit");
        s_mq2_initialized  = true;
        s_mq2_hw_connected = false;
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG, "MQ-2 hardware confirmed, warming up 30s...");
    for (int i = 0; i < 30; i++) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        if (i % 10 == 0) ESP_LOGI(TAG, "MQ-2 warmup: %d/30s", i);
    }

    ret = mq2_calibrate(&s_mq2_data);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "MQ-2 calibration failed: %s", esp_err_to_name(ret));
    }

    s_mq2_initialized  = true;
    s_mq2_hw_connected = true;
    s_mq2_last_read_ms = esp_timer_get_time() / 1000;
    ESP_LOGI(TAG, "MQ-2 ready");

    bool last_alert  = false;
    bool blink_state = false;

    while (1) {
        ret = mq2_read(&s_mq2_data);
        if (ret == ESP_OK) {
            s_mq2_last_read_ms = esp_timer_get_time() / 1000;

            bool current_alert = (s_mq2_data.ppm > MQ2_DEFAULT_THRESHOLD_PPM);
            ESP_LOGI(TAG, "MQ-2: PPM=%.1f Raw=%d Alert=%s",
                     s_mq2_data.ppm, s_mq2_data.raw_value,
                     current_alert ? "YES" : "NO");

            if (current_alert != last_alert) {
                s_mq2_alert_active = current_alert;
                if (current_alert) {
                    ESP_LOGW(TAG, "!!! MQ-2 ALERT !!! PPM=%.1f", s_mq2_data.ppm);
                    led_set_gas_alert(true);
                } else {
                    ESP_LOGI(TAG, "MQ-2 alert cleared");
                    if (!s_mq4_alert_active) led_set_gas_alert(false);
                }
                last_alert = current_alert;
            }

            if (current_alert) {
                blink_state = !blink_state;
                led_set_color_with_priority(
                    blink_state ? 255 : 100, 0, 0, LED_PRIO_GAS);
            }
        } else {
            ESP_LOGE(TAG, "MQ-2 read failed");
        }
        vTaskDelay(pdMS_TO_TICKS(2000));
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

    vTaskDelay(pdMS_TO_TICKS(200));
    if (!mq_hw_is_connected(MQ4_ADC_CHANNEL)) {
        ESP_LOGW(TAG, "MQ-4 NOT connected (floating pin detected), task exit");
        s_mq4_initialized  = true;
        s_mq4_hw_connected = false;
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG, "MQ-4 hardware confirmed, warming up 30s...");
    for (int i = 0; i < 30; i++) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        if (i % 10 == 0) ESP_LOGI(TAG, "MQ-4 warmup: %d/30s", i);
    }

    ret = mq4_calibrate(&s_mq4_data);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "MQ-4 calibration failed: %s", esp_err_to_name(ret));
    }

    s_mq4_initialized  = true;
    s_mq4_hw_connected = true;
    s_mq4_last_read_ms = esp_timer_get_time() / 1000;
    ESP_LOGI(TAG, "MQ-4 ready");

    bool last_alert  = false;
    bool blink_state = false;

    while (1) {
        ret = mq4_read(&s_mq4_data);
        if (ret == ESP_OK) {
            s_mq4_last_read_ms = esp_timer_get_time() / 1000;

            bool current_alert = (s_mq4_data.ppm > MQ4_DEFAULT_THRESHOLD_PPM);
            ESP_LOGI(TAG, "MQ-4: PPM=%.1f Raw=%d Alert=%s",
                     s_mq4_data.ppm, s_mq4_data.raw_value,
                     current_alert ? "YES" : "NO");

            if (current_alert != last_alert) {
                s_mq4_alert_active = current_alert;
                if (current_alert) {
                    ESP_LOGW(TAG, "!!! MQ-4 ALERT !!! PPM=%.1f", s_mq4_data.ppm);
                    led_set_gas_alert(true);
                } else {
                    ESP_LOGI(TAG, "MQ-4 alert cleared");
                    if (!s_mq2_alert_active) led_set_gas_alert(false);
                }
                last_alert = current_alert;
            }

            if (current_alert) {
                blink_state = !blink_state;
                led_set_color_with_priority(
                    blink_state ? 255 : 100, 0, 0, LED_PRIO_GAS);
            }
        } else {
            ESP_LOGE(TAG, "MQ-4 read failed");
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
        // 校准失败时才用方差检测判断硬件是否接入
        ESP_LOGW(TAG, "YL-38 calibration failed, checking hardware connection...");
        if (!mq_hw_is_connected(YL38_ADC_CHANNEL)) {
            ESP_LOGW(TAG, "YL-38 NOT connected (floating pin), task exit");
            s_yl38_initialized  = true;
            s_yl38_hw_connected = false;
            vTaskDelete(NULL);
            return;
        }
        // 硬件存在但校准失败，使用默认阈值继续运行
        ESP_LOGW(TAG, "YL-38 hardware present, running with default threshold");
    }

    // 校准成功或硬件确认存在，直接标记在线
    s_yl38_initialized  = true;
    s_yl38_hw_connected = true;
    s_yl38_last_read_ms = esp_timer_get_time() / 1000;
    ESP_LOGI(TAG, "YL-38 ready");

    bool last_flame   = false;
    bool blink        = false;
    int  stable_count = 0;

    while (1) {
        ret = yl38_read(&s_yl38_data);
        if (ret == ESP_OK) {
            s_yl38_last_read_ms = esp_timer_get_time() / 1000;

            bool current_flame = s_yl38_data.flame_detected;

            ESP_LOGI(TAG, "YL-38: Raw=%d Base=%d Volt=%.2fV Level=%s Detected=%s",
                     s_yl38_data.raw_value,
                     s_yl38_data.baseline_raw,
                     s_yl38_data.voltage,
                     yl38_get_level_string(s_yl38_data.flame_level),
                     current_flame ? "YES" : "NO");

            if (current_flame != last_flame) {
                stable_count++;
                if (stable_count >= 2) {
                    if (current_flame) {
                        ESP_LOGW(TAG, "!!! FLAME DETECTED !!! Level: %s",
                                 yl38_get_level_string(s_yl38_data.flame_level));
                        led_set_flame_alert(true, s_yl38_data.flame_level);
                    } else {
                        ESP_LOGI(TAG, "Flame cleared, turning off LED");
                        led_set_flame_alert(false, YL38_NO_FLAME);
                    }
                    last_flame   = current_flame;
                    stable_count = 0;
                }
            } else {
                stable_count = 0;
            }

            if (current_flame) {
                blink = !blink;
                int intensity = (s_yl38_data.flame_level == YL38_FLAME_STRONG) ? 255 : 150;
                int g_val = (s_yl38_data.flame_level == YL38_FLAME_WEAK)   ? 255 :
                            (s_yl38_data.flame_level == YL38_FLAME_MEDIUM) ? 50  : 0;
                led_set_color_with_priority(
                    blink ? intensity : intensity / 2, g_val, 0, LED_PRIO_FLAME);
            }
        } else {
            ESP_LOGE(TAG, "YL-38 read failed");
        }
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}

static void sensor_sht40_task(void *pvParameters)
{
    esp_err_t ret = sht40_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "SHT40 init failed: %s", esp_err_to_name(ret));
        vTaskDelete(NULL);
        return;
    }

    uint32_t serial = 0;
    if (sht40_read_serial(&serial) == ESP_OK) {
        ESP_LOGI(TAG, "SHT40 Serial: 0x%08lX", (unsigned long)serial);
    }

    s_sht40_initialized = true;
    ESP_LOGI(TAG, "SHT40 ready");

    while (1) {
        ret = sht40_read(&s_sht40_data, SHT40_PRECISION_HIGH);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "SHT40 read failed: %s", esp_err_to_name(ret));
            s_sht40_data.valid = false;
            vTaskDelay(pdMS_TO_TICKS(3000));
            continue;
        }
        ESP_LOGI(TAG, "SHT40: Temp=%.2f°C Hum=%.2f%%RH",
                 sensor_sht40_get_temperature(),
                 sensor_sht40_get_humidity());
        vTaskDelay(pdMS_TO_TICKS(5000));
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

    int msg_id = esp_mqtt_client_publish(
        client, TOPIC_STATUS, json, 0, 1, 0);
    ESP_LOGI(TAG, "Published status, msg_id=%d", msg_id);
    free(json);
}

void mqtt_publish_gas_alert(esp_mqtt_client_handle_t client, float ppm, bool alert)
{
    char *json = json_build_gas_alert(ppm, alert);
    if (json == NULL) return;

    int msg_id = esp_mqtt_client_publish(
        client, TOPIC_GAS_ALERT, json, 0, 2, 0);
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
            if (s_mq4_hw_connected) {
                ESP_LOGW(TAG, "Manual MQ-4 calibration triggered via MQTT");
                mq4_calibrate(&s_mq4_data);
            } else {
                ESP_LOGW(TAG, "MQ-4 not connected, skip calibration");
            }
            mqtt_publish_status(client);
        }
        else if (strcmp(target_str, "mq2_calibrate") == 0) {
            if (s_mq2_hw_connected) {
                ESP_LOGW(TAG, "Manual MQ-2 calibration triggered via MQTT");
                mq2_calibrate(&s_mq2_data);
            } else {
                ESP_LOGW(TAG, "MQ-2 not connected, skip calibration");
            }
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
        .broker.address.uri = MQTT_BROKER_URL,
    };

    esp_mqtt_client_handle_t client = esp_mqtt_client_init(&mqtt_cfg);
    esp_mqtt_client_register_event(client, ESP_EVENT_ANY_ID,
                                   mqtt_event_handler, NULL);
    esp_mqtt_client_start(client);

    xTaskCreate(sensor_dht11_task,   "dht11_task",   4096, NULL,   2, NULL);
    xTaskCreate(sensor_sht40_task,   "sht40_task",   4096, NULL,   2, NULL);
    xTaskCreate(sensor_mq2_task,     "mq2_task",     4096, NULL,   3, NULL);
    xTaskCreate(sensor_mq4_task,     "mq4_task",     4096, NULL,   3, NULL);
    xTaskCreate(sensor_yl38_task,    "yl38_task",    4096, NULL,   4, NULL);
    xTaskCreate(status_publish_task, "status_task",  4096, client, 5, NULL);
}