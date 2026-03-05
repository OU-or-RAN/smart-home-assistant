#ifndef PIN_CONFIG_H
#define PIN_CONFIG_H

// ===== ADC 模拟输入 =====
#define PIN_MQ2_AO      GPIO_NUM_1
#define MQ4_AO_GPIO      GPIO_NUM_2
#define YL38_AO_GPIO     GPIO_NUM_3

// ===== 数字输入 =====
#define PIN_MQ2_DO      GPIO_NUM_14
#define MQ4_DO_GPIO      GPIO_NUM_15
#define YL38_DO_GPIO     GPIO_NUM_16
#define DHT11_GPIO  GPIO_NUM_13

// ===== I2C (SHT40) =====
#define PIN_I2C_SDA     GPIO_NUM_11
#define PIN_I2C_SCL     GPIO_NUM_12
#define I2C_PORT        I2C_NUM_0

#endif