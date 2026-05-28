#ifndef ADC_SHARED_H
#define ADC_SHARED_H

#include "esp_adc/adc_oneshot.h"

// 共享 ADC 单元句柄
extern adc_oneshot_unit_handle_t g_shared_adc_handle;

// 共享 ADC 单元初始化（只执行一次）
esp_err_t shared_adc_unit_init(void);

#endif