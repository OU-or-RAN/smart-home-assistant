#include "mqtt_cam.h"
#include "http_stream.h"
#include "esp_camera.h"  
#include "esp_log.h"
#include "mqtt_client.h"
#include "cJSON.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <string.h>
#include <stdlib.h>

static const char *TAG = "mqtt_cam";
static esp_mqtt_client_handle_t s_client = NULL;

// ==================== 获取本机IP ====================

static void get_ip_str(char *buf, size_t len)
{
    esp_netif_ip_info_t ip_info;
    esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    if (netif && esp_netif_get_ip_info(netif, &ip_info) == ESP_OK) {
        snprintf(buf, len, IPSTR, IP2STR(&ip_info.ip));
    } else {
        strncpy(buf, "0.0.0.0", len);
    }
}

// ==================== 发布状态 ====================

void mqtt_cam_publish_status(void)
{
    if (s_client == NULL) return;

    char ip[20];
    get_ip_str(ip, sizeof(ip));

    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "device_id",  CAM_DEVICE_ID);
    cJSON_AddStringToObject(root, "type",        "esp32cam");
    cJSON_AddStringToObject(root, "ip",          ip);
    cJSON_AddNumberToObject(root, "fps",         http_stream_get_fps());

    // 流地址
    char stream_url[64];
    snprintf(stream_url, sizeof(stream_url), "http://%s/stream", ip);
    char capture_url[64];
    snprintf(capture_url, sizeof(capture_url), "http://%s/capture", ip);

    cJSON_AddStringToObject(root, "stream_url",  stream_url);
    cJSON_AddStringToObject(root, "capture_url", capture_url);
    cJSON_AddStringToObject(root, "resolution",  "VGA");
    cJSON_AddStringToObject(root, "status",      "active");

    char *json = cJSON_PrintUnformatted(root);
    esp_mqtt_client_publish(s_client, TOPIC_CAM_STATUS, json, 0, 1, 1);
    ESP_LOGI(TAG, "Published status: %s", json);

    free(json);
    cJSON_Delete(root);
}

// ==================== 控制命令解析 ====================

static void handle_control(const char *payload)
{
    cJSON *root = cJSON_Parse(payload);
    if (!root) return;

    cJSON *cmd    = cJSON_GetObjectItemCaseSensitive(root, "cmd");
    cJSON *target = cJSON_GetObjectItemCaseSensitive(root, "target");

    if (cJSON_IsString(cmd) && cJSON_IsString(target)) {
        ESP_LOGI(TAG, "CMD: %s  TARGET: %s",
                 cmd->valuestring, target->valuestring);

        if (strcmp(cmd->valuestring, "set") == 0) {
            sensor_t *s = esp_camera_sensor_get();

            if (strcmp(target->valuestring, "resolution") == 0) {
                cJSON *val = cJSON_GetObjectItemCaseSensitive(root, "value");
                if (cJSON_IsString(val)) {
                    if (strcmp(val->valuestring, "QVGA") == 0)
                        s->set_framesize(s, FRAMESIZE_QVGA);
                    else if (strcmp(val->valuestring, "VGA") == 0)
                        s->set_framesize(s, FRAMESIZE_VGA);
                    else if (strcmp(val->valuestring, "SVGA") == 0)
                        s->set_framesize(s, FRAMESIZE_SVGA);
                    ESP_LOGI(TAG, "Resolution set to %s", val->valuestring);
                }
            }
            else if (strcmp(target->valuestring, "quality") == 0) {
                cJSON *val = cJSON_GetObjectItemCaseSensitive(root, "value");
                if (cJSON_IsNumber(val)) {
                    s->set_quality(s, val->valueint);
                    ESP_LOGI(TAG, "JPEG quality set to %d", val->valueint);
                }
            }
            else if (strcmp(target->valuestring, "brightness") == 0) {
                cJSON *val = cJSON_GetObjectItemCaseSensitive(root, "value");
                if (cJSON_IsNumber(val)) {
                    s->set_brightness(s, val->valueint);
                }
            }
        }
        else if (strcmp(cmd->valuestring, "get") == 0) {
            if (strcmp(target->valuestring, "status") == 0) {
                mqtt_cam_publish_status();
            }
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
        esp_mqtt_client_subscribe(client, TOPIC_CAM_CONTROL, 1);
        mqtt_cam_publish_status();
        break;

    case MQTT_EVENT_DATA: {
        char *topic = malloc(event->topic_len + 1);
        char *data  = malloc(event->data_len  + 1);
        if (topic && data) {
            memcpy(topic, event->topic, event->topic_len);
            topic[event->topic_len] = '\0';
            memcpy(data,  event->data,  event->data_len);
            data[event->data_len]   = '\0';

            if (strncmp(topic, TOPIC_CAM_CONTROL,
                        strlen(TOPIC_CAM_CONTROL)) == 0) {
                handle_control(data);
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

// ==================== 定时发布状态任务 ====================

static void status_publish_task(void *pvParameters)
{
    vTaskDelay(pdMS_TO_TICKS(5000));
    while (1) {
        mqtt_cam_publish_status();
        vTaskDelay(pdMS_TO_TICKS(120000));  // OPTIMIZED: 延长到120s，减少负载
    }
}

esp_err_t mqtt_cam_start(void)
{
    esp_mqtt_client_config_t cfg = {
        .broker.address.uri    = CAM_BROKER_URL,
        .session.keepalive     = 60,        // 60秒心跳，默认10秒太短
        .network.timeout_ms    = 10000,
        .network.reconnect_timeout_ms = 5000,
    };

    s_client = esp_mqtt_client_init(&cfg);
    esp_mqtt_client_register_event(s_client, ESP_EVENT_ANY_ID,
                                   mqtt_event_handler, NULL);
    esp_mqtt_client_start(s_client);

    xTaskCreate(status_publish_task, "cam_status", 4096, NULL, 3, NULL);
    return ESP_OK;
}