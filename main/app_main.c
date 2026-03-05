/* MQTT Smart Home Example for YD-ESP32-S3-N16R8 (V1.4)
   Features:
   - SNTP time synchronization
   - RGB LED control on GPIO48 (WS2812B)
   - DHT11 temperature & humidity sensor
   - MQ-4 methane gas sensor
   - YL-38 flame sensor
   - JSON format communication
*/

#include <stdio.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <time.h>
#include <sys/time.h>
#include "esp_wifi.h"
#include "esp_system.h"
#include "nvs_flash.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "protocol_examples_common.h"
#include "esp_sntp.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "freertos/queue.h"

#include "lwip/sockets.h"
#include "lwip/dns.h"
#include "lwip/netdb.h"

#include "esp_log.h"
#include "mqtt_client.h"
#include "cJSON.h"
#include "driver/gpio.h"
#include "led_strip.h"
#include "dht11.h"
#include "mq4.h"
#include "yl38.h"

static const char *TAG = "smart_home_s3";

// ==================== 硬件配置 ====================
#define LED_STRIP_GPIO      GPIO_NUM_48
#define LED_STRIP_NUM_LEDS  1

// ==================== MQTT 主题配置 ====================
#define TOPIC_STATUS        "smart_home/s3/s3_001/status"
#define TOPIC_CONTROL       "smart_home/s3/s3_001/control"
#define TOPIC_GAS_ALERT     "smart_home/s3/s3_001/gas_alert"
#define TOPIC_FLAME_STATUS  "smart_home/s3/s3_001/flame"

// ==================== 设备信息 ====================
#define DEVICE_ID           "s3_001"
#define DEVICE_TYPE         "s3"

// ==================== SNTP 配置 ====================
#define SNTP_SERVER         "pool.ntp.org"
#define TIMEZONE            "CST-8"

// ==================== 全局变量 ====================
static dht11_data_t g_dht11_data = {0};
static bool  g_time_synced = false;
static mq4_data_t g_mq4_data = {0};
static bool g_mq4_initialized = false;
static bool g_mq4_alert_active = false;  // MQ-4 报警状态（基于ppm）
static yl38_data_t g_yl38_data = {0};
static bool g_yl38_initialized = false;
static SemaphoreHandle_t g_led_mutex = NULL;
static volatile int g_led_priority = 0;

// LED 优先级定义
#define LED_PRIO_NONE   0
#define LED_PRIO_FLAME  1
#define LED_PRIO_GAS    2

// LED 状态
static int   g_led_r = 0;
static int   g_led_g = 0;
static int   g_led_b = 0;
static int   g_led_brightness = 0;
static bool g_led_forced_by_gas = false;

static led_strip_handle_t g_led_strip = NULL;

// ==================== 函数声明 ====================
static void initialize_sntp(void);
static void obtain_time(void);
static void led_strip_init(void);
static void set_led_color(int r, int g, int b);
static void set_led_state(int on);
static void restore_led_normal(void);
static char* build_status_json(void);
static void sensor_read_task(void *pvParameters);
static void status_publish_task(void *pvParameters);
static void parse_control_json(const char *json_str, esp_mqtt_client_handle_t client);
static void publish_status(esp_mqtt_client_handle_t client);
static void publish_gas_alert(esp_mqtt_client_handle_t client, float ppm, bool alert);
static void mq4_read_task(void *pvParameters);
static void yl38_read_task(void *pvParameters);
static bool set_led_color_with_priority(int r, int g, int b, int priority);
static void set_led_gas_alert(bool alert);
static void set_led_flame_alert(bool alert, yl38_flame_level_t level);

// ==================== LED 控制函数 ====================

static void led_strip_init(void)
{
    led_strip_config_t strip_config = {
        .strip_gpio_num = LED_STRIP_GPIO,
        .max_leds = LED_STRIP_NUM_LEDS,
    };

    led_strip_rmt_config_t rmt_config = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = 10 * 1000 * 1000,
        .flags.with_dma = false,
    };

    ESP_ERROR_CHECK(led_strip_new_rmt_device(&strip_config, &rmt_config, &g_led_strip));
    
    set_led_color(0, 0, 0);
    
    ESP_LOGI(TAG, "WS2812B LED initialized on GPIO%d", LED_STRIP_GPIO);
}

