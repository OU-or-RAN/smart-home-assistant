idf_component_register(
    SRCS "json_builder.c"
    INCLUDE_DIRS "include"
    REQUIRES 
        log
        sensor_dht11
        sensor_mq2
        sensor_mq4
        sensor_yl38
        sensor_sht40    # 添加 SHT40 传感器支持
        mqtt_handler
        mqtt
        json
        time_sync
        led_control
        esp_timer
        esp_hw_support    # 添加 CPU 支持
)
