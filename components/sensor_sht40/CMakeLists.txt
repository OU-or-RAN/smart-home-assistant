idf_component_register(
    SRCS "sensor_sht40.c"
    INCLUDE_DIRS "include"
    REQUIRES
        pin_config
        driver
        freertos
        log
)