static bool set_led_color_with_priority(int r, int g, int b, int priority)
{
    if (g_led_strip == NULL) return false;
    
    xSemaphoreTake(g_led_mutex, portMAX_DELAY);
    
    // 检查优先级：高优先级可以抢占，低优先级不能覆盖高优先级
    if (priority < g_led_priority && g_led_priority != LED_PRIO_NONE) {
        ESP_LOGD(TAG, "LED control rejected, current priority %d > requested %d", 
                 g_led_priority, priority);
        xSemaphoreGive(g_led_mutex);
        return false;
    }
    
    // 执行颜色设置
    r = (r < 0) ? 0 : (r > 255) ? 255 : r;
    g = (g < 0) ? 0 : (g > 255) ? 255 : g;
    b = (b < 0) ? 0 : (b > 255) ? 255 : b;
    
    g_led_r = r;
    g_led_g = g;
    g_led_b = b;
    g_led_brightness = (int)(0.299 * r + 0.587 * g + 0.114 * b);
    g_led_priority = priority;
    
    if (priority == LED_PRIO_NONE) {
        g_led_forced_by_gas = false;
    } else if (priority == LED_PRIO_GAS) {
        g_led_forced_by_gas = true;
    }
    
    ESP_ERROR_CHECK(led_strip_set_pixel(g_led_strip, 0, r, g, b));
    ESP_ERROR_CHECK(led_strip_refresh(g_led_strip));
    
    xSemaphoreGive(g_led_mutex);
    
    ESP_LOGI(TAG, "LED set to R:%d G:%d B:%d (priority:%d)", r, g, b, priority);
    return true;
}

static void set_led_color(int r, int g, int b)
{
    set_led_color_with_priority(r, g, b, LED_PRIO_NONE);
}

static void set_led_state(int on)
{
    if (on) {
        set_led_color(255, 255, 255);
    } else {
        set_led_color(0, 0, 0);
    }
}

static void set_led_gas_alert(bool alert)
{
    if (alert) {
        set_led_color_with_priority(255, 0, 0, LED_PRIO_GAS);
    } else {
        xSemaphoreTake(g_led_mutex, portMAX_DELAY);
        if (g_led_priority == LED_PRIO_GAS) {
            g_led_priority = LED_PRIO_NONE;
            g_led_forced_by_gas = false;
            set_led_color_with_priority(0, 0, 0, LED_PRIO_NONE);
        }
        xSemaphoreGive(g_led_mutex);
    }
}

static void set_led_flame_alert(bool alert, yl38_flame_level_t level)
{
    if (alert) {
        xSemaphoreTake(g_led_mutex, portMAX_DELAY);
        if (g_led_priority >= LED_PRIO_GAS) {
            ESP_LOGD(TAG, "Flame alert suppressed by gas alert");
            xSemaphoreGive(g_led_mutex);
            return;
        }
        xSemaphoreGive(g_led_mutex);
        
        int r = 255, g = (level == YL38_FLAME_WEAK) ? 255 : 
                       (level == YL38_FLAME_MEDIUM) ? 100 : 0;
        set_led_color_with_priority(r, g, 0, LED_PRIO_FLAME);
    } else {
        xSemaphoreTake(g_led_mutex, portMAX_DELAY);
        if (g_led_priority == LED_PRIO_FLAME) {
            g_led_priority = LED_PRIO_NONE;
            set_led_color_with_priority(0, 0, 0, LED_PRIO_NONE);
        }
        xSemaphoreGive(g_led_mutex);
    }
}

static void restore_led_normal(void)
{
    g_led_forced_by_gas = false;
    set_led_color(0, 0, 0);
    ESP_LOGI(TAG, "LED restored to normal state");
}

// ==================== 时间同步函数 ====================

static void time_sync_notification_cb(struct timeval *tv)
{
    ESP_LOGI(TAG, "Time synchronized");
    g_time_synced = true;
}

static void initialize_sntp(void)
{
    ESP_LOGI(TAG, "Initializing SNTP");
    
    esp_sntp_setoperatingmode(ESP_SNTP_OPMODE_POLL);
    esp_sntp_setservername(0, SNTP_SERVER);
    esp_sntp_set_time_sync_notification_cb(time_sync_notification_cb);
    esp_sntp_set_sync_mode(SNTP_SYNC_MODE_SMOOTH);
    esp_sntp_init();
}

