#ifndef MQTT_HANDLER_H
#define MQTT_HANDLER_H

#include <stdbool.h>
#include "mqtt_client.h"
#include "sensor_yl38.h"

// ==================== 设备标识 ====================
// 烧录不同板时只需修改 DEVICE_INDEX，无敏感信息，可上传 GitHub
#define DEVICE_INDEX        2                       // S3_001=1, S3_002=2
#define DEVICE_ID           "s3_002"               // 与 DEVICE_INDEX 保持一致
#define DEVICE_TYPE         "s3"

// ==================== MQTT 主题 ====================
#define TOPIC_STATUS        "smart_home/s3/" DEVICE_ID "/status"
#define TOPIC_CONTROL       "smart_home/s3/" DEVICE_ID "/control"
#define TOPIC_GAS_ALERT     "smart_home/s3/" DEVICE_ID "/gas_alert"
#define TOPIC_FLAME         "smart_home/s3/" DEVICE_ID "/flame"

// ==================== 初始化与启动 ====================
void mqtt_handler_start(void);

// ==================== 发布接口 ====================
void mqtt_publish_status(esp_mqtt_client_handle_t client);
void mqtt_publish_gas_alert(esp_mqtt_client_handle_t client, float ppm, bool alert);

// ==================== 控制命令解析 ====================
void mqtt_parse_control(const char *json_str, esp_mqtt_client_handle_t client);

// ==================== 传感器 Getter ====================
// DHT11
bool        sensor_dht11_is_valid(void);
float       sensor_dht11_get_temperature(void);
float       sensor_dht11_get_humidity(void);

// MQ2
bool        sensor_mq2_is_initialized(void);
bool        sensor_mq2_is_alert(void);
float       sensor_mq2_get_ppm(void);
int         sensor_mq2_get_raw(void);
bool        sensor_mq2_is_calibrated(void);

// MQ4
bool        sensor_mq4_is_initialized(void);
bool        sensor_mq4_is_alert(void);
float       sensor_mq4_get_ppm(void);
int         sensor_mq4_get_raw(void);
bool        sensor_mq4_is_calibrated(void);

// YL38
bool        sensor_yl38_is_initialized(void);
bool        sensor_yl38_is_flame_detected(void);
float       sensor_yl38_get_voltage(void);
int         sensor_yl38_get_raw(void);
const char *sensor_yl38_get_level_string(void);
float       sensor_yl38_get_intensity(void);
bool        sensor_yl38_is_digital_triggered(void);

// SHT40
bool        sensor_sht40_is_valid(void);
float       sensor_sht40_get_temperature(void);
float       sensor_sht40_get_humidity(void);

#endif // MQTT_HANDLER_H