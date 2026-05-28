#include "http_stream.h"
#include "esp_camera.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include <string.h>

static const char *TAG = "http_stream";

#define MJPEG_BOUNDARY     "gc0p4Jq0M2Yt08jU534c0p"
#define MJPEG_CONTENT_TYPE "multipart/x-mixed-replace;boundary=" MJPEG_BOUNDARY
#define PART_HEADER        "--" MJPEG_BOUNDARY "\r\nContent-Type: image/jpeg\r\nContent-Length: %zu\r\n\r\n"

static httpd_handle_t s_server      = NULL;
static float          s_fps         = 0.0f;
static int64_t        s_last_time   = 0;
static int            s_frame_count = 0;
static volatile bool  s_streaming   = false;

static esp_err_t stream_handler(httpd_req_t *req)
{
    // 同时只允许一个客户端连接，防止多客户端耗尽内存
    if (s_streaming) {
        httpd_resp_set_status(req, "503 Service Unavailable");
        httpd_resp_sendstr(req, "Only one viewer allowed");
        return ESP_OK;
    }

    s_streaming = true;
    ESP_LOGI(TAG, "Client connected");

    ESP_ERROR_CHECK_WITHOUT_ABORT(
        httpd_resp_set_type(req, MJPEG_CONTENT_TYPE));
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(req, "Cache-Control", "no-cache");
    httpd_resp_set_hdr(req, "Pragma", "no-cache");

    char part_buf[128];
    esp_err_t ret = ESP_OK;

    while (true) {
        camera_fb_t *fb = esp_camera_fb_get();
        if (!fb) {
            ESP_LOGE(TAG, "Frame capture failed");
            vTaskDelay(pdMS_TO_TICKS(50));  // OPTIMIZED: 缩短重试延时
            continue;
        }

        // 写帧头
        size_t hlen = snprintf(part_buf, sizeof(part_buf),
                               PART_HEADER, fb->len);
        ret = httpd_resp_send_chunk(req, part_buf, hlen);
        if (ret != ESP_OK) {
            esp_camera_fb_return(fb);
            break;
        }

        // 写 JPEG 数据（分块发送，避免单次过大）
        size_t offset = 0;
        while (offset < fb->len && ret == ESP_OK) {
            size_t chunk = fb->len - offset;
            if (chunk > 8192) chunk = 8192;  // OPTIMIZED: 增大 chunk size，减少开销
            ret = httpd_resp_send_chunk(
                req, (const char *)fb->buf + offset, chunk);
            offset += chunk;
        }
        esp_camera_fb_return(fb);
        if (ret != ESP_OK) break;

        ret = httpd_resp_send_chunk(req, "\r\n", 2);
        if (ret != ESP_OK) break;

        // 帧率统计
        s_frame_count++;
        int64_t now = esp_timer_get_time() / 1000;
        if (now - s_last_time >= 1000) {
            s_fps         = (float)s_frame_count * 1000.0f / (now - s_last_time);
            s_frame_count = 0;
            s_last_time   = now;
            ESP_LOGD(TAG, "FPS: %.1f", s_fps);
        }

        // OPTIMIZED: 移除 vTaskDelay(100)，全速运行，根据硬件限帧率
        // 如果过快，可加小延时如 vTaskDelay(10) ~100 FPS limit
    }

    s_streaming = false;
    ESP_LOGI(TAG, "Client disconnected");
    return ESP_OK;
}

static esp_err_t capture_handler(httpd_req_t *req)
{
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) {
        httpd_resp_send_500(req);
        return ESP_FAIL;
    }
    httpd_resp_set_type(req, "image/jpeg");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(req, "Content-Disposition",
                       "inline; filename=capture.jpg");
    httpd_resp_send(req, (const char *)fb->buf, fb->len);
    esp_camera_fb_return(fb);
    return ESP_OK;
}

esp_err_t http_stream_start(void)
{
    httpd_config_t config   = HTTPD_DEFAULT_CONFIG();
    config.server_port      = 80;
    config.ctrl_port        = 32768;
    config.max_uri_handlers = 4;
    config.stack_size       = 12288;  // OPTIMIZED: 增大栈，防止溢出
    // 增大发送超时，防止慢网络下断连
    config.send_wait_timeout = 60;    // OPTIMIZED: 延长超时
    config.recv_wait_timeout = 60;    // OPTIMIZED: 延长超时
    // 只允许一个连接，节省内存
    config.max_open_sockets  = 2;

    esp_err_t ret = httpd_start(&s_server, &config);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "HTTP server start failed: %s", esp_err_to_name(ret));
        return ret;
    }

    httpd_uri_t stream_uri = {
        .uri     = "/stream",
        .method  = HTTP_GET,
        .handler = stream_handler,
    };
    httpd_register_uri_handler(s_server, &stream_uri);

    httpd_uri_t capture_uri = {
        .uri     = "/capture",
        .method  = HTTP_GET,
        .handler = capture_handler,
    };
    httpd_register_uri_handler(s_server, &capture_uri);

    ESP_LOGI(TAG, "HTTP server started");
    ESP_LOGI(TAG, "Stream : http://%s/stream",  "172.20.10.4");
    ESP_LOGI(TAG, "Capture: http://%s/capture", "172.20.10.4");
    return ESP_OK;
}

void http_stream_stop(void)
{
    if (s_server) {
        httpd_stop(s_server);
        s_server = NULL;
    }
}

float http_stream_get_fps(void) { return s_fps; }