static void obtain_time(void)
{
    initialize_sntp();

    int retry = 0;
    const int retry_count = 10;
    while (esp_sntp_get_sync_status() == SNTP_SYNC_STATUS_RESET && ++retry < retry_count) {
        ESP_LOGI(TAG, "Waiting for time sync... (%d/%d)", retry, retry_count);
        vTaskDelay(2000 / portTICK_PERIOD_MS);
    }
    
    if (retry >= retry_count) {
        ESP_LOGW(TAG, "Failed to sync time");
    } else {
        setenv("TZ", TIMEZONE, 1);
        tzset();
        
        time_t now = time(NULL);
        struct tm timeinfo;
        localtime_r(&now, &timeinfo);
        ESP_LOGI(TAG, "Current time: %s", asctime(&timeinfo));
    }
}

static time_t get_timestamp(void)
{
    return time(NULL);
}

// ==================== JSON 处理 ====================

static char* build_status_json(void)
{
    cJSON *root = cJSON_CreateObject();
    cJSON *data = cJSON_CreateObject();
    
    if (root == NULL || data == NULL) {
        ESP_LOGE(TAG, "Failed to create JSON");
        return NULL;
    }
    
    time_t now = get_timestamp();
    struct tm timeinfo;
    localtime_r(&now, &timeinfo);
    
    char time_str[64];
    strftime(time_str, sizeof(time_str), "%Y-%m-%dT%H:%M:%S", &timeinfo);
    
    int tz_offset = 8;
    char tz_str[10];
    snprintf(tz_str, sizeof(tz_str), "+%02d:00", tz_offset);
    strncat(time_str, tz_str, sizeof(time_str) - strlen(time_str) - 1);
    
    float temperature = dht11_get_temperature(&g_dht11_data);
    float humidity = dht11_get_humidity(&g_dht11_data);
    
    cJSON_AddStringToObject(root, "device_id", DEVICE_ID);
    cJSON_AddStringToObject(root, "type", DEVICE_TYPE);
    cJSON_AddNumberToObject(root, "timestamp", (double)now);
    cJSON_AddStringToObject(root, "datetime", time_str);
    
    if (g_dht11_data.valid && temperature > -100) {
        cJSON_AddNumberToObject(data, "temperature", temperature);
        cJSON_AddNumberToObject(data, "humidity", (int)humidity);
    } else {
        cJSON_AddNumberToObject(data, "temperature", 25.0);
        cJSON_AddNumberToObject(data, "humidity", 50);
        cJSON_AddStringToObject(data, "dht11_status", "offline");
    }

    // MQ-4 气体数据 - 修复：使用 g_mq4_alert_active 而不是 digital_alert
    cJSON *gas_obj = cJSON_CreateObject();
    if (g_mq4_initialized) {
        cJSON_AddNumberToObject(gas_obj, "ppm", g_mq4_data.ppm);
        cJSON_AddNumberToObject(gas_obj, "raw_adc", g_mq4_data.raw_value);
        cJSON_AddBoolToObject(gas_obj, "alert", g_mq4_alert_active);  // 修复：使用 ppm 判断的状态
        cJSON_AddBoolToObject(gas_obj, "calibrated", g_mq4_data.calibrated);
        cJSON_AddStringToObject(gas_obj, "status", "active");
    } else {
        cJSON_AddStringToObject(gas_obj, "status", "initializing");
        cJSON_AddBoolToObject(gas_obj, "calibrated", false);
    }
    cJSON_AddItemToObject(data, "gas", gas_obj);
    
    // YL-38 火焰数据
    cJSON *flame_obj = cJSON_CreateObject();
    if (g_yl38_initialized) {
        cJSON_AddNumberToObject(flame_obj, "raw", g_yl38_data.raw_value);
        cJSON_AddNumberToObject(flame_obj, "voltage", g_yl38_data.voltage);
        cJSON_AddStringToObject(flame_obj, "level", yl38_get_level_string(g_yl38_data.flame_level));
        cJSON_AddBoolToObject(flame_obj, "detected", g_yl38_data.flame_detected);
        cJSON_AddNumberToObject(flame_obj, "intensity_percent", g_yl38_data.intensity_percent);
        cJSON_AddBoolToObject(flame_obj, "digital_trigger", g_yl38_data.digital_detected);
    } else {
        cJSON_AddStringToObject(flame_obj, "status", "offline");
    }
    cJSON_AddItemToObject(data, "flame", flame_obj);

    cJSON *led_obj = cJSON_CreateObject();
    cJSON_AddNumberToObject(led_obj, "state", g_led_brightness > 0 ? 1 : 0);
    cJSON_AddNumberToObject(led_obj, "r", g_led_r);
    cJSON_AddNumberToObject(led_obj, "g", g_led_g);
    cJSON_AddNumberToObject(led_obj, "b", g_led_b);
    cJSON_AddNumberToObject(led_obj, "brightness", g_led_brightness);
    cJSON_AddBoolToObject(led_obj, "forced_alert", g_led_forced_by_gas);
    cJSON_AddItemToObject(data, "led", led_obj);
    
    cJSON_AddItemToObject(root, "data", data);
    
    char *json_str = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    
    return json_str;
}

