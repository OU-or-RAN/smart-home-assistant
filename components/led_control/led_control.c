#include "led_control.h"
#include "pin_config.h"
#include "esp_log.h"
#include "led_strip.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static const char *TAG = "led_control";

// ==================== 内部状态 ====================
static led_strip_handle_t s_led_strip   = NULL;
static SemaphoreHandle_t  s_led_mutex   = NULL;
static volatile int       s_priority    = LED_PRIO_NONE;
static int  s_r = 0, s_g = 0, s_b = 0, s_brightness = 0;
static bool s_forced_by_gas = false;

// ==================== 初始化 ====================

void led_control_init(void)
{
    s_led_mutex = xSemaphoreCreateMutex();
    if (s_led_mutex == NULL) {
        ESP_LOGE(TAG, "Failed to create LED mutex");
        return;
    }

    led_strip_config_t strip_cfg = {
        .strip_gpio_num = LED_STRIP_GPIO,
        .max_leds       = LED_STRIP_NUM_LEDS,
    };
    led_strip_rmt_config_t rmt_cfg = {
        .clk_src        = RMT_CLK_SRC_DEFAULT,
        .resolution_hz  = 10 * 1000 * 1000,
        .flags.with_dma = false,
    };

    ESP_ERROR_CHECK(led_strip_new_rmt_device(&strip_cfg, &rmt_cfg, &s_led_strip));
    led_set_color(0, 0, 0);

    ESP_LOGI(TAG, "WS2812B LED initialized on GPIO%d", LED_STRIP_GPIO);
}

// ==================== 优先级控制 ====================

bool led_set_color_with_priority(int r, int g, int b, int priority)
{
    if (s_led_strip == NULL) return false;

    xSemaphoreTake(s_led_mutex, portMAX_DELAY);

    if (priority < s_priority && s_priority != LED_PRIO_NONE) {
        ESP_LOGD(TAG, "LED rejected: cur_prio=%d > req_prio=%d", s_priority, priority);
        xSemaphoreGive(s_led_mutex);
        return false;
    }

    r = (r < 0) ? 0 : (r > 255) ? 255 : r;
    g = (g < 0) ? 0 : (g > 255) ? 255 : g;
    b = (b < 0) ? 0 : (b > 255) ? 255 : b;

    s_r          = r;
    s_g          = g;
    s_b          = b;
    s_brightness = (int)(0.299f * r + 0.587f * g + 0.114f * b);
    s_priority   = priority;

    if (priority == LED_PRIO_NONE) {
        s_forced_by_gas = false;
    } else if (priority == LED_PRIO_GAS) {
        s_forced_by_gas = true;
    }

    ESP_ERROR_CHECK(led_strip_set_pixel(s_led_strip, 0, r, g, b));
    ESP_ERROR_CHECK(led_strip_refresh(s_led_strip));

    xSemaphoreGive(s_led_mutex);

    ESP_LOGI(TAG, "LED R:%d G:%d B:%d (prio:%d)", r, g, b, priority);
    return true;
}

// ==================== 基础控制 ====================

void led_set_color(int r, int g, int b)
{
    led_set_color_with_priority(r, g, b, LED_PRIO_NONE);
}

void led_set_state(int on)
{
    if (on) {
        led_set_color(255, 255, 255);
    } else {
        led_set_color(0, 0, 0);
    }
}

void led_restore_normal(void)
{
    s_forced_by_gas = false;
    led_set_color(0, 0, 0);
    ESP_LOGI(TAG, "LED restored to normal");
}

// ==================== 报警联动 ====================

void led_set_gas_alert(bool alert)
{
    if (alert) {
        led_set_color_with_priority(255, 0, 0, LED_PRIO_GAS);
    } else {
        xSemaphoreTake(s_led_mutex, portMAX_DELAY);
        if (s_priority == LED_PRIO_GAS) {
            s_priority      = LED_PRIO_NONE;
            s_forced_by_gas = false;
        }
        xSemaphoreGive(s_led_mutex);
        led_set_color_with_priority(0, 0, 0, LED_PRIO_NONE);
    }
}

void led_set_flame_alert(bool alert, yl38_flame_level_t level)
{
    if (alert) {
        xSemaphoreTake(s_led_mutex, portMAX_DELAY);
        if (s_priority >= LED_PRIO_GAS) {
            ESP_LOGD(TAG, "Flame alert suppressed by gas alert");
            xSemaphoreGive(s_led_mutex);
            return;
        }
        xSemaphoreGive(s_led_mutex);

        int g_val = (level == YL38_FLAME_WEAK)   ? 255 :
                    (level == YL38_FLAME_MEDIUM)  ? 100 : 0;
        led_set_color_with_priority(255, g_val, 0, LED_PRIO_FLAME);
    } else {
        xSemaphoreTake(s_led_mutex, portMAX_DELAY);
        if (s_priority == LED_PRIO_FLAME) {
            s_priority = LED_PRIO_NONE;
        }
        xSemaphoreGive(s_led_mutex);
        led_set_color_with_priority(0, 0, 0, LED_PRIO_NONE);
    }
}

// ==================== Getter ====================

int  led_get_r(void)           { return s_r; }
int  led_get_g(void)           { return s_g; }
int  led_get_b(void)           { return s_b; }
int  led_get_brightness(void)  { return s_brightness; }
bool led_is_forced_by_gas(void){ return s_forced_by_gas; }