#include "camera_init.h"
#include "esp_log.h"
#include "esp_heap_caps.h"

static const char *TAG = "camera_init";

esp_err_t camera_init(void)
{
    // 检查 PSRAM 是否可用
    size_t psram_size = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    ESP_LOGI(TAG, "PSRAM free: %d bytes", psram_size);

    camera_fb_location_t fb_location;
    framesize_t frame_size;

    if (psram_size > 100000) {
        fb_location = CAMERA_FB_IN_PSRAM;
        frame_size  = FRAMESIZE_VGA;       // PSRAM可用：640x480
        ESP_LOGI(TAG, "Using PSRAM frame buffer, VGA");
    } else {
        fb_location = CAMERA_FB_IN_DRAM;
        frame_size  = FRAMESIZE_QVGA;      // 无PSRAM：降级到320x240
        ESP_LOGW(TAG, "PSRAM unavailable, using DRAM frame buffer, QVGA");
    }

    camera_config_t config = {
        .pin_pwdn       = CAM_PIN_PWDN,
        .pin_reset      = CAM_PIN_RESET,
        .pin_xclk       = CAM_PIN_XCLK,
        .pin_sccb_sda   = CAM_PIN_SIOD,
        .pin_sccb_scl   = CAM_PIN_SIOC,
        .pin_d7         = CAM_PIN_D7,
        .pin_d6         = CAM_PIN_D6,
        .pin_d5         = CAM_PIN_D5,
        .pin_d4         = CAM_PIN_D4,
        .pin_d3         = CAM_PIN_D3,
        .pin_d2         = CAM_PIN_D2,
        .pin_d1         = CAM_PIN_D1,
        .pin_d0         = CAM_PIN_D0,
        .pin_vsync      = CAM_PIN_VSYNC,
        .pin_href       = CAM_PIN_HREF,
        .pin_pclk       = CAM_PIN_PCLK,

        .xclk_freq_hz   = 20000000,
        .ledc_timer     = LEDC_TIMER_0,
        .ledc_channel   = LEDC_CHANNEL_0,

        .pixel_format   = PIXFORMAT_JPEG,
        .frame_size     = frame_size,
        .jpeg_quality   = 10,              // OPTIMIZED: 降低质量，提高压缩率和帧率
        .fb_count       = 2,               // OPTIMIZED: 双缓冲，提高流畅度
        .fb_location    = fb_location,
        .grab_mode      = CAMERA_GRAB_WHEN_EMPTY,  // OPTIMIZED: 确保不丢帧
    };

    esp_err_t ret = esp_camera_init(&config);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Camera init failed: %s", esp_err_to_name(ret));
        return ret;
    }

    sensor_t *s = esp_camera_sensor_get();
    if (s != NULL && s->id.PID == OV3660_PID) {
        s->set_vflip(s, 1);
        s->set_brightness(s, 1);       // OPTIMIZED: 调整亮度
        s->set_contrast(s, 1);         // OPTIMIZED: 提高对比度，加速采集
        s->set_saturation(s, -1);      // OPTIMIZED: 微调饱和度
        ESP_LOGI(TAG, "OV3660 sensor configured");
    }

    ESP_LOGI(TAG, "Camera initialized successfully");
    return ESP_OK;
}