// ==================== DHT11 传感器任务 ====================

static void sensor_read_task(void *pvParameters)
{
    ESP_ERROR_CHECK(dht11_init());
    
    vTaskDelay(2000 / portTICK_PERIOD_MS);
    
    while (1) {
        esp_err_t ret = dht11_read(&g_dht11_data);
        
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "DHT11 read failed: %s", esp_err_to_name(ret));
            g_dht11_data.valid = false;
            vTaskDelay(3000 / portTICK_PERIOD_MS);
            continue;
        }
        
        ESP_LOGI(TAG, "DHT11: Temp=%.1f°C, Hum=%.1f%%",
                 dht11_get_temperature(&g_dht11_data),
                 dht11_get_humidity(&g_dht11_data));
        
        vTaskDelay(5000 / portTICK_PERIOD_MS);
    }
}

// ==================== MQ-4 任务 ====================
static void mq4_read_task(void *pvParameters)
{
    esp_err_t ret = mq4_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "MQ-4 init failed: %s", esp_err_to_name(ret));
        vTaskDelete(NULL);
        return;
    }
    
    ESP_LOGI(TAG, "MQ-4 warming up, waiting 30 seconds...");
    for (int i = 0; i < 30; i++) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        if (i % 10 == 0) {
            ESP_LOGI(TAG, "MQ-4 warmup: %d/30s", i);
        }
    }
    
    ret = mq4_calibrate(&g_mq4_data);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "MQ-4 calibration failed: %s", esp_err_to_name(ret));
    }
    
    g_mq4_initialized = true;
    ESP_LOGI(TAG, "MQ-4 initialization complete");
    
    bool last_alert_state = false;
    bool blink_state = false;  // 移到循环外，保持闪烁状态
    
    while (1) {
        ret = mq4_read(&g_mq4_data);
        
        if (ret == ESP_OK) {
            bool current_alert = (g_mq4_data.ppm > MQ4_DEFAULT_THRESHOLD_PPM);
            
            ESP_LOGI(TAG, "MQ-4: PPM=%.1f, Raw=%d, Alert=%s",
                     g_mq4_data.ppm, g_mq4_data.raw_value,
                     current_alert ? "YES" : "NO");
            
            // 状态变化时处理
            if (current_alert != last_alert_state) {
                if (current_alert) {
                    ESP_LOGW(TAG, "!!! GAS ALERT !!! PPM=%.1f", g_mq4_data.ppm);
                    set_led_gas_alert(true);
                    g_mq4_alert_active = true;
                } else {
                    ESP_LOGI(TAG, "Gas alert cleared, PPM=%.1f", g_mq4_data.ppm);
                    set_led_gas_alert(false);
                    g_mq4_alert_active = false;
                }
                last_alert_state = current_alert;
            }
            
            // 持续报警时闪烁（只在状态为报警时执行）
            if (current_alert) {
                blink_state = !blink_state;
                int intensity = blink_state ? 255 : 100;
                set_led_color_with_priority(intensity, 0, 0, LED_PRIO_GAS);
            }
        } else {
            ESP_LOGE(TAG, "MQ-4 read failed");
            g_mq4_data.calibrated = false;
        }
        
        vTaskDelay(2000 / portTICK_PERIOD_MS);
    }
}

