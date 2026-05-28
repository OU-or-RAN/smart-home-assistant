#ifndef LED_CONTROL_H
#define LED_CONTROL_H

#include <stdbool.h>
#include "sensor_yl38.h"

// LED 优先级定义
#define LED_PRIO_NONE   0
#define LED_PRIO_FLAME  1
#define LED_PRIO_GAS    2

// ==================== 初始化 ====================
void led_control_init(void);

// ==================== 基础控制 ====================
void led_set_color(int r, int g, int b);
void led_set_state(int on);
void led_restore_normal(void);

// ==================== 优先级控制 ====================
bool led_set_color_with_priority(int r, int g, int b, int priority);

// ==================== 报警联动 ====================
void led_set_gas_alert(bool alert);
void led_set_flame_alert(bool alert, yl38_flame_level_t level);

// ==================== 状态读取（供 json_builder 使用）====================
int  led_get_r(void);
int  led_get_g(void);
int  led_get_b(void);
int  led_get_brightness(void);
bool led_is_forced_by_gas(void);

#endif // LED_CONTROL_H