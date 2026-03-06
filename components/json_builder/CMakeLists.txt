idf_component_register(
    SRCS "json_builder.c"
    INCLUDE_DIRS "include"
    REQUIRES 
        mqtt_handler
        mqtt
        json
        time_sync
        led_control
        esp_timer
        esp_hw_support    # 添加 CPU 支持
)
