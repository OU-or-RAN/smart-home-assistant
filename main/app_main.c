/* MQTT Smart Home Example for YD-ESP32-S3-N16R8 (V1.4)
   Features:
   - SNTP time synchronization
   - RGB LED control on GPIO48 (WS2812B)
   - DHT11 temperature & humidity sensor
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

static const char *TAG = "smart_home_s3";

// ==================== 硬件配置 ====================
#define LED_STRIP_GPIO      GPIO_NUM_48
#define LED_STRIP_NUM_LEDS  1

// ==================== MQTT 主题配置 ====================
#define TOPIC_STATUS        "smart_home/s3/s3_001/status"
#define TOPIC_CONTROL       "smart_home/s3/s3_001/control"

// ==================== 设备信息 ====================
#define DEVICE_ID           "s3_001"
#define DEVICE_TYPE         "s3"

// ==================== SNTP 配置 ====================
#define SNTP_SERVER         "pool.ntp.org"
#define TIMEZONE            "CST-8"

// ==================== 全局变量 ====================
static dht11_data_t g_dht11_data = {0};
static bool  g_time_synced = false;

// LED 状态
static int   g_led_r = 0;
static int   g_led_g = 0;
static int   g_led_b = 0;
static int   g_led_brightness = 0;

static led_strip_handle_t g_led_strip = NULL;

// ==================== 函数声明 ====================
static void initialize_sntp(void);
static void obtain_time(void);
static void led_strip_init(void);
static void set_led_color(int r, int g, int b);
static void set_led_state(int on);
static char* build_status_json(void);
static void sensor_read_task(void *pvParameters);
static void status_publish_task(void *pvParameters);
static void parse_control_json(const char *json_str, esp_mqtt_client_handle_t client);
static void publish_status(esp_mqtt_client_handle_t client);

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
    strcat(time_str, tz_str);
    
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
        cJSON_AddStringToObject(data, "sensor_status", "offline");
    }
    
    cJSON *led_obj = cJSON_CreateObject();
    cJSON_AddNumberToObject(led_obj, "state", g_led_brightness > 0 ? 1 : 0);
    cJSON_AddNumberToObject(led_obj, "r", g_led_r);
    cJSON_AddNumberToObject(led_obj, "g", g_led_g);
    cJSON_AddNumberToObject(led_obj, "b", g_led_b);
    cJSON_AddNumberToObject(led_obj, "brightness", g_led_brightness);
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

// ==================== MQTT 状态上报任务 ====================

static void status_publish_task(void *pvParameters)
{
    esp_mqtt_client_handle_t client = (esp_mqtt_client_handle_t)pvParameters;
    
    // 等待 MQTT 连接和初始数据收集
    vTaskDelay(5000 / portTICK_PERIOD_MS);
    
    while (1) {
        // 上报当前状态（包含 DHT11 数据）
        publish_status(client);
        
        // 每 30 秒上报一次
        vTaskDelay(30000 / portTICK_PERIOD_MS);
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
    
    // 创建两个任务：传感器读取 + 状态上报
    xTaskCreate(sensor_read_task, "sensor_task", 4096, NULL, 4, NULL);
    xTaskCreate(status_publish_task, "status_task", 4096, client, 5, NULL);
}

void app_main(void)
{
    ESP_LOGI(TAG, "Starting Smart Home S3 Device...");
    
    esp_log_level_set("*", ESP_LOG_INFO);
    esp_log_level_set(TAG, ESP_LOG_DEBUG);

    ESP_ERROR_CHECK(nvs_flash_init());
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    
    led_strip_init();
    
    ESP_ERROR_CHECK(example_connect());
    
    obtain_time();

    mqtt_app_start();
}