// ==================== YL-38 任务 ====================
static void yl38_read_task(void *pvParameters)
{
    esp_err_t ret = yl38_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "YL-38 init failed: %s", esp_err_to_name(ret));
        vTaskDelete(NULL);
        return;
    }
    
    // 执行基线校准（必须在无火焰环境下！）
    ret = yl38_calibrate_baseline();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "YL-38 calibration failed: %s", esp_err_to_name(ret));
        // 继续使用默认阈值
    }
    
    g_yl38_initialized = true;
    ESP_LOGI(TAG, "YL-38 ready");
    
    bool last_flame_state = false;
    bool blink = false;
    
    while (1) {
        ret = yl38_read(&g_yl38_data);
        
        if (ret == ESP_OK) {
            bool current_flame = g_yl38_data.flame_detected;
            
            ESP_LOGI(TAG, "YL-38: Raw=%d, Base=%d, Volt=%.2fV, Level=%s, Detected=%s",
                     g_yl38_data.raw_value,
                     g_yl38_data.baseline_raw,
                     g_yl38_data.voltage,
                     yl38_get_level_string(g_yl38_data.flame_level),
                     current_flame ? "YES" : "NO");
            
            // 状态变化处理
            if (current_flame != last_flame_state) {
                if (current_flame) {
                    ESP_LOGW(TAG, "!!! FLAME DETECTED !!! Level: %s", 
                             yl38_get_level_string(g_yl38_data.flame_level));
                    set_led_flame_alert(true, g_yl38_data.flame_level);
                } else {
                    ESP_LOGI(TAG, "Flame cleared");
                    set_led_flame_alert(false, YL38_NO_FLAME);
                }
                last_flame_state = current_flame;
            }
            
            // 持续火焰闪烁
            if (current_flame) {
                blink = !blink;
                int intensity = g_yl38_data.flame_level == YL38_FLAME_STRONG ? 255 : 150;
                int g_val = g_yl38_data.flame_level == YL38_FLAME_WEAK ? 255 : 
                           (g_yl38_data.flame_level == YL38_FLAME_MEDIUM ? 50 : 0);
                int r_val = blink ? intensity : intensity/2;
                
                set_led_color_with_priority(r_val, g_val, 0, LED_PRIO_FLAME);
            }
        } else {
            ESP_LOGE(TAG, "YL-38 read failed");
        }
        
        vTaskDelay(500 / portTICK_PERIOD_MS);
    }
}

// ==================== MQTT 状态上报任务 ====================

static void status_publish_task(void *pvParameters)
{
    esp_mqtt_client_handle_t client = (esp_mqtt_client_handle_t)pvParameters;
    
    vTaskDelay(5000 / portTICK_PERIOD_MS);
    
    while (1) {
        publish_status(client);
        vTaskDelay(30000 / portTICK_PERIOD_MS);
    }
}

// ==================== 报警推送函数 ====================

static void publish_gas_alert(esp_mqtt_client_handle_t client, float ppm, bool alert)
{
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) return;
    
    cJSON_AddStringToObject(root, "device_id", DEVICE_ID);
    cJSON_AddNumberToObject(root, "timestamp", (double)get_timestamp());
    cJSON_AddNumberToObject(root, "ppm", ppm);
    cJSON_AddBoolToObject(root, "alert", alert);
    cJSON_AddStringToObject(root, "type", "gas_alert");
    
    char *json_str = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    
    if (json_str) {
        int msg_id = esp_mqtt_client_publish(client, TOPIC_GAS_ALERT, json_str, 0, 2, 0);
        ESP_LOGW(TAG, "Published gas alert, msg_id=%d", msg_id);
        free(json_str);
    }
}

// ==================== 控制命令处理 ====================

static void parse_control_json(const char *json_str, esp_mqtt_client_handle_t client)
{
    cJSON *root = cJSON_Parse(json_str);
    if (root == NULL) {
        ESP_LOGE(TAG, "JSON parse error");
        return;
    }
    
    cJSON *cmd = cJSON_GetObjectItemCaseSensitive(root, "cmd");
    cJSON *target = cJSON_GetObjectItemCaseSensitive(root, "target");
    
    if (!cJSON_IsString(cmd) || !cJSON_IsString(target)) {
        ESP_LOGE(TAG, "Invalid command format");
        cJSON_Delete(root);
        return;
    }
    
    const char *cmd_str = cmd->valuestring;
    const char *target_str = target->valuestring;
    
    ESP_LOGI(TAG, "Command: %s, Target: %s", cmd_str, target_str);
    
    if (strcmp(cmd_str, "set") == 0) {
        if (strcmp(target_str, "led") == 0) {
            cJSON *value = cJSON_GetObjectItemCaseSensitive(root, "value");
            if (cJSON_IsNumber(value)) {
                set_led_state(value->valueint);
                publish_status(client);
            }
        }
        else if (strcmp(target_str, "rgb") == 0) {
            cJSON *r = cJSON_GetObjectItemCaseSensitive(root, "r");
            cJSON *g = cJSON_GetObjectItemCaseSensitive(root, "g");
            cJSON *b = cJSON_GetObjectItemCaseSensitive(root, "b");
            
            if (cJSON_IsNumber(r) && cJSON_IsNumber(g) && cJSON_IsNumber(b)) {
                set_led_color(r->valueint, g->valueint, b->valueint);
                publish_status(client);
            }
        }
        else if (strcmp(target_str, "mq4_calibrate") == 0) {
            ESP_LOGW(TAG, "Manual MQ-4 calibration triggered via MQTT");
            mq4_calibrate(&g_mq4_data);
            publish_status(client);
        }
    } else if (strcmp(cmd_str, "get") == 0) {
        if (strcmp(target_str, "status") == 0) {
            publish_status(client);
        }
    }
    
    cJSON_Delete(root);
}

