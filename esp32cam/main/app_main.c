#include "esp_netif.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_err.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "lwip/inet.h"
#include "camera_init.h"
#include "http_stream.h"
#include "mqtt_cam.h"

static const char *TAG = "app_main";

// ==================== WiFi 配置（直接定义，不依赖 protocol_examples_common）====================
#define WIFI_SSID       "iPhone (3)"
#define WIFI_PASSWORD   "24006556766"
#define MAX_RETRY       10

// ==================== 静态 IP 配置 ====================
#define CAM_STATIC_IP   "172.20.10.4"
#define CAM_GATEWAY     "172.20.10.1"
#define CAM_NETMASK     "255.255.255.240"

static EventGroupHandle_t s_wifi_event_group;
#define WIFI_CONNECTED_BIT  BIT0
#define WIFI_FAIL_BIT       BIT1
static int s_retry_num = 0;

// ==================== WiFi 事件处理 ====================

static void wifi_event_handler(void *arg, esp_event_base_t base,
                                int32_t event_id, void *event_data)
{
    if (base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();

    } else if (base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_retry_num < MAX_RETRY) {
            esp_wifi_connect();
            s_retry_num++;
            ESP_LOGW(TAG, "Retry WiFi %d/%d", s_retry_num, MAX_RETRY);
        } else {
            xEventGroupSetBits(s_wifi_event_group, WIFI_FAIL_BIT);
            ESP_LOGE(TAG, "WiFi connection failed after %d retries", MAX_RETRY);
        }

    } else if (base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&event->ip_info.ip));
        s_retry_num = 0;
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

// ==================== WiFi 静态 IP 初始化 ====================

static void wifi_init_static_ip(void)
{
    s_wifi_event_group = xEventGroupCreate();

    esp_netif_t *netif = esp_netif_create_default_wifi_sta();

    // 停止 DHCP 客户端，改用静态 IP
    ESP_ERROR_CHECK(esp_netif_dhcpc_stop(netif));

    esp_netif_ip_info_t ip_info;
    ip4addr_aton(CAM_STATIC_IP, (ip4_addr_t *)&ip_info.ip);
    ip4addr_aton(CAM_GATEWAY,   (ip4_addr_t *)&ip_info.gw);
    ip4addr_aton(CAM_NETMASK,   (ip4_addr_t *)&ip_info.netmask);
    ESP_ERROR_CHECK(esp_netif_set_ip_info(netif, &ip_info));

    ESP_LOGI(TAG, "Static IP configured: %s", CAM_STATIC_IP);

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    esp_event_handler_instance_t instance_any_id;
    esp_event_handler_instance_t instance_got_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID,   &wifi_event_handler, NULL, &instance_any_id));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT,   IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, &instance_got_ip));

    wifi_config_t wifi_config = {
        .sta = {
            .ssid               = WIFI_SSID,
            .password           = WIFI_PASSWORD,
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    EventBits_t bits = xEventGroupWaitBits(
        s_wifi_event_group,
        WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
        pdFALSE, pdFALSE,
        pdMS_TO_TICKS(30000));

    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "WiFi connected, static IP: %s", CAM_STATIC_IP);
    } else {
        ESP_LOGE(TAG, "WiFi connection failed");
    }
}

// ==================== 主入口 ====================

void app_main(void)
{
    ESP_LOGI(TAG, "ESP32-CAM starting...");

    // NVS 初始化
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    // 网络与事件循环
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    // WiFi 静态 IP 连接
    wifi_init_static_ip();

    // 摄像头初始化
    ESP_ERROR_CHECK(camera_init());

    // HTTP MJPEG 流服务器
    ESP_ERROR_CHECK(http_stream_start());

    // MQTT 客户端
    ESP_ERROR_CHECK(mqtt_cam_start());

    ESP_LOGI(TAG, "All services started");
    ESP_LOGI(TAG, "Stream : http://%s/stream",  CAM_STATIC_IP);
    ESP_LOGI(TAG, "Capture: http://%s/capture", CAM_STATIC_IP);
}