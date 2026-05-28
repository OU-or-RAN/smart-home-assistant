#ifndef MQTT_CAM_H
#define MQTT_CAM_H

#include "esp_err.h"

#define CAM_STATIC_IP       "172.20.10.4"
#define CAM_GATEWAY         "172.20.10.1"
#define CAM_NETMASK         "255.255.255.240"

#define CAM_DEVICE_ID       "esp32cam_001"
#define CAM_BROKER_URL      "mqtt://172.20.10.2:1883"

#define TOPIC_CAM_STATUS    "smart_home/cam/" CAM_DEVICE_ID "/status"
#define TOPIC_CAM_CONTROL   "smart_home/cam/" CAM_DEVICE_ID "/control"
#define TOPIC_CAM_DETECT    "smart_home/cam/" CAM_DEVICE_ID "/detect"

esp_err_t mqtt_cam_start(void);
void      mqtt_cam_publish_status(void);

#endif