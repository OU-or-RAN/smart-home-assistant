#ifndef DHT11_H
#define DHT11_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "pin_config.h"


typedef struct {
    uint8_t humidity_int;
    uint8_t humidity_dec;
    uint8_t temperature_int;
    uint8_t temperature_dec;
    bool valid;
} dht11_data_t;

esp_err_t dht11_init(void);
esp_err_t dht11_read(dht11_data_t *data);
float dht11_get_temperature(dht11_data_t *data);
float dht11_get_humidity(dht11_data_t *data);

#endif