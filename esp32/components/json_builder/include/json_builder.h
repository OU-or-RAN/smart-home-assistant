#ifndef JSON_BUILDER_H
#define JSON_BUILDER_H

#include <stdbool.h>
#include "mqtt_client.h"

// ==================== 状态 JSON ====================

/**
 * @brief 构建完整设备状态 JSON 字符串
 * @note  调用方负责 free() 返回的指针
 * @return char* JSON字符串，失败返回 NULL
 */
char *json_build_status(void);

// ==================== 报警 JSON ====================

/**
 * @brief 构建气体报警 JSON 字符串
 * @note  调用方负责 free() 返回的指针
 * @param ppm    当前 PPM 值
 * @param alert  是否处于报警状态
 * @return char* JSON字符串，失败返回 NULL
 */
char *json_build_gas_alert(float ppm, bool alert);

#endif // JSON_BUILDER_H