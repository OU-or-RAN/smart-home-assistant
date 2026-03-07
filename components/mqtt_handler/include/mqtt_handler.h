#ifndef MQTT_HANDLER_H
#define MQTT_HANDLER_H

#include <stdbool.h>
#include "mqtt_client.h"
#include "sensor_yl38.h"

#define MQTT_BROKER_URL     "mqtt://172.20.10.3:1883"

// ==================== 设备标识 ====================
// 烧录不同板时修改以下两行
#define DEVICE_INDEX        1
#define DEVICE_ID           "s3_001"
#define DEVICE_TYPE         "s3"

// ==================== MQTT 主题 ====================
#define TOPIC_STATUS        "smart_home/s3/" DEVICE_ID "/status"
#define TOPIC_CONTROL       "smart_home/s3/" DEVICE_ID "/control"
#define TOPIC_GAS_ALERT     "smart_home/s3/" DEVICE_ID "/gas_alert"
#define TOPIC_FLAME         "smart_home/s3/" DEVICE_ID "/flame"

// ==================== ADC 传感器合理性检测阈值 ====================
// MQ 系列传感器正常工作时 ADC 读数应在此范围内
// 低于下限说明未接线（浮空接近0）或短路
// 高于上限说明传感器饱和或供电异常
#define MQ_RAW_MIN_VALID    200     // 低于此值视为未接线
#define MQ_RAW_MAX_VALID    4000    // 高于此值视为异常

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
bool        sensor_mq2_is_online(void);
bool        sensor_mq2_is_alert(void);
float       sensor_mq2_get_ppm(void);
int         sensor_mq2_get_raw(void);
bool        sensor_mq2_is_calibrated(void);

// MQ4
bool        sensor_mq4_is_initialized(void);
bool        sensor_mq4_is_online(void);
bool        sensor_mq4_is_alert(void);
float       sensor_mq4_get_ppm(void);
int         sensor_mq4_get_raw(void);
bool        sensor_mq4_is_calibrated(void);

// YL38
bool        sensor_yl38_is_initialized(void);
bool        sensor_yl38_is_online(void);
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