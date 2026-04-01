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
#include "led_control.h"
#include "time_sync.h"
#include "mqtt_handler.h"

static const char *TAG = "app_main";

// ==================== WiFi 配置（由 sdkconfig.defaults 提供）====================
// SSID 和密码通过 CONFIG_EXAMPLE_WIFI_SSID / CONFIG_EXAMPLE_WIFI_PASSWORD 注入
// 不在代码中硬编码，避免上传 GitHub 泄露

// ==================== 静态 IP 配置 ====================
// 根据板子编号选择不同 IP，修改 DEVICE_INDEX 后重新编译即可
// .2 = LubanCat4, .3 = Windows PC, .4 = ESP32-CAM
// .5 = S3_001,    .6 = S3_002,     .7+ 预留

#if DEVICE_INDEX == 1
    #define S3_STATIC_IP    "172.20.10.5"
#elif DEVICE_INDEX == 2
    #define S3_STATIC_IP    "172.20.10.6"
#elif DEVICE_INDEX == 3
    #define S3_STATIC_IP    "172.20.10.7"
#else
    #define S3_STATIC_IP    "172.20.10.8"
#endif

#define S3_GATEWAY      "172.20.10.1"
#define S3_NETMASK      "255.255.255.240"
#define MAX_RETRY       10

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
            ESP_LOGW(TAG, "WiFi retry %d/%d", s_retry_num, MAX_RETRY);
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

    // 停止 DHCP，使用静态 IP
    ESP_ERROR_CHECK(esp_netif_dhcpc_stop(netif));

    // 设置 IP、网关、掩码
    esp_netif_ip_info_t ip_info;
    ip4addr_aton(S3_STATIC_IP, (ip4_addr_t *)&ip_info.ip);
    ip4addr_aton(S3_GATEWAY,   (ip4_addr_t *)&ip_info.gw);
    ip4addr_aton(S3_NETMASK,   (ip4_addr_t *)&ip_info.netmask);
    ESP_ERROR_CHECK(esp_netif_set_ip_info(netif, &ip_info));

    // ========== 关键修复：设置 DNS 服务器 ==========
    // 使用网关作为 DNS（常见家庭路由器/热点）
    esp_netif_dns_info_t dns;
    ip4addr_aton(S3_GATEWAY, &dns.ip.u_addr.ip4);
    dns.ip.type = IPADDR_TYPE_V4;
    ESP_ERROR_CHECK(esp_netif_set_dns_info(netif, ESP_NETIF_DNS_MAIN, &dns));

    // 可选：同时设置备用 DNS（如公共 DNS）
    // esp_netif_dns_info_t dns2;
    // ip4addr_aton("8.8.8.8", &dns2.ip.u_addr.ip4);
    // dns2.ip.type = IPADDR_TYPE_V4;
    // ESP_ERROR_CHECK(esp_netif_set_dns_info(netif, ESP_NETIF_DNS_BACKUP, &dns2));

    ESP_LOGI(TAG, "Static IP: %s, Gateway: %s, DNS: %s", S3_STATIC_IP, S3_GATEWAY, S3_GATEWAY);

    // WiFi 初始化
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    esp_event_handler_instance_t instance_any_id;
    esp_event_handler_instance_t instance_got_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID,    &wifi_event_handler, NULL, &instance_any_id));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT,   IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, &instance_got_ip));

    // SSID/Password 来自 sdkconfig.defaults，不硬编码
    wifi_config_t wifi_config = {
        .sta = {
            .ssid               = CONFIG_EXAMPLE_WIFI_SSID,
            .password           = CONFIG_EXAMPLE_WIFI_PASSWORD,
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    // 关闭 WiFi 省电，降低延迟
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_start());

    EventBits_t bits = xEventGroupWaitBits(
        s_wifi_event_group,
        WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
        pdFALSE, pdFALSE,
        pdMS_TO_TICKS(30000));

    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "WiFi connected → IP: %s", S3_STATIC_IP);
    } else {
        ESP_LOGE(TAG, "WiFi connection failed, continuing anyway...");
    }
}

// ==================== 主入口 ====================

void app_main(void)
{
    ESP_LOGI(TAG, "=== Smart Home S3 [%s] starting ===", DEVICE_ID);
    ESP_LOGI(TAG, "Static IP: %s", S3_STATIC_IP);

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

    // LED 初始化（在 WiFi 连接前初始化，方便状态指示）
    led_control_init();

    // WiFi 静态 IP 连接
    wifi_init_static_ip();

    // NTP 时间同步
    time_sync_obtain();

    // 启动 MQTT 及所有传感器任务
    mqtt_handler_start();

    ESP_LOGI(TAG, "All services started on %s", S3_STATIC_IP);
}