// ==================== MQTT 函数 ====================

static void publish_status(esp_mqtt_client_handle_t client)
{
    char *status_json = build_status_json();
    if (status_json == NULL) return;
    
    int msg_id = esp_mqtt_client_publish(client, TOPIC_STATUS, status_json, 0, 1, 0);
    ESP_LOGI(TAG, "Published status, msg_id=%d", msg_id);
    
    free(status_json);
}

static void mqtt_event_handler(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data)
{
    esp_mqtt_event_handle_t event = event_data;
    esp_mqtt_client_handle_t client = event->client;
    
    switch ((esp_mqtt_event_id_t)event_id) {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "MQTT connected");
        esp_mqtt_client_subscribe(client, TOPIC_CONTROL, 1);
        publish_status(client);
        break;
        
    case MQTT_EVENT_DATA:
        ESP_LOGI(TAG, "MQTT data received");
        
        char *topic = malloc(event->topic_len + 1);
        char *data = malloc(event->data_len + 1);
        
        if (topic && data) {
            memcpy(topic, event->topic, event->topic_len);
            topic[event->topic_len] = '\0';
            memcpy(data, event->data, event->data_len);
            data[event->data_len] = '\0';
            
            ESP_LOGI(TAG, "Topic: %s, Data: %s", topic, data);
            
            if (strncmp(topic, TOPIC_CONTROL, strlen(TOPIC_CONTROL)) == 0) {
                parse_control_json(data, client);
            }
            
            free(topic);
            free(data);
        }
        break;
        
    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGI(TAG, "MQTT disconnected");
        break;
        
    default:
        break;
    }
}

// ==================== 主函数 ====================

static void mqtt_app_start(void)
{
    esp_mqtt_client_config_t mqtt_cfg = {
        .broker.address.uri = CONFIG_BROKER_URL,
    };

    esp_mqtt_client_handle_t client = esp_mqtt_client_init(&mqtt_cfg);
    esp_mqtt_client_register_event(client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);
    esp_mqtt_client_start(client);
    
    // 修复：优先级调整为 3-5 范围（避免超过默认配置）
    // 优先级：MQTT(5) > 火焰(4) > 气体(3) > 温湿度(2)
    xTaskCreate(sensor_read_task, "dht11_task", 4096, NULL, 2, NULL);
    xTaskCreate(mq4_read_task, "mq4_task", 4096, NULL, 3, NULL);
    xTaskCreate(yl38_read_task, "flame_task", 4096, NULL, 4, NULL);
    xTaskCreate(status_publish_task, "status_task", 4096, client, 5, NULL);
}

void app_main(void)
{
    ESP_LOGI(TAG, "Starting Smart Home S3 Device...");
    
    esp_log_level_set("*", ESP_LOG_INFO);
    esp_log_level_set(TAG, ESP_LOG_DEBUG);
    esp_log_level_set("mq4", ESP_LOG_DEBUG);
    esp_log_level_set("yl38", ESP_LOG_DEBUG);

    g_led_mutex = xSemaphoreCreateMutex();
    if (g_led_mutex == NULL) {
        ESP_LOGE(TAG, "Failed to create LED mutex");
        return;
    }
    
    ESP_ERROR_CHECK(nvs_flash_init());
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    
    led_strip_init();
    
    ESP_ERROR_CHECK(example_connect());
    
    obtain_time();

    mqtt_app_start();
}