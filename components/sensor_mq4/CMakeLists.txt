idf_component_register(
    SRCS "sensor_mq4.c"
    INCLUDE_DIRS "include"
    REQUIRES 
        adc_shared
        pin_config
        freertos
        esp_adc
        esp_timer
        esp_hw_support
        driver
        esp_driver_rmt        
)
