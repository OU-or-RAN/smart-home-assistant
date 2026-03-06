idf_component_register(
    SRCS "led_control.c"
    INCLUDE_DIRS "include"
    REQUIRES 
        log
        pin_config
        freertos
        sensor_yl38
        driver
        esp_driver_rmt
        led_strip
        esp_timer
        esp_hw_support    # 添加 CPU 支持
)
