#include "time_sync.h"
#include "esp_log.h"
#include "esp_sntp.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <stdlib.h>
#include <time.h>
#include <sys/time.h>

static const char *TAG = "time_sync";

static volatile bool s_time_synced = false;

// ==================== 内部回调 ====================

static void time_sync_notification_cb(struct timeval *tv)
{
    ESP_LOGI(TAG, "Time synchronized");
    s_time_synced = true;
}

// ==================== 内部初始化 ====================

static void initialize_sntp(void)
{
    ESP_LOGI(TAG, "Initializing SNTP, server: %s", SNTP_SERVER);

    esp_sntp_setoperatingmode(ESP_SNTP_OPMODE_POLL);
    esp_sntp_setservername(0, SNTP_SERVER);
    esp_sntp_set_time_sync_notification_cb(time_sync_notification_cb);
    esp_sntp_set_sync_mode(SNTP_SYNC_MODE_SMOOTH);
    esp_sntp_init();
}

// ==================== 对外接口 ====================

void time_sync_obtain(void)
{
    initialize_sntp();

    const int retry_max = 10;
    int retry = 0;

    while (esp_sntp_get_sync_status() == SNTP_SYNC_STATUS_RESET
           && ++retry < retry_max) {
        ESP_LOGI(TAG, "Waiting for time sync... (%d/%d)", retry, retry_max);
        vTaskDelay(pdMS_TO_TICKS(2000));
    }

    if (retry >= retry_max) {
        ESP_LOGW(TAG, "Time sync failed, continuing without sync");
        return;
    }

    setenv("TZ", TIMEZONE, 1);
    tzset();

    time_t now = time(NULL);
    struct tm timeinfo;
    localtime_r(&now, &timeinfo);
    ESP_LOGI(TAG, "Current time: %s", asctime(&timeinfo));
}

time_t time_sync_get_timestamp(void)
{
    return time(NULL);
}

bool time_sync_is_synced(void)
{
    return s_time_synced;
}