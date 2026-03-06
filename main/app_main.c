#include "esp_log.h"
#include "nvs_flash.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "protocol_examples_common.h"

#include "led_control.h"
#include "time_sync.h"
#include "mqtt_handler.h"

static const char *TAG = "main";

void app_main(void)
{
    ESP_LOGI(TAG, "Starting Smart Home S3 Device...");

    // 日志级别
    esp_log_level_set("*",    ESP_LOG_INFO);
    esp_log_level_set("main", ESP_LOG_DEBUG);
    esp_log_level_set("mq4",  ESP_LOG_DEBUG);
    esp_log_level_set("yl38", ESP_LOG_DEBUG);

    // 系统基础初始化
    ESP_ERROR_CHECK(nvs_flash_init());
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    // LED 初始化（含 mutex 创建）
    led_control_init();

    // 连接 WiFi
    ESP_ERROR_CHECK(example_connect());

    // SNTP 时间同步
    time_sync_obtain();

    // 启动 MQTT 及所有传感器任务
    mqtt_handler_start();
}