idf_component_register(
    SRCS "sensor_dht11.c"
    INCLUDE_DIRS "include"
    REQUIRES 
        pin_config
        freertos
        esp_adc
        esp_timer
        esp_hw_support
        driver
        esp_driver_rmt      
        log  
)
