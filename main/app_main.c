/* MQTT Smart Home Example for YD-ESP32-S3-N16R8 (V1.4)
   Features:
   - SNTP time synchronization
   - RGB LED control on GPIO48 (WS2812B)
   - DHT11 temperature & humidity sensor
   - MQ-4 methane gas sensor
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
#define TOPIC_GAS_ALERT     "smart_home/s3/s3_001/gas_alert"  // 新增：气体报警专用主题
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
static bool g_mq4_initialized = false;  // 新增：MQ-4 初始化标志
static yl38_data_t g_yl38_data = {0};
static bool g_yl38_initialized = false;

// LED 状态
static int   g_led_r = 0;
static int   g_led_g = 0;
static int   g_led_b = 0;
static int   g_led_brightness = 0;
static bool g_led_forced_by_gas = false;  // 新增：LED 是否被气体报警强制控制

static led_strip_handle_t g_led_strip = NULL;

// ==================== 函数声明 ====================
static void initialize_sntp(void);
static void obtain_time(void);
static void led_strip_init(void);
static void set_led_color(int r, int g, int b);
static void set_led_state(int on);
static void restore_led_normal(void);  // 新增：恢复正常 LED 状态
static char* build_status_json(void);
static void sensor_read_task(void *pvParameters);
static void status_publish_task(void *pvParameters);
static void parse_control_json(const char *json_str, esp_mqtt_client_handle_t client);
static void publish_status(esp_mqtt_client_handle_t client);
static void publish_gas_alert(esp_mqtt_client_handle_t client, float ppm, bool alert);  // 新增
static void mq4_read_task(void *pvParameters);
static void yl38_read_task(void *pvParameters);  // 新增：火焰传感器任务


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

static void set_led_color(int r, int g, int b)
{
    if (g_led_strip == NULL) return;
    
    r = (r < 0) ? 0 : (r > 255) ? 255 : r;
    g = (g < 0) ? 0 : (g > 255) ? 255 : g;
    b = (b < 0) ? 0 : (b > 255) ? 255 : b;
    
    g_led_r = r;
    g_led_g = g;
    g_led_b = b;
    
    g_led_brightness = (int)(0.299 * r + 0.587 * g + 0.114 * b);
    g_led_forced_by_gas = false;  // 手动设置颜色时解除强制标志
    
    ESP_ERROR_CHECK(led_strip_set_pixel(g_led_strip, 0, r, g, b));
    ESP_ERROR_CHECK(led_strip_refresh(g_led_strip));
    
    ESP_LOGI(TAG, "LED color set to R:%d G:%d B:%d", r, g, b);
}

static void set_led_state(int on)
{
    if (on) {
        set_led_color(255, 255, 255);
    } else {
        set_led_color(0, 0, 0);
    }
}

// 新增：设置报警 LED（带强制标志）
static void set_led_gas_alert(bool alert)
{
    if (g_led_strip == NULL) return;
    
    if (alert) {
        g_led_forced_by_gas = true;
        g_led_r = 255;
        g_led_g = 0;
        g_led_b = 0;
        g_led_brightness = 255;
        
        ESP_ERROR_CHECK(led_strip_set_pixel(g_led_strip, 0, 255, 0, 0));
        ESP_ERROR_CHECK(led_strip_refresh(g_led_strip));
        ESP_LOGW(TAG, "LED set to ALERT RED (gas detected)");
    } else if (g_led_forced_by_gas) {
        // 只有之前是被气体强制控制的才恢复
        restore_led_normal();
    }
}

// 新增：恢复正常 LED 状态（关闭或默认颜色）
static void restore_led_normal(void)
{
    g_led_forced_by_gas = false;
    set_led_color(0, 0, 0);  // 默认关闭，或改为其他默认状态
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
    // 安全拼接，避免缓冲区溢出
    strncat(time_str, tz_str, sizeof(time_str) - strlen(time_str) - 1);
    
    // 获取 DHT11 数据
    float temperature = dht11_get_temperature(&g_dht11_data);
    float humidity = dht11_get_humidity(&g_dht11_data);
    
    cJSON_AddStringToObject(root, "device_id", DEVICE_ID);
    cJSON_AddStringToObject(root, "type", DEVICE_TYPE);
    cJSON_AddNumberToObject(root, "timestamp", (double)now);
    cJSON_AddStringToObject(root, "datetime", time_str);
    
    // 如果 DHT11 数据有效，使用真实数据；否则使用默认值
    if (g_dht11_data.valid && temperature > -100) {
        cJSON_AddNumberToObject(data, "temperature", temperature);
        cJSON_AddNumberToObject(data, "humidity", (int)humidity);
    } else {
        cJSON_AddNumberToObject(data, "temperature", 25.0);
        cJSON_AddNumberToObject(data, "humidity", 50);
        cJSON_AddStringToObject(data, "dht11_status", "offline");
    }

    // MQ-4 气体数据（检查是否已初始化）
    cJSON *gas_obj = cJSON_CreateObject();
    if (g_mq4_initialized) {
        cJSON_AddNumberToObject(gas_obj, "ppm", g_mq4_data.ppm);
        cJSON_AddNumberToObject(gas_obj, "raw_adc", g_mq4_data.raw_value);
        cJSON_AddBoolToObject(gas_obj, "alert", g_mq4_data.digital_alert);
        cJSON_AddBoolToObject(gas_obj, "calibrated", g_mq4_data.calibrated);
        cJSON_AddStringToObject(gas_obj, "status", "active");
    } else {
        cJSON_AddStringToObject(gas_obj, "status", "initializing");
        cJSON_AddBoolToObject(gas_obj, "calibrated", false);
    }
    cJSON_AddItemToObject(data, "gas", gas_obj);
    
    // 新增：YL-38 火焰数据
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
    cJSON_AddBoolToObject(led_obj, "forced_alert", g_led_forced_by_gas);  // 新增：报警状态标志
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
    
    // 首次读取前等待2秒（DHT11上电稳定时间）
    vTaskDelay(2000 / portTICK_PERIOD_MS);
    
    while (1) {
        esp_err_t ret = dht11_read(&g_dht11_data);
        
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "DHT11 read failed: %s", esp_err_to_name(ret));
            g_dht11_data.valid = false;
            // 失败时延长等待时间，让传感器复位
            vTaskDelay(3000 / portTICK_PERIOD_MS);
            continue;
        }
        
        ESP_LOGI(TAG, "DHT11: Temp=%.1f°C, Hum=%.1f%%",
                 dht11_get_temperature(&g_dht11_data),
                 dht11_get_humidity(&g_dht11_data));
        
        // DHT11 最小采样间隔为2秒，这里每5秒读取一次
        vTaskDelay(5000 / portTICK_PERIOD_MS);
    }
}

// ==================== MQ-4 任务（优化版） ====================
static void mq4_read_task(void *pvParameters)
{
    // 初始化 MQ-4
    esp_err_t ret = mq4_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "MQ-4 init failed: %s", esp_err_to_name(ret));
        vTaskDelete(NULL);  // 初始化失败，删除任务
        return;
    }
    
    // 等待传感器预热（非常重要！MQ-4 需要 2-3 分钟预热）
    ESP_LOGI(TAG, "MQ-4 warming up, waiting 30 seconds...");
    // 分段延时，避免看门狗复位
    for (int i = 0; i < 30; i++) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        if (i % 10 == 0) {
            ESP_LOGI(TAG, "MQ-4 warmup: %d/30s", i);
        }
    }
    
    // 在清洁空气中校准（只需执行一次，可保存到 NVS）
    ret = mq4_calibrate(&g_mq4_data);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "MQ-4 calibration failed: %s", esp_err_to_name(ret));
        // 校准失败也继续运行，使用默认 RO 值
    }
    
    g_mq4_initialized = true;  // 标记初始化完成
    ESP_LOGI(TAG, "MQ-4 initialization complete, entering monitoring loop");
    
    bool last_alert_state = false;  // 记录上次报警状态，用于检测变化
    
    while (1) {
        ret = mq4_read(&g_mq4_data);
        
        if (ret == ESP_OK) {
            // bool current_alert = g_mq4_data.digital_alert || 
            //                     (g_mq4_data.ppm > MQ4_DEFAULT_THRESHOLD_PPM);
            bool current_alert =  (g_mq4_data.ppm > MQ4_DEFAULT_THRESHOLD_PPM);
            
            ESP_LOGI(TAG, "MQ-4: PPM=%.1f, Raw=%d, Alert=%s",
                     g_mq4_data.ppm, 
                     g_mq4_data.raw_value,
                     current_alert ? "YES" : "NO");
            
            // 报警状态变化时处理
            if (current_alert != last_alert_state) {
                if (current_alert) {
                    ESP_LOGW(TAG, "!!! GAS ALERT TRIGGERED !!! PPM=%.1f", g_mq4_data.ppm);
                    set_led_gas_alert(true);
                    // 可在这里添加 MQTT 报警推送
                } else {
                    ESP_LOGI(TAG, "Gas alert cleared, PPM normalized to %.1f", g_mq4_data.ppm);
                    set_led_gas_alert(false);
                }
                last_alert_state = current_alert;
            }
            
            // 持续报警时，每秒闪烁 LED（可选）
            if (current_alert) {
                // 简单的闪烁效果
                static bool blink_state = false;
                blink_state = !blink_state;
                if (blink_state) {
                    set_led_color(255, 0, 0);
                } else {
                    set_led_color(100, 0, 0);  // 暗红色
                }
            }
        } else {
            ESP_LOGE(TAG, "MQ-4 read failed: %s", esp_err_to_name(ret));
            g_mq4_data.calibrated = false;  // 标记数据无效
        }
        
        // MQ-4 读取间隔 2 秒
        vTaskDelay(2000 / portTICK_PERIOD_MS);
    }
}

static void yl38_read_task(void *pvParameters)
{
    esp_err_t ret = yl38_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "YL-38 init failed: %s", esp_err_to_name(ret));
        vTaskDelete(NULL);
        return;
    }
    
    g_yl38_initialized = true;
    ESP_LOGI(TAG, "YL-38 ready, starting flame monitoring");
    
    bool last_flame_state = false;
    
    while (1) {
        ret = yl38_read(&g_yl38_data);
        
        if (ret == ESP_OK) {
            ESP_LOGI(TAG, "YL-38: Raw=%d, Volt=%.2fV, Level=%s, Detected=%s",
                     g_yl38_data.raw_value,
                     g_yl38_data.voltage,
                     yl38_get_level_string(g_yl38_data.flame_level),
                     g_yl38_data.flame_detected ? "YES" : "NO");
            
            // 火焰状态变化时处理
            if (g_yl38_data.flame_detected != last_flame_state) {
                if (g_yl38_data.flame_detected) {
                    ESP_LOGW(TAG, "!!! FLAME DETECTED !!! Level: %s", 
                             yl38_get_level_string(g_yl38_data.flame_level));
                    
                    // 火焰报警：LED 变橙色/红色闪烁
                    // 如果 MQ-4 没有报警，显示橙色；如果都有，红色优先
                    if (!g_mq4_data.digital_alert && g_mq4_data.ppm < MQ4_DEFAULT_THRESHOLD_PPM) {
                        set_led_color(255, 100, 0);  // 橙色
                    }
                } else {
                    ESP_LOGI(TAG, "Flame cleared");
                    // 恢复 LED（如果 MQ-4 也没有报警）
                    if (!g_mq4_data.digital_alert && g_mq4_data.ppm < MQ4_DEFAULT_THRESHOLD_PPM) {
                        restore_led_normal();
                    }
                }
                last_flame_state = g_yl38_data.flame_detected;
            }
            
            // 持续火焰时闪烁
            if (g_yl38_data.flame_detected) {
                static bool blink = false;
                blink = !blink;
                int intensity = g_yl38_data.flame_level == YL38_FLAME_STRONG ? 255 : 150;
                set_led_color(blink ? intensity : intensity/2, 
                             g_yl38_data.flame_level == YL38_FLAME_STRONG ? 0 : 50, 
                             0);
            }
        } else {
            ESP_LOGE(TAG, "YL-38 read failed");
        }
        
        vTaskDelay(500 / portTICK_PERIOD_MS);  // 火焰检测需要更快响应，500ms
    }
}

// ==================== MQTT 状态上报任务 ====================

static void status_publish_task(void *pvParameters)
{
    esp_mqtt_client_handle_t client = (esp_mqtt_client_handle_t)pvParameters;
    
    // 等待 MQTT 连接和初始数据收集
    vTaskDelay(5000 / portTICK_PERIOD_MS);
    
    while (1) {
        // 上报当前状态（包含 DHT11 和 MQ-4 数据）
        publish_status(client);
        
        // 每 30 秒上报一次
        vTaskDelay(30000 / portTICK_PERIOD_MS);
    }
}

// 新增：发布气体报警（独立主题，便于紧急处理）
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
        int msg_id = esp_mqtt_client_publish(client, TOPIC_GAS_ALERT, json_str, 0, 2, 0);  // QoS 2 确保送达
        ESP_LOGW(TAG, "Published gas alert, msg_id=%d", msg_id);
        free(json_str);
    }
}

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
        // 新增：手动控制 MQ-4 校准（调试用途）
        else if (strcmp(target_str, "mq4_calibrate") == 0) {
            ESP_LOGW(TAG, "Manual MQ-4 calibration triggered via MQTT");
            // 注意：实际校准需要在清洁空气中执行
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
    
    // 四个任务，优先级：MQTT > 火焰 > 气体 > 温湿度
    xTaskCreate(sensor_read_task, "dht11_task", 4096, NULL, 3, NULL);  // 降低 DHT11 优先级
    xTaskCreate(mq4_read_task, "mq4_task", 4096, NULL, 4, NULL);        // MQ-4 中等优先级
    xTaskCreate(yl38_read_task, "flame_task", 4096, NULL, 5, NULL);  // 火焰优先级高
    xTaskCreate(status_publish_task, "status_task", 4096, client, 5, NULL);  // MQTT 最高
}

void app_main(void)
{
    ESP_LOGI(TAG, "Starting Smart Home S3 Device...");
    
    esp_log_level_set("*", ESP_LOG_INFO);
    esp_log_level_set(TAG, ESP_LOG_DEBUG);
    esp_log_level_set("mq4", ESP_LOG_DEBUG);  // 新增：查看 MQ-4 详细日志

    ESP_ERROR_CHECK(nvs_flash_init());
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    
    led_strip_init();
    
    ESP_ERROR_CHECK(example_connect());
    
    obtain_time();

    mqtt_app_start();
}