#ifndef MQTT_HANDLER_H
#define MQTT_HANDLER_H

#include <stdbool.h>
#include "mqtt_client.h"
#include "sensor_yl38.h"

#define MQTT_BROKER_URL     "mqtt://172.20.10.2:1883"

// ==================== 设备标识 ====================
#define DEVICE_INDEX        2
#define DEVICE_ID           "s3_002"
#define DEVICE_TYPE         "s3"

// ==================== 本机传感器配置 ====================
// 根据实际接线情况修改，1=已接入，0=未接入
// S3_001: DHT11=1, SHT40=0, MQ2=1, MQ4=1, YL38=1
// S3_002: DHT11=0, SHT40=1, MQ2=0, MQ4=1, YL38=0
#define SENSOR_DHT11_ENABLED    0
#define SENSOR_SHT40_ENABLED    1
#define SENSOR_MQ2_ENABLED      0
#define SENSOR_MQ4_ENABLED      1
#define SENSOR_YL38_ENABLED     0

// #define DEVICE_INDEX            1
// #define DEVICE_ID               "s3_001"
// #define DEVICE_TYPE             "s3"
// #define SENSOR_DHT11_ENABLED    1
// #define SENSOR_SHT40_ENABLED    0
// #define SENSOR_MQ2_ENABLED      0
// #define SENSOR_MQ4_ENABLED      0
// #define SENSOR_YL38_ENABLED     1

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
bool        sensor_dht11_is_valid(void);
float       sensor_dht11_get_temperature(void);
float       sensor_dht11_get_humidity(void);

bool        sensor_mq2_is_initialized(void);
bool        sensor_mq2_is_online(void);
bool        sensor_mq2_is_alert(void);
float       sensor_mq2_get_ppm(void);
int         sensor_mq2_get_raw(void);
bool        sensor_mq2_is_calibrated(void);

bool        sensor_mq4_is_initialized(void);
bool        sensor_mq4_is_online(void);
bool        sensor_mq4_is_alert(void);
float       sensor_mq4_get_ppm(void);
int         sensor_mq4_get_raw(void);
bool        sensor_mq4_is_calibrated(void);

bool        sensor_yl38_is_initialized(void);
bool        sensor_yl38_is_online(void);
bool        sensor_yl38_is_flame_detected(void);
float       sensor_yl38_get_voltage(void);
int         sensor_yl38_get_raw(void);
const char *sensor_yl38_get_level_string(void);
float       sensor_yl38_get_intensity(void);
bool        sensor_yl38_is_digital_triggered(void);

bool        sensor_sht40_is_valid(void);
float       sensor_sht40_get_temperature(void);
float       sensor_sht40_get_humidity(void);

#endif // MQTT_HANDLER_H