idf_component_register(
    SRCS "sensor_mq2.c"
    INCLUDE_DIRS "include"
    REQUIRES
        pin_config
        adc_shared
        driver
        freertos
        esp_adc
        log
)