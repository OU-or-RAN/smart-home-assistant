#ifndef HTTP_STREAM_H
#define HTTP_STREAM_H

#include "esp_err.h"
#include "esp_http_server.h"

esp_err_t http_stream_start(void);
void      http_stream_stop(void);

// 获取当前帧率（供 MQTT 上报）
float     http_stream_get_fps(void);

#endif