#include "camera_init.h"
#include "esp_log.h"
#include "esp_heap_caps.h"

static const char *TAG = "camera_init";

esp_err_t camera_init(void)
{
    size_t psram_size = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    ESP_LOGI(TAG, "PSRAM free: %d bytes", psram_size);

    camera_fb_location_t fb_location;
    framesize_t frame_size;

    if (psram_size > 100000) {
        fb_location = CAMERA_FB_IN_PSRAM;
        frame_size  = FRAMESIZE_VGA;
        ESP_LOGI(TAG, "Using PSRAM frame buffer, VGA");
    } else {
        fb_location = CAMERA_FB_IN_DRAM;
        frame_size  = FRAMESIZE_QVGA;
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
        .jpeg_quality   = 10,
        .fb_count       = 2,
        .fb_location    = fb_location,
        .grab_mode      = CAMERA_GRAB_WHEN_EMPTY,
    };

    esp_err_t ret = esp_camera_init(&config);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Camera init failed: %s", esp_err_to_name(ret));
        return ret;
    }

    sensor_t *s = esp_camera_sensor_get();
    if (s == NULL) {
        ESP_LOGE(TAG, "Failed to get camera sensor");
        return ESP_FAIL;
    }

    // ==================== 曝光控制（解决过曝问题）====================

    // 开启自动曝光控制
    s->set_exposure_ctrl(s, 1);

    // 开启 AEC DSP 算法（比基础 AEC 更稳定）
    s->set_aec2(s, 1);

    // 降低 AE 目标亮度：范围 -2 ~ +2，-1 可有效抑制过曝
    s->set_ae_level(s, -1);

    // 限制最大曝光时间，防止强光下曝光过长
    // 单位：行数，OV2640 @ 20MHz，1600 约对应 1/12s
    s->set_aec_value(s, 800);

    // 开启自动增益控制
    s->set_gain_ctrl(s, 1);

    // 限制最大增益倍数：0=2x, 1=4x, 2=8x, 3=16x, 4=32x
    // 设为 1（4x），避免暗场景下增益过高引入噪声
    s->set_agc_gain(s, 1);

    // ==================== 白平衡（减少色偏）====================
    s->set_whitebal(s, 1);     // 开启自动白平衡
    s->set_awb_gain(s, 1);     // 开启 AWB 增益

    // ==================== 图像质量调整 ====================
    s->set_brightness(s, 0);   // 亮度：0 为中性（原来是+1，会加重过曝）
    s->set_contrast(s, 1);     // 对比度：+1 提高层次感
    s->set_saturation(s, 0);   // 饱和度：0 为自然色

    // OV3660 特殊处理
    if (s->id.PID == OV3660_PID) {
        s->set_vflip(s, 1);
        ESP_LOGI(TAG, "OV3660 sensor: vflip enabled");
    }

    // ==================== 镜头校正（减少边缘畸变）====================
    s->set_lenc(s, 1);         // 开启镜头畸变校正
    s->set_dcw(s, 1);          // 开启降采样插值

    ESP_LOGI(TAG, "Camera initialized: AEC=on, AE_level=-1, AWB=on");
    ESP_LOGI(TAG, "Camera initialized successfully");
    return ESP_